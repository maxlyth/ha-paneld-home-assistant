"""Discover bounded install choices; selections still require verification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .build_feed import BuildFeed, FeedBuild, FeedInstallBundle, feed_release_artifact
from .feed_coordinator import async_get_feed_coordinator
from .release import (
    _API_HEADERS,
    _LATEST_RELEASE_URL,
    _MAX_RELEASE_RESPONSE_BYTES,
    InstallReleaseBundle,
    ReleaseArtifact,
    ReleaseResolutionError,
    _async_fetch_bounded,
    _object_without_duplicates,
    _parse_release_metadata,
    _reject_json_constant,
    async_resolve_install_bundle,
    async_resolve_rc_release,
    async_resolve_stable_release,
    feed_build_code,
    feed_build_tag,
    is_rc_release_tag,
)

_RECENT_RELEASES_URL = URL(
    "https://api.github.com/repos/maxlyth/ha-paneld/releases?per_page=30"
)
_MAX_RECENT_RELEASES = 30
_MAX_CATALOG_BYTES = 1024 * 1024


def _decode(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as err:
        raise ReleaseResolutionError from err


def _choice(document: Any, *, prerelease: bool) -> dict[str, str | bool] | None:
    if not isinstance(document, dict):
        return None
    tag = document.get("tag_name")
    if prerelease and not is_rc_release_tag(tag):
        return None
    try:
        tag, _, assets = _parse_release_metadata(
            json.dumps(document).encode(),
            expected_rc_tag=tag if prerelease else None,
        )
    except ReleaseResolutionError:
        return None
    descriptor = f"ha-paneld-{tag}-install.json"
    if descriptor not in assets or f"{descriptor}.sig" not in assets:
        return None
    return {"tag": tag, "prerelease": prerelease}


async def async_list_install_releases(
    session: ClientSession,
) -> list[dict[str, str | bool]]:
    """List latest stable and recent RCs with complete installation assets.

    Metadata discovery neither authenticates assets nor opts into testing.
    Invalid/incomplete releases are omitted; transport or catalogue failures
    raise ReleaseResolutionError, rather than reporting an empty catalogue.
    """
    try:
        async with asyncio.timeout(20):
            latest = _decode(
                await _async_fetch_bounded(
                    session,
                    _LATEST_RELEASE_URL,
                    _MAX_RELEASE_RESPONSE_BYTES,
                    allow_release_redirects=False,
                    headers=_API_HEADERS,
                )
            )
            recent = _decode(
                await _async_fetch_bounded(
                    session,
                    _RECENT_RELEASES_URL,
                    _MAX_CATALOG_BYTES,
                    allow_release_redirects=False,
                    headers=_API_HEADERS,
                )
            )
    except TimeoutError as err:
        raise ReleaseResolutionError from err
    if (
        not isinstance(latest, dict)
        or not isinstance(recent, list)
        or len(recent) > _MAX_RECENT_RELEASES
    ):
        raise ReleaseResolutionError
    choices = []
    stable = _choice(latest, prerelease=False)
    if stable is not None:
        choices.append(stable)
    seen = set()
    for document in recent:
        choice = _choice(document, prerelease=True)
        if choice is not None and choice["tag"] not in seen:
            choices.append(choice)
            seen.add(choice["tag"])
            if len(choices) == _MAX_RECENT_RELEASES:
                break
    return choices


async def _async_current_feed(hass: HomeAssistant) -> BuildFeed | None:
    """Read the configured build feed afresh, or None when there is none."""
    coordinator = async_get_feed_coordinator(hass)
    if coordinator is None:
        return None
    await coordinator.async_refresh()
    if not coordinator.last_update_success or coordinator.data is None:
        return None
    return coordinator.data


async def async_list_install_choices(
    hass: HomeAssistant,
) -> list[dict[str, str | bool]]:
    """The one version list every install path offers.

    GitHub releases come first; builds from a configured signed build feed
    follow, newest first, each named by its version code. A GitHub outage still
    leaves the feed's builds on offer, and the reverse.
    """
    feed = await _async_current_feed(hass)
    builds: list[dict[str, str | bool]] = [
        {
            "tag": feed_build_tag(build.version_code),
            "prerelease": True,
            "name": build.label,
        }
        for build in (feed.builds if feed is not None else ())
    ]
    try:
        releases = await async_list_install_releases(async_get_clientsession(hass))
    except ReleaseResolutionError:
        if builds:
            return builds
        raise
    return [*releases, *builds]


async def async_resolve_feed_choice(
    hass: HomeAssistant, tag: str
) -> tuple[BuildFeed, FeedBuild]:
    """Find exactly the feed build a choice names, in a freshly read feed."""
    code = feed_build_code(tag)
    feed = await _async_current_feed(hass) if code is not None else None
    build = feed.find(code) if feed is not None and code is not None else None
    if feed is None or build is None:
        raise ReleaseResolutionError
    return feed, build


async def async_resolve_install_choice(
    hass: HomeAssistant, tag: str | None
) -> ReleaseArtifact:
    """The one resolver behind every install path: stable, an RC, or a feed build."""
    if tag is None:
        return await async_resolve_stable_release(async_get_clientsession(hass))
    if feed_build_code(tag) is not None:
        _feed, build = await async_resolve_feed_choice(hass, tag)
        return feed_release_artifact(build)
    if not is_rc_release_tag(tag):
        raise ReleaseResolutionError
    return await async_resolve_rc_release(async_get_clientsession(hass), tag)


async def async_resolve_install_bundle_choice(
    hass: HomeAssistant, tag: str | None
) -> InstallReleaseBundle | FeedInstallBundle:
    """Resolve a choice with the signed bytes a browser verifies for itself."""
    if tag is not None and feed_build_code(tag) is not None:
        feed, build = await async_resolve_feed_choice(hass, tag)
        return FeedInstallBundle(
            artifact=feed_release_artifact(build),
            feed=feed.raw,
            feed_signature=feed.signature,
        )
    if tag is not None and not is_rc_release_tag(tag):
        raise ReleaseResolutionError
    return await async_resolve_install_bundle(async_get_clientsession(hass), rc_tag=tag)
