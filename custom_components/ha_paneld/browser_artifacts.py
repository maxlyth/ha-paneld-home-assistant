"""Browser release custody isolated from native installation job cleanup."""

from collections.abc import Collection

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .install_artifacts import (
    InstallArtifact,
    _async_download_artifact,
    _async_executor,
    _async_reconcile_artifacts,
    _remove_owned_files,
    _validate_job_id,
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
