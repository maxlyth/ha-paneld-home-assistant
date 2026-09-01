"""Diagnostics support for ha-paneld."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HaPaneldConfigEntry

_ENTRY_KEYS_TO_REDACT = {CONF_ADDRESS}
_HEALTH_KEYS_TO_REDACT = {"panel_id"}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: HaPaneldConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    return _diagnostics(entry)


async def async_get_device_diagnostics(
    _hass: HomeAssistant, entry: HaPaneldConfigEntry, _device: DeviceEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a panel device."""
    return _diagnostics(entry)


def _diagnostics(entry: HaPaneldConfigEntry) -> dict[str, Any]:
    """Build diagnostics only from cached data."""
    snapshot = entry.runtime_data.coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), _ENTRY_KEYS_TO_REDACT),
        "health": async_redact_data(snapshot.health.as_dict(), _HEALTH_KEYS_TO_REDACT),
        "status": snapshot.status.as_dict() if snapshot.status is not None else None,
        "status_error": snapshot.status_error,
    }
