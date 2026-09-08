"""Discover bounded install choices; selections still require verification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientSession
from yarl import URL

from .release import (
    _API_HEADERS,
    _LATEST_RELEASE_URL,
    _MAX_RELEASE_RESPONSE_BYTES,
    ReleaseResolutionError,
    _async_fetch_bounded,
    _object_without_duplicates,
    _parse_release_metadata,
    _reject_json_constant,
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
