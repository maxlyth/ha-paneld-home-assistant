"""Configured-panel update projection tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.panel_assistant import update as panel_update
from custom_components.panel_assistant.client import (
    CannotConnectError,
    PanelHealth,
    PanelInstallStatus,
    UpdateApprovalRequiredError,
    UpdateBusyError,
    UpdateRejectedError,
)
from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.coordinator import (
    HaPaneldDataUpdateCoordinator,
    PanelSnapshot,
)
from custom_components.panel_assistant.status import PanelCachedUpdate, PanelStatus
from custom_components.panel_assistant.update import HaPaneldUpdateEntity
from custom_components.panel_assistant.update_coordinator import (
    PanelUpdateCoordinator,
    PanelUpdateSnapshot,
)

HEALTH = PanelHealth(
    version="0.9.9",
    panel_id="alpha",
    build="1000",
    config_hash="1a2b3c4d",
)
OFFER = PanelCachedUpdate("0.9.9", "0.9.10", "v0.9.10")


def _assert_translated(error: HomeAssistantError, key: str) -> None:
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key


def _entity(
    hass: HomeAssistant,
    *,
    version: str = "0.9.9",
    offer: PanelCachedUpdate | None = OFFER,
    operation: PanelInstallStatus | None = None,
    update_error: str | None = None,
) -> tuple[HaPaneldUpdateEntity, SimpleNamespace]:
    client = SimpleNamespace(
        configuration_url="http://panel.local:8888",
        async_start_panel_update=AsyncMock(),
        async_get_panel_install_status=AsyncMock(),
    )
    health = HaPaneldDataUpdateCoordinator(hass, client)  # type: ignore[arg-type]
    health.data = PanelSnapshot(
        health=PanelHealth(
            version=version,
            panel_id=HEALTH.panel_id,
            build=HEALTH.build,
            config_hash=HEALTH.config_hash,
        ),
        status=PanelStatus(
            warning_count=0,
            capability_count=0,
            panel_assistant_update=offer,
        ),
        status_error=None,
    )
    updates = PanelUpdateCoordinator(hass, client)  # type: ignore[arg-type]
    updates.data = PanelUpdateSnapshot(operation=operation, error=update_error)
    entity = HaPaneldUpdateEntity("entry-id", health, updates)
    entity.hass = hass
    return entity, client


def test_update_entity_uses_existing_config_entry_and_offers_only_newer_stable(
    hass: HomeAssistant,
) -> None:
    """The entity has no separate device identity or arbitrary-version selector."""
    entity, _ = _entity(hass)

    assert entity.unique_id == "entry-id_update"
    assert entity.installed_version == "0.9.9"
    assert entity.latest_version == "0.9.10"
    assert entity.version_is_newer(entity.latest_version, entity.installed_version)
    assert entity.device_info["identifiers"] == {("panel_assistant", "entry-id")}


def test_update_entity_hides_downgrades_and_unsupported_updater(
    hass: HomeAssistant,
) -> None:
    """A stale, lower, or unreadable panel offer never becomes an update action."""
    lower, _ = _entity(hass, offer=PanelCachedUpdate("0.9.9", "0.9.8", "v0.9.8"))
    stale, _ = _entity(hass, offer=PanelCachedUpdate("0.9.8", "0.9.10", "v0.9.10"))

    assert lower.latest_version == lower.installed_version
    assert stale.latest_version == stale.installed_version


async def test_update_entity_starts_only_the_current_panel_offer(
    hass: HomeAssistant,
) -> None:
    """The standard HA action starts one exact tag and proves the replacement health."""
    entity, client = _entity(hass)
    entity.async_write_ha_state = MagicMock()

    async def refresh_health() -> None:
        entity.coordinator.data = PanelSnapshot(
            health=PanelHealth(
                version="0.9.10",
                panel_id=HEALTH.panel_id,
                build=HEALTH.build,
                config_hash=HEALTH.config_hash,
            ),
            status=PanelStatus(warning_count=0, capability_count=0),
            status_error=None,
        )

    entity.coordinator.async_request_refresh = AsyncMock(side_effect=refresh_health)
    entity._update_coordinator.async_request_refresh = AsyncMock()

    await entity.async_install(None, backup=False)

    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")
    assert entity.installed_version == "0.9.10"
    assert entity.in_progress is False


@pytest.mark.parametrize("version", ["0.9.9", "0.9.11"])
async def test_update_entity_rejects_versions_outside_the_current_offer(
    hass: HomeAssistant, version: str
) -> None:
    """HA cannot turn the update service into a version or downgrade control surface."""
    entity, client = _entity(hass)

    with pytest.raises(HomeAssistantError, match="unavailable") as error:
        await entity.async_install(version, backup=False)

    _assert_translated(error.value, "update_unavailable")
    client.async_start_panel_update.assert_not_awaited()


async def test_update_entity_maps_panel_busy_without_starting_a_retry(
    hass: HomeAssistant,
) -> None:
    """Another panel operation remains authoritative over the shared slot."""
    entity, client = _entity(hass)
    client.async_start_panel_update.side_effect = UpdateBusyError

    with pytest.raises(HomeAssistantError, match="busy") as error:
        await entity.async_install(None, backup=False)

    _assert_translated(error.value, "update_busy")
    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")


async def test_update_entity_maps_a_panel_refusal_without_retrying(
    hass: HomeAssistant,
) -> None:
    """Physical approval and panel admission remain the panel's authority."""
    entity, client = _entity(hass)
    client.async_start_panel_update.side_effect = UpdateRejectedError

    with pytest.raises(HomeAssistantError, match="refused") as error:
        await entity.async_install(None, backup=False)

    _assert_translated(error.value, "update_rejected")
    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")


async def test_update_entity_requests_physical_approval_without_retrying(
    hass: HomeAssistant,
) -> None:
    """Hardened mode remains a panel-local approval and an explicit retry."""
    entity, client = _entity(hass)
    client.async_start_panel_update.side_effect = UpdateApprovalRequiredError

    with pytest.raises(
        HomeAssistantError, match="Approve this update on the panel"
    ) as error:
        await entity.async_install(None, backup=False)

    _assert_translated(error.value, "update_approval_required")
    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")


async def test_update_entity_localizes_transport_failure(
    hass: HomeAssistant,
) -> None:
    """A rejected transport response uses the integration exception catalogue."""
    entity, client = _entity(hass)
    client.async_start_panel_update.side_effect = CannotConnectError

    with pytest.raises(HomeAssistantError, match="did not accept") as error:
        await entity.async_install(None, backup=False)

    _assert_translated(error.value, "update_not_accepted")
    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")


async def test_update_entity_reports_a_completed_panel_operation_without_new_health(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished operation is not reported successful until new health proves it."""
    entity, client = _entity(hass)
    entity.async_write_ha_state = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity._update_coordinator.async_request_refresh = AsyncMock()
    entity._update_coordinator.data = PanelUpdateSnapshot(
        operation=PanelInstallStatus(
            running=False,
            component="ha-paneld",
        ),
        error=None,
    )
    monkeypatch.setattr(panel_update, "_TERMINAL_STATUS_GRACE_SECONDS", 0)

    with pytest.raises(HomeAssistantError, match="did not complete") as error:
        await entity.async_install(None, backup=False)

    _assert_translated(error.value, "update_not_complete")
    client.async_start_panel_update.assert_awaited_once_with("v0.9.10")


async def test_update_entity_observes_health_through_terminal_restart_race(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished progress slot does not preempt fresh health after replacement."""
    entity, _ = _entity(hass)
    entity.async_write_ha_state = MagicMock()
    refreshes = 0

    async def refresh_health() -> None:
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            entity.coordinator.data = PanelSnapshot(
                health=PanelHealth(
                    version="0.9.10",
                    panel_id=HEALTH.panel_id,
                    build=HEALTH.build,
                    config_hash=HEALTH.config_hash,
                ),
                status=PanelStatus(warning_count=0, capability_count=0),
                status_error=None,
            )

    entity.coordinator.async_request_refresh = AsyncMock(side_effect=refresh_health)
    entity._update_coordinator.async_request_refresh = AsyncMock()
    entity._update_coordinator.data = PanelUpdateSnapshot(
        operation=PanelInstallStatus(
            running=False,
            component="ha-paneld",
        ),
        error=None,
    )
    monkeypatch.setattr(panel_update.asyncio, "sleep", AsyncMock())

    await entity.async_install(None, backup=False)

    assert refreshes == 2
    assert entity.installed_version == "0.9.10"


def test_update_timeout_covers_android_download_install_and_restart_bounds() -> None:
    """HA must not time out during the panel's declared transaction bounds."""
    assert panel_update._UPDATE_TIMEOUT_SECONDS == 14 * 60


def test_update_entity_restores_a_panel_owned_operation_after_reload(
    hass: HomeAssistant,
) -> None:
    """A running panel operation stays in progress without entity-local state."""
    entity, _ = _entity(
        hass,
        operation=PanelInstallStatus(running=True, component="ha-paneld"),
    )

    assert entity.in_progress is True


async def test_reload_observer_latches_through_transient_and_terminal_status(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reload recovery waits for fresh target health without issuing an install."""
    entity, client = _entity(
        hass,
        operation=PanelInstallStatus(running=True, component="ha-paneld"),
    )
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    health_refreshes = 0
    guarded_samples: list[bool] = []

    async def refresh_health() -> None:
        nonlocal health_refreshes
        health_refreshes += 1
        guarded_samples.append(entity.in_progress)
        if health_refreshes == 3:
            entity.coordinator.data = PanelSnapshot(
                health=PanelHealth(
                    version="0.9.10",
                    panel_id=HEALTH.panel_id,
                    build=HEALTH.build,
                    config_hash=HEALTH.config_hash,
                ),
                status=PanelStatus(warning_count=0, capability_count=0),
                status_error=None,
            )

    update_samples = iter(
        [
            PanelUpdateSnapshot(operation=None, error="unavailable"),
            PanelUpdateSnapshot(
                operation=PanelInstallStatus(
                    running=False,
                    component="ha-paneld",
                ),
                error=None,
            ),
            PanelUpdateSnapshot(operation=None, error=None),
        ]
    )

    async def refresh_update() -> None:
        entity._update_coordinator.data = next(update_samples)

    entity.coordinator.async_request_refresh = AsyncMock(side_effect=refresh_health)
    entity._update_coordinator.async_request_refresh = AsyncMock(
        side_effect=refresh_update
    )
    monkeypatch.setattr(panel_update.asyncio, "sleep", AsyncMock())

    entity._resume_running_operation()
    observer = entity._observer_task
    assert observer is not None
    await observer

    assert guarded_samples == [True, True, True]
    assert entity.installed_version == "0.9.10"
    assert entity.in_progress is False
    client.async_start_panel_update.assert_not_awaited()


async def test_unload_cancels_recovered_observer_without_install_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entity teardown owns and cancels its sole recovered observer."""
    entity, client = _entity(
        hass,
        operation=PanelInstallStatus(running=True, component="ha-paneld"),
    )
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    waiting = asyncio.Event()

    async def wait_forever(_expected_version: str) -> None:
        await waiting.wait()

    monkeypatch.setattr(entity, "_async_wait_for_installed_version", wait_forever)

    entity._resume_running_operation()
    observer = entity._observer_task
    assert observer is not None
    await asyncio.sleep(0)
    entity._cancel_observer()
    await asyncio.sleep(0)

    assert observer.cancelled()
    assert entity._observer_task is None
    client.async_start_panel_update.assert_not_awaited()


async def test_update_coordinator_keeps_local_operation_faults_out_of_entry_setup(
    hass: HomeAssistant,
) -> None:
    """A missing progress endpoint cannot make an existing status entry unavailable."""
    client = SimpleNamespace(async_get_panel_install_status=AsyncMock())
    client.async_get_panel_install_status.side_effect = CannotConnectError
    coordinator = PanelUpdateCoordinator(hass, client)  # type: ignore[arg-type]

    snapshot = await coordinator._async_update_data()

    assert snapshot == PanelUpdateSnapshot(operation=None, error="unavailable")
