"""Local operation recovery for configured Panel Assistant updates."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidResponseError,
    PanelInstallStatus,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PanelUpdateSnapshot:
    """One local operation-status observation, without a release lookup."""

    operation: PanelInstallStatus | None
    error: str | None


class PanelUpdateCoordinator(DataUpdateCoordinator[PanelUpdateSnapshot]):
    """Follow only the panel's local destructive-operation state."""

    def __init__(self, hass: HomeAssistant, client: HaPaneldClient) -> None:
        """Initialize a local-only companion to the health coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_update",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> PanelUpdateSnapshot:
        """Keep an unavailable local progress endpoint out of normal entry setup."""
        try:
            operation = await self.client.async_get_panel_install_status()
        except CannotConnectError:
            return PanelUpdateSnapshot(operation=None, error="unavailable")
        except InvalidResponseError:
            return PanelUpdateSnapshot(operation=None, error="invalid_response")
        return PanelUpdateSnapshot(operation=operation, error=None)
