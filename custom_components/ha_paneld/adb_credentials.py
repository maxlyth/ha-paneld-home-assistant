"""Persistent integration-owned Android Debug Bridge credentials."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
import struct
from binascii import Error as BinasciiError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}.adb_key"
_MANAGER_DATA_KEY = f"{DOMAIN}.adb_credential_manager"
_MAX_PRIVATE_KEY_LENGTH = 4096
_MAX_PUBLIC_KEY_LENGTH = 1024
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
        mode = path.lstat().st_mode
        return stat.S_ISREG(mode) and (mode & 0o777) == 0o600
    except OSError as err:
        raise AdbCredentialError from err


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

    async def async_get_signer(self) -> PythonRSASigner:
        """Return the shared signer, generating and persisting it only once."""
        async with self._lock:
            try:
                if self._credential is None:
                    existed, corrupt = await self._hass.async_add_executor_job(
                        _store_presence, self._store.path
                    )
                    stored = await self._store.async_load()
                    if stored is None:
                        if existed or corrupt:
                            raise AdbCredentialError
                        credential = await self._hass.async_add_executor_job(
                            _generate_credential
                        )
                        serialized = {
                            "format": _FORMAT,
                            "private_key_pkcs8_pem": credential.private_key,
                            "public_key_adb": credential.public_key,
                        }
                        await self._store.async_save(serialized)
                        # Store logs and absorbs write failures. Reopen and validate
                        # the durable bytes before this identity is offered to ADB.
                        verifier: Store[dict[str, str]] = Store(
                            self._hass,
                            _STORE_VERSION,
                            _STORE_KEY,
                            private=True,
                            atomic_writes=True,
                        )
                        persisted = await verifier.async_load()
                        if (
                            persisted != serialized
                            or not await self._hass.async_add_executor_job(
                                _store_is_private, self._store.path
                            )
                        ):
                            raise AdbCredentialError
                        self._credential = _parse_stored_credential(persisted)
                    else:
                        if not await self._hass.async_add_executor_job(
                            _store_is_private, self._store.path
                        ):
                            raise AdbCredentialError
                        self._credential = _parse_stored_credential(stored)

                return PythonRSASigner(
                    self._credential.public_key,
                    self._credential.private_key,
                )
            except AdbCredentialError:
                raise
            except Exception as err:
                raise AdbCredentialError from err


async def async_get_adb_signer(hass: HomeAssistant) -> PythonRSASigner:
    """Return the process-wide persistent ADB signer for ha-paneld."""
    manager = hass.data.get(_MANAGER_DATA_KEY)
    if manager is None:
        manager = AdbCredentialManager(hass)
        hass.data[_MANAGER_DATA_KEY] = manager
    if not isinstance(manager, AdbCredentialManager):
        raise AdbCredentialError
    return await manager.async_get_signer()
