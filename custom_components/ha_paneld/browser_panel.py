"""Register the integration's hidden administrator USB panel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

DATA_BROWSER_PANEL = "browser_panel"
STATIC_PATH = Path(__file__).parent / "static"
STATIC_URL = "/ha_paneld/usb"
PANEL_PATH = "ha-paneld-usb"
INSTALLER_URL = "https://install.ha-paneld.com/"


@dataclass
class _Registration:
    """Retain successful registration steps across concurrent calls and retries."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    static_registered: bool = False
    panel_registered: bool = False


async def async_register_browser_panel(hass: HomeAssistant) -> None:
    """Register once per HA lifetime, independently of configuration entries."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registration = domain_data.setdefault(DATA_BROWSER_PANEL, _Registration())
    async with registration.lock:
        if not registration.static_registered:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(STATIC_URL, str(STATIC_PATH), cache_headers=False)]
            )
            registration.static_registered = True
        if not registration.panel_registered:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=PANEL_PATH,
                webcomponent_name="ha-paneld-usb-install",
                module_url=f"{STATIC_URL}/ha-panel.js",
                sidebar_title=None,
                embed_iframe=False,
                require_admin=True,
                config={"installer_url": INSTALLER_URL},
            )
            registration.panel_registered = True
