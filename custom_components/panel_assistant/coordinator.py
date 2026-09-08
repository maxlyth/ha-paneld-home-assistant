"""Data coordinator for the ha-paneld integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    CannotConnectError,
    HaPaneldClient,
    HaPaneldError,
    InvalidResponseError,
    PanelHealth,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .status import PanelStatus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PanelSnapshot:
    """Cached health authority and optional sanitized status diagnostics."""

    health: PanelHealth
    status: PanelStatus | None
    status_error: str | None


class HaPaneldDataUpdateCoordinator(DataUpdateCoordinator[PanelSnapshot]):
    """Poll stable health plus optional read-only status diagnostics."""

    def __init__(self, hass: HomeAssistant, client: HaPaneldClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> PanelSnapshot:
        """Fetch health authority, then best-effort sanitized status."""
        try:
            health = await self.client.async_get_health()
        except HaPaneldError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="health_update_failed",
            ) from err

        try:
            status = await self.client.async_get_status()
        except CannotConnectError:
            return PanelSnapshot(health=health, status=None, status_error="unavailable")
        except InvalidResponseError:
            return PanelSnapshot(
                health=health, status=None, status_error="invalid_response"
            )
        return PanelSnapshot(health=health, status=status, status_error=None)
