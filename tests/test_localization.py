"""Native translation loading without changing machine state or entity identity."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    async_get_translations,
    async_translate_state,
)
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.coordinator import HaPaneldDataUpdateCoordinator
from custom_components.panel_assistant.sensor import HaPaneldStatusSensor

TRANSLATIONS = (
    Path(__file__).parents[1] / "custom_components" / "panel_assistant" / "translations"
)
TRANSLATION_SAMPLES = {
    "en": (
        "Set up a panel",
        "Status",
        "Online",
        "Unable to read panel health",
    ),
    "de": (
        "Panel einrichten",
        "Status",
        "Online",
        "Der Funktionsstatus des Panels konnte nicht gelesen werden",
    ),
    "es": (
        "Configurar un panel",
        "Estado",
        "En línea",
        "No se puede leer el estado del panel",
    ),
    "fr": (
        "Configurer un panneau",
        "État",
        "En ligne",
        "Impossible de lire l'état du panneau",
    ),
    "it": (
        "Configura un pannello",
        "Stato",
        "Online",
        "Impossibile leggere lo stato di funzionamento del pannello",
    ),
    "zh-Hans": (
        "设置面板",
        "状态",
        "在线",
        "无法读取面板健康状态",
    ),
}


def _translation_cases(
    translations: Path = TRANSLATIONS,
    samples: dict[str, tuple[str, str, str, str]] = TRANSLATION_SAMPLES,
) -> list[tuple[str, str, str, str, str]]:
    shipped = sorted(path.stem for path in translations.glob("*.json"))
    assert set(samples) == set(shipped), (
        "runtime translation samples must exactly cover shipped locale files"
    )
    return [(language, *samples[language]) for language in shipped]


def test_runtime_translation_cases_reject_locale_coverage_drift(tmp_path: Path) -> None:
    """A catalogue or sample cannot be added without extending runtime coverage."""
    (tmp_path / "en.json").touch()
    (tmp_path / "de.json").touch()
    sample = ("setup", "status", "online", "error")

    with pytest.raises(AssertionError, match="exactly cover"):
        _translation_cases(tmp_path, {"en": sample})
    with pytest.raises(AssertionError, match="exactly cover"):
        _translation_cases(tmp_path, {"en": sample, "de": sample, "fr": sample})


@pytest.mark.parametrize(
    ("language", "setup_title", "status_name", "online_state", "health_error"),
    _translation_cases(),
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
    selector_strings = await async_get_translations(
        hass, language, "selector", {DOMAIN}
    )
    assert (
        selector_strings.get(
            f"component.{DOMAIN}.selector.release_channel.options.resume_existing"
        )
        == {
            "en": "Resume existing job only",
            "de": "Nur bestehenden Auftrag fortsetzen",
            "es": "Solo reanudar un trabajo existente",
            "fr": "Reprendre une tâche existante uniquement",
            "it": "Riprendi solo un’attività esistente",  # noqa: RUF001
            "zh-Hans": "仅继续现有任务",
        }[language]
    )
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
