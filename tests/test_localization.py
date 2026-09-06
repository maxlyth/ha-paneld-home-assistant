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


@pytest.mark.parametrize(
    ("language", "setup_title", "status_name", "online_state", "health_error"),
    [
        (
            "en",
            "Set up a panel",
            "Status",
            "Online",
            "Unable to read panel health",
        ),
        (
            "de",
            "Panel einrichten",
            "Status",
            "Online",
            "Der Funktionsstatus des Panels konnte nicht gelesen werden",
        ),
        (
            "es",
            "Configurar un panel",
            "Estado",
            "En línea",
            "No se puede leer el estado del panel",
        ),
        (
            "fr",
            "Configurer un panneau",
            "État",
            "En ligne",
            "Impossible de lire l'état du panneau",
        ),
        (
            "it",
            "Configura un pannello",
            "Stato",
            "Online",
            "Impossibile leggere lo stato di funzionamento del pannello",
        ),
        (
            "zh-Hans",
            "设置面板",
            "状态",
            "在线",
            "无法读取面板健康状态",
        ),
    ],
)
async def test_native_status_and_exception_translations(
    hass: HomeAssistant,
    language: str,
    setup_title: str,
    status_name: str,
    online_state: str,
    health_error: str,
) -> None:
    """Every shipped locale loads rather than using Home Assistant fallback."""
    hass.config.language = language
    assert await async_setup_component(hass, DOMAIN, {})
    config_strings = await async_get_translations(hass, language, "config", {DOMAIN})
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
        config_strings.get(f"component.{DOMAIN}.config.step.user.title") == setup_title
    )
    assert (
        entity_strings.get(f"component.{DOMAIN}.entity.sensor.status.name")
        == status_name
    )
    assert (
        entity_strings.get(f"component.{DOMAIN}.entity.sensor.status.state.online")
        == online_state
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
        == online_state
    )
    assert (
        error_strings.get(f"component.{DOMAIN}.exceptions.health_update_failed.message")
        == health_error
    )
