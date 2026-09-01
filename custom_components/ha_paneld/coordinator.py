"""Data coordinator for the ha-paneld integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import HaPaneldClient, HaPaneldError, PanelHealth
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class HaPaneldDataUpdateCoordinator(DataUpdateCoordinator[PanelHealth]):
    """Poll the panel's stable health endpoint."""

    def __init__(self, hass: HomeAssistant, client: HaPaneldClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> PanelHealth:
        """Fetch the latest health snapshot."""
        try:
            return await self.client.async_get_health()
        except HaPaneldError as err:
            raise UpdateFailed(f"Unable to read panel health: {err}") from err
