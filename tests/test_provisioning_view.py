"""Setup observations are scoped to an authenticated existing config entry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant.client import CannotConnectError
from custom_components.panel_assistant.fleet import ProvisioningPlanView
from custom_components.panel_assistant.provisioning_plan import ProvisioningPlan

URL = "/api/panel_assistant/fleet/example/provisioning"


@pytest.fixture
async def setup_view(hass, hass_client, monkeypatch):
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(ProvisioningPlanView(hass))
    entry = SimpleNamespace(
        domain="panel_assistant",
        state=ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(client=object())),
    )
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda _: entry)
    fetch = AsyncMock(return_value=ProvisioningPlan("satisfied", (), False))
    monkeypatch.setattr(
        "custom_components.panel_assistant.fleet.async_get_provisioning_plan", fetch
    )
    return await hass_client(), entry, fetch


async def test_view_returns_only_plan(setup_view):
    client, entry, fetch = setup_view
    response = await client.get(URL)
    assert response.status == 200
    assert await response.json() == {
        "state": "satisfied",
        "items": [],
        "needs_updated_client": False,
    }
    assert response.headers["Cache-Control"] == "no-store"
    fetch.assert_awaited_once_with(entry.runtime_data.coordinator.client)


async def test_view_requires_admin(
    setup_view, hass_client_no_auth, hass_read_only_access_token
):
    _, _, fetch = setup_view
    client = await hass_client_no_auth()
    assert (await client.get(URL)).status == 401
    assert (
        await client.get(
            URL, headers={"Authorization": f"Bearer {hass_read_only_access_token}"}
        )
    ).status == 401
    fetch.assert_not_awaited()


async def test_wrong_domain_and_unloaded_do_not_contact_panel(setup_view):
    client, entry, fetch = setup_view
    entry.domain = "mqtt"
    assert (await client.get(URL)).status == 404
    entry.domain = "panel_assistant"
    entry.state = ConfigEntryState.NOT_LOADED
    assert (await client.get(URL)).status == 503
    assert (await client.get(URL + "?host=example.com")).status == 400
    fetch.assert_not_awaited()


async def test_missing_endpoint(setup_view):
    client, _entry, fetch = setup_view
    fetch.side_effect = CannotConnectError
    assert (await client.get(URL)).status == 503


async def test_runtime_replaced_or_unloaded_during_request(setup_view):
    client, entry, fetch = setup_view

    async def replace(_):
        entry.runtime_data = SimpleNamespace(
            coordinator=SimpleNamespace(client=object())
        )
        return ProvisioningPlan("satisfied", (), False)

    fetch.side_effect = replace
    assert (await client.get(URL)).status == 503

    async def unload(_):
        entry.state = ConfigEntryState.NOT_LOADED
        return ProvisioningPlan("satisfied", (), False)

    fetch.side_effect = unload
    assert (await client.get(URL)).status == 503
