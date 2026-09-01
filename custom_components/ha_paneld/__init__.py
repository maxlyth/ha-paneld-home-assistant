"""The ha-paneld integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import HaPaneldClient, normalize_address
from .coordinator import HaPaneldDataUpdateCoordinator

PLATFORMS = [Platform.SENSOR]


@dataclass(slots=True)
class HaPaneldRuntimeData:
    """Runtime data for one config entry."""

    client: HaPaneldClient
    coordinator: HaPaneldDataUpdateCoordinator


type HaPaneldConfigEntry = ConfigEntry[HaPaneldRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> bool:
    """Set up ha-paneld from a config entry."""
    address = normalize_address(entry.data[CONF_ADDRESS])
    client = HaPaneldClient(async_get_clientsession(hass), address)
    coordinator = HaPaneldDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HaPaneldRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> bool:
    """Unload a ha-paneld config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> None:
    """Reload a ha-paneld config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
