"""Bounded, user-bound custody of signed browser installation bundles."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant

from .browser_artifacts import (
    async_cleanup_browser_artifact,
    async_download_browser_artifact,
    async_reconcile_browser_artifacts,
)
from .build_feed import FeedInstallBundle
from .install_artifacts import InstallArtifact
from .release import InstallReleaseBundle
from .release_catalog import async_resolve_install_bundle_choice

_CAPACITY = 2
_TTL_SECONDS = 900.0
_MAX_APK_BYTES = 64 * 1024 * 1024


class BrowserReleaseCacheErrorCode(StrEnum):
    """Public failures without filesystem or upstream response details."""

    BUSY = "browser_release_busy"
    NOT_FOUND = "browser_release_not_found"
    PREPARE_FAILED = "browser_release_prepare_failed"
    CLEANUP_FAILED = "browser_release_cleanup_failed"
    CLOSED = "browser_release_closed"


class BrowserReleaseCacheError(Exception):
    """A stable browser bundle failure."""

    def __init__(self, code: BrowserReleaseCacheErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, repr=False)
class BrowserReleaseRecord:
    """Internal verified bundle; callers must explicitly select public fields."""

    id: str
    bundle: InstallReleaseBundle | FeedInstallBundle
    artifact: InstallArtifact


@dataclass(slots=True)
class _Entry:
    record: BrowserReleaseRecord
    user_id: str
    rc_tag: str | None
    expires: float
    leases: int = 0
    retiring: bool = False


async def _finish_cleanup(operation: Coroutine[Any, Any, None]) -> None:
    """Drain cleanup even if its caller receives repeated cancellation."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


class BrowserReleaseCache:
    """One event-loop-owned cache, independent of native installer jobs."""

    def __init__(self, hass: HomeAssistant, session: ClientSession) -> None:
        self._hass = hass
        self._session = session
        self._entries: dict[str, _Entry] = {}
        self._initialized = False
        self._busy = False
        self._closed = False
        self._failed = False

    def _check_available(self) -> None:
        if self._failed:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.CLEANUP_FAILED)
        if self._closed:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.CLOSED)

    async def _cleanup(self, bundle_id: str) -> None:
        if entry := self._entries.get(bundle_id):
            entry.retiring = True
        try:
            await async_cleanup_browser_artifact(self._hass, bundle_id)
        except Exception:
            self._failed = True
            raise BrowserReleaseCacheError(
                BrowserReleaseCacheErrorCode.CLEANUP_FAILED
            ) from None
        self._entries.pop(bundle_id, None)

    async def _reap(self) -> None:
        for bundle_id, entry in tuple(self._entries.items()):
            if not entry.leases and (self._closed or entry.expires <= monotonic()):
                await self._cleanup(bundle_id)

    async def async_prepare(
        self, user_id: str, *, rc_tag: str | None = None
    ) -> BrowserReleaseRecord:
        """Return a cached selection or prepare one without queuing downloads."""
        self._check_available()
        for entry in self._entries.values():
            if (
                entry.user_id == user_id
                and not entry.retiring
                and entry.rc_tag == rc_tag
                and entry.expires > monotonic()
            ):
                return entry.record
        if self._busy:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.BUSY)
        self._busy = True
        reservation: str | None = None
        try:
            if not self._initialized:
                try:
                    await _finish_cleanup(
                        async_reconcile_browser_artifacts(self._hass, ())
                    )
                except Exception:
                    self._failed = True
                    raise BrowserReleaseCacheError(
                        BrowserReleaseCacheErrorCode.CLEANUP_FAILED
                    ) from None
                self._initialized = True
            await _finish_cleanup(self._reap())
            if len(self._entries) >= _CAPACITY:
                raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.BUSY)
            reservation = secrets.token_hex(16)
            while reservation in self._entries:
                reservation = secrets.token_hex(16)
            bundle = await async_resolve_install_bundle_choice(self._hass, rc_tag)
            descriptor = bundle.artifact.descriptor
            if descriptor is None or not 0 < descriptor.apk_size <= _MAX_APK_BYTES:
                raise BrowserReleaseCacheError(
                    BrowserReleaseCacheErrorCode.PREPARE_FAILED
                )
            artifact = await async_download_browser_artifact(
                self._hass, self._session, bundle.artifact, reservation
            )
            record = BrowserReleaseRecord(reservation, bundle, artifact)
            self._entries[reservation] = _Entry(
                record, user_id, rc_tag, monotonic() + _TTL_SECONDS
            )
            reservation = None
            return record
        except BrowserReleaseCacheError:
            raise
        except Exception:
            raise BrowserReleaseCacheError(
                BrowserReleaseCacheErrorCode.PREPARE_FAILED
            ) from None
        finally:
            try:
                if reservation is not None:
                    await _finish_cleanup(self._cleanup(reservation))
            finally:
                self._busy = False

    @asynccontextmanager
    async def async_lease(
        self, user_id: str, bundle_id: str
    ) -> AsyncIterator[BrowserReleaseRecord]:
        """Pin an unexpired user's bundle for the entire response lifetime."""
        self._check_available()
        entry = self._entries.get(bundle_id)
        if (
            entry is None
            or entry.retiring
            or entry.user_id != user_id
            or entry.expires <= monotonic()
        ):
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.NOT_FOUND)
        entry.leases += 1
        try:
            yield entry.record
        finally:
            entry.leases -= 1
            if not self._busy:
                self._busy = True
                try:
                    await _finish_cleanup(self._reap())
                finally:
                    self._busy = False

    async def async_close(self) -> None:
        """Refuse new requests and clean files as existing leases finish."""
        if self._busy:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.BUSY)
        self._closed = True
        self._busy = True
        try:
            await _finish_cleanup(self._reap())
        finally:
            self._busy = False
