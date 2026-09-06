"""Authenticated, bounded delivery of verified browser installation releases."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.components.http.decorators import require_admin
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import HomeAssistantView

from .browser_artifacts import async_read_browser_artifact
from .browser_release_cache import (
    BrowserReleaseCache,
    BrowserReleaseCacheError,
    BrowserReleaseCacheErrorCode,
)
from .const import DOMAIN
from .release import is_rc_release_tag

DATA_BROWSER_DELIVERY = "browser_delivery"
_BODY_LIMIT = 1024
_BODY_TIMEOUT = 5.0
_DOWNLOAD_TIMEOUT = 60.0
_CHUNK_SIZE = 64 * 1024
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_ID = re.compile(r"[0-9a-f]{32}")


def _error(code: str, status: int) -> web.Response:
    return web.json_response({"error": code}, status=status, headers=_HEADERS)


def _cache_error(error: BrowserReleaseCacheError) -> web.Response:
    status = 404 if error.code == BrowserReleaseCacheErrorCode.NOT_FOUND else 503
    return _error(error.code.value, status)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _selection(request: web.Request) -> str | None:
    if (
        request.query
        or request.content_type != "application/json"
        or request.headers.get("Content-Encoding", "identity") != "identity"
    ):
        raise ValueError
    body = bytearray()
    async with asyncio.timeout(_BODY_TIMEOUT):
        async for chunk in request.content.iter_chunked(_BODY_LIMIT + 1):
            body.extend(chunk)
            if len(body) > _BODY_LIMIT:
                raise ValueError
    value = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) - {"release_candidate"}:
        raise ValueError
    if not value:
        return None
    tag = value["release_candidate"]
    if not is_rc_release_tag(tag):
        raise ValueError
    assert isinstance(tag, str)
    return tag


class BrowserDelivery:
    """Own request admission and drain requests before cache shutdown."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache = BrowserReleaseCache(hass, async_get_clientsession(hass))
        self._requests: set[asyncio.Task[Any]] = set()
        self._closed = False

    @asynccontextmanager
    async def async_admit(self) -> AsyncIterator[None]:
        if self._closed:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.CLOSED)
        if len(self._requests) >= 2:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.BUSY)
        task = asyncio.current_task()
        assert task is not None
        self._requests.add(task)
        try:
            yield
        finally:
            self._requests.discard(task)

    async def async_stop(self, event: Event) -> None:
        """Cancel active work, drain its cleanup, then close artifact custody."""
        self._closed = True
        tasks = tuple(self._requests)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.cache.async_close()


class BrowserReleaseView(HomeAssistantView):
    """Prepare a fixed-repository signed release for the authenticated admin."""

    url = "/api/ha_paneld/usb/release"
    name = "api:ha_paneld:usb:release"
    requires_auth = True

    def __init__(self, service: BrowserDelivery) -> None:
        self.service = service

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        user: User = request["hass_user"]
        try:
            async with self.service.async_admit():
                try:
                    tag = await _selection(request)
                except ValueError, TimeoutError, ConnectionError:
                    return _error("browser_release_invalid_request", 400)
                record = await self.service.cache.async_prepare(user.id, rc_tag=tag)
                metadata = record.bundle.metadata
                return web.json_response(
                    {
                        "id": record.id,
                        "tag": record.bundle.artifact.tag,
                        "checksum": base64.b64encode(metadata.checksum).decode("ascii"),
                        "checksum_signature": base64.b64encode(
                            metadata.checksum_signature
                        ).decode("ascii"),
                        "descriptor": base64.b64encode(
                            metadata.descriptor_bytes
                        ).decode("ascii"),
                        "descriptor_signature": base64.b64encode(
                            metadata.descriptor_signature
                        ).decode("ascii"),
                        "apk_size": record.artifact.size,
                        "apk_sha256": record.artifact.sha256,
                    },
                    headers=_HEADERS,
                )
        except BrowserReleaseCacheError as error:
            return _cache_error(error)
        except Exception:
            return _error("browser_release_prepare_failed", 503)


class BrowserApkView(HomeAssistantView):
    """Serve only the current admin's opaque, leased browser artifact."""

    url = "/api/ha_paneld/usb/release/{bundle_id}/apk"
    name = "api:ha_paneld:usb:apk"
    requires_auth = True

    def __init__(self, service: BrowserDelivery) -> None:
        self.service = service

    @require_admin
    async def get(self, request: web.Request, bundle_id: str) -> web.StreamResponse:
        user: User = request["hass_user"]
        if request.query or _ID.fullmatch(bundle_id) is None:
            return _error("browser_release_not_found", 404)
        response: web.StreamResponse | None = None
        headers_sent = False
        try:
            async with (
                self.service.async_admit(),
                self.service.cache.async_lease(user.id, bundle_id) as record,
            ):
                async with asyncio.timeout(_DOWNLOAD_TIMEOUT):
                    data = await async_read_browser_artifact(
                        self.service.hass, bundle_id, record.artifact
                    )
                    response = web.StreamResponse(headers=_HEADERS)
                    response.content_type = "application/vnd.android.package-archive"
                    response.content_length = len(data)
                    await response.prepare(request)
                    headers_sent = True
                    for offset in range(0, len(data), _CHUNK_SIZE):
                        await response.write(data[offset : offset + _CHUNK_SIZE])
                    await response.write_eof()
                    return response
        except BrowserReleaseCacheError as error:
            if not headers_sent and (response is None or not response.prepared):
                return _cache_error(error)
        except asyncio.CancelledError:
            if (
                response is not None
                and (headers_sent or response.prepared)
                and request.transport
            ):
                request.transport.close()
            raise
        except Exception:
            if not headers_sent and (response is None or not response.prepared):
                return _error("browser_release_download_failed", 503)
        # Once headers have been sent, truncation signals failure to the client.
        if request.transport:
            request.transport.close()
        assert response is not None
        return response


@callback
def async_register_browser_delivery(hass: HomeAssistant) -> None:
    """Register once per domain, without requiring a panel config entry."""
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_BROWSER_DELIVERY in data:
        return
    service = BrowserDelivery(hass)
    hass.http.register_view(BrowserReleaseView(service))
    hass.http.register_view(BrowserApkView(service))
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, service.async_stop)
    data[DATA_BROWSER_DELIVERY] = service
