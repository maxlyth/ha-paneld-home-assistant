"""Poll a signed build feed, only when one is configured."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from yarl import URL

from .build_feed import BuildFeed, BuildFeedError, async_fetch_build_feed
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
DATA_BUILD_FEED = "build_feed"
CONF_BUILD_FEED = "build_feed"
FEED_REFRESH = timedelta(minutes=15)


class BuildFeedCoordinator(DataUpdateCoordinator[BuildFeed]):
    """One authenticated feed shared by every panel on this Home Assistant."""

    def __init__(self, hass: HomeAssistant, feed_url: URL) -> None:
        """Remember exactly which feed this Home Assistant was pointed at."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN} build feed",
            update_interval=FEED_REFRESH,
        )
        self.feed_url = feed_url

    async def _async_update_data(self) -> BuildFeed:
        try:
            return await async_fetch_build_feed(
                async_get_clientsession(self.hass), self.feed_url
            )
        except BuildFeedError as err:
            raise UpdateFailed("The build feed could not be authenticated") from err


def async_get_feed_coordinator(hass: HomeAssistant) -> BuildFeedCoordinator | None:
    """Return the feed coordinator, or None when no feed is configured."""
    coordinator = hass.data.get(DOMAIN, {}).get(DATA_BUILD_FEED)
    return coordinator if isinstance(coordinator, BuildFeedCoordinator) else None
