"""Hidden panel registration, static delivery and retry contracts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import frontend
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant import browser_panel


@pytest.fixture
async def panel_http(hass, tmp_path, monkeypatch):
    """Serve only a temporary module until production assets are selected."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "ha-panel.js").write_text("export const testModule = true;\n")
    (tmp_path / "private.txt").write_text("not a static asset")
    monkeypatch.setattr(browser_panel, "STATIC_PATH", static)
    assert await async_setup_component(hass, "http", {})


@pytest.mark.usefixtures("panel_http")
async def test_real_panel_registration_and_admin_visibility(hass):
    await browser_panel.async_register_browser_panel(hass)
    await browser_panel.async_register_browser_panel(hass)
    assert not hass.config_entries.async_entries("panel_assistant")
    panel = hass.data[frontend.DATA_PANELS][browser_panel.PANEL_PATH]
    assert panel.sidebar_title is None
    assert panel.require_admin is True
    fleet = hass.data[frontend.DATA_PANELS][browser_panel.FLEET_PANEL_PATH]
    assert fleet.sidebar_title == "Panel Assistant"
    assert fleet.require_admin is True
    assert panel.config == {
        "installer_url": "https://install.panel-assistant.io/",
        "_panel_custom": {
            "name": "panel-assistant-usb-install",
            "embed_iframe": False,
            "trust_external": False,
            "handle_safe_area": False,
            "module_url": "/panel_assistant/usb/ha-panel.js",
        },
    }
    hass.data[frontend.DATA_PANELS_CONFIG] = {}
    for is_admin in (True, False):
        connection = SimpleNamespace(
            hass=hass, user=SimpleNamespace(is_admin=is_admin), send_message=Mock()
        )
        frontend.websocket_get_panels(hass, connection, {"id": 1})
        result = connection.send_message.call_args.args[0]["result"]
        assert (browser_panel.PANEL_PATH in result) is is_admin
        assert (browser_panel.FLEET_PANEL_PATH in result) is is_admin


@pytest.mark.usefixtures("panel_http")
async def test_legacy_domain_panel_does_not_block_setup(hass):
    """The predecessor owns `ha-paneld-usb`; sharing that path aborted our setup."""
    from homeassistant.components import panel_custom

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="ha-paneld-usb",
        webcomponent_name="ha-paneld-usb-install",
        module_url="/ha_paneld/usb/ha-panel.js",
        embed_iframe=False,
        require_admin=True,
    )

    await browser_panel.async_register_browser_panel(hass)

    panels = hass.data[frontend.DATA_PANELS]
    assert browser_panel.PANEL_PATH in panels
    assert browser_panel.FLEET_PANEL_PATH in panels
    assert panels["ha-paneld-usb"].config["_panel_custom"]["name"] == (
        "ha-paneld-usb-install"
    )


def test_registered_panel_paths_carry_the_current_brand() -> None:
    """A rename that misses a panel path silently collides with the old domain."""
    for path in (browser_panel.PANEL_PATH, browser_panel.FLEET_PANEL_PATH):
        assert path == "panel-assistant" or path.startswith("panel-assistant-")


@pytest.mark.usefixtures("panel_http")
async def test_actual_static_scope_and_cache(hass, hass_client_no_auth):
    await browser_panel.async_register_browser_panel(hass)
    client = await hass_client_no_auth()
    response = await client.get("/panel_assistant/usb/ha-panel.js")
    assert response.status == 200
    assert await response.text() == "export const testModule = true;\n"
    assert "max-age" not in response.headers.get("Cache-Control", "")
    for path in ("private.txt", "manifest.json", "browser_panel.py"):
        response = await client.get(f"/panel_assistant/usb/{path}")
        assert response.status == 404


async def test_concurrent_registration_is_awaited_once(hass, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def static_paths(paths):
        assert len(paths) == 1
        assert paths[0].url_path == "/panel_assistant/usb"
        assert paths[0].path == str(browser_panel.STATIC_PATH)
        assert paths[0].cache_headers is False
        entered.set()
        await release.wait()

    static = AsyncMock(side_effect=static_paths)
    hass.http = SimpleNamespace(
        async_register_static_paths=static, register_view=Mock()
    )
    panel = AsyncMock()
    monkeypatch.setattr(browser_panel.panel_custom, "async_register_panel", panel)
    first = asyncio.create_task(browser_panel.async_register_browser_panel(hass))
    await entered.wait()
    second = asyncio.create_task(browser_panel.async_register_browser_panel(hass))
    await asyncio.sleep(0)
    panel.assert_not_called()
    release.set()
    await asyncio.gather(first, second)
    static.assert_awaited_once()
    assert panel.await_count == 2


async def test_actual_shipped_module_is_served(hass, hass_client_no_auth):
    """The production route serves the committed standalone module only."""
    assert await async_setup_component(hass, "http", {})
    await browser_panel.async_register_browser_panel(hass)
    client = await hass_client_no_auth()
    response = await client.get("/panel_assistant/usb/ha-panel.js")
    assert response.status == 200
    expected = (browser_panel.STATIC_PATH / "ha-panel.js").read_bytes()
    assert await response.read() == expected
    assert b"panel-assistant-usb-install" in expected
    assert b"sourceMappingURL" not in expected
    for path in ("index.html", "package.json", "src/ha-install-panel.mjs"):
        assert (await client.get(f"/panel_assistant/usb/{path}")).status == 404


@pytest.mark.parametrize("failure_step", ["static", "panel"])
async def test_retry_preserves_completed_steps(hass, monkeypatch, failure_step):
    static = AsyncMock()
    panel = AsyncMock()
    hass.http = SimpleNamespace(
        async_register_static_paths=static, register_view=Mock()
    )
    monkeypatch.setattr(browser_panel.panel_custom, "async_register_panel", panel)
    failing = static if failure_step == "static" else panel
    failing.side_effect = [RuntimeError("registration failed"), None, None]
    with pytest.raises(RuntimeError, match="registration failed"):
        await browser_panel.async_register_browser_panel(hass)
    if failure_step == "static":
        panel.assert_not_called()
    await browser_panel.async_register_browser_panel(hass)
    await browser_panel.async_register_browser_panel(hass)
    assert static.await_count == (2 if failure_step == "static" else 1)
    assert panel.await_count == (3 if failure_step == "panel" else 2)


def test_installer_address_defaults_to_published_and_can_be_self_hosted(monkeypatch):
    """Self-hosters and development instances point Home Assistant at their own copy."""
    import importlib

    monkeypatch.delenv("PANEL_ASSISTANT_INSTALLER_URL", raising=False)
    assert importlib.reload(browser_panel).INSTALLER_URL == (
        "https://install.panel-assistant.io/"
    )
    monkeypatch.setenv("PANEL_ASSISTANT_INSTALLER_URL", "https://installer.example/")
    try:
        assert importlib.reload(browser_panel).INSTALLER_URL == (
            "https://installer.example/"
        )
    finally:
        monkeypatch.delenv("PANEL_ASSISTANT_INSTALLER_URL")
        importlib.reload(browser_panel)
