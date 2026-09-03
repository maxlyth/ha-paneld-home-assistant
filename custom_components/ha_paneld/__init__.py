"""The ha-paneld integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import HaPaneldClient, normalize_address
from .coordinator import HaPaneldDataUpdateCoordinator
from .install_executor import (
    async_get_install_executor,
    async_resume_loaded_install_jobs,
)
from .install_jobs import (
    InstallPhase,
    InstallResultCode,
    async_get_install_job_manager,
)

PLATFORMS = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HaPaneldRuntimeData:
    """Runtime data for one config entry."""

    client: HaPaneldClient
    coordinator: HaPaneldDataUpdateCoordinator


type HaPaneldConfigEntry = ConfigEntry[HaPaneldRuntimeData]


async def _async_resume_install_jobs(hass: HomeAssistant) -> None:
    """Best-effort resume after an existing entry has loaded the domain."""
    try:
        await async_resume_loaded_install_jobs(hass)
    except Exception:
        _LOGGER.warning("Unable to resume durable ha-paneld install jobs")


async def _async_reconcile_install_receipt(
    hass: HomeAssistant,
    entry: HaPaneldConfigEntry,
    address: str,
    health_version: str,
) -> None:
    """Best-effort handoff for a healthy receipt left by a completed install."""
    executor = None
    receipt = None
    finalizer_id = f"setup_{entry.entry_id}"
    acquired = False
    try:
        executor = await async_get_install_executor(hass)
        manager = await async_get_install_job_manager(hass)
        receipts = await manager.async_list()
        receipt = next(
            (
                candidate
                for candidate in receipts
                if candidate.phase is InstallPhase.HEALTHY_UNCLAIMED
                and candidate.target.address == address
            ),
            None,
        )
        if receipt is None:
            return
        acquired = await executor.async_acquire_finalizer(receipt.job_id, finalizer_id)
        if not acquired:
            return
        if health_version == receipt.artifact.version_name:
            await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                InstallPhase.CONSUMED,
                result_code=InstallResultCode.ENTRY_CREATED,
                consumed_entry_id=entry.entry_id,
            )
            return
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.RECOVERY_REQUIRED,
            result_code=InstallResultCode.VERIFICATION_REQUIRED,
        )
    except Exception:
        _LOGGER.warning("Unable to reconcile a durable ha-paneld install receipt")
    finally:
        if acquired and executor is not None and receipt is not None:
            try:
                await executor.async_release_finalizer(receipt.job_id, finalizer_id)
            except Exception:
                _LOGGER.warning(
                    "Unable to release a durable ha-paneld install finalizer"
                )


async def async_setup_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> bool:
    """Set up ha-paneld from a config entry."""
    await _async_resume_install_jobs(hass)
    address = normalize_address(entry.data[CONF_ADDRESS])
    client = HaPaneldClient(async_get_clientsession(hass), address)
    coordinator = HaPaneldDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    await _async_reconcile_install_receipt(
        hass,
        entry,
        address.stored_value,
        coordinator.data.health.version,
    )

    entry.runtime_data = HaPaneldRuntimeData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> bool:
    """Unload a ha-paneld config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: HaPaneldConfigEntry) -> None:
    """Reload a ha-paneld config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
