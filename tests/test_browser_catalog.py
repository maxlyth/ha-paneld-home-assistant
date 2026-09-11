"""Release catalogue uses the real HA authentication and admin boundary."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant import browser_delivery as delivery
from custom_components.panel_assistant import release_catalog

URL = "/api/panel_assistant/usb/releases"


@pytest.fixture
async def catalog_endpoint(hass, hass_client, monkeypatch):
    assert await async_setup_component(hass, "http", {})
    delivery.async_register_browser_delivery(hass)
    listing = AsyncMock(return_value=[{"tag": "v1.2.3-rc1", "prerelease": True}])
    monkeypatch.setattr(release_catalog, "async_list_install_releases", listing)
    return await hass_client(), listing


async def test_authenticated_catalog_and_empty(catalog_endpoint):
    client, listing = catalog_endpoint
    response = await client.get(URL)
    assert response.status == 200
    assert await response.json() == {
        "releases": [{"tag": "v1.2.3-rc1", "prerelease": True}]
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    listing.return_value = []
    response = await client.get(URL)
    assert await response.json() == {"releases": []}


async def test_catalog_requires_admin(
    catalog_endpoint, hass_client_no_auth, hass_read_only_access_token
):
    _, listing = catalog_endpoint
    client = await hass_client_no_auth()
    response = await client.get(URL)
    assert response.status == 401
    response = await client.get(
        URL, headers={"Authorization": f"Bearer {hass_read_only_access_token}"}
    )
    assert response.status == 401
    listing.assert_not_called()


async def test_catalog_rejects_query_and_redacts_failures(catalog_endpoint):
    client, listing = catalog_endpoint
    response = await client.get(URL + "?url=https://example.com")
    assert response.status == 400
    listing.assert_not_called()
    listing.side_effect = RuntimeError("private path/token/upstream prose")
    response = await client.get(URL)
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_catalog_failed"}


async def test_closed_service_refuses_discovery(catalog_endpoint, hass):
    client, listing = catalog_endpoint
    service = hass.data["panel_assistant"][delivery.DATA_BROWSER_DELIVERY]
    service._closed = True
    response = await client.get(URL)
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_closed"}
    listing.assert_not_called()
