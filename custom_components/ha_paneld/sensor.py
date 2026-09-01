"""Diagnostic sensor for ha-paneld."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HaPaneldConfigEntry
from .const import DOMAIN
from .coordinator import HaPaneldDataUpdateCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaPaneldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ha-paneld diagnostic sensor."""
    async_add_entities(
        [HaPaneldStatusSensor(entry.entry_id, entry.runtime_data.coordinator)]
    )


class HaPaneldStatusSensor(
    CoordinatorEntity[HaPaneldDataUpdateCoordinator], SensorEntity
):
    """Represent the health of one ha-paneld panel."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "status"

    def __init__(
        self, entry_id: str, coordinator: HaPaneldDataUpdateCoordinator
    ) -> None:
        """Initialize the sensor from cached coordinator data."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_status"

    @property
    def native_value(self) -> str:
        """Return the cached panel status."""
        return "online"

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Return the cached health diagnostics."""
        health = self.coordinator.data
        return {
            "build": health.build,
            "config_hash": health.config_hash,
            "home_assistant_state": health.ha_state,
            "home_assistant_source": health.ha_source,
            "home_assistant_subscription_refused": health.ha_subscription_refused,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return API-backed device information."""
        health = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=health.panel_id,
            model="ha-paneld",
            sw_version=health.version,
            configuration_url=self.coordinator.client.configuration_url,
        )
