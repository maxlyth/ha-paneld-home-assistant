"""Integration lifecycle, device and diagnostics tests."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_paneld import async_reload_entry
from custom_components.ha_paneld.client import (
    CannotConnectError,
    InvalidResponseError,
    PanelHealth,
)
from custom_components.ha_paneld.const import DOMAIN
from custom_components.ha_paneld.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ha_paneld.status import PanelStatus

HEALTH = PanelHealth(
    version="0.9.0",
    panel_id="alpha",
    build="1000",
    config_hash="1a2b3c4d",
    ha_state="normal",
    ha_source="mqtt",
)
BETA_HEALTH = PanelHealth(
    version="0.9.1",
    panel_id="beta",
    build="1001",
    config_hash="2a2b3c4d",
)
STATUS = PanelStatus(
    warning_count=2,
    capability_count=3,
    renderer={"mode": "builtin", "state": "rendered", "rendered": True},
    camera={"state": "absent", "live": False},
)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="alpha",
        data={CONF_ADDRESS: "panel.local"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_device_diagnostics_unload_reload(hass: HomeAssistant) -> None:
    """The vertical slice creates one device and unloads/reloads cleanly."""
    entry = _entry(hass)
    health_mock = AsyncMock(side_effect=[HEALTH, BETA_HEALTH])
    status_mock = AsyncMock(return_value=STATUS)

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_entries = er.async_get(hass).entities.values()
        entities = [
            item for item in entity_entries if item.config_entry_id == entry.entry_id
        ]
        devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

        assert len(entities) == 1
        assert len(devices) == 1
        state = hass.states.get(entities[0].entity_id)
        assert state is not None
        assert state.state == "online"
        assert state.attributes["build"] == HEALTH.build
        assert devices[0].identifiers == {(DOMAIN, entry.entry_id)}
        assert devices[0].sw_version == HEALTH.version

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        device_diagnostics = await async_get_device_diagnostics(hass, entry, devices[0])
        assert diagnostics["entry"][CONF_ADDRESS] == "**REDACTED**"
        assert diagnostics["health"]["panel_id"] == "**REDACTED**"
        assert diagnostics["health"]["build"] == HEALTH.build
        assert diagnostics["status"] == STATUS.as_dict()
        assert diagnostics["status_error"] is None
        assert device_diagnostics == diagnostics
        assert health_mock.await_count == 1
        assert status_mock.await_count == 1

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED

        await async_reload_entry(hass, entry)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        reloaded_entities = [
            item
            for item in er.async_get(hass).entities.values()
            if item.config_entry_id == entry.entry_id
        ]
        reloaded_devices = dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        assert [item.id for item in reloaded_entities] == [item.id for item in entities]
        assert [item.id for item in reloaded_devices] == [item.id for item in devices]
        assert entry.runtime_data.coordinator.data.health.panel_id == "beta"

    assert health_mock.await_count == 2
    assert status_mock.await_count == 2


async def test_setup_retries_when_panel_is_offline(hass: HomeAssistant) -> None:
    """A transient setup failure becomes a Home Assistant retry."""
    entry = _entry(hass)

    status_mock = AsyncMock(return_value=STATUS)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    status_mock.assert_not_awaited()


async def test_coordinator_translates_client_failure(hass: HomeAssistant) -> None:
    """Polling keeps client exceptions behind the coordinator boundary."""
    entry = _entry(hass)
    status_mock = AsyncMock(return_value=STATUS)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entry.runtime_data.client.async_get_health = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnectError
    )
    try:
        await entry.runtime_data.coordinator._async_update_data()
    except UpdateFailed:
        pass
    else:
        raise AssertionError("Client failure was not translated to UpdateFailed")
    assert status_mock.await_count == 1


@pytest.mark.parametrize(
    ("status_error", "diagnostic_error"),
    [
        (InvalidResponseError(), "invalid_response"),
        (CannotConnectError(), "unavailable"),
    ],
)
async def test_status_failure_does_not_override_health_authority(
    hass: HomeAssistant, status_error: Exception, diagnostic_error: str
) -> None:
    """Rejected optional diagnostics leave the existing health sensor online."""
    entry = _entry(hass)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(side_effect=status_error),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entities = [
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    assert len(entities) == 1
    state = hass.states.get(entities[0].entity_id)
    assert state is not None
    assert state.state == "online"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["status"] is None
    assert diagnostics["status_error"] == diagnostic_error
    assert diagnostics["health"]["build"] == HEALTH.build
