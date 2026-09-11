"""Cache admission, custody lifetime and cancellation boundaries."""

from __future__ import annotations

import asyncio
import re
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from custom_components.panel_assistant import browser_release_cache as cache_module
from custom_components.panel_assistant.browser_release_cache import (
    BrowserReleaseCache,
    BrowserReleaseCacheError,
)
from custom_components.panel_assistant.install_artifacts import InstallArtifact
from custom_components.panel_assistant.release import (
    InstallReleaseBundle,
    ReleaseArtifact,
    SignedReleaseMetadata,
)


@pytest.fixture
def services(monkeypatch):
    state = SimpleNamespace(
        now=100.0,
        files=set(),
        cleaned=[],
        reconciled=[],
        resolved=[],
        started=asyncio.Event(),
        cleanup_started=asyncio.Event(),
        download_gate=None,
        cleanup_gate=None,
        download_error=None,
        cleanup_error=None,
        reconcile_error=None,
        size=17,
    )

    async def resolve(session, *, rc_tag=None):
        state.resolved.append(rc_tag)
        return InstallReleaseBundle(
            ReleaseArtifact(
                rc_tag or "v1.2.3",
                "1.2.3",
                "release.apk",
                "https://example.com",
                "a" * 64,
                SimpleNamespace(apk_size=state.size),
            ),
            SignedReleaseMetadata(b"checksum", b"signature", b"json", b"sig"),
        )

    async def download(hass, session, artifact, bundle_id):
        state.files.add(bundle_id)
        state.started.set()
        if state.download_gate is not None:
            await state.download_gate.wait()
        if state.download_error:
            raise state.download_error
        return InstallArtifact(bundle_id, "/private/" + bundle_id, 17, "a" * 64)

    async def cleanup(hass, bundle_id):
        state.cleaned.append(bundle_id)
        state.cleanup_started.set()
        if state.cleanup_gate is not None:
            await state.cleanup_gate.wait()
        if state.cleanup_error:
            raise state.cleanup_error
        state.files.discard(bundle_id)

    async def reconcile(hass, retained):
        state.reconciled.append(tuple(retained))
        if state.reconcile_error:
            raise state.reconcile_error
        state.files.intersection_update(retained)

    async def resolve_choice(hass, tag):
        return await resolve(None, rc_tag=tag)

    monkeypatch.setattr(
        cache_module, "async_resolve_install_bundle_choice", resolve_choice
    )
    monkeypatch.setattr(cache_module, "async_download_browser_artifact", download)
    monkeypatch.setattr(cache_module, "async_cleanup_browser_artifact", cleanup)
    monkeypatch.setattr(cache_module, "async_reconcile_browser_artifacts", reconcile)
    monkeypatch.setattr(cache_module, "monotonic", lambda: state.now)
    state.cache = BrowserReleaseCache(SimpleNamespace(), SimpleNamespace())
    return state


async def test_user_bound_immutable_dedup_and_startup_reconcile(services):
    services.files.add("stale")
    record = await services.cache.async_prepare("alice")
    assert re.fullmatch("[0-9a-f]{32}", record.id)
    assert services.reconciled == [()]
    assert services.files == {record.id}
    assert await services.cache.async_prepare("alice") is record
    assert services.resolved == [None]
    with pytest.raises(FrozenInstanceError):
        record.id = "changed"
    assert "/private/" not in repr(record)
    async with services.cache.async_lease("alice", record.id) as leased:
        assert leased is record
    for user, bundle_id in [("bob", record.id), ("alice", "../invalid")]:
        with pytest.raises(BrowserReleaseCacheError, match="browser_release_not_found"):
            async with services.cache.async_lease(user, bundle_id):
                pytest.fail("unauthorized lease")


async def test_capacity_and_leases_protect_files(services):
    first = await services.cache.async_prepare("alice")
    second = await services.cache.async_prepare("bob", rc_tag="v1.2.4-rc1")
    async with services.cache.async_lease("alice", first.id):
        async with services.cache.async_lease("bob", second.id):
            with pytest.raises(BrowserReleaseCacheError, match="browser_release_busy"):
                await services.cache.async_prepare("charlie")
            assert services.files == {first.id, second.id}
        with pytest.raises(BrowserReleaseCacheError, match="browser_release_busy"):
            await services.cache.async_prepare("charlie")
        assert services.files == {first.id, second.id}
        services.now += 900
        third = await services.cache.async_prepare("charlie")
        assert services.files == {first.id, third.id}
        assert services.cleaned == [second.id]
    fourth = await services.cache.async_prepare("dan")
    assert services.files == {third.id, fourth.id}
    assert services.resolved == [None, "v1.2.4-rc1", None, None]


async def test_expiry_refuses_new_leases_but_keeps_active_response(services):
    first = await services.cache.async_prepare("alice")
    async with services.cache.async_lease("alice", first.id):
        services.now += 900
        with pytest.raises(BrowserReleaseCacheError, match="browser_release_not_found"):
            async with services.cache.async_lease("alice", first.id):
                pytest.fail("expired lease")
        second = await services.cache.async_prepare("alice")
        assert second.id != first.id
        assert services.files == {first.id, second.id}
    assert services.files == {second.id}
    services.now += 900
    third = await services.cache.async_prepare("alice")
    assert services.files == {third.id}


async def test_busy_refuses_inflight_even_same_selection_and_allows_cached(services):
    existing = await services.cache.async_prepare("alice")
    services.started.clear()
    services.download_gate = asyncio.Event()
    pending = asyncio.create_task(services.cache.async_prepare("bob"))
    await services.started.wait()
    assert await services.cache.async_prepare("alice") is existing
    for user in ("bob", "charlie"):
        with pytest.raises(BrowserReleaseCacheError, match="browser_release_busy"):
            await services.cache.async_prepare(user)
    assert len(services.files) == 2
    services.download_gate.set()
    await pending


async def test_download_failure_cleans_and_releases_admission(services):
    services.download_error = RuntimeError("private peer and path")
    with pytest.raises(BrowserReleaseCacheError) as error:
        await services.cache.async_prepare("alice")
    assert str(error.value) == "browser_release_prepare_failed"
    assert not services.files
    assert len(services.cleaned) == 1
    services.download_error = None
    await services.cache.async_prepare("alice")
    assert len(services.files) == 1


async def test_repeated_cancellation_drains_cleanup_before_next_admission(services):
    services.download_gate = asyncio.Event()
    services.cleanup_gate = asyncio.Event()
    pending = asyncio.create_task(services.cache.async_prepare("alice"))
    await services.started.wait()
    pending.cancel()
    await services.cleanup_started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    with pytest.raises(BrowserReleaseCacheError, match="browser_release_busy"):
        await services.cache.async_prepare("bob")
    services.cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not services.files
    services.download_gate.set()
    await services.cache.async_prepare("bob")
    assert len(services.files) == 1


@pytest.mark.parametrize("stage", ["reconcile", "cleanup"])
async def test_cleanup_failures_poison_cache(services, stage):
    if stage == "cleanup":
        await services.cache.async_prepare("alice")
        services.now += 900
    setattr(services, stage + "_error", OSError("/private/path"))
    for _ in range(2):
        with pytest.raises(BrowserReleaseCacheError) as error:
            await services.cache.async_prepare("bob")
        assert str(error.value) == "browser_release_cleanup_failed"
    assert len(services.files) <= 1


async def test_expiry_cleanup_keeps_admission_reserved(services):
    first = await services.cache.async_prepare("alice")
    await services.cache.async_prepare("bob")
    services.now += 900
    services.cleanup_gate = asyncio.Event()
    pending = asyncio.create_task(services.cache.async_prepare("charlie"))
    await services.cleanup_started.wait()
    with pytest.raises(BrowserReleaseCacheError, match="browser_release_not_found"):
        async with services.cache.async_lease("alice", first.id):
            pytest.fail("retiring entry leased")
    with pytest.raises(BrowserReleaseCacheError, match="browser_release_busy"):
        await services.cache.async_prepare("alice")
    services.cleanup_gate.set()
    await pending
    assert first.id not in services.files


async def test_close_defers_leased_file_cleanup(services):
    first = await services.cache.async_prepare("alice")
    second = await services.cache.async_prepare("bob")
    async with services.cache.async_lease("alice", first.id):
        await services.cache.async_close()
        assert services.files == {first.id}
        assert services.cleaned == [second.id]
        with pytest.raises(BrowserReleaseCacheError, match="browser_release_closed"):
            await services.cache.async_prepare("alice")
    assert not services.files
    await services.cache.async_close()


@pytest.mark.parametrize("size", [0, 64 * 1024 * 1024 + 1])
async def test_oversized_or_empty_bundle_refused_before_download(services, size):
    services.size = size
    with pytest.raises(
        BrowserReleaseCacheError, match="browser_release_prepare_failed"
    ):
        await services.cache.async_prepare("alice")
    assert not services.started.is_set()
    assert not services.files
