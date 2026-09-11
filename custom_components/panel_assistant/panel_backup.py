"""Keep the panel's own settings backup in Home Assistant before changing its app."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_KEEP_PER_PANEL = 5


def _write_backup(directory: Path, name: str, prefix: str, data: bytes) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / name
    partial = directory / f".{name}.partial"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, target)
    kept = sorted(directory.glob(f"{prefix}*.zip"))
    for old in kept[:-_KEEP_PER_PANEL]:
        old.unlink(missing_ok=True)
    return target


async def async_store_panel_backup(
    hass: HomeAssistant, entry_id: str, version_code: int | None, data: bytes
) -> Path:
    """Write one backup atomically and keep only the newest few for this panel."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{entry_id}-"
    name = f"{prefix}{stamp}-vc{version_code if version_code else 'unknown'}.zip"
    directory = Path(hass.config.path(DOMAIN, "backups"))
    return await hass.async_add_executor_job(
        _write_backup, directory, name, prefix, data
    )
