"""Real HA HTTP authentication and bounded browser delivery contracts."""

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientPayloadError
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.setup import async_setup_component

from custom_components.panel_assistant import browser_delivery as delivery
from custom_components.panel_assistant.browser_release_cache import (
    BrowserReleaseCacheError,
    BrowserReleaseCacheErrorCode,
    BrowserReleaseRecord,
)
from custom_components.panel_assistant.install_artifacts import InstallArtifact
from custom_components.panel_assistant.release import (
    InstallReleaseBundle,
    ReleaseArtifact,
    SignedReleaseMetadata,
)

URL = "/api/panel_assistant/usb/release"
BUNDLE_ID = "a" * 32


@pytest.fixture
async def endpoint(hass, hass_client, monkeypatch):
    assert await async_setup_component(hass, "http", {})
    delivery.async_register_browser_delivery(hass)
    service = hass.data["panel_assistant"][delivery.DATA_BROWSER_DELIVERY]
    record = BrowserReleaseRecord(
        BUNDLE_ID,
        InstallReleaseBundle(
            ReleaseArtifact("v1.2.3", "1.2.3", "release.apk", "private-url", "b" * 64),
            SignedReleaseMetadata(b"checksum\n", b"sig\x00", b'{"raw": 1}', b"sig2"),
        ),
        InstallArtifact(BUNDLE_ID, "/private/path", 3, "b" * 64),
    )
    state = SimpleNamespace(owner=None, leased=False, record=record)

    async def prepare(user_id, *, rc_tag=None):
        state.owner = user_id
        return record

    @asynccontextmanager
    async def lease(user_id, bundle_id):
        if user_id != state.owner or bundle_id != BUNDLE_ID:
            raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.NOT_FOUND)
        state.leased = True
        try:
            yield record
        finally:
            state.leased = False

    service.cache = SimpleNamespace(
        async_prepare=AsyncMock(side_effect=prepare),
        async_lease=lease,
        async_close=AsyncMock(),
    )
    monkeypatch.setattr(
        delivery, "async_read_browser_artifact", AsyncMock(return_value=b"apk")
    )
    state.service = service
    state.client = await hass_client()
    return state


async def test_idempotent_no_entries_and_exact_public_bytes(endpoint, hass):
    delivery.async_register_browser_delivery(hass)
    assert (
        hass.data["panel_assistant"][delivery.DATA_BROWSER_DELIVERY] is endpoint.service
    )
    assert not hass.config_entries.async_entries("panel_assistant")
    response = await endpoint.client.post(URL, json={})
    assert response.status == 200
    assert await response.json() == {
        "id": BUNDLE_ID,
        "tag": "v1.2.3",
        "checksum": base64.b64encode(b"checksum\n").decode(),
        "checksum_signature": base64.b64encode(b"sig\x00").decode(),
        "descriptor": base64.b64encode(b'{"raw": 1}').decode(),
        "descriptor_signature": base64.b64encode(b"sig2").decode(),
        "apk_size": 3,
        "apk_sha256": "b" * 64,
    }
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    assert response.status == 200
    assert await response.read() == b"apk"
    assert response.headers["Content-Type"] == "application/vnd.android.package-archive"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert not endpoint.leased
    assert not endpoint.service._requests


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "null",
        "1",
        "{",
        '{"url":"https://example.com"}',
        '{"release_candidate":null}',
        '{"release_candidate":"v1.2.3"}',
        '{"release_candidate":"v1.2.3-rc1","release_candidate":"v1.2.3-rc2"}',
        " " * 1025 + "{}",
    ],
)
async def test_closed_body_schema(endpoint, body):
    response = await endpoint.client.post(
        URL, data=body, headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    assert await response.json() == {"error": "browser_release_invalid_request"}
    endpoint.service.cache.async_prepare.assert_not_called()


async def test_exact_rc_and_query_refusal(endpoint):
    response = await endpoint.client.post(URL, json={"release_candidate": "v1.2.3-rc1"})
    assert response.status == 200
    assert endpoint.service.cache.async_prepare.call_args.kwargs == {
        "rc_tag": "v1.2.3-rc1"
    }
    response = await endpoint.client.post(URL + "?url=https://example.com", json={})
    assert response.status == 400
    response = await endpoint.client.get(
        f"{URL}/{BUNDLE_ID}/apk?url=https://example.com"
    )
    assert response.status == 404


async def test_auth_and_nonadmin(
    endpoint, hass_client_no_auth, hass_read_only_access_token
):
    client = await hass_client_no_auth()
    for method, url in [("post", URL), ("get", f"{URL}/{BUNDLE_ID}/apk")]:
        response = await getattr(client, method)(url)
        assert response.status == 401
        response = await getattr(client, method)(
            url, headers={"Authorization": f"Bearer {hass_read_only_access_token}"}
        )
        # HA's require_admin raises Unauthorized, mapped to HTTP 401 by core.
        assert response.status == 401
        assert await response.text() == "401: Unauthorized"
    endpoint.service.cache.async_prepare.assert_not_called()


async def test_ownership_and_unknown_id_share_refusal(endpoint):
    await endpoint.client.post(URL, json={})
    endpoint.owner = "another-admin"
    for bundle_id in [BUNDLE_ID, "b" * 32, "invalid"]:
        response = await endpoint.client.get(f"{URL}/{bundle_id}/apk")
        assert response.status == 404
        assert await response.json() == {"error": "browser_release_not_found"}


async def test_failures_do_not_expose_exception(endpoint, monkeypatch):
    endpoint.service.cache.async_prepare.side_effect = RuntimeError(
        "/private secret token"
    )
    response = await endpoint.client.post(URL, json={})
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_prepare_failed"}

    # The reader is reached only after the ownership boundary permits this user.
    @asynccontextmanager
    async def lease(*args):
        yield endpoint.record

    endpoint.service.cache.async_lease = lease
    monkeypatch.setattr(
        delivery,
        "async_read_browser_artifact",
        AsyncMock(side_effect=RuntimeError("/private token")),
    )
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_download_failed"}


async def test_admission_and_stop_cancel_and_drain(endpoint):
    entered = asyncio.Event()
    cleaned = []

    async def hold():
        async with endpoint.service.async_admit():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

    first = asyncio.create_task(hold())
    await entered.wait()
    entered.clear()
    second = asyncio.create_task(hold())
    await entered.wait()
    response = await endpoint.client.post(URL, json={})
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_busy"}
    await endpoint.service.async_stop(None)
    assert first.cancelled() and second.cancelled()
    assert cleaned == [True, True]
    endpoint.service.cache.async_close.assert_awaited_once()
    response = await endpoint.client.post(URL, json={})
    assert await response.json() == {"error": "browser_release_closed"}


async def test_chunked_body_limit_and_deadline(endpoint, monkeypatch):
    async def oversized():
        for _ in range(5):
            yield b" " * 256
        yield b"{}"

    response = await endpoint.client.post(
        URL, data=oversized(), headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    monkeypatch.setattr(delivery, "_BODY_TIMEOUT", 0.01)

    async def stalled():
        yield b"{"
        await asyncio.Event().wait()

    response = await endpoint.client.post(
        URL, data=stalled(), headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    endpoint.service.cache.async_prepare.assert_not_called()


async def test_stream_holds_admission_and_lease_through_eof(endpoint, monkeypatch):
    await endpoint.client.post(URL, json={})
    entered = asyncio.Event()
    release = asyncio.Event()
    original = delivery.web.StreamResponse.write_eof
    writes = []
    original_write = delivery.web.StreamResponse.write

    async def write(response, data):
        writes.append(len(data))
        await original_write(response, data)

    async def eof(response, data=b""):
        if response.content_type == "application/vnd.android.package-archive":
            entered.set()
            await release.wait()
        await original(response, data)

    monkeypatch.setattr(delivery.web.StreamResponse, "write_eof", eof)
    monkeypatch.setattr(delivery.web.StreamResponse, "write", write)
    apk = b"x" * (delivery._CHUNK_SIZE * 2 + 1)
    monkeypatch.setattr(
        delivery, "async_read_browser_artifact", AsyncMock(return_value=apk)
    )
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    await entered.wait()
    assert endpoint.leased
    assert len(endpoint.service._requests) == 1
    assert writes == [delivery._CHUNK_SIZE, delivery._CHUNK_SIZE, 1]
    release.set()
    assert await response.read() == apk
    await asyncio.sleep(0)


async def test_read_timeout_and_request_cancellation(endpoint, monkeypatch):
    await endpoint.client.post(URL, json={})
    monkeypatch.setattr(delivery, "_DOWNLOAD_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def read(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(delivery, "async_read_browser_artifact", read)
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    assert response.status == 503
    assert await response.json() == {"error": "browser_release_download_failed"}
    assert cancelled.is_set()
    assert not endpoint.leased
    assert not endpoint.service._requests


async def test_send_timeout_closes_committed_stream(endpoint, monkeypatch):
    await endpoint.client.post(URL, json={})
    monkeypatch.setattr(delivery, "_DOWNLOAD_TIMEOUT", 0.01)

    async def stalled_write(response, data):
        await asyncio.Event().wait()

    monkeypatch.setattr(delivery.web.StreamResponse, "write", stalled_write)
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    assert response.status == 200
    with pytest.raises(ClientPayloadError):
        await response.read()
    assert not endpoint.leased
    assert not endpoint.service._requests


async def test_stop_listener_cancels_actual_prepare_before_cache_close(endpoint, hass):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def prepare(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()

    endpoint.service.cache.async_prepare.side_effect = prepare
    request = asyncio.create_task(endpoint.client.post(URL, json={}))
    await started.wait()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await cleanup_started.wait()
    assert endpoint.service._closed
    endpoint.service.cache.async_close.assert_not_awaited()
    assert len(endpoint.service._requests) == 1
    cleanup_release.set()
    await hass.async_block_till_done()
    await asyncio.gather(request, return_exceptions=True)
    endpoint.service.cache.async_close.assert_awaited_once()
    assert not endpoint.service._requests


async def test_cleanup_failure_after_eof_does_not_send_second_response(
    endpoint, monkeypatch
):
    @asynccontextmanager
    async def lease(*args):
        yield endpoint.record
        raise BrowserReleaseCacheError(BrowserReleaseCacheErrorCode.CLEANUP_FAILED)

    endpoint.service.cache.async_lease = lease
    error_response = Mock(wraps=delivery._error)
    monkeypatch.setattr(delivery, "_error", error_response)
    response = await endpoint.client.get(f"{URL}/{BUNDLE_ID}/apk")
    assert response.status == 200
    assert await response.read() == b"apk"
    error_response.assert_not_called()
    assert not endpoint.service._requests
