"""Integration lifecycle, device and diagnostics tests."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_paneld import async_reload_entry
from custom_components.ha_paneld.client import (
    CannotConnectError,
    InvalidResponseError,
    PanelHealth,
)
from custom_components.ha_paneld.const import DOMAIN
from custom_components.ha_paneld.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ha_paneld.install_artifacts import (
    ArtifactCustodyError,
    ArtifactErrorCode,
)
from custom_components.ha_paneld.install_executor import InstallExecutor
from custom_components.ha_paneld.install_jobs import (
    InstallJobRevisionError,
    InstallJobStoreError,
    InstallPhase,
    InstallResultCode,
)
from custom_components.ha_paneld.status import PanelStatus

HEALTH = PanelHealth(
    version="0.9.0",
    panel_id="alpha",
    build="1000",
    config_hash="1a2b3c4d",
    ha_state="normal",
    ha_source="mqtt",
)
BETA_HEALTH = PanelHealth(
    version="0.9.1",
    panel_id="beta",
    build="1001",
    config_hash="2a2b3c4d",
)
STATUS = PanelStatus(
    warning_count=2,
    capability_count=3,
    renderer={"mode": "builtin", "state": "rendered", "rendered": True},
    camera={"state": "absent", "live": False},
)


def _entry(hass: HomeAssistant, address: str = "panel.local") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="alpha",
        data={CONF_ADDRESS: address},
    )
    entry.add_to_hass(hass)
    return entry


def _receipt(
    *,
    address: str = "panel.local",
    version: str = HEALTH.version,
    phase: InstallPhase = InstallPhase.HEALTHY_UNCLAIMED,
) -> SimpleNamespace:
    """Return the receipt fields used by setup reconciliation."""
    return SimpleNamespace(
        job_id="1" * 32,
        revision=7,
        phase=phase,
        target=SimpleNamespace(address=address),
        artifact=SimpleNamespace(version_name=version),
    )


def _installer_doubles(
    receipts: tuple[SimpleNamespace, ...] = (),
    *,
    finalizer_active: bool = False,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Return isolated process-executor and receipt-manager doubles."""
    executor = SimpleNamespace(
        async_acquire_finalizer=AsyncMock(return_value=not finalizer_active),
        async_release_finalizer=AsyncMock(),
    )
    manager = SimpleNamespace(
        async_list=AsyncMock(return_value=receipts),
        async_transition=AsyncMock(),
    )
    return executor, manager


def _assert_healthy_entry_loaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Assert the existing entry, device and diagnostic sensor remain available."""
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.health == HEALTH
    entities = [
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    assert len(entities) == 1
    state = hass.states.get(entities[0].entity_id)
    assert state is not None
    assert state.state == "online"
    assert (
        len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 1
    )


async def test_setup_device_diagnostics_unload_reload(hass: HomeAssistant) -> None:
    """The vertical slice creates one device and unloads/reloads cleanly."""
    entry = _entry(hass)
    health_mock = AsyncMock(side_effect=[HEALTH, BETA_HEALTH])
    status_mock = AsyncMock(return_value=STATUS)
    resume_mock = AsyncMock(return_value=())
    executor, manager = _installer_doubles()

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            resume_mock,
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_entries = er.async_get(hass).entities.values()
        entities = [
            item for item in entity_entries if item.config_entry_id == entry.entry_id
        ]
        devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

        assert len(entities) == 1
        assert len(devices) == 1
        state = hass.states.get(entities[0].entity_id)
        assert state is not None
        assert state.state == "online"
        assert state.attributes["build"] == HEALTH.build
        assert devices[0].identifiers == {(DOMAIN, entry.entry_id)}
        assert devices[0].sw_version == HEALTH.version

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        device_diagnostics = await async_get_device_diagnostics(hass, entry, devices[0])
        assert diagnostics["entry"][CONF_ADDRESS] == "**REDACTED**"
        assert diagnostics["health"]["panel_id"] == "**REDACTED**"
        assert diagnostics["health"]["build"] == HEALTH.build
        assert diagnostics["status"] == STATUS.as_dict()
        assert diagnostics["status_error"] is None
        assert device_diagnostics == diagnostics
        assert health_mock.await_count == 1
        assert status_mock.await_count == 1

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED

        await async_reload_entry(hass, entry)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        reloaded_entities = [
            item
            for item in er.async_get(hass).entities.values()
            if item.config_entry_id == entry.entry_id
        ]
        reloaded_devices = dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        assert [item.id for item in reloaded_entities] == [item.id for item in entities]
        assert [item.id for item in reloaded_devices] == [item.id for item in devices]
        assert entry.runtime_data.coordinator.data.health.panel_id == "beta"

    assert health_mock.await_count == 2
    assert status_mock.await_count == 2
    assert resume_mock.await_count == 2
    assert manager.async_list.await_count == 2


async def test_setup_survives_install_job_resume_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt installer state cannot block an ordinary existing entry."""
    entry = _entry(hass)
    executor, manager = _installer_doubles()
    private_detail = "panel-secret.local"

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(side_effect=InstallJobStoreError(private_detail)),
        ) as resume_mock,
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.health == HEALTH
    resume_mock.assert_awaited_once_with(hass)
    assert private_detail not in caplog.text
    assert "Unable to resume durable ha-paneld install jobs" in caplog.text


async def test_setup_survives_install_artifact_resume_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Artifact custody failure cannot block an ordinary existing entry."""
    entry = _entry(hass)
    executor, manager = _installer_doubles()
    private_detail = "private-artifact-path"
    error = ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
    error.args = (private_detail,)

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(side_effect=error),
        ) as resume_mock,
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    _assert_healthy_entry_loaded(hass, entry)
    resume_mock.assert_awaited_once_with(hass)
    manager.async_list.assert_awaited_once_with()
    assert private_detail not in caplog.text
    assert "Unable to resume durable ha-paneld install jobs" in caplog.text


async def test_setup_survives_install_receipt_store_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Post-refresh receipt corruption leaves platform setup available."""
    entry = _entry(hass)
    executor, _manager = _installer_doubles()
    private_detail = "192.168.1.44"

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(side_effect=InstallJobStoreError(private_detail)),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert any(
        item.config_entry_id == entry.entry_id
        for item in er.async_get(hass).entities.values()
    )
    assert private_detail not in caplog.text
    assert "Unable to reconcile a durable ha-paneld install receipt" in caplog.text


async def test_setup_survives_install_artifact_reconciliation_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Artifact cleanup failure cannot block a healthy existing entry."""
    entry = _entry(hass)
    private_detail = "private-artifact-path"
    error = ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
    error.args = (private_detail,)

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(side_effect=error),
        ) as executor_mock,
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(),
        ) as manager_mock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    _assert_healthy_entry_loaded(hass, entry)
    executor_mock.assert_awaited_once_with(hass)
    manager_mock.assert_not_awaited()
    assert private_detail not in caplog.text
    assert "Unable to reconcile a durable ha-paneld install receipt" in caplog.text


async def test_artifact_resume_failure_does_not_hide_health_setup_failure(
    hass: HomeAssistant,
) -> None:
    """Installer containment leaves coordinator retry authority unchanged."""
    entry = _entry(hass)
    status_mock = AsyncMock(return_value=STATUS)

    with (
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(side_effect=ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    status_mock.assert_not_awaited()


async def test_setup_consumes_matching_healthy_install_receipt(
    hass: HomeAssistant,
) -> None:
    """A loaded entry completes the cross-store handoff with its actual ID."""
    entry = _entry(hass, "Panel.Local")
    receipt = _receipt()
    executor, manager = _installer_doubles((receipt,))

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert len(entry.entry_id) == 26
    executor.async_acquire_finalizer.assert_awaited_once_with(
        receipt.job_id, f"setup_{entry.entry_id}"
    )
    manager.async_transition.assert_awaited_once_with(
        receipt.job_id,
        receipt.revision,
        InstallPhase.CONSUMED,
        result_code=InstallResultCode.ENTRY_CREATED,
        consumed_entry_id=entry.entry_id,
    )
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, f"setup_{entry.entry_id}"
    )


async def test_setup_leaves_flow_owned_healthy_receipt_unconsumed(
    hass: HomeAssistant,
) -> None:
    """An active finalizer keeps ownership of entry creation handoff."""
    entry = _entry(hass)
    receipt = _receipt()
    executor, manager = _installer_doubles((receipt,), finalizer_active=True)

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    executor.async_acquire_finalizer.assert_awaited_once_with(
        receipt.job_id, f"setup_{entry.entry_id}"
    )
    executor.async_release_finalizer.assert_not_awaited()
    manager.async_transition.assert_not_awaited()


async def test_setup_quarantines_matching_receipt_on_version_mismatch(
    hass: HomeAssistant,
) -> None:
    """Unexpected installed version requires recovery without blocking setup."""
    entry = _entry(hass)
    receipt = _receipt(version="9.9.9")
    executor, manager = _installer_doubles((receipt,))

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    manager.async_transition.assert_awaited_once_with(
        receipt.job_id,
        receipt.revision,
        InstallPhase.RECOVERY_REQUIRED,
        result_code=InstallResultCode.VERIFICATION_REQUIRED,
    )
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, f"setup_{entry.entry_id}"
    )


@pytest.mark.parametrize(
    ("observed_health", "expected_phase"),
    [
        (HEALTH, InstallPhase.CONSUMED),
        (BETA_HEALTH, InstallPhase.RECOVERY_REQUIRED),
    ],
)
async def test_setup_holds_finalizer_lease_through_receipt_transition(
    hass: HomeAssistant,
    observed_health: PanelHealth,
    expected_phase: InstallPhase,
) -> None:
    """A flow cannot acquire finalization during consume or quarantine."""
    entry = _entry(hass)
    receipt = _receipt()
    transition_entered = asyncio.Event()
    allow_transition = asyncio.Event()

    async def _blocking_transition(*_args: object, **_kwargs: object) -> None:
        transition_entered.set()
        await allow_transition.wait()
        receipt.phase = expected_phase

    manager = SimpleNamespace(
        async_get=AsyncMock(return_value=receipt),
        async_list=AsyncMock(return_value=(receipt,)),
        async_transition=AsyncMock(side_effect=_blocking_transition),
    )
    executor = InstallExecutor(hass, manager)

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=observed_health),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        setup_task = hass.async_create_task(
            hass.config_entries.async_setup(entry.entry_id),
            "setup entry during finalizer race",
        )
        await asyncio.wait_for(transition_entered.wait(), timeout=1)

        assert not await executor.async_acquire_finalizer(
            receipt.job_id, "flow_finalizer"
        )

        allow_transition.set()
        assert await setup_task
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert receipt.phase is expected_phase


async def test_setup_survives_install_receipt_revision_failure(
    hass: HomeAssistant,
) -> None:
    """Finalization persistence failures cannot remove or block the entry."""
    entry = _entry(hass)
    receipt = _receipt()
    executor, manager = _installer_doubles((receipt,))
    manager.async_transition.side_effect = InstallJobRevisionError
    executor.async_release_finalizer.side_effect = InstallJobStoreError

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.health == HEALTH


@pytest.mark.parametrize(
    "receipts",
    [
        (_receipt(address="other-panel.local"),),
        (_receipt(phase=InstallPhase.CONSUMED),),
    ],
)
async def test_setup_ignores_unrelated_or_terminal_install_receipts(
    hass: HomeAssistant, receipts: tuple[SimpleNamespace, ...]
) -> None:
    """Only the matching active handoff receipt can affect setup."""
    entry = _entry(hass)
    executor, manager = _installer_doubles(receipts)

    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(return_value=STATUS),
        ),
        patch(
            "custom_components.ha_paneld.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.ha_paneld.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    executor.async_acquire_finalizer.assert_not_awaited()
    executor.async_release_finalizer.assert_not_awaited()
    manager.async_transition.assert_not_awaited()


async def test_setup_retries_when_panel_is_offline(hass: HomeAssistant) -> None:
    """A transient setup failure becomes a Home Assistant retry."""
    entry = _entry(hass)

    status_mock = AsyncMock(return_value=STATUS)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    status_mock.assert_not_awaited()


async def test_coordinator_translates_client_failure(hass: HomeAssistant) -> None:
    """Polling keeps client exceptions behind the coordinator boundary."""
    entry = _entry(hass)
    status_mock = AsyncMock(return_value=STATUS)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            status_mock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entry.runtime_data.client.async_get_health = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnectError
    )
    try:
        await entry.runtime_data.coordinator._async_update_data()
    except UpdateFailed:
        pass
    else:
        raise AssertionError("Client failure was not translated to UpdateFailed")
    assert status_mock.await_count == 1


@pytest.mark.parametrize(
    ("status_error", "diagnostic_error"),
    [
        (InvalidResponseError(), "invalid_response"),
        (CannotConnectError(), "unavailable"),
    ],
)
async def test_status_failure_does_not_override_health_authority(
    hass: HomeAssistant, status_error: Exception, diagnostic_error: str
) -> None:
    """Rejected optional diagnostics leave the existing health sensor online."""
    entry = _entry(hass)
    with (
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.ha_paneld.client.HaPaneldClient.async_get_status",
            AsyncMock(side_effect=status_error),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entities = [
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    assert len(entities) == 1
    state = hass.states.get(entities[0].entity_id)
    assert state is not None
    assert state.state == "online"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["status"] is None
    assert diagnostics["status_error"] == diagnostic_error
    assert diagnostics["health"]["build"] == HEALTH.build
