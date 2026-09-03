"""Persistent integration-owned Android Debug Bridge credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import struct
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}.adb_key"
_MANAGER_DATA_KEY = f"{DOMAIN}.adb_credential_manager"
_MAX_PRIVATE_KEY_LENGTH = 4096
_MAX_PUBLIC_KEY_LENGTH = 1024
_MAX_STORE_BYTES = 16 * 1024
_FORMAT = "adb-rsa-2048-v1"
_RSA_BITS = 2048
_MODULUS_BYTES = _RSA_BITS // 8
_MODULUS_WORDS = _MODULUS_BYTES // 4
_PUBLIC_KEY_STRUCT = f"<LL{_MODULUS_BYTES}s{_MODULUS_BYTES}sL"
_PUBLIC_KEY_COMMENT = " ha-paneld@home-assistant"


class AdbCredentialError(Exception):
    """Raised when the durable ADB credential cannot be loaded safely."""


@dataclass(frozen=True, repr=False, slots=True)
class _StoredCredential:
    private_key: str
    public_key: str


@dataclass(frozen=True, repr=False, slots=True)
class AdbCredential:
    """Usable signer plus a stable non-secret generation identifier."""

    signer: PythonRSASigner
    generation_id: str


def _encode_android_public_key(public_numbers: rsa.RSAPublicNumbers) -> str:
    """Encode an RSA key using Android's ADB public-key representation."""
    modulus = public_numbers.n
    word_base = 1 << 32
    n0inv = word_base - pow(modulus % word_base, -1, word_base)
    rr = pow(1 << _RSA_BITS, 2, modulus)
    encoded = struct.pack(
        _PUBLIC_KEY_STRUCT,
        _MODULUS_WORDS,
        n0inv,
        modulus.to_bytes(_MODULUS_BYTES, "little"),
        rr.to_bytes(_MODULUS_BYTES, "little"),
        public_numbers.e,
    )
    return base64.b64encode(encoded).decode("ascii") + _PUBLIC_KEY_COMMENT


def _credential_generation_id(public_key: str) -> str:
    """Hash only the canonical Android public-key bytes for receipt binding."""
    encoded_public = public_key.partition(" ")[0]
    return sha256(base64.b64decode(encoded_public, validate=True)).hexdigest()


def _generate_credential() -> _StoredCredential:
    """Generate one bounded ADB credential in a private temporary directory."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ha-paneld-adb-") as directory:
        os.chmod(directory, 0o700)
        private_path = Path(directory, "adbkey")
        public_path = Path(directory, "adbkey.pub")
        private_path.touch(mode=0o600)
        public_path.touch(mode=0o600)
        keygen(str(private_path))
        if (
            private_path.stat().st_size > _MAX_PRIVATE_KEY_LENGTH
            or public_path.stat().st_size > _MAX_PUBLIC_KEY_LENGTH
        ):
            raise AdbCredentialError
        private_key = private_path.read_text(encoding="ascii")
        generated_public = public_path.read_text(encoding="ascii")

    public_key = generated_public.partition(" ")[0] + _PUBLIC_KEY_COMMENT
    return _parse_stored_credential(
        {
            "format": _FORMAT,
            "private_key_pkcs8_pem": private_key,
            "public_key_adb": public_key,
        }
    )


def _parse_stored_credential(data: Any) -> _StoredCredential:
    """Validate the bounded persisted representation before using its private key."""
    if not isinstance(data, dict) or data.keys() != {
        "format",
        "private_key_pkcs8_pem",
        "public_key_adb",
    }:
        raise AdbCredentialError
    private_key = data.get("private_key_pkcs8_pem")
    public_key = data.get("public_key_adb")
    if (
        data.get("format") != _FORMAT
        or not isinstance(private_key, str)
        or not 1 <= len(private_key) <= _MAX_PRIVATE_KEY_LENGTH
        or not isinstance(public_key, str)
        or not 1 <= len(public_key) <= _MAX_PUBLIC_KEY_LENGTH
    ):
        raise AdbCredentialError

    try:
        encoded_public, separator, comment = public_key.partition(" ")
        if separator != " " or f" {comment}" != _PUBLIC_KEY_COMMENT:
            raise AdbCredentialError
        if len(base64.b64decode(encoded_public, validate=True)) != struct.calcsize(
            _PUBLIC_KEY_STRUCT
        ):
            raise AdbCredentialError
        loaded_key = serialization.load_pem_private_key(
            private_key.encode("ascii"), password=None
        )
        if (
            not isinstance(loaded_key, rsa.RSAPrivateKey)
            or loaded_key.key_size != _RSA_BITS
            or loaded_key.public_key().public_numbers().e != 65537
            or loaded_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("ascii")
            != private_key
            or _encode_android_public_key(loaded_key.public_key().public_numbers())
            != public_key
        ):
            raise AdbCredentialError
        signer = PythonRSASigner(public_key, private_key)
    except (
        BinasciiError,
        IndexError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ) as err:
        raise AdbCredentialError from err
    if signer.GetPublicKey() != public_key:
        raise AdbCredentialError
    return _StoredCredential(private_key=private_key, public_key=public_key)


def _store_presence(path_text: str) -> tuple[bool, bool]:
    """Return exact-store and prior-corruption presence without reading contents."""
    path = Path(path_text)
    try:
        exists = os.path.lexists(path)
        if not path.parent.exists():
            return exists, False
        corrupt = any(
            candidate.name.startswith(f"{path.name}.corrupt.")
            for candidate in path.parent.iterdir()
        )
    except OSError as err:
        raise AdbCredentialError from err
    return exists, corrupt


def _store_is_private(path_text: str) -> bool:
    """Return whether a persisted secret is a regular owner-only POSIX file."""
    path = Path(path_text)
    try:
        metadata = path.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and (metadata.st_mode & 0o777) == 0o600
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
        )
    except OSError as err:
        raise AdbCredentialError from err


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise AdbCredentialError
        document[key] = value
    return document


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _parse_store_document(body: bytes) -> _StoredCredential:
    try:
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except AdbCredentialError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError) as err:
        raise AdbCredentialError from err
    if not isinstance(document, dict) or document.keys() != {
        "version",
        "minor_version",
        "key",
        "data",
    }:
        raise AdbCredentialError
    if (
        type(document["version"]) is not int
        or document["version"] != _STORE_VERSION
        or type(document["minor_version"]) is not int
        or document["minor_version"] != 1
        or document["key"] != _STORE_KEY
    ):
        raise AdbCredentialError
    return _parse_stored_credential(document["data"])


def _serialize_credential(credential: _StoredCredential) -> dict[str, str]:
    """Return the exact Store payload for one validated credential."""
    return {
        "format": _FORMAT,
        "private_key_pkcs8_pem": credential.private_key,
        "public_key_adb": credential.public_key,
    }


def _validated_credential_for_save(
    credential: _StoredCredential,
) -> dict[str, str]:
    """Strictly round-trip the full candidate before Store may write it."""
    document = _serialize_credential(credential)
    wrapped = {
        "version": _STORE_VERSION,
        "minor_version": 1,
        "key": _STORE_KEY,
        "data": document,
    }
    try:
        body = json.dumps(
            wrapped,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as err:
        raise AdbCredentialError from err
    if not 1 <= len(body) <= _MAX_STORE_BYTES:
        raise AdbCredentialError
    verified = _parse_store_document(body)
    if verified != credential or _serialize_credential(verified) != document:
        raise AdbCredentialError
    return document


def _read_durable_credential(path_text: str) -> _StoredCredential:
    """Read and validate one exact no-follow Store file descriptor."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        file_fd = os.open(path_text, flags)
    except OSError as err:
        raise AdbCredentialError from err
    try:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_STORE_BYTES
        ):
            raise AdbCredentialError
        body = bytearray()
        while chunk := os.read(file_fd, min(4096, _MAX_STORE_BYTES + 1 - len(body))):
            body.extend(chunk)
            if len(body) > _MAX_STORE_BYTES:
                raise AdbCredentialError
        after = os.fstat(file_fd)
        path_after = os.lstat(path_text)
        if (
            _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(after) != _metadata_identity(path_after)
            or len(body) != before.st_size
        ):
            raise AdbCredentialError
        credential = _parse_store_document(bytes(body))
        final = os.fstat(file_fd)
        final_path = os.lstat(path_text)
        if _metadata_identity(after) != _metadata_identity(final) or _metadata_identity(
            final
        ) != _metadata_identity(final_path):
            raise AdbCredentialError
        return credential
    except AdbCredentialError:
        raise
    except OSError as err:
        raise AdbCredentialError from err
    finally:
        os.close(file_fd)


class AdbCredentialManager:
    """Load or create the one durable ADB identity owned by the integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, str]] = Store(
            hass,
            _STORE_VERSION,
            _STORE_KEY,
            private=True,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._credential: _StoredCredential | None = None

    async def async_get_credential(self) -> AdbCredential:
        """Return the shared signer and generation, persisting it only once."""
        async with self._lock:
            try:
                if self._credential is None:
                    existed, corrupt = await self._hass.async_add_executor_job(
                        _store_presence, self._store.path
                    )
                    if corrupt:
                        raise AdbCredentialError
                    if not existed:
                        credential = await self._hass.async_add_executor_job(
                            _generate_credential
                        )
                        self._credential = await self._async_persist_locked(credential)
                    else:
                        self._credential = await self._hass.async_add_executor_job(
                            _read_durable_credential, self._store.path
                        )

                return AdbCredential(
                    signer=PythonRSASigner(
                        self._credential.public_key,
                        self._credential.private_key,
                    ),
                    generation_id=_credential_generation_id(
                        self._credential.public_key
                    ),
                )
            except asyncio.CancelledError:
                self._credential = None
                raise
            except AdbCredentialError:
                self._credential = None
                raise
            except Exception as err:
                self._credential = None
                raise AdbCredentialError from err

    async def _async_persist_locked(
        self, credential: _StoredCredential
    ) -> _StoredCredential:
        """Persist and verify one identity without detaching a Store writer."""
        document = _validated_credential_for_save(credential)
        cancellation: asyncio.CancelledError | None = None
        _save_result, failure, cancellation = await self._async_drain_operation(
            lambda: self._async_store_save(document),
            name=f"{DOMAIN}-adb-credential-store-save",
            cancellation=cancellation,
        )
        presence, presence_error, cancellation = await self._async_drain_operation(
            lambda: self._hass.async_add_executor_job(
                _store_presence, self._store.path
            ),
            name=f"{DOMAIN}-adb-credential-store-presence",
            cancellation=cancellation,
        )
        if failure is None:
            failure = presence_error

        verified: _StoredCredential | None = None
        if presence_error is None:
            existed, corrupt = presence
            if not existed or corrupt:
                if failure is None:
                    failure = AdbCredentialError()
            else:
                (
                    verified_result,
                    read_error,
                    cancellation,
                ) = await self._async_drain_operation(
                    lambda: self._hass.async_add_executor_job(
                        _read_durable_credential, self._store.path
                    ),
                    name=f"{DOMAIN}-adb-credential-store-readback",
                    cancellation=cancellation,
                )
                if failure is None:
                    failure = read_error
                if read_error is None:
                    verified = verified_result
                    if verified != credential and failure is None:
                        failure = AdbCredentialError()

        if cancellation is not None:
            if failure is not None:
                raise cancellation from failure
            raise cancellation
        if failure is not None:
            if isinstance(failure, asyncio.CancelledError):
                raise AdbCredentialError from failure
            raise failure
        if verified is None:
            raise AdbCredentialError
        return verified

    async def _async_store_save(self, document: dict[str, str]) -> None:
        """Enter Store's immediate-write path without an intervening yield."""
        if self._hass.state in {CoreState.stopping, CoreState.final_write}:
            raise AdbCredentialError
        await self._store.async_save(document)

    async def _async_drain_operation(
        self,
        operation_factory: Callable[[], Awaitable[Any]],
        *,
        name: str,
        cancellation: asyncio.CancelledError | None,
    ) -> tuple[Any, BaseException | None, asyncio.CancelledError | None]:
        """Drain one authority operation despite repeated caller cancellation."""
        operation_task = self._hass.async_create_task(
            self._async_operation_outcome(operation_factory),
            name,
            eager_start=False,
        )
        while not operation_task.done():
            try:
                await asyncio.shield(operation_task)
            except asyncio.CancelledError as err:
                if cancellation is None:
                    cancellation = err
        result, error = operation_task.result()
        return result, error, cancellation

    @staticmethod
    async def _async_operation_outcome(
        operation_factory: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, BaseException | None]:
        """Capture an operation result so cancelled shield wrappers stay quiet."""
        try:
            return await operation_factory(), None
        except asyncio.CancelledError as err:
            return None, err
        except Exception as err:
            return None, err

    async def async_get_signer(self) -> PythonRSASigner:
        """Return the shared signer for existing ADB consumers."""
        return (await self.async_get_credential()).signer

    async def async_get_durable_credential(self) -> AdbCredential:
        """Reload and verify the exact persisted identity without regenerating it."""
        async with self._lock:
            try:
                _existed, corrupt = await self._hass.async_add_executor_job(
                    _store_presence, self._store.path
                )
                if corrupt:
                    raise AdbCredentialError
                credential = await self._hass.async_add_executor_job(
                    _read_durable_credential, self._store.path
                )
                self._credential = credential
                return AdbCredential(
                    signer=PythonRSASigner(
                        credential.public_key,
                        credential.private_key,
                    ),
                    generation_id=_credential_generation_id(credential.public_key),
                )
            except asyncio.CancelledError:
                self._credential = None
                raise
            except AdbCredentialError:
                self._credential = None
                raise
            except Exception as err:
                self._credential = None
                raise AdbCredentialError from err


def _get_adb_credential_manager(hass: HomeAssistant) -> AdbCredentialManager:
    """Return the guarded process-wide credential authority."""
    manager = hass.data.get(_MANAGER_DATA_KEY)
    if manager is None:
        manager = AdbCredentialManager(hass)
        hass.data[_MANAGER_DATA_KEY] = manager
    if not isinstance(manager, AdbCredentialManager):
        raise AdbCredentialError
    return manager


async def async_get_adb_credential(hass: HomeAssistant) -> AdbCredential:
    """Return the process-wide signer and stable public-key generation ID."""
    return await _get_adb_credential_manager(hass).async_get_credential()


async def async_get_durable_adb_credential(hass: HomeAssistant) -> AdbCredential:
    """Reload the current durable ADB identity for a mutation boundary."""
    return await _get_adb_credential_manager(hass).async_get_durable_credential()


async def async_get_adb_signer(hass: HomeAssistant) -> PythonRSASigner:
    """Return the process-wide persistent ADB signer for ha-paneld."""
    return (await async_get_adb_credential(hass)).signer
