"""Tests for the persistent integration-owned ADB credential."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import struct
import threading
from hashlib import sha1, sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState, HomeAssistant

from custom_components.ha_paneld import adb_credentials
from custom_components.ha_paneld.adb_credentials import (
    AdbCredentialError,
    AdbCredentialManager,
    _generate_credential,
    _parse_stored_credential,
    _StoredCredential,
    async_get_adb_credential,
    async_get_adb_signer,
    async_get_durable_adb_credential,
)
from custom_components.ha_paneld.const import DOMAIN


def _serialized(credential: _StoredCredential) -> dict[str, str]:
    return {
        "format": "adb-rsa-2048-v1",
        "private_key_pkcs8_pem": credential.private_key,
        "public_key_adb": credential.public_key,
    }


def _stored_document(credential: _StoredCredential) -> dict[str, object]:
    return {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.adb_key",
        "data": _serialized(credential),
    }


def _write_store(path: Path, credential: _StoredCredential) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(_stored_document(credential)), encoding="utf-8")
    path.chmod(0o600)


def _other_public_key() -> str:
    return _generate_credential().public_key


def test_generated_credential_is_a_consistent_android_adb_key() -> None:
    """Generated private material, Android public bytes and signer all agree."""
    credential = _generate_credential()
    loaded = serialization.load_pem_private_key(
        credential.private_key.encode("ascii"), password=None
    )
    assert isinstance(loaded, rsa.RSAPrivateKey)
    assert loaded.key_size == 2048

    encoded_key, comment = credential.public_key.split(" ", maxsplit=1)
    assert comment == "ha-paneld@home-assistant"
    android_key = base64.b64decode(encoded_key, validate=True)
    words, n0inv, modulus_le, rr_le, exponent = struct.unpack(
        "<II256s256sI", android_key
    )

    public_numbers = loaded.public_key().public_numbers()
    assert words == 64
    assert exponent == public_numbers.e == 65537
    assert int.from_bytes(modulus_le, "little") == public_numbers.n
    assert n0inv == (1 << 32) - pow(public_numbers.n % (1 << 32), -1, 1 << 32)
    assert int.from_bytes(rr_le, "little") == pow(1 << 2048, 2, public_numbers.n)

    parsed = _parse_stored_credential(_serialized(credential))
    assert parsed == credential
    signer = adb_credentials.PythonRSASigner(parsed.public_key, parsed.private_key)
    challenge_digest = sha1(b"ha-paneld credential consistency").digest()
    loaded.public_key().verify(
        signer.Sign(challenge_digest),
        challenge_digest,
        padding.PKCS1v15(),
        utils.Prehashed(hashes.SHA1()),
    )
    assert signer.GetPublicKey() == credential.public_key


def test_generation_uses_private_temporary_paths() -> None:
    """Generation exposes no group/world-readable directory or key path."""
    keygen = adb_credentials.keygen
    observed_modes: dict[str, int] = {}

    def audited_keygen(path_text: str) -> None:
        private_path = Path(path_text)
        public_path = Path(f"{path_text}.pub")
        observed_modes.update(
            directory=private_path.parent.stat().st_mode & 0o777,
            private=private_path.stat().st_mode & 0o777,
            public=public_path.stat().st_mode & 0o777,
        )
        keygen(path_text)

    with patch.object(adb_credentials, "keygen", side_effect=audited_keygen):
        _generate_credential()

    assert observed_modes == {"directory": 0o700, "private": 0o600, "public": 0o600}


@pytest.mark.parametrize("oversize_path", ["private", "public"])
def test_generation_rejects_oversize_generated_files(oversize_path: str) -> None:
    """Unexpectedly large keygen output is rejected before being read or persisted."""

    def oversize_keygen(path_text: str) -> None:
        private_path = Path(path_text)
        public_path = Path(f"{path_text}.pub")
        private_path.write_text(
            "x" * (4097 if oversize_path == "private" else 1), encoding="ascii"
        )
        public_path.write_text(
            "x" * (1025 if oversize_path == "public" else 1), encoding="ascii"
        )

    with (
        patch.object(adb_credentials, "keygen", side_effect=oversize_keygen),
        pytest.raises(AdbCredentialError),
    ):
        _generate_credential()


async def test_manager_constructs_private_atomic_store(
    hass: HomeAssistant,
) -> None:
    """The credential lives in an integration-private atomically written Store."""
    with patch.object(adb_credentials, "Store") as store_class:
        manager = AdbCredentialManager(hass)

    assert manager is not None
    store_class.assert_called_once_with(
        hass,
        1,
        f"{DOMAIN}.adb_key",
        private=True,
        atomic_writes=True,
    )


async def test_manager_persists_and_reuses_one_credential(
    hass: HomeAssistant,
) -> None:
    """A new manager after an in-process restart reuses durable key material."""
    generation_count = 0
    generate = _generate_credential
    persisted: dict[str, str] = {}

    initial_store = MagicMock()
    initial_store.path = "/not/read/by/this/test"

    async def save(data: dict[str, str]) -> None:
        persisted.update(data)

    initial_store.async_save = AsyncMock(side_effect=save)
    restarted_store = MagicMock()
    restarted_store.path = "/not/read/by/this/test"

    def counted_generate() -> _StoredCredential:
        nonlocal generation_count
        generation_count += 1
        return generate()

    with (
        patch.object(
            adb_credentials,
            "Store",
            side_effect=[initial_store, restarted_store],
        ),
        patch.object(
            adb_credentials,
            "_store_presence",
            side_effect=[(False, False), (True, False), (True, False)],
        ),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=lambda _path: _parse_stored_credential(persisted.copy()),
        ),
        patch.object(
            adb_credentials, "_generate_credential", side_effect=counted_generate
        ),
    ):
        first = await AdbCredentialManager(hass).async_get_signer()
        second = await AdbCredentialManager(hass).async_get_signer()

    assert generation_count == 1
    assert second.GetPublicKey() == first.GetPublicKey()
    digest = sha1(b"durable identity").digest()
    assert second.Sign(digest) == first.Sign(digest)


async def test_manager_serializes_concurrent_generation(
    hass: HomeAssistant,
) -> None:
    """All overlapping consumers wait for the same one-time generation."""
    load_started = asyncio.Event()
    release_load = asyncio.Event()
    generation_count = 0
    generate = _generate_credential
    persisted: dict[str, str] = {}

    initial_store = MagicMock()
    initial_store.path = "/not/read/by/this/test"

    async def save(data: dict[str, str]) -> None:
        persisted.update(data)

    initial_store.async_save = AsyncMock(side_effect=save)

    def counted_generate() -> _StoredCredential:
        nonlocal generation_count
        generation_count += 1
        return generate()

    with (
        patch.object(
            adb_credentials,
            "Store",
            return_value=initial_store,
        ),
        patch.object(
            adb_credentials,
            "_store_presence",
            side_effect=[(False, False), (True, False)],
        ),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=lambda _path: _parse_stored_credential(persisted.copy()),
        ),
        patch.object(
            adb_credentials, "_generate_credential", side_effect=counted_generate
        ),
    ):
        manager = AdbCredentialManager(hass)
        persist = manager._async_persist_locked

        async def gated_persist(credential: _StoredCredential) -> _StoredCredential:
            load_started.set()
            await release_load.wait()
            return await persist(credential)

        with patch.object(manager, "_async_persist_locked", side_effect=gated_persist):
            first_task = asyncio.create_task(manager.async_get_signer())
            await asyncio.wait_for(load_started.wait(), timeout=10)
            waiting_tasks = [
                asyncio.create_task(manager.async_get_signer()) for _ in range(15)
            ]
            await asyncio.sleep(0)
            release_load.set()
            signers = await asyncio.gather(first_task, *waiting_tasks)

    assert generation_count == 1
    assert {signer.GetPublicKey() for signer in signers} == {signers[0].GetPublicKey()}
    digest = sha1(b"single flight").digest()
    assert {signer.Sign(digest) for signer in signers} == {signers[0].Sign(digest)}


async def test_cancelled_store_write_drains_before_queued_consumer(
    hass: HomeAssistant,
) -> None:
    """A cancelled old writer cannot late-replace a newer generated identity."""
    first_credential = _generate_credential()
    second_credential = _generate_credential()
    generated = iter((first_credential, second_credential))
    manager = AdbCredentialManager(hass)
    writer_started = asyncio.Event()
    presence_started = asyncio.Event()
    readback_started = asyncio.Event()
    queued_read_started = asyncio.Event()
    release_writer = threading.Event()
    release_presence = threading.Event()
    release_readback = threading.Event()
    release_queued_read = threading.Event()
    loop = asyncio.get_running_loop()
    writes: list[_StoredCredential] = []
    generation_count = 0
    first_task: asyncio.Task | None = None
    queued_task: asyncio.Task | None = None

    def write(document: dict[str, Any]) -> None:
        credential = _parse_stored_credential(document["data"])
        writes.append(credential)
        loop.call_soon_threadsafe(writer_started.set)
        if not release_writer.wait(timeout=10):
            raise RuntimeError("timed out waiting to release credential writer")
        _write_store(Path(manager._store.path), credential)

    async def write_data(document: dict[str, Any]) -> None:
        await hass.async_add_executor_job(write, copy.deepcopy(document))

    manager._store._async_write_data = write_data  # type: ignore[method-assign]
    tracked_before = set(hass._tasks)
    background_before = set(hass._background_tasks)
    presence = adb_credentials._store_presence
    presence_calls = 0
    read_durable = adb_credentials._read_durable_credential
    read_calls = 0

    def generate_sequence() -> _StoredCredential:
        nonlocal generation_count
        generation_count += 1
        return next(generated)

    def initially_absent(path_text: str) -> tuple[bool, bool]:
        nonlocal presence_calls
        presence_calls += 1
        if presence_calls == 1:
            return False, False
        if presence_calls == 2:
            loop.call_soon_threadsafe(presence_started.set)
            if not release_presence.wait(timeout=10):
                raise RuntimeError("timed out waiting to release presence barrier")
        return presence(path_text)

    def gated_read(path_text: str) -> _StoredCredential:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            loop.call_soon_threadsafe(readback_started.set)
            if not release_readback.wait(timeout=10):
                raise RuntimeError("timed out waiting to release readback barrier")
        elif read_calls == 2:
            loop.call_soon_threadsafe(queued_read_started.set)
            if not release_queued_read.wait(timeout=10):
                raise RuntimeError("timed out waiting to release queued reader")
        return read_durable(path_text)

    try:
        with (
            patch.object(
                adb_credentials,
                "_generate_credential",
                new=generate_sequence,
            ),
            patch.object(adb_credentials, "_store_presence", new=initially_absent),
            patch.object(adb_credentials, "_read_durable_credential", new=gated_read),
        ):
            first_task = asyncio.create_task(manager.async_get_credential())
            await asyncio.wait_for(writer_started.wait(), timeout=10)

            added_tracked = hass._tasks - tracked_before
            added_background = hass._background_tasks - background_before
            writer_futures = {
                future
                for future in added_tracked | added_background
                if not isinstance(future, asyncio.Task)
            }
            assert len(writer_futures) == 1
            assert writer_futures <= added_tracked

            first_task.cancel()
            await asyncio.sleep(0)
            first_task.cancel()
            await asyncio.sleep(0)
            assert not first_task.done()
            assert manager._lock.locked()

            queued_task = asyncio.create_task(manager.async_get_credential())
            await asyncio.sleep(0)
            assert not queued_task.done()
            assert manager._lock._waiters is not None  # type: ignore[attr-defined]
            assert any(
                not waiter.done()
                for waiter in manager._lock._waiters  # type: ignore[attr-defined]
            )

            for future in added_background:
                future.cancel("Home Assistant is stopping")
            hass.set_state(CoreState.stopping)
            await asyncio.sleep(0)
            assert not first_task.done()
            assert manager._lock.locked()
            assert not any(future.cancelled() for future in writer_futures)
            assert writes == [first_credential]
            hass.set_state(CoreState.running)

            release_writer.set()
            await asyncio.wait_for(presence_started.wait(), timeout=10)
            presence_futures = {
                future
                for future in hass._tasks - tracked_before
                if not isinstance(future, asyncio.Task) and not future.done()
            }
            assert len(presence_futures) == 1
            assert not {
                future
                for future in hass._background_tasks - background_before
                if not isinstance(future, asyncio.Task) and not future.done()
            }
            first_task.cancel()
            await asyncio.sleep(0)
            first_task.cancel()
            await asyncio.sleep(0)
            assert not first_task.done()
            assert manager._lock.locked()
            assert not queued_task.done()

            release_presence.set()
            await asyncio.wait_for(readback_started.wait(), timeout=10)
            readback_futures = {
                future
                for future in hass._tasks - tracked_before
                if not isinstance(future, asyncio.Task) and not future.done()
            }
            assert len(readback_futures) == 1
            assert not {
                future
                for future in hass._background_tasks - background_before
                if not isinstance(future, asyncio.Task) and not future.done()
            }
            first_task.cancel()
            await asyncio.sleep(0)
            first_task.cancel()
            await asyncio.sleep(0)
            assert not first_task.done()
            assert manager._lock.locked()
            assert not queued_task.done()

            release_readback.set()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            await asyncio.wait_for(queued_read_started.wait(), timeout=10)
            assert manager._credential is None
            assert manager._lock.locked()
            assert not queued_task.done()

            release_queued_read.set()
            queued = await asyncio.wait_for(queued_task, timeout=10)

        assert queued.signer.GetPublicKey() == first_credential.public_key
        assert generation_count == 1
        assert writes == [first_credential]
        assert read_calls == 2
        assert manager._credential == first_credential
    finally:
        release_writer.set()
        release_presence.set()
        release_readback.set()
        release_queued_read.set()
        hass.set_state(CoreState.running)
        for task in (first_task, queued_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.final_write])
async def test_shutdown_state_refuses_deferred_credential_write(
    hass: HomeAssistant, state: CoreState
) -> None:
    """Credential authority never enters Store's deferred shutdown writer."""
    credential = _generate_credential()
    store = MagicMock()
    store.path = "/not/read/by/this/test"
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    hass.set_state(state)

    try:
        with (
            patch.object(adb_credentials, "Store", return_value=store),
            patch.object(
                adb_credentials, "_store_presence", return_value=(False, False)
            ),
            patch.object(
                adb_credentials, "_generate_credential", return_value=credential
            ),
            pytest.raises(AdbCredentialError),
        ):
            await AdbCredentialManager(hass).async_get_credential()

        store.async_save.assert_not_awaited()
        assert store.method_calls == []
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        store.async_save.assert_not_awaited()
    finally:
        hass.set_state(CoreState.running)


async def test_invalid_generated_credential_is_rejected_before_store_write(
    hass: HomeAssistant,
) -> None:
    """The complete candidate is validated before Store can mutate authority."""
    valid = _generate_credential()
    invalid = _StoredCredential(
        private_key="x" * 4097,
        public_key=valid.public_key,
    )
    store = MagicMock()
    store.path = "/not/read/by/this/test"
    store.async_save = AsyncMock()

    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=(False, False)),
        patch.object(adb_credentials, "_generate_credential", return_value=invalid),
        pytest.raises(AdbCredentialError),
    ):
        await AdbCredentialManager(hass).async_get_credential()

    store.async_save.assert_not_awaited()


async def test_process_wide_accessor_reuses_one_manager(
    hass: HomeAssistant,
) -> None:
    """Independent callers receive signers backed by the process-wide authority."""
    credential = _generate_credential()
    store = MagicMock()
    store.path = "/not/read/by/this/test"
    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=(True, False)),
        patch.object(
            adb_credentials, "_read_durable_credential", return_value=credential
        ),
    ):
        first, second = await asyncio.gather(
            async_get_adb_signer(hass), async_get_adb_signer(hass)
        )

    assert first.GetPublicKey() == second.GetPublicKey()
    bound = await async_get_adb_credential(hass)
    encoded_public = bound.signer.GetPublicKey().partition(" ")[0]
    assert (
        bound.generation_id
        == sha256(base64.b64decode(encoded_public, validate=True)).hexdigest()
    )
    assert bound.generation_id not in repr(bound)
    managers = [
        value for value in hass.data.values() if isinstance(value, AdbCredentialManager)
    ]
    assert len(managers) == 1


async def test_process_wide_accessor_rejects_foreign_manager_state(
    hass: HomeAssistant,
) -> None:
    """Corrupt process state cannot silently replace the credential authority."""
    hass.data[f"{DOMAIN}.adb_credential_manager"] = object()

    with pytest.raises(AdbCredentialError):
        await async_get_adb_signer(hass)


async def test_durable_accessor_reloads_replaced_key_instead_of_cached_signer(
    hass: HomeAssistant,
) -> None:
    """Mutation checks observe current Store bytes, not an earlier memory cache."""
    original = _generate_credential()
    replacement = _generate_credential()
    manager = AdbCredentialManager(hass)
    manager._credential = original

    with (
        patch.object(adb_credentials, "_store_presence", return_value=(True, False)),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            return_value=replacement,
        ) as read,
    ):
        durable = await manager.async_get_durable_credential()

    read.assert_called_once_with(manager._store.path)
    assert durable.signer.GetPublicKey() == replacement.public_key
    assert durable.signer.GetPublicKey() != original.public_key
    assert (
        durable.generation_id
        == sha256(
            base64.b64decode(replacement.public_key.partition(" ")[0], validate=True)
        ).hexdigest()
    )


async def test_durable_accessor_never_regenerates_a_missing_cached_key(
    hass: HomeAssistant,
) -> None:
    """Deletion after caching revokes mutation authority instead of rotating it."""
    manager = AdbCredentialManager(hass)
    manager._credential = _generate_credential()

    with (
        patch.object(adb_credentials, "_store_presence", return_value=(False, False)),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=AdbCredentialError,
        ) as read,
        patch.object(adb_credentials, "_generate_credential") as generate,
        pytest.raises(AdbCredentialError),
    ):
        await manager.async_get_durable_credential()

    read.assert_called_once_with(manager._store.path)
    generate.assert_not_called()
    assert manager._credential is None


async def test_cancelled_durable_reload_invalidates_cached_authority(
    hass: HomeAssistant,
) -> None:
    """Cancellation cannot leave an unverified cached signer authoritative."""
    credential = _generate_credential()
    manager = AdbCredentialManager(hass)
    manager._credential = credential
    read_started = asyncio.Event()
    release_read = threading.Event()
    loop = asyncio.get_running_loop()

    def present(_path: str) -> tuple[bool, bool]:
        return True, False

    def blocking_read(_path: str) -> _StoredCredential:
        loop.call_soon_threadsafe(read_started.set)
        if not release_read.wait(timeout=5):
            raise RuntimeError("timed out waiting to release durable reader")
        return credential

    task: asyncio.Task | None = None
    try:
        with (
            patch.object(adb_credentials, "_store_presence", new=present),
            patch.object(
                adb_credentials, "_read_durable_credential", new=blocking_read
            ),
        ):
            task = asyncio.create_task(manager.async_get_durable_credential())
            await asyncio.wait_for(read_started.wait(), timeout=1)
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert manager._credential is None
    finally:
        release_read.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_process_wide_durable_accessor_uses_shared_manager(
    hass: HomeAssistant,
) -> None:
    """Executor callers use the same process authority as authorization flows."""
    manager = MagicMock(spec=AdbCredentialManager)
    expected = object()
    manager.async_get_durable_credential = AsyncMock(return_value=expected)
    hass.data[f"{DOMAIN}.adb_credential_manager"] = manager

    assert await async_get_durable_adb_credential(hass) is expected
    manager.async_get_durable_credential.assert_awaited_once_with()


@pytest.mark.parametrize("presence", [(True, False), (False, True)])
async def test_missing_loaded_data_with_store_evidence_never_regenerates(
    hass: HomeAssistant, presence: tuple[bool, bool]
) -> None:
    """A missing or quarantined credential is an error, not key rotation."""
    store = MagicMock()
    store.path = "/not/read/by/this/test"

    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=presence),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=AdbCredentialError,
        ) as read,
        patch.object(adb_credentials, "_generate_credential") as generate,
        pytest.raises(AdbCredentialError),
    ):
        await AdbCredentialManager(hass).async_get_signer()

    generate.assert_not_called()
    if presence == (True, False):
        read.assert_called_once_with(store.path)
    else:
        read.assert_not_called()


async def test_corrupt_loaded_credential_never_regenerates(
    hass: HomeAssistant,
) -> None:
    """Invalid persisted bytes fail closed without replacing panel trust."""
    store = MagicMock()
    store.path = "/not/read/by/this/test"

    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=(True, False)),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=AdbCredentialError,
        ),
        patch.object(adb_credentials, "_generate_credential") as generate,
        pytest.raises(AdbCredentialError),
    ):
        await AdbCredentialManager(hass).async_get_signer()

    generate.assert_not_called()


async def test_loaded_credential_is_refused_without_private_file(
    hass: HomeAssistant,
) -> None:
    """Valid key bytes are not enough when their backing file is not private."""
    store = MagicMock()
    store.path = "/not/read/by/this/test"

    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=(True, False)),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=AdbCredentialError,
        ),
        pytest.raises(AdbCredentialError),
    ):
        await AdbCredentialManager(hass).async_get_signer()


async def test_manager_translates_unexpected_store_failure(
    hass: HomeAssistant,
) -> None:
    """Backend failures do not escape the credential authority abstraction."""
    store = MagicMock()
    store.path = "/not/read/by/this/test"

    with (
        patch.object(adb_credentials, "Store", return_value=store),
        patch.object(adb_credentials, "_store_presence", return_value=(True, False)),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=RuntimeError("backend failed"),
        ),
        pytest.raises(AdbCredentialError) as caught,
    ):
        await AdbCredentialManager(hass).async_get_signer()

    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    "persisted",
    [
        None,
        {},
        {"private_key": "changed", "public_key": "changed"},
    ],
)
async def test_new_credential_is_refused_without_exact_persisted_readback(
    hass: HomeAssistant, persisted: object
) -> None:
    """A generated identity is never offered to ADB unless durable bytes agree."""
    initial_store = MagicMock()
    initial_store.async_save = AsyncMock()

    initial_store.path = "/not/read/by/this/test"
    with (
        patch.object(adb_credentials, "Store", return_value=initial_store),
        patch.object(
            adb_credentials,
            "_store_presence",
            side_effect=[(False, False), (True, False)],
        ),
        patch.object(
            adb_credentials, "_read_durable_credential", return_value=persisted
        ) as read,
    ):
        manager = AdbCredentialManager(hass)
        with pytest.raises(AdbCredentialError):
            await manager.async_get_signer()

    initial_store.async_save.assert_awaited_once()
    read.assert_called_once_with(initial_store.path)


async def test_new_credential_is_refused_when_readback_is_not_exactly_private(
    hass: HomeAssistant,
) -> None:
    """Even an exact read-back is unusable unless the secret file is mode 0600."""
    credential = _generate_credential()
    serialized = _serialized(credential)
    initial_store = MagicMock()
    initial_store.path = "/not/read/by/this/test"
    initial_store.async_save = AsyncMock()

    with (
        patch.object(adb_credentials, "Store", return_value=initial_store),
        patch.object(
            adb_credentials,
            "_store_presence",
            side_effect=[(False, False), (True, False)],
        ),
        patch.object(
            adb_credentials,
            "_read_durable_credential",
            side_effect=AdbCredentialError,
        ),
        patch.object(adb_credentials, "_generate_credential", return_value=credential),
        pytest.raises(AdbCredentialError),
    ):
        await AdbCredentialManager(hass).async_get_signer()

    initial_store.async_save.assert_awaited_once_with(serialized)


def test_store_privacy_check_requires_regular_owner_only_file(tmp_path: Path) -> None:
    """Credential read-back accepts exactly a regular 0600 file."""
    credential_path = tmp_path / "credential"
    credential_path.write_text("secret", encoding="ascii")
    credential_path.chmod(0o600)
    assert adb_credentials._store_is_private(str(credential_path))

    hardlink_path = tmp_path / "credential-hardlink"
    os.link(credential_path, hardlink_path)
    assert not adb_credentials._store_is_private(str(credential_path))
    hardlink_path.unlink()

    credential_path.chmod(0o640)
    assert not adb_credentials._store_is_private(str(credential_path))

    credential_path.chmod(0o600)
    with patch.object(adb_credentials.os, "geteuid", return_value=os.geteuid() + 1):
        assert not adb_credentials._store_is_private(str(credential_path))


def test_durable_reader_binds_exact_private_store_file(tmp_path: Path) -> None:
    """The returned key is parsed from the same bounded inode that was checked."""
    credential = _generate_credential()
    store_path = tmp_path / "ha_paneld.adb_key"
    _write_store(store_path, credential)

    assert adb_credentials._read_durable_credential(str(store_path)) == credential


def test_durable_reader_rejects_hardlinked_store_file(tmp_path: Path) -> None:
    """An alias cannot retain usable ADB key authority after Store replacement."""
    store_path = tmp_path / "ha_paneld.adb_key"
    _write_store(store_path, _generate_credential())
    os.link(store_path, tmp_path / "credential-hardlink")

    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_durable_reader_rejects_link_count_drift_during_read(
    tmp_path: Path,
) -> None:
    """A new alias invalidates the opened credential even without other drift."""
    store_path = tmp_path / "ha_paneld.adb_key"
    credential = _generate_credential()
    _write_store(store_path, credential)
    actual = os.stat(store_path)

    def metadata(link_count: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
            st_mode=actual.st_mode,
            st_uid=actual.st_uid,
            st_nlink=link_count,
            st_size=actual.st_size,
            st_mtime_ns=actual.st_mtime_ns,
            st_ctime_ns=actual.st_ctime_ns,
        )

    before = metadata(1)
    linked = metadata(2)
    with (
        patch.object(adb_credentials.os, "fstat", side_effect=[before, linked, linked]),
        patch.object(adb_credentials.os, "lstat", side_effect=[linked, linked]),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._read_durable_credential(str(store_path))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(extra=True),
        lambda document: document.update(version=True),
        lambda document: document.update(version=2),
        lambda document: document.update(minor_version=False),
        lambda document: document.update(key="other"),
        lambda document: document.update(data={}),
    ],
)
def test_durable_reader_rejects_invalid_store_wrapper(tmp_path: Path, mutation) -> None:
    """Direct reads retain Home Assistant Store identity and schema checks."""
    store_path = tmp_path / "ha_paneld.adb_key"
    document = _stored_document(_generate_credential())
    mutation(document)
    store_path.write_text(json.dumps(document), encoding="utf-8")
    store_path.chmod(0o600)

    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_durable_reader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """A duplicate wrapper or credential field cannot override trusted bytes."""
    credential = _generate_credential()
    store_path = tmp_path / "ha_paneld.adb_key"
    body = json.dumps(_stored_document(credential))
    body = body.replace('"version": 1,', '"version": 1, "version": 1,', 1)
    store_path.write_text(body, encoding="utf-8")
    store_path.chmod(0o600)

    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_durable_reader_rejects_path_replacement_during_read(tmp_path: Path) -> None:
    """An atomic path replacement cannot authorize bytes from a stale inode."""
    original = _generate_credential()
    replacement = _generate_credential()
    store_path = tmp_path / "ha_paneld.adb_key"
    replacement_path = tmp_path / "replacement"
    _write_store(store_path, original)
    _write_store(replacement_path, replacement)
    real_read = os.read
    replaced = False

    def replace_after_read(file_fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(file_fd, size)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement_path, store_path)
        return chunk

    with (
        patch.object(adb_credentials.os, "read", side_effect=replace_after_read),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._read_durable_credential(str(store_path))

    assert replaced


async def test_manager_rejects_existing_path_replacement_without_store_load(
    hass: HomeAssistant,
) -> None:
    """Existing authority is loaded from one path-bound fd, never Store cache."""
    manager = AdbCredentialManager(hass)
    original = _generate_credential()
    replacement = _generate_credential()
    store_path = Path(manager._store.path)
    replacement_path = store_path.with_name(f"{store_path.name}.replacement")
    _write_store(store_path, original)
    _write_store(replacement_path, replacement)
    real_read = os.read
    replaced = False

    def replace_after_read(file_fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(file_fd, size)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement_path, store_path)
        return chunk

    manager._store.async_load = AsyncMock()  # type: ignore[method-assign]
    with (
        patch.object(adb_credentials.os, "read", side_effect=replace_after_read),
        pytest.raises(AdbCredentialError),
    ):
        await manager.async_get_credential()

    assert replaced
    manager._store.async_load.assert_not_awaited()
    assert manager._credential is None


def test_durable_reader_rejects_same_inode_overwrite_during_read(
    tmp_path: Path,
) -> None:
    """Same-size mutation cannot return bytes no longer held by the durable path."""
    original = _generate_credential()
    original_body = json.dumps(_stored_document(original)).encode()
    replacement_document = _stored_document(original)
    replacement_document["minor_version"] = 2
    replacement_body = json.dumps(replacement_document).encode()
    assert len(original_body) == len(replacement_body)
    store_path = tmp_path / "ha_paneld.adb_key"
    store_path.write_bytes(original_body)
    store_path.chmod(0o600)
    real_read = os.read
    replaced = False

    def overwrite_after_read(file_fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(file_fd, size)
        if chunk and not replaced:
            replaced = True
            overwrite_fd = os.open(store_path, os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(overwrite_fd, replacement_body)
                os.fsync(overwrite_fd)
            finally:
                os.close(overwrite_fd)
        return chunk

    with (
        patch.object(adb_credentials.os, "read", side_effect=overwrite_after_read),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._read_durable_credential(str(store_path))

    assert replaced


@pytest.mark.parametrize("body", [b"\xff", b"{", b"[]", b""])
def test_durable_reader_rejects_malformed_or_empty_json(
    tmp_path: Path, body: bytes
) -> None:
    """Invalid on-disk serialization never reaches the credential parser."""
    store_path = tmp_path / "ha_paneld.adb_key"
    store_path.write_bytes(body)
    store_path.chmod(0o600)

    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_durable_reader_rejects_foreign_owner(tmp_path: Path) -> None:
    """Owner-only mode does not authorize a Store file owned by another uid."""
    store_path = tmp_path / "ha_paneld.adb_key"
    _write_store(store_path, _generate_credential())

    with (
        patch.object(adb_credentials.os, "geteuid", return_value=os.geteuid() + 1),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._read_durable_credential(str(store_path))


@pytest.mark.parametrize("mode", [0o000, 0o400, 0o640])
def test_durable_reader_rejects_non_private_or_missing_file(
    tmp_path: Path, mode: int
) -> None:
    """Direct mutation authority requires one present owner-readable 0600 file."""
    store_path = tmp_path / "ha_paneld.adb_key"
    _write_store(store_path, _generate_credential())
    store_path.chmod(mode)

    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))

    store_path.unlink()
    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_durable_reader_rejects_symlink_and_excessive_file(tmp_path: Path) -> None:
    """No link or unbounded JSON body can become ADB mutation authority."""
    target_path = tmp_path / "target"
    _write_store(target_path, _generate_credential())
    store_path = tmp_path / "ha_paneld.adb_key"
    store_path.symlink_to(target_path)
    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))

    store_path.unlink()
    store_path.write_bytes(b" " * (16 * 1024 + 1))
    store_path.chmod(0o600)
    with pytest.raises(AdbCredentialError):
        adb_credentials._read_durable_credential(str(store_path))


def test_store_privacy_check_rejects_symlink(tmp_path: Path) -> None:
    """A 0600 target cannot make a symlink masquerade as the secret Store file."""
    target_path = tmp_path / "target"
    target_path.write_text("secret", encoding="ascii")
    target_path.chmod(0o600)
    link_path = tmp_path / "credential"
    link_path.symlink_to(target_path)

    assert adb_credentials._store_presence(str(link_path)) == (True, False)
    assert not adb_credentials._store_is_private(str(link_path))


def test_store_presence_detects_exact_and_quarantined_files(tmp_path: Path) -> None:
    """Store evidence survives a missing current file and blocks key replacement."""
    storage_path = tmp_path / "ha_paneld.adb_credentials"
    assert adb_credentials._store_presence(str(storage_path)) == (False, False)

    storage_path.write_text("current", encoding="ascii")
    assert adb_credentials._store_presence(str(storage_path)) == (True, False)

    storage_path.unlink()
    (tmp_path / "ha_paneld.adb_credentials.corrupt.2026-09-02").write_text(
        "quarantined", encoding="ascii"
    )
    assert adb_credentials._store_presence(str(storage_path)) == (False, True)

    absent_parent_path = tmp_path / "absent" / "credential"
    assert adb_credentials._store_presence(str(absent_parent_path)) == (False, False)


def test_store_presence_translates_filesystem_failures() -> None:
    """Presence inspection errors fail closed behind the credential exception."""
    with (
        patch.object(adb_credentials.os.path, "lexists", side_effect=OSError),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._store_presence("/not/read/by/this/test")


def test_store_privacy_translates_missing_or_unreadable_file() -> None:
    """Missing or unreadable persisted secrets fail closed."""
    with pytest.raises(AdbCredentialError):
        adb_credentials._store_is_private("/not/read/by/this/test")

    with (
        patch.object(Path, "lstat", side_effect=OSError),
        pytest.raises(AdbCredentialError),
    ):
        adb_credentials._store_is_private("/not/read/by/this/test")


def _rsa_private_pem(*, key_size: int, public_exponent: int = 65537) -> str:
    key = rsa.generate_private_key(
        public_exponent=public_exponent,
        key_size=key_size,
    )
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _corrupt_cases() -> list[object]:
    valid = _generate_credential()
    valid_data = _serialized(valid)
    return [
        None,
        [],
        "not an object",
        {},
        {"private_key_pkcs8_pem": valid.private_key},
        {"public_key_adb": valid.public_key},
        {**valid_data, "format": "adb-rsa-4096-v2"},
        {**valid_data, "private_key_pkcs8_pem": None},
        {**valid_data, "public_key_adb": None},
        {**valid_data, "private_key_pkcs8_pem": ""},
        {**valid_data, "public_key_adb": ""},
        {**valid_data, "private_key_pkcs8_pem": "x" * 4097},
        {**valid_data, "public_key_adb": "x" * 1025},
        {**valid_data, "private_key_pkcs8_pem": "not a PEM key"},
        {**valid_data, "private_key_pkcs8_pem": "\N{SNOWMAN}"},
        {
            **valid_data,
            "private_key_pkcs8_pem": _rsa_private_pem(key_size=1024),
        },
        {
            **valid_data,
            "private_key_pkcs8_pem": _rsa_private_pem(key_size=2048, public_exponent=3),
        },
        {**valid_data, "public_key_adb": _other_public_key()},
        {**valid_data, "public_key_adb": valid.public_key.partition(" ")[0]},
        {**valid_data, "public_key_adb": "%%% ha-paneld@home-assistant"},
        {**valid_data, "public_key_adb": "YQ== ha-paneld@home-assistant"},
    ]


@pytest.mark.parametrize("data", _corrupt_cases())
def test_parser_rejects_corrupt_mismatched_or_oversize_data(data: object) -> None:
    """Malformed, excessive or internally inconsistent durable data fails closed."""
    with pytest.raises(AdbCredentialError):
        _parse_stored_credential(data)


def test_parser_rejects_additive_unknown_fields_without_version_migration() -> None:
    """Unversioned schema additions cannot silently change the secret contract."""
    credential = _generate_credential()
    stored: dict[str, Any] = {
        **_serialized(credential),
        "created_at": "future metadata",
        "future": {"nested": [1, 2, 3]},
    }

    with pytest.raises(AdbCredentialError):
        _parse_stored_credential(stored)


def test_parser_rejects_signer_public_key_disagreement() -> None:
    """The signer adapter must return the same Android public identity."""
    credential = _generate_credential()
    signer = MagicMock()
    signer.GetPublicKey.return_value = _other_public_key()

    with (
        patch.object(adb_credentials, "PythonRSASigner", return_value=signer),
        pytest.raises(AdbCredentialError),
    ):
        _parse_stored_credential(_serialized(credential))
