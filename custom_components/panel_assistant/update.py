"""Panel-owned ha-paneld software update entity."""

from __future__ import annotations

import asyncio

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HaPaneldConfigEntry
from .client import (
    CannotConnectError,
    InvalidResponseError,
    UpdateApprovalRequiredError,
    UpdateBusyError,
    UpdateRejectedError,
    is_newer_stable_version,
)
from .const import DOMAIN
from .coordinator import HaPaneldDataUpdateCoordinator
from .status import PanelCachedUpdate
from .update_coordinator import PanelUpdateCoordinator

_ANDROID_DOWNLOAD_MAX_SECONDS = 10 * 60
_ANDROID_PACKAGE_INSTALL_MAX_SECONDS = 3 * 60
_RESTART_HEALTH_GRACE_SECONDS = 60
# Android bounds the signed release download to ten minutes and package-manager
# installation to three minutes. Retain one more minute for service replacement
# and fresh health before declaring the panel unreachable.
_UPDATE_TIMEOUT_SECONDS = (
    _ANDROID_DOWNLOAD_MAX_SECONDS
    + _ANDROID_PACKAGE_INSTALL_MAX_SECONDS
    + _RESTART_HEALTH_GRACE_SECONDS
)
_UPDATE_RECHECK_SECONDS = 2
_TERMINAL_STATUS_GRACE_SECONDS = 60


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaPaneldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the one panel-owned firmware update entity."""
    async_add_entities(
        [
            HaPaneldUpdateEntity(
                entry.entry_id,
                entry.runtime_data.coordinator,
                entry.runtime_data.update_coordinator,
            )
        ]
    )


class HaPaneldUpdateEntity(
    CoordinatorEntity[HaPaneldDataUpdateCoordinator], UpdateEntity
):
    """Project a selected stable update through the panel's own transaction."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_has_entity_name = True
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    _attr_translation_key = "paneld_update"
    _attr_title = "ha-paneld"

    def __init__(
        self,
        entry_id: str,
        coordinator: HaPaneldDataUpdateCoordinator,
        update_coordinator: PanelUpdateCoordinator,
    ) -> None:
        """Bind update state to the existing config-entry and health authority."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._update_coordinator = update_coordinator
        self._attr_unique_id = f"{entry_id}_update"
        self._attr_in_progress = False

    async def async_added_to_hass(self) -> None:
        """Refresh presentation when the panel's local operation state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._update_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Use the existing health authority for availability."""
        return super().available

    @property
    def in_progress(self) -> bool:
        """Retain local work and recover panel-owned work after an HA restart."""
        operation = self._update_coordinator.data.operation
        return self._attr_in_progress or (
            operation is not None
            and operation.running
            and operation.component == "ha-paneld"
        )

    @property
    def installed_version(self) -> str:
        """Return the current version from the health authority."""
        return self.coordinator.data.health.version

    def _offered_update(self) -> PanelCachedUpdate | None:
        """Return only a fresh cached stable target matching installed health."""
        status = self.coordinator.data.status
        offer = status.panel_assistant_update if status is not None else None
        if (
            offer is None
            or offer.current_version != self.installed_version
            or not is_newer_stable_version(offer.target_version, self.installed_version)
        ):
            return None
        return offer

    @property
    def latest_version(self) -> str:
        """Report installed version if no newer panel-approved stable target exists."""
        offer = self._offered_update()
        return offer.target_version if offer is not None else self.installed_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Use the same stable-versus-RC comparison as the bounded client parser."""
        return is_newer_stable_version(latest_version, installed_version)

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the existing config-entry device without a second identity."""
        health = self.coordinator.data.health
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=health.panel_id,
            model="ha-paneld",
            sw_version=health.version,
            configuration_url=self.coordinator.client.configuration_url,
        )

    async def async_install(
        self, version: str | None, backup: bool, **_kwargs: object
    ) -> None:
        """Start one exact stable offer, then follow the expected panel restart."""
        offer = self._offered_update()
        if (
            self.in_progress
            or backup
            or offer is None
            or version not in (None, offer.target_version)
        ):
            raise HomeAssistantError("The requested ha-paneld update is unavailable")
        try:
            await self.coordinator.client.async_start_panel_update(offer.tag)
        except UpdateBusyError as err:
            raise HomeAssistantError(
                "The panel is busy with another operation"
            ) from err
        except UpdateApprovalRequiredError as err:
            raise HomeAssistantError(
                "Approve this update on the panel, then try again"
            ) from err
        except UpdateRejectedError as err:
            raise HomeAssistantError("The panel refused the update request") from err
        except (CannotConnectError, InvalidResponseError) as err:
            raise HomeAssistantError(
                "The panel did not accept the update request"
            ) from err

        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            await self._async_wait_for_installed_version(offer.target_version)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _async_wait_for_installed_version(self, expected_version: str) -> None:
        """Poll status through restart, then prove the health version changed."""
        deadline = asyncio.get_running_loop().time() + _UPDATE_TIMEOUT_SECONDS
        terminal_status_deadline: float | None = None
        while asyncio.get_running_loop().time() < deadline:
            await self.coordinator.async_request_refresh()
            if self.coordinator.last_update_success and (
                self.installed_version == expected_version
            ):
                await self._update_coordinator.async_request_refresh()
                return
            try:
                await self._update_coordinator.async_request_refresh()
                status = self._update_coordinator.data.operation
            except CannotConnectError, InvalidResponseError:
                status = None
            if (
                status is not None
                and not status.running
                and status.component == "ha-paneld"
            ):
                # Android finishes its progress slot just before its process
                # replacement is observable. Keep polling health long enough
                # to distinguish that successful hand-off from a real failure.
                if terminal_status_deadline is None:
                    terminal_status_deadline = min(
                        deadline,
                        asyncio.get_running_loop().time()
                        + _TERMINAL_STATUS_GRACE_SECONDS,
                    )
                if asyncio.get_running_loop().time() >= terminal_status_deadline:
                    raise HomeAssistantError("The panel update did not complete")
            await asyncio.sleep(_UPDATE_RECHECK_SECONDS)
        raise HomeAssistantError("The panel did not return after the update")
