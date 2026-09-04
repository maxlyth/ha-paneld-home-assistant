"""Native translation loading without changing machine state or entity identity."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    async_get_translations,
    async_translate_state,
)
from homeassistant.setup import async_setup_component

from custom_components.ha_paneld.const import DOMAIN
from custom_components.ha_paneld.coordinator import HaPaneldDataUpdateCoordinator
from custom_components.ha_paneld.sensor import HaPaneldStatusSensor


@pytest.mark.parametrize("language", ["en", "de", "zh-Hans"])
async def test_native_status_and_exception_translations(
    hass: HomeAssistant, language: str
) -> None:
    """Missing locales use HA's English fallback, not a new translation engine."""
    hass.config.language = language
    assert await async_setup_component(hass, DOMAIN, {})
    entity_strings = await async_get_translations(hass, language, "entity", {DOMAIN})
    error_strings = await async_get_translations(hass, language, "exceptions", {DOMAIN})
    sensor = HaPaneldStatusSensor(
        "stable-entry-id", HaPaneldDataUpdateCoordinator(hass, AsyncMock())
    )

    assert sensor.native_value == "online"
    assert sensor.unique_id == "stable-entry-id_status"
    assert sensor.translation_key == "status"
    assert sensor.device_class is None
    assert (
        entity_strings.get(f"component.{DOMAIN}.entity.sensor.status.name") == "Status"
    )
    assert (
        entity_strings.get(f"component.{DOMAIN}.entity.sensor.status.state.online")
        == "Online"
    )
    assert (
        async_translate_state(
            hass,
            sensor.native_value,
            "sensor",
            DOMAIN,
            sensor.translation_key,
            sensor.device_class,
        )
        == "Online"
    )
    assert (
        error_strings.get(f"component.{DOMAIN}.exceptions.health_update_failed.message")
        == "Unable to read panel health"
    )
