"""Panel-owned ha-paneld software update entity."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HaPaneldConfigEntry
from .build_feed import (
    BuildFeed,
    BuildFeedError,
    FeedBuild,
    async_download_build,
    build_label,
    parse_build_request,
)
from .client import (
    CannotConnectError,
    HaPaneldError,
    InvalidResponseError,
    UpdateApprovalRequiredError,
    UpdateBusyError,
    UpdateRejectedError,
    UploadDisabledError,
    is_newer_stable_version,
)
from .const import DOMAIN, update_unique_id
from .coordinator import HaPaneldDataUpdateCoordinator
from .device import panel_device_info
from .feed_coordinator import BuildFeedCoordinator, async_get_feed_coordinator
from .panel_backup import async_store_panel_backup
from .release import _PACKAGE_ID, _RELEASE_SIGNER_CERTIFICATE_SHA256
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

_LOGGER = logging.getLogger(__name__)


def _update_error(translation_key: str, fallback: str) -> HomeAssistantError:
    """Return a localized update-action error with a useful log fallback."""
    return HomeAssistantError(
        fallback,
        translation_domain=DOMAIN,
        translation_key=translation_key,
    )


async def async_setup_entry(
    hass: HomeAssistant,
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
                async_get_feed_coordinator(hass),
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
        feed: BuildFeedCoordinator | None = None,
    ) -> None:
        """Bind update state to the existing config-entry and health authority."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._update_coordinator = update_coordinator
        # A signed build feed, when configured. Internal builds
        # share one version name, so they are told apart by build number.
        self._feed = feed
        self._installed_code: int | None = None
        self._code_key: tuple[str, str] | None = None
        self._code_task: asyncio.Task[None] | None = None
        if feed is not None:
            self._attr_supported_features |= UpdateEntityFeature.SPECIFIC_VERSION
        self._attr_unique_id = update_unique_id(entry_id)
        self._attr_in_progress = False
        self._observer_task: asyncio.Task[None] | None = None
        self._recovery_started = False

    async def async_added_to_hass(self) -> None:
        """Refresh presentation when the panel's local operation state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._update_coordinator.async_add_listener(
                self._handle_update_coordinator_update
            )
        )
        self.async_on_remove(self._cancel_observer)
        if self._feed is not None:
            self.async_on_remove(
                self._feed.async_add_listener(self.async_write_ha_state)
            )
            self._refresh_installed_code()
        self._resume_running_operation()

    def _handle_coordinator_update(self) -> None:
        """Re-read the build number whenever the app on the panel changes."""
        self._refresh_installed_code()
        super()._handle_coordinator_update()

    def _refresh_installed_code(self) -> None:
        """Read the running build number once per install, never per poll."""
        if self._feed is None or not self.coordinator.last_update_success:
            return
        health = self.coordinator.data.health
        key = (health.version, health.build)
        if key == self._code_key or (
            self._code_task is not None and not self._code_task.done()
        ):
            return
        self._code_task = self.hass.async_create_task(
            self._async_read_installed_code(key),
            f"read ha-paneld build {self._entry_id}",
        )

    async def _async_read_installed_code(self, key: tuple[str, str]) -> None:
        try:
            name, code = await self.coordinator.client.async_get_version_code()
        except HaPaneldError:
            return
        # Record the key either way: a disagreeing name is retried only when the
        # panel's app changes again, never on every poll.
        self._code_key = key
        self._installed_code = code if name == key[0] else None
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """A manual refresh also re-reads the build feed, so a build just
        published can be installed straight away."""
        await super().async_update()
        if self._feed is not None:
            await self._feed.async_refresh()

    def _feed_mode(self) -> BuildFeed | None:
        """Return the feed only when it and the panel's build number are known."""
        if (
            self._feed is None
            or not self._feed.last_update_success
            or self._feed.data is None
            or self._installed_code is None
        ):
            return None
        return self._feed.data

    def _handle_update_coordinator_update(self) -> None:
        """Keep a recovered operation latched across transient status samples."""
        self._resume_running_operation()
        self.async_write_ha_state()

    def _resume_running_operation(self) -> None:
        """Resume one bounded observer for panel-owned work found after reload."""
        operation = self._update_coordinator.data.operation
        if (
            self._recovery_started
            or operation is None
            or not operation.running
            or operation.component != "ha-paneld"
        ):
            return
        offer = self._offered_update()
        if offer is None:
            return
        self._recovery_started = True
        self._start_observer(offer.target_version)

    def _start_observer(self, expected_version: str) -> asyncio.Task[None]:
        """Start or reuse the entity's sole bounded health observer."""
        if self._observer_task is not None and not self._observer_task.done():
            return self._observer_task
        self._attr_in_progress = True
        task = self.hass.async_create_task(
            self._run_observer(expected_version),
            f"observe ha-paneld update {self._entry_id}",
        )
        self._observer_task = task
        task.add_done_callback(self._observer_finished)
        return task

    async def _run_observer(self, expected_version: str) -> None:
        """Observe one target and release its latch before awaiters resume."""
        try:
            await self._async_wait_for_installed_version(expected_version)
        finally:
            self._attr_in_progress = False
            self._recovery_started = False
            if asyncio.current_task() is self._observer_task:
                self._observer_task = None
            self.async_write_ha_state()

    def _observer_finished(self, task: asyncio.Task[None]) -> None:
        """Release the latch only when its observer exits or is cancelled."""
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.warning("ha-paneld update observer stopped: %s", error)

    def _cancel_observer(self) -> None:
        """Stop polling when Home Assistant unloads the entity."""
        task = self._observer_task
        self._observer_task = None
        self._attr_in_progress = False
        self._recovery_started = False
        if task is not None and not task.done():
            task.cancel()

    @property
    def available(self) -> bool:
        """Use the existing health authority for availability."""
        return super().available

    @property
    def in_progress(self) -> bool:
        """Retain local work and recover panel-owned work after an HA restart."""
        operation = self._update_coordinator.data.operation
        return (
            (self._observer_task is not None and not self._observer_task.done())
            or self._attr_in_progress
            or (
                operation is not None
                and operation.running
                and operation.component == "ha-paneld"
            )
        )

    @property
    def installed_version(self) -> str:
        """Return the current version from the health authority."""
        version = self.coordinator.data.health.version
        if self._feed_mode() is not None and self._installed_code is not None:
            return build_label(version, self._installed_code)
        return version

    def _offered_update(self) -> PanelCachedUpdate | None:
        """Return only a fresh cached stable target matching installed health."""
        status = self.coordinator.data.status
        offer = status.panel_assistant_update if status is not None else None
        if (
            offer is None
            or offer.current_version != self.coordinator.data.health.version
            or not is_newer_stable_version(
                offer.target_version, self.coordinator.data.health.version
            )
        ):
            return None
        return offer

    @property
    def latest_version(self) -> str:
        """Report installed version if no newer panel-approved stable target exists."""
        feed = self._feed_mode()
        if feed is not None:
            newest = feed.newest()
            if (
                newest is not None
                and self._installed_code is not None
                and newest.version_code > self._installed_code
            ):
                return newest.label
            return self.installed_version
        offer = self._offered_update()
        return offer.target_version if offer is not None else self.installed_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Use the same stable-versus-RC comparison as the bounded client parser."""
        if self._feed_mode() is not None:
            latest = parse_build_request(latest_version)
            installed = parse_build_request(installed_version)
            if latest is not None and installed is not None:
                return latest > installed
        return is_newer_stable_version(latest_version, installed_version)

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the existing config-entry device without a second identity."""
        return panel_device_info(
            self._entry_id,
            self.coordinator.data,
            self.coordinator.client.configuration_url,
        )

    async def async_install(
        self, version: str | None, backup: bool, **_kwargs: object
    ) -> None:
        """Start one exact stable offer, then follow the expected panel restart."""
        feed = self._feed_mode()
        if feed is not None:
            await self._async_install_feed_build(feed, version, backup)
            return
        offer = self._offered_update()
        if (
            self.in_progress
            or backup
            or offer is None
            or version not in (None, offer.target_version)
        ):
            raise _update_error(
                "update_unavailable",
                "The requested ha-paneld update is unavailable",
            )
        try:
            await self.coordinator.client.async_start_panel_update(offer.tag)
        except UpdateBusyError as err:
            raise _update_error(
                "update_busy", "The panel is busy with another operation"
            ) from err
        except UpdateApprovalRequiredError as err:
            raise _update_error(
                "update_approval_required",
                "Approve this update on the panel, then try again",
            ) from err
        except UpdateRejectedError as err:
            raise _update_error(
                "update_rejected", "The panel refused the update request"
            ) from err
        except (CannotConnectError, InvalidResponseError) as err:
            raise _update_error(
                "update_not_accepted", "The panel did not accept the update request"
            ) from err

        observer = self._start_observer(offer.target_version)
        self.async_write_ha_state()
        await observer

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
                    raise _update_error(
                        "update_not_complete", "The panel update did not complete"
                    )
            await asyncio.sleep(_UPDATE_RECHECK_SECONDS)
        raise _update_error(
            "update_did_not_return", "The panel did not return after the update"
        )

    async def _async_install_feed_build(
        self, feed: BuildFeed, version: str | None, backup: bool
    ) -> None:
        """Back up, verify, upload and commit one signed feed build, then prove it."""
        newest = feed.newest()
        code = (
            parse_build_request(version)
            if version is not None
            else (newest.version_code if newest is not None else None)
        )
        if self.in_progress or backup:
            raise _update_error(
                "update_unavailable",
                "The requested ha-paneld update is unavailable",
            )
        build = feed.find(code) if code is not None else None
        if build is None and code is not None and self._feed is not None:
            # The build may have been published since the last scheduled read.
            await self._feed.async_refresh()
            if self._feed.last_update_success and self._feed.data is not None:
                build = self._feed.data.find(code)
        if (
            self.in_progress
            or backup
            or build is None
            or build.version_code == self._installed_code
        ):
            raise _update_error(
                "update_unavailable",
                "The requested ha-paneld update is unavailable",
            )
        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            await self._async_deliver_feed_build(build)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _async_deliver_feed_build(self, build: FeedBuild) -> None:
        client = self.coordinator.client
        before = self.coordinator.data.health.build
        try:
            await async_store_panel_backup(
                self.hass,
                self._entry_id,
                self._installed_code,
                await client.async_backup_panel(),
            )
        except UpdateApprovalRequiredError as err:
            raise _update_error(
                "update_approval_required",
                "Approve this update on the panel, then try again",
            ) from err
        except (HaPaneldError, OSError) as err:
            raise _update_error(
                "panel_backup_failed", "The panel could not be backed up first"
            ) from err
        try:
            apk = await async_download_build(async_get_clientsession(self.hass), build)
        except BuildFeedError as err:
            raise _update_error(
                "build_verification_failed",
                "The build did not match the signed build feed",
            ) from err
        try:
            staged = await client.async_stage_apk(apk)
            if (
                staged.package != _PACKAGE_ID
                or staged.signer != _RELEASE_SIGNER_CERTIFICATE_SHA256
                or staged.version != build.version_name
            ):
                with contextlib.suppress(HaPaneldError):
                    await client.async_discard_apk(staged.token)
                raise _update_error(
                    "build_verification_failed",
                    "The build did not match the signed build feed",
                )
            await client.async_commit_apk(staged.token)
        except UploadDisabledError as err:
            raise _update_error(
                "upload_disabled", "The panel does not accept app uploads"
            ) from err
        except UpdateBusyError as err:
            raise _update_error(
                "update_busy", "The panel is busy with another operation"
            ) from err
        except UpdateApprovalRequiredError as err:
            raise _update_error(
                "update_approval_required",
                "Approve this update on the panel, then try again",
            ) from err
        except UpdateRejectedError as err:
            raise _update_error(
                "update_rejected", "The panel refused the update request"
            ) from err
        except (CannotConnectError, InvalidResponseError) as err:
            raise _update_error(
                "update_not_accepted", "The panel did not accept the update request"
            ) from err
        await self._async_wait_for_build(build, before)

    async def _async_wait_for_build(self, build: FeedBuild, before: str) -> None:
        """Wait for the panel to restart into exactly the committed build."""
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time()
            + _ANDROID_PACKAGE_INSTALL_MAX_SECONDS
            + _RESTART_HEALTH_GRACE_SECONDS
        )
        while loop.time() < deadline:
            await self.coordinator.async_request_refresh()
            health = self.coordinator.data.health if self.coordinator.data else None
            if (
                self.coordinator.last_update_success
                and health is not None
                and health.build != before
                and health.version == build.version_name
            ):
                try:
                    _name, code = await self.coordinator.client.async_get_version_code()
                except HaPaneldError:
                    code = None
                if code == build.version_code:
                    self._installed_code = code
                    self._code_key = (health.version, health.build)
                    return
                if code is not None:
                    raise _update_error(
                        "update_not_complete", "The panel update did not complete"
                    )
            await asyncio.sleep(_UPDATE_RECHECK_SECONDS)
        raise _update_error(
            "update_did_not_return", "The panel did not return after the update"
        )
