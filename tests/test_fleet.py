"""Fleet inventory is admin-only and never refreshes cached panel data."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant.fleet import FleetView


@pytest.fixture
async def endpoint(hass, hass_client):
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(FleetView(hass))
    return await hass_client()


async def test_authentication(
    endpoint, hass_client_no_auth, hass_read_only_access_token
):
    client = await hass_client_no_auth()
    assert (await client.get(FleetView.url)).status == 401
    response = await client.get(
        FleetView.url,
        headers={"Authorization": f"Bearer {hass_read_only_access_token}"},
    )
    assert response.status == 401


async def test_empty_and_query(endpoint):
    response = await endpoint.get(FleetView.url)
    assert await response.json() == {"panels": [], "truncated": False}
    assert response.headers["Cache-Control"] == "no-store"
    assert (await endpoint.get(FleetView.url + "?refresh=true")).status == 400


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("has_status", [True, False])
async def test_projection(endpoint, hass, monkeypatch, available, has_status):
    coordinator = SimpleNamespace(
        last_update_success=available,
        data=SimpleNamespace(
            health=SimpleNamespace(
                version="0.9.7", panel_id="private", config_hash="private"
            ),
            status=SimpleNamespace(warning_count=2) if has_status else None,
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry123",
        title="Panel",
        state=ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    listing = Mock(return_value=[entry])
    monkeypatch.setattr(hass.config_entries, "async_entries", listing)
    response = await endpoint.get(FleetView.url)
    assert await response.json() == {
        "panels": [
            {
                "entry_id": "entry123",
                "name": "Panel",
                "available": available,
                "version": "0.9.7" if available else None,
                "warning_count": 2 if available and has_status else None,
                "status_available": available and has_status,
            }
        ],
        "truncated": False,
    }
    listing.assert_called_once_with("panel_assistant")
    monkeypatch.undo()


async def test_unloaded_and_bounded(endpoint, hass, monkeypatch):
    entries = [
        SimpleNamespace(
            entry_id=str(i), title="x" * 300, state=ConfigEntryState.NOT_LOADED
        )
        for i in range(201)
    ]
    monkeypatch.setattr(
        hass.config_entries, "async_entries", Mock(return_value=entries)
    )
    result = await (await endpoint.get(FleetView.url)).json()
    assert result["truncated"] is True
    assert len(result["panels"]) == 200
    assert all(
        not row["available"] and row["version"] is None for row in result["panels"]
    )
    assert all(len(row["name"]) == 256 for row in result["panels"])
    monkeypatch.undo()
