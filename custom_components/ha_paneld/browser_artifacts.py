"""Browser release custody isolated from native installation job cleanup."""

import hashlib
import os
from collections.abc import Collection
from pathlib import Path

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .install_artifacts import (
    ArtifactCustodyError,
    ArtifactErrorCode,
    InstallArtifact,
    _async_download_artifact,
    _async_executor,
    _async_reconcile_artifacts,
    _close_file,
    _custody_file_identity,
    _remove_owned_files,
    _validate_job_id,
    _verify_directory_binding,
    _verify_private_directory,
)
from .release import ReleaseArtifact

_STORAGE_DIRECTORY = f"{DOMAIN}.browser_artifacts"


def _directory(hass: HomeAssistant) -> str:
    """Never accept storage paths or namespaces from a browser request."""
    return hass.config.path(".storage", _STORAGE_DIRECTORY)


async def async_download_browser_artifact(
    hass: HomeAssistant,
    session: ClientSession,
    artifact: ReleaseArtifact,
    bundle_id: str,
) -> InstallArtifact:
    """Apply the native bounded download and signature-bound size/hash custody."""
    return await _async_download_artifact(
        hass, session, artifact, bundle_id, _directory(hass)
    )


async def async_cleanup_browser_artifact(hass: HomeAssistant, bundle_id: str) -> None:
    """Remove only this browser bundle's owned partial and ready files."""
    validated_id = _validate_job_id(bundle_id)
    await _async_executor(
        hass, _remove_owned_files, _directory(hass), validated_id, True
    )


async def async_reconcile_browser_artifacts(
    hass: HomeAssistant, retained_ids: Collection[str]
) -> None:
    """Clean browser custody without touching native installation artifacts."""
    await _async_reconcile_artifacts(hass, retained_ids, _directory(hass))


def _read_artifact(directory: str, bundle_id: str, expected: InstallArtifact) -> bytes:
    """Read bounded immutable bytes, never following a substituted file link."""
    name = f"{bundle_id}.apk"
    if (
        expected.job_id != bundle_id
        or expected.path != str(Path(directory, name))
        or isinstance(expected.size, bool)
        or not 1 <= expected.size <= 64 * 1024 * 1024
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)
    directory_fd = _verify_private_directory(directory, create=False)
    if directory_fd is None:
        raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)
    file_fd: int | None = None
    try:
        directory_stat = os.fstat(directory_fd)
        file_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        identity = _custody_file_identity(os.fstat(file_fd))
        if identity[-1] != expected.size:
            raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)
        body = bytearray()
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, 64 * 1024):
            if len(body) + len(chunk) > expected.size:
                raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)
            body.extend(chunk)
            digest.update(chunk)
        if (
            len(body) != expected.size
            or digest.hexdigest() != expected.sha256
            or _custody_file_identity(os.fstat(file_fd)) != identity
        ):
            raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)
        _verify_directory_binding(
            directory, (directory_stat.st_dev, directory_stat.st_ino)
        )
        return bytes(body)
    except OSError:
        raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED) from None
    finally:
        if file_fd is not None:
            _close_file(file_fd)
        _close_file(directory_fd)


async def async_read_browser_artifact(
    hass: HomeAssistant, bundle_id: str, expected: InstallArtifact
) -> bytes:
    """Read while the caller holds a cache lease and bounded response admission."""
    validated_id = _validate_job_id(bundle_id)
    return await _async_executor(
        hass, _read_artifact, _directory(hass), validated_id, expected
    )
