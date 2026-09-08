"""Administrator inventory projected from existing coordinator caches."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.http.decorators import require_admin
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

from .const import DOMAIN

MAX_PANELS = 200
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class FleetView(HomeAssistantView):
    """Read cached inventory without triggering network activity or mutations."""

    url = "/api/panel_assistant/fleet"
    name = "api:panel_assistant:fleet"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        if request.query:
            return web.json_response(
                {"error": "invalid_request"}, status=400, headers=HEADERS
            )
        entries = self.hass.config_entries.async_entries(DOMAIN)
        panels = []
        for entry in entries[:MAX_PANELS]:
            runtime = getattr(entry, "runtime_data", None)
            coordinator = getattr(runtime, "coordinator", None)
            available = bool(
                entry.state is ConfigEntryState.LOADED
                and coordinator is not None
                and coordinator.last_update_success
                and coordinator.data is not None
            )
            snapshot = coordinator.data if available and coordinator else None
            status = snapshot.status if snapshot else None
            panels.append(
                {
                    "entry_id": entry.entry_id,
                    "name": entry.title[:256],
                    "available": available,
                    "version": snapshot.health.version if snapshot else None,
                    "warning_count": status.warning_count if status else None,
                    "status_available": status is not None,
                }
            )
        return web.json_response(
            {"panels": panels, "truncated": len(entries) > MAX_PANELS},
            headers=HEADERS,
        )
