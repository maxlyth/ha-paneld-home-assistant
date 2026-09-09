"""Register the integration's hidden administrator USB panel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .fleet import FleetView, ProvisioningPlanView

DATA_BROWSER_PANEL = "browser_panel"
STATIC_PATH = Path(__file__).parent / "static"
STATIC_URL = "/panel_assistant/usb"
PANEL_PATH = "panel-assistant-usb"
FLEET_PANEL_PATH = "panel-assistant"
INSTALLER_URL = "https://install.panel-assistant.io/"


@dataclass
class _Registration:
    """Retain successful registration steps across concurrent calls and retries."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    static_registered: bool = False
    panel_registered: bool = False
    fleet_registered: bool = False
    fleet_view_registered: bool = False


async def async_register_browser_panel(hass: HomeAssistant) -> None:
    """Register once per HA lifetime, independently of configuration entries."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registration = domain_data.setdefault(DATA_BROWSER_PANEL, _Registration())
    async with registration.lock:
        if not registration.fleet_view_registered:
            hass.http.register_view(FleetView(hass))
            hass.http.register_view(ProvisioningPlanView(hass))
            registration.fleet_view_registered = True
        if not registration.static_registered:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(STATIC_URL, str(STATIC_PATH), cache_headers=False)]
            )
            registration.static_registered = True
        if not registration.panel_registered:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=PANEL_PATH,
                webcomponent_name="panel-assistant-usb-install",
                module_url=f"{STATIC_URL}/ha-panel.js",
                sidebar_title=None,
                embed_iframe=False,
                require_admin=True,
                config={"installer_url": INSTALLER_URL},
            )
            registration.panel_registered = True
        if not registration.fleet_registered:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=FLEET_PANEL_PATH,
                webcomponent_name="panel-assistant-fleet",
                module_url=f"{STATIC_URL}/ha-panel.js",
                sidebar_title="Panel Assistant",
                sidebar_icon="mdi:tablet-dashboard",
                embed_iframe=False,
                require_admin=True,
            )
            registration.fleet_registered = True
