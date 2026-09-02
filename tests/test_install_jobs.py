"""Tests for bounded durable first-install receipts."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Generator
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.ha_paneld import install_jobs
from custom_components.ha_paneld.const import DOMAIN
from custom_components.ha_paneld.install_jobs import (
    InstallArtifact,
    InstallJobCapacityError,
    InstallJobConflictError,
    InstallJobManager,
    InstallJobNotFoundError,
    InstallJobReceipt,
    InstallJobRevisionError,
    InstallJobStoreError,
    InstallJobTransitionError,
    InstallPhase,
    InstallResultCode,
    InstallTarget,
    async_get_install_job_manager,
    install_plan_sha256,
)

SHA = "a" * 64
CREDENTIAL_ID = "b" * 64
CURRENT_ENTRY_ID = "01M1J723MDQ69QDQVCRXKYZBJV"
LEGACY_ENTRY_ID = "0123456789abcdef0123456789abcdef"
CLEANUP_BARRIER_PHASES = (
    InstallPhase.STAGING,
    InstallPhase.INSTALLING,
    InstallPhase.LAUNCHING,
)
_REAL_STORE_PRESENCE = install_jobs._store_presence


@pytest.fixture(autouse=True)
def emulate_home_assistant_store_file() -> Generator[None]:
    """Model the Store file hidden by HA's in-memory test storage manager."""
    observed_paths: set[str] = set()

    def _presence(path: str) -> tuple[bool, bool]:
        exists, corrupt = _REAL_STORE_PRESENCE(path)
        if exists or corrupt:
            return exists, corrupt
        if path in observed_paths:
            return True, False
        observed_paths.add(path)
        return False, False

    with patch.object(install_jobs, "_store_presence", side_effect=_presence):
        yield


class Clock:
    """Deterministic canonical UTC clock."""

    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> str:
        return self.value.isoformat(timespec="seconds")

    def advance(self, delta: timedelta = timedelta(seconds=1)) -> None:
        self.value += delta


def target(
    address: str = "panel.local",
    serial: str = "SERIAL-1",
    pinned_address: str = "192.168.1.23",
) -> InstallTarget:
    return InstallTarget(
        address=address,
        pinned_address=pinned_address,
        adb_serial=serial,
        model="Test Panel",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )


def artifact(*, apk_size: int = 12_345) -> InstallArtifact:
    return InstallArtifact(
        descriptor_schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v0.1.0",
        version_name="0.1.0",
        version_code=100,
        apk_name="ha-paneld-v0.1.0-manual-setup-required.apk",
        apk_sha256=SHA,
        apk_size=apk_size,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=(
            "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
        ),
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:1:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )


async def create(
    manager: InstallJobManager,
    *,
    install_target: InstallTarget | None = None,
    install_artifact: InstallArtifact | None = None,
):
    selected_target = install_target or target()
    selected_artifact = install_artifact or artifact()
    return await manager.async_create_or_join(
        selected_target,
        selected_artifact,
        install_plan_sha256(selected_target, selected_artifact, CREDENTIAL_ID),
        CREDENTIAL_ID,
    )


async def transition_to_staging(
    manager: InstallJobManager, job_id: str, revision: int = 0
):
    receipt = await manager.async_claim(job_id, revision)
    revision = receipt.revision
    for phase in (
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
    ):
        receipt = await manager.async_transition(job_id, revision, phase)
        revision = receipt.revision
    receipt = await manager.async_transition(
        job_id,
        revision,
        InstallPhase.ARTIFACT_READY,
        actual_apk_bytes=artifact().apk_size,
    )
    receipt = await manager.async_transition(
        job_id, receipt.revision, InstallPhase.REVALIDATING
    )
    return await manager.async_transition(
        job_id, receipt.revision, InstallPhase.STAGING
    )


async def receipt_at_phase(
    manager: InstallJobManager, phase: InstallPhase
) -> InstallJobReceipt:
    """Build a valid claimed receipt at each durable phase for barrier tests."""
    receipt, _ = await create(manager)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    if phase is InstallPhase.APPROVED:
        return receipt
    if phase is InstallPhase.CANCELLED:
        receipt = await manager.async_request_cancel(receipt.job_id, receipt.revision)
        return await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.CANCELLED,
            result_code=InstallResultCode.CANCELLED_BY_USER,
        )
    receipt = await manager.async_transition(
        receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
    )
    if phase is InstallPhase.AUTHORIZING:
        return receipt
    if phase is InstallPhase.FAILED:
        return await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.FAILED,
            result_code=InstallResultCode.AUTHORIZATION_FAILED,
        )
    for next_phase in (
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
        InstallPhase.HEALTHY_UNCLAIMED,
        InstallPhase.CONSUMED,
    ):
        if next_phase is InstallPhase.ARTIFACT_READY:
            receipt = await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                next_phase,
                actual_apk_bytes=artifact().apk_size,
            )
        elif next_phase is InstallPhase.HEALTHY_UNCLAIMED:
            receipt = await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                next_phase,
                health_checked_at=Clock()(),
            )
        elif next_phase is InstallPhase.CONSUMED:
            receipt = await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                next_phase,
                result_code=InstallResultCode.ENTRY_CREATED,
                consumed_entry_id=CURRENT_ENTRY_ID,
            )
        else:
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, next_phase
            )
        if phase is next_phase:
            return receipt
        if (
            phase is InstallPhase.RECOVERY_REQUIRED
            and next_phase is InstallPhase.STAGING
        ):
            return await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                InstallPhase.RECOVERY_REQUIRED,
                result_code=InstallResultCode.AMBIGUOUS_MUTATION,
            )
    raise AssertionError(f"unsupported test phase: {phase}")


async def overwrite_stored_cancel_requested(
    hass: HomeAssistant, job_id: str, cancel_requested: bool
) -> None:
    """Change only the persisted cancellation bit for fail-closed tests."""
    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await store.async_load()
    assert document is not None
    stored_receipt = next(item for item in document["jobs"] if item["job_id"] == job_id)
    stored_receipt["cancel_requested"] = cancel_requested
    await store.async_save(document)


async def test_manager_constructs_private_atomic_store(hass: HomeAssistant) -> None:
    """Receipts use an integration-private atomic HA Store outside HACS files."""
    with patch.object(install_jobs, "Store") as store_class:
        manager = InstallJobManager(hass)

    assert manager is not None
    store_class.assert_called_once_with(
        hass,
        1,
        f"{DOMAIN}.install_jobs",
        private=True,
        atomic_writes=True,
    )


async def test_create_is_durable_private_and_has_no_unsafe_fields(
    hass: HomeAssistant,
) -> None:
    """A receipt is read-back verified and stores no URL, secret, or raw error."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    receipt, created = await create(manager)

    assert created is True
    assert len(receipt.job_id) == 32
    assert receipt.phase == InstallPhase.APPROVED
    assert receipt.revision == 0
    assert receipt.adb_credential_id == CREDENTIAL_ID
    assert receipt.target.address == "panel.local"
    assert receipt.artifact.apk_size == 12_345

    verifier: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    persisted = await verifier.async_load()
    assert persisted is not None
    raw = json.dumps(persisted)
    assert "http://" not in raw
    assert "https://" not in raw
    assert "error" not in raw
    assert "secret" not in raw
    assert json.loads(raw)["jobs"][0]["adb_credential_id"] == CREDENTIAL_ID


async def test_create_rejects_a_plan_digest_not_bound_to_frozen_facts(
    hass: HomeAssistant,
) -> None:
    """A caller cannot persist an arbitrary well-shaped plan digest."""
    manager = InstallJobManager(hass, now=Clock())

    with pytest.raises(InstallJobTransitionError):
        await manager.async_create_or_join(
            target(), artifact(), "c" * 64, CREDENTIAL_ID
        )

    assert await manager.async_list() == ()


@pytest.mark.parametrize(
    "invalid_target",
    [
        target(address="192.168.1.24", pinned_address="192.168.1.23"),
        target(address="203.0.113.23", pinned_address="192.168.1.23"),
        target(address="bad_host.local", pinned_address="192.168.1.23"),
        target(address="192.168.1", pinned_address="192.168.1.23"),
    ],
)
def test_job_authority_rejects_targets_the_network_plan_cannot_produce(
    invalid_target: InstallTarget,
) -> None:
    """Direct callers cannot bypass original-to-pinned identity validation."""
    with pytest.raises(InstallJobStoreError):
        install_plan_sha256(invalid_target, artifact(), CREDENTIAL_ID)


@pytest.mark.parametrize(
    "invalid_artifact",
    [
        InstallArtifact(
            **{
                **asdict(artifact()),
                "release_tag": "v" + "1" * 62 + ".2.3",
                "version_name": "1" * 62 + ".2.3",
                "apk_name": "ha-paneld-v" + "1" * 62 + ".2.3-manual-setup-required.apk",
            }
        ),
        InstallArtifact(
            **{
                **asdict(artifact()),
                "database_compatibility": ("hapaneld-db:v1:ha-paneld.db:1:9999999999"),
            }
        ),
    ],
)
def test_job_authority_matches_authenticated_descriptor_bounds(
    invalid_artifact: InstallArtifact,
) -> None:
    """Receipt validation accepts no descriptor the release parser rejects."""
    with pytest.raises(InstallJobStoreError):
        install_plan_sha256(target(), invalid_artifact, CREDENTIAL_ID)


async def test_restart_reloads_same_immutable_receipt(hass: HomeAssistant) -> None:
    """Pre-entry work survives a manager restart without a placeholder entry."""
    clock = Clock()
    receipt, _ = await create(InstallJobManager(hass, now=clock))
    restarted = InstallJobManager(hass, now=clock)

    loaded = await restarted.async_get(receipt.job_id)
    assert loaded == receipt
    assert not hasattr(loaded, "entry")
    with pytest.raises(FrozenInstanceError):
        loaded.phase = InstallPhase.CONSUMED  # type: ignore[misc]


async def test_restart_reclaims_pre_mutation_but_quarantines_ambiguous_phase(
    hass: HomeAssistant,
) -> None:
    """A restart resumes safe preparation but never replays possible mutation."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    safe, _ = await create(manager)
    safe = await manager.async_claim(safe.job_id, safe.revision)
    safe = await manager.async_transition(
        safe.job_id, safe.revision, InstallPhase.AUTHORIZING
    )

    restarted = InstallJobManager(hass, now=clock)
    reclaimed = await restarted.async_claim(safe.job_id, safe.revision)
    assert reclaimed.phase == InstallPhase.AUTHORIZING
    assert reclaimed.executor_generation == 2
    assert reclaimed.attempt == 2

    risky, _ = await create(
        restarted,
        install_target=target("risky.local", "RISKY-SERIAL", "192.168.1.24"),
    )
    risky = await transition_to_staging(restarted, risky.job_id)
    after_crash = InstallJobManager(hass, now=clock)
    quarantined = await after_crash.async_claim(risky.job_id, risky.revision)
    assert quarantined.phase == InstallPhase.RECOVERY_REQUIRED
    assert quarantined.result_code == InstallResultCode.VERIFICATION_REQUIRED
    assert quarantined.is_terminal


@pytest.mark.parametrize(
    "phase", [InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING]
)
async def test_restart_quarantines_every_ambiguous_mutation_phase(
    hass: HomeAssistant, phase: InstallPhase
) -> None:
    """A new process never replays a phase whose side effect may have started."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    if phase is InstallPhase.INSTALLING:
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.INSTALLING
        )
    elif phase is InstallPhase.LAUNCHING:
        for next_phase in (
            InstallPhase.INSTALLING,
            InstallPhase.INSTALLED,
            InstallPhase.LAUNCHING,
        ):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, next_phase
            )

    quarantined = await InstallJobManager(hass, now=Clock()).async_claim(
        receipt.job_id, receipt.revision
    )

    assert quarantined.phase is InstallPhase.RECOVERY_REQUIRED
    assert quarantined.result_code is InstallResultCode.VERIFICATION_REQUIRED


@pytest.mark.parametrize("phase", [InstallPhase.INSTALLED, InstallPhase.HEALTH_CHECK])
async def test_restart_reclaims_safe_post_mutation_phase(
    hass: HomeAssistant, phase: InstallPhase
) -> None:
    """Known install success and read-only health polling remain resumable."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    for next_phase in (InstallPhase.INSTALLING, InstallPhase.INSTALLED):
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, next_phase
        )
    if phase is InstallPhase.HEALTH_CHECK:
        for next_phase in (InstallPhase.LAUNCHING, InstallPhase.HEALTH_CHECK):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, next_phase
            )

    reclaimed = await InstallJobManager(hass, now=Clock()).async_claim(
        receipt.job_id, receipt.revision
    )

    assert reclaimed.phase is phase
    assert reclaimed.executor_generation == receipt.executor_generation + 1


@pytest.mark.parametrize("at_mutation_barrier", [False, True])
async def test_restart_at_attempt_ceiling_becomes_recovery_required(
    hass: HomeAssistant, at_mutation_barrier: bool
) -> None:
    """No safe or ambiguous job can remain active forever after 32 claims."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    receipt, _ = await create(manager)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    receipt = await manager.async_transition(
        receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
    )
    for _ in range(31):
        manager = InstallJobManager(hass, now=clock)
        receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    assert receipt.attempt == receipt.executor_generation == 32

    if at_mutation_barrier:
        for phase in (InstallPhase.PREFLIGHT, InstallPhase.DOWNLOADING):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, phase
            )
        receipt = await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.ARTIFACT_READY,
            actual_apk_bytes=artifact().apk_size,
        )
        for phase in (InstallPhase.REVALIDATING, InstallPhase.STAGING):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, phase
            )

    restarted = InstallJobManager(hass, now=clock)
    exhausted = await restarted.async_claim(receipt.job_id, receipt.revision)
    assert exhausted.phase == InstallPhase.RECOVERY_REQUIRED
    assert exhausted.result_code == InstallResultCode.VERIFICATION_REQUIRED
    assert exhausted.attempt == exhausted.executor_generation == 32
    assert exhausted.revision == receipt.revision + 1


async def test_restart_quarantine_prunes_full_terminal_history_before_save(
    hass: HomeAssistant,
) -> None:
    """The crash path cannot persist an unreadable thirty-third terminal job."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    for number in range(32):
        receipt, _ = await create(
            manager,
            install_target=target(
                f"old-{number}.local",
                f"OLD-SERIAL-{number}",
                f"192.168.4.{number + 20}",
            ),
        )
        receipt = await manager.async_claim(receipt.job_id, receipt.revision)
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
        )
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.FAILED,
            result_code=InstallResultCode.AUTHORIZATION_FAILED,
        )
        clock.advance()
    active, _ = await create(
        manager,
        install_target=target("active.local", "ACTIVE-SERIAL", "192.168.4.100"),
    )
    active = await transition_to_staging(manager, active.job_id)
    clock.advance()

    recovered = await InstallJobManager(hass, now=clock).async_claim(
        active.job_id, active.revision
    )
    receipts = await InstallJobManager(hass, now=clock).async_list()

    assert recovered.phase is InstallPhase.RECOVERY_REQUIRED
    assert len(receipts) == 32
    assert recovered.job_id in {receipt.job_id for receipt in receipts}


async def test_unclaimed_executor_cannot_progress_or_cross_barrier(
    hass: HomeAssistant,
) -> None:
    """Only the in-process owner of the durable generation may do work."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
        )

    staging = await transition_to_staging(manager, receipt.job_id)
    restarted = InstallJobManager(hass, now=Clock())
    with pytest.raises(InstallJobTransitionError):
        await restarted.async_verify_mutation_barrier(
            staging.job_id, staging.revision, InstallPhase.STAGING
        )


async def test_exact_duplicate_calls_join_one_job_under_concurrency(
    hass: HomeAssistant,
) -> None:
    """Overlapping exact requests single-flight to one durable receipt."""
    manager = InstallJobManager(hass, now=Clock())
    results = await asyncio.gather(*(create(manager) for _ in range(20)))

    assert sum(created for _, created in results) == 1
    assert len({receipt.job_id for receipt, _ in results}) == 1
    assert len(await manager.async_list()) == 1


@pytest.mark.parametrize(
    ("other_target", "other_artifact", "credential_id"),
    [
        (target(serial="DIFFERENT"), artifact(), CREDENTIAL_ID),
        (target(address="other.local"), artifact(), CREDENTIAL_ID),
        (
            target("alias.local", "ALIAS-SERIAL", "192.168.1.23"),
            artifact(),
            CREDENTIAL_ID,
        ),
        (target(), artifact(apk_size=12_346), CREDENTIAL_ID),
        (target(), artifact(), "c" * 64),
    ],
)
async def test_colliding_active_frozen_facts_conflict(
    hass: HomeAssistant,
    other_target: InstallTarget,
    other_artifact: InstallArtifact,
    credential_id: str,
) -> None:
    """Address or serial collisions join only when every frozen fact matches."""
    manager = InstallJobManager(hass, now=Clock())
    await create(manager)

    with pytest.raises(InstallJobConflictError):
        await manager.async_create_or_join(
            other_target,
            other_artifact,
            install_plan_sha256(other_target, other_artifact, credential_id),
            credential_id,
        )


async def test_distinct_targets_are_bounded(hass: HomeAssistant) -> None:
    """At most four active panel mutations can be represented."""
    manager = InstallJobManager(hass, now=Clock())
    for number in range(4):
        await create(
            manager,
            install_target=target(
                f"panel-{number}.local",
                f"SERIAL-{number}",
                f"192.168.1.{number + 20}",
            ),
        )

    with pytest.raises(InstallJobCapacityError):
        await create(
            manager,
            install_target=target("panel-4.local", "SERIAL-4", "192.168.1.24"),
        )


async def test_find_active_requires_exact_original_and_pinned_identity(
    hass: HomeAssistant,
) -> None:
    """A reopened flow attaches only to its exact frozen network identity."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)

    assert await manager.async_find_active("panel.local", "192.168.1.23") == receipt
    assert await manager.async_find_active("other.local", "192.168.1.24") is None
    with pytest.raises(InstallJobConflictError):
        await manager.async_find_active("alias.local", "192.168.1.23")
    with pytest.raises(InstallJobConflictError):
        await manager.async_find_active("panel.local", "192.168.1.24")
    with pytest.raises(InstallJobStoreError):
        await manager.async_find_active("Panel.local", "192.168.1.23")


async def test_transitions_are_cas_serialized(hass: HomeAssistant) -> None:
    """Only one of two callers can consume the same expected revision."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)

    results = await asyncio.gather(
        manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
        ),
        manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InstallJobRevisionError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert (await manager.async_get(receipt.job_id)).revision == receipt.revision + 1


async def test_stale_manager_cannot_overwrite_newer_durable_revision(
    hass: HomeAssistant,
) -> None:
    """Every mutation re-reads storage under the process-wide lock."""
    clock = Clock()
    first = InstallJobManager(hass, now=clock)
    receipt, _ = await create(first)
    stale = InstallJobManager(hass, now=clock)
    assert await stale.async_get(receipt.job_id) == receipt

    claimed = await first.async_claim(receipt.job_id, receipt.revision)
    with pytest.raises(InstallJobRevisionError):
        await stale.async_request_cancel(receipt.job_id, receipt.revision)

    assert await stale.async_get(receipt.job_id) == claimed


async def test_failed_readback_invalidates_cache_and_executor_claim(
    hass: HomeAssistant,
) -> None:
    """An ambiguous Store write cannot leave reusable in-memory authority."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    claimed = await manager.async_claim(receipt.job_id, receipt.revision)
    manager._store.async_save = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(InstallJobStoreError):
        await manager.async_transition(
            claimed.job_id, claimed.revision, InstallPhase.AUTHORIZING
        )

    assert manager._jobs is None
    assert manager._claimed_jobs == {}
    persisted = await manager.async_get(claimed.job_id)
    assert persisted == claimed
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            persisted.job_id, persisted.revision, InstallPhase.AUTHORIZING
        )


async def test_full_success_path_requires_health_before_entry_consumption(
    hass: HomeAssistant,
) -> None:
    """A config-entry ID appears only after persisted healthy-unclaimed state."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    for phase in (
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    ):
        clock.advance()
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, phase
        )
    clock.advance()
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=clock(),
    )
    assert receipt.consumed_entry_id is None
    assert receipt.result_code is None

    clock.advance()
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.CONSUMED,
            health_checked_at=clock(),
            result_code=InstallResultCode.ENTRY_CREATED,
            consumed_entry_id=CURRENT_ENTRY_ID,
        )
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.CONSUMED,
        result_code=InstallResultCode.ENTRY_CREATED,
        consumed_entry_id=CURRENT_ENTRY_ID,
    )
    assert receipt.is_terminal
    assert receipt.consumed_entry_id == CURRENT_ENTRY_ID


async def test_healthy_receipt_can_be_quarantined_after_late_health_drift(
    hass: HomeAssistant,
) -> None:
    """A stale healthy observation cannot force config-entry creation."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    for phase in (
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    ):
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, phase
        )
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=Clock()(),
    )

    quarantined = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.RECOVERY_REQUIRED,
        result_code=InstallResultCode.VERIFICATION_REQUIRED,
    )

    assert quarantined.is_terminal
    assert quarantined.result_code is InstallResultCode.VERIFICATION_REQUIRED


@pytest.mark.parametrize("entry_id", [CURRENT_ENTRY_ID, LEGACY_ENTRY_ID])
async def test_consumption_accepts_current_and_legacy_home_assistant_entry_ids(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Receipt identity follows Home Assistant across its entry-ID migration."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    for phase in (
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    ):
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, phase
        )
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=Clock()(),
    )

    consumed = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.CONSUMED,
        result_code=InstallResultCode.ENTRY_CREATED,
        consumed_entry_id=entry_id,
    )

    assert consumed.consumed_entry_id == entry_id


@pytest.mark.parametrize(
    "entry_id",
    [
        "01m1j723mdq69qdqvcrxkyzbjv",
        "8" * 26,
        "not-an-entry-id",
        "g" * 32,
    ],
)
async def test_consumption_rejects_noncanonical_entry_ids(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Only canonical HA identities may close a healthy receipt."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    for phase in (
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    ):
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, phase
        )
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=Clock()(),
    )

    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.CONSUMED,
            result_code=InstallResultCode.ENTRY_CREATED,
            consumed_entry_id=entry_id,
        )


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        (InstallPhase.APPROVED, InstallPhase.DOWNLOADING),
        (InstallPhase.APPROVED, InstallPhase.CONSUMED),
        (InstallPhase.HEALTHY_UNCLAIMED, InstallPhase.FAILED),
    ],
)
async def test_illegal_phase_skips_are_rejected(
    hass: HomeAssistant, source: InstallPhase, destination: InstallPhase
) -> None:
    """The explicit transition graph cannot be skipped."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    if source == InstallPhase.APPROVED:
        receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    if source == InstallPhase.HEALTHY_UNCLAIMED:
        receipt = await transition_to_staging(manager, receipt.job_id)
        for phase in (
            InstallPhase.INSTALLING,
            InstallPhase.INSTALLED,
            InstallPhase.LAUNCHING,
            InstallPhase.HEALTH_CHECK,
        ):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, phase
            )
        receipt = await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.HEALTHY_UNCLAIMED,
            health_checked_at=Clock()(),
        )
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            destination,
            result_code=InstallResultCode.HEALTH_CHECK_FAILED,
        )


async def test_terminal_outcome_requires_claim_and_cancellation_request(
    hass: HomeAssistant,
) -> None:
    """Only the executor may record outcomes, and cancellation is two-phase."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.FAILED,
            result_code=InstallResultCode.AUTHORIZATION_FAILED,
        )
    claimed = await manager.async_claim(receipt.job_id, receipt.revision)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            claimed.job_id,
            claimed.revision,
            InstallPhase.CANCELLED,
            result_code=InstallResultCode.CANCELLED_BY_USER,
        )


async def test_transition_metadata_and_failure_codes_are_phase_specific(
    hass: HomeAssistant,
) -> None:
    """Receipts cannot claim evidence or outcomes from a phase not reached."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.AUTHORIZING,
            actual_apk_bytes=artifact().apk_size,
        )
    receipt = await manager.async_transition(
        receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
    )
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.FAILED,
            result_code=InstallResultCode.HEALTH_CHECK_FAILED,
        )


async def test_cancel_is_requested_then_acknowledged_at_safe_phase(
    hass: HomeAssistant,
) -> None:
    """Requesting cancellation is distinct from a durable cancelled outcome."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    requested = await manager.async_request_cancel(receipt.job_id, receipt.revision)
    assert requested.cancel_requested
    assert requested.phase == InstallPhase.APPROVED
    requested = await manager.async_claim(requested.job_id, requested.revision)
    cancelled = await manager.async_transition(
        requested.job_id,
        requested.revision,
        InstallPhase.CANCELLED,
        result_code=InstallResultCode.CANCELLED_BY_USER,
    )
    assert cancelled.is_terminal
    with pytest.raises(InstallJobTransitionError):
        await manager.async_request_cancel(cancelled.job_id, cancelled.revision)


async def test_staging_can_cancel_but_installing_requires_terminal_recovery(
    hass: HomeAssistant,
) -> None:
    """Post-install-start cancellation cannot misrepresent an ambiguous panel."""
    manager = InstallJobManager(hass, now=Clock())
    first, _ = await create(manager)
    staging = await transition_to_staging(manager, first.job_id)
    staging = await manager.async_request_cancel(staging.job_id, staging.revision)
    cancelled = await manager.async_transition(
        staging.job_id,
        staging.revision,
        InstallPhase.CANCELLED,
        result_code=InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP,
    )
    assert cancelled.phase == InstallPhase.CANCELLED

    second, _ = await create(
        manager,
        install_target=target("second.local", "SERIAL-2", "192.168.1.24"),
    )
    staging = await transition_to_staging(manager, second.job_id)
    installing = await manager.async_transition(
        staging.job_id, staging.revision, InstallPhase.INSTALLING
    )
    with pytest.raises(InstallJobTransitionError):
        await manager.async_transition(
            installing.job_id,
            installing.revision,
            InstallPhase.CANCELLED,
            result_code=InstallResultCode.CANCELLED_BY_USER,
        )
    recovery = await manager.async_transition(
        installing.job_id,
        installing.revision,
        InstallPhase.RECOVERY_REQUIRED,
        result_code=InstallResultCode.AMBIGUOUS_MUTATION,
    )
    assert recovery.is_terminal


async def test_mutation_barrier_uses_fresh_store_and_rejects_cancel(
    hass: HomeAssistant,
) -> None:
    """External mutation requires a fresh exact durable phase/revision read."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    staging = await transition_to_staging(manager, receipt.job_id)
    assert (
        await manager.async_verify_mutation_barrier(
            staging.job_id, staging.revision, InstallPhase.STAGING
        )
        == staging
    )

    raw_store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await raw_store.async_load()
    assert document is not None
    document["jobs"][0]["cancel_requested"] = True
    await raw_store.async_save(document)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_verify_mutation_barrier(
            staging.job_id, staging.revision, InstallPhase.STAGING
        )


@pytest.mark.parametrize("phase", list(InstallPhase))
@pytest.mark.parametrize("cancel_requested", [False, True])
async def test_cleanup_barrier_allows_only_exact_phase_and_cancel_polarity(
    hass: HomeAssistant,
    phase: InstallPhase,
    cancel_requested: bool,
) -> None:
    """Every durable phase and cancellation polarity has an explicit outcome."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, phase)
    cancellable_phases = {
        InstallPhase.APPROVED,
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
    }
    if cancel_requested != receipt.cancel_requested:
        if cancel_requested and phase in cancellable_phases:
            receipt = await manager.async_request_cancel(
                receipt.job_id, receipt.revision
            )
        else:
            await overwrite_stored_cancel_requested(
                hass, receipt.job_id, cancel_requested
            )

    allowed = (phase is InstallPhase.STAGING and cancel_requested) or (
        phase in {InstallPhase.INSTALLING, InstallPhase.LAUNCHING}
        and not cancel_requested
    )
    if not allowed:
        with pytest.raises(InstallJobTransitionError):
            await manager.async_verify_cleanup_barrier(
                receipt.job_id, receipt.revision, phase
            )
        return

    claims_before = manager._claimed_jobs.copy()
    verified = await manager.async_verify_cleanup_barrier(
        receipt.job_id, receipt.revision, phase
    )

    assert verified == receipt
    assert await manager.async_get(receipt.job_id) == receipt
    assert manager._claimed_jobs == claims_before


@pytest.mark.parametrize(
    ("receipt_phase", "requested_phase"),
    [
        (receipt_phase, requested_phase)
        for receipt_phase in CLEANUP_BARRIER_PHASES
        for requested_phase in CLEANUP_BARRIER_PHASES
        if receipt_phase is not requested_phase
    ],
)
async def test_cleanup_barrier_rejects_every_other_allowed_phase(
    hass: HomeAssistant,
    receipt_phase: InstallPhase,
    requested_phase: InstallPhase,
) -> None:
    """Cleanup authority is bound to one phase, even at equal polarity."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, receipt_phase)
    if receipt_phase is InstallPhase.STAGING:
        receipt = await manager.async_request_cancel(receipt.job_id, receipt.revision)

    with pytest.raises(InstallJobTransitionError):
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, requested_phase
        )


async def test_cleanup_barrier_rejects_stale_revision_and_generation(
    hass: HomeAssistant,
) -> None:
    """Cleanup authority is bound to the exact revision and claimed generation."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, InstallPhase.INSTALLING)

    with pytest.raises(InstallJobRevisionError):
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision - 1, receipt.phase
        )

    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await store.async_load()
    assert document is not None
    stored_receipt = document["jobs"][0]
    stored_receipt["executor_generation"] += 1
    stored_receipt["attempt"] += 1
    await store.async_save(document)

    with pytest.raises(InstallJobTransitionError):
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )
    assert manager._claimed_jobs == {}


async def test_cleanup_barrier_rejects_fresh_manager_without_claim(
    hass: HomeAssistant,
) -> None:
    """A newly constructed manager cannot inherit remote-cleanup authority."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, InstallPhase.INSTALLING)
    restarted = InstallJobManager(hass, now=Clock())

    with pytest.raises(InstallJobTransitionError):
        await restarted.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )


async def test_cleanup_barrier_rejects_store_drift_between_fresh_reads(
    hass: HomeAssistant,
) -> None:
    """Two individually valid but unequal durable snapshots fail closed."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, InstallPhase.INSTALLING)
    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await store.async_load()
    assert document is not None
    drifted = copy.deepcopy(document)
    drifted["jobs"][0]["updated_at"] = "2026-09-02T12:00:01+00:00"
    first_reader = MagicMock()
    first_reader.async_load = AsyncMock(return_value=document)
    independent_reader = MagicMock()
    independent_reader.async_load = AsyncMock(return_value=drifted)

    with (
        patch.object(
            install_jobs, "Store", side_effect=[first_reader, independent_reader]
        ),
        pytest.raises(InstallJobTransitionError),
    ):
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )


async def test_cleanup_barrier_rejects_corrupt_independent_store_read(
    hass: HomeAssistant,
) -> None:
    """Independent Store corruption cannot authorize remote cleanup."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, InstallPhase.INSTALLING)
    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await store.async_load()
    assert document is not None
    first_reader = MagicMock()
    first_reader.async_load = AsyncMock(return_value=document)
    corrupt_reader = MagicMock()
    corrupt_reader.async_load = AsyncMock(
        return_value={"format": "unexpected", "jobs": []}
    )

    with (
        patch.object(install_jobs, "Store", side_effect=[first_reader, corrupt_reader]),
        pytest.raises(InstallJobStoreError),
    ):
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )


async def test_cancelled_staging_uses_cleanup_not_ordinary_mutation_barrier(
    hass: HomeAssistant,
) -> None:
    """Cancellation cleanup authority never weakens ordinary mutation gating."""
    manager = InstallJobManager(hass, now=Clock())
    receipt = await receipt_at_phase(manager, InstallPhase.STAGING)
    receipt = await manager.async_request_cancel(receipt.job_id, receipt.revision)

    assert (
        await manager.async_verify_cleanup_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )
        == receipt
    )
    with pytest.raises(InstallJobTransitionError):
        await manager.async_verify_mutation_barrier(
            receipt.job_id, receipt.revision, receipt.phase
        )


async def test_barrier_rejects_wrong_phase_and_revision(hass: HomeAssistant) -> None:
    """A barrier cannot be reused for another operation or stale revision."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    staging = await transition_to_staging(manager, receipt.job_id)
    with pytest.raises(InstallJobTransitionError):
        await manager.async_verify_mutation_barrier(
            staging.job_id, staging.revision, InstallPhase.PREFLIGHT
        )
    with pytest.raises(InstallJobRevisionError):
        await manager.async_verify_mutation_barrier(
            staging.job_id, staging.revision - 1, InstallPhase.STAGING
        )


@pytest.mark.parametrize(
    "barrier", [InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING]
)
async def test_every_mutation_phase_has_a_positive_durable_barrier(
    hass: HomeAssistant, barrier: InstallPhase
) -> None:
    """Each external side-effect boundary can be verified at its exact receipt."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    receipt = await transition_to_staging(manager, receipt.job_id)
    if barrier is InstallPhase.INSTALLING:
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.INSTALLING
        )
    elif barrier is InstallPhase.LAUNCHING:
        for phase in (
            InstallPhase.INSTALLING,
            InstallPhase.INSTALLED,
            InstallPhase.LAUNCHING,
        ):
            receipt = await manager.async_transition(
                receipt.job_id, receipt.revision, phase
            )

    verified = await manager.async_verify_mutation_barrier(
        receipt.job_id, receipt.revision, barrier
    )

    assert verified == receipt


async def test_swallowed_store_write_failure_fails_closed(hass: HomeAssistant) -> None:
    """A save is never accepted without a byte-equivalent fresh Store load."""
    writer = MagicMock()
    writer.path = "/not/read/by/this/test"
    writer.async_load = AsyncMock(return_value=None)
    writer.async_save = AsyncMock(return_value=None)
    verifier = MagicMock()
    verifier.async_load = AsyncMock(return_value=None)

    with (
        patch.object(install_jobs, "Store", side_effect=[writer, verifier]),
        patch.object(install_jobs, "_store_presence", return_value=(False, False)),
        pytest.raises(InstallJobStoreError),
    ):
        await create(InstallJobManager(hass, now=Clock()))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(extra=True),
        lambda document: document.update(format="future"),
        lambda document: document["jobs"][0].update(job_id="not-opaque"),
        lambda document: document["jobs"][0].update(result_code="raw traceback"),
        lambda document: document["jobs"][0]["artifact"].update(
            download_url="https://example.invalid/secret"
        ),
        lambda document: document["jobs"][0]["artifact"].update(
            apk_size=64 * 1024 * 1024 + 1
        ),
        lambda document: document["jobs"][0]["target"].update(address="HTTP://x"),
        lambda document: document["jobs"][0]["target"].update(
            pinned_address="panel.local"
        ),
        lambda document: document["jobs"][0]["target"].update(pinned_address="8.8.8.8"),
        lambda document: document["jobs"][0]["target"].update(
            pinned_address="192.168.1.23:9999"
        ),
        lambda document: document["jobs"][0]["target"].update(
            pinned_address="[fd00:0:0:0:0:0:0:1]"
        ),
        lambda document: document["jobs"][0].update(attempt=1),
        lambda document: document["jobs"][0].update(actual_apk_bytes=12_345),
        lambda document: document["jobs"][0].update(plan_sha256="c" * 64),
    ],
)
async def test_corrupt_or_excessive_stored_documents_fail_closed(
    hass: HomeAssistant, mutation
) -> None:
    """The versioned schema is closed and every retained value is bounded."""
    manager = InstallJobManager(hass, now=Clock())
    await create(manager)
    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.install_jobs", private=True, atomic_writes=True
    )
    document = await store.async_load()
    assert document is not None
    mutation(document)
    await store.async_save(document)

    with pytest.raises(InstallJobStoreError):
        await InstallJobManager(hass, now=Clock()).async_list()


@pytest.mark.parametrize(
    "incompatible_target",
    [
        InstallTarget(
            address="panel.local",
            pinned_address="192.168.1.23",
            adb_serial="SERIAL-1",
            model="Test Panel",
            primary_abi="x86",
            android_sdk=34,
        ),
        InstallTarget(
            address="panel.local",
            pinned_address="192.168.1.23",
            adb_serial="SERIAL-1",
            model="Test Panel",
            primary_abi="arm64-v8a",
            android_sdk=25,
        ),
    ],
)
def test_reload_rejects_incompatible_frozen_target_artifact_pair(
    incompatible_target: InstallTarget,
) -> None:
    """Storage parsing repeats cross-object API and ABI admission."""
    receipt = install_jobs.InstallJobReceipt(
        job_id="1" * 32,
        revision=0,
        executor_generation=0,
        created_at=Clock()(),
        updated_at=Clock()(),
        phase=InstallPhase.APPROVED,
        cancel_requested=False,
        attempt=0,
        target=incompatible_target,
        artifact=artifact(),
        plan_sha256=install_plan_sha256(incompatible_target, artifact(), CREDENTIAL_ID),
        adb_credential_id=CREDENTIAL_ID,
    )

    with pytest.raises(InstallJobStoreError):
        install_jobs._parse_document(
            {
                "format": "ha-paneld-install-jobs-v1",
                "jobs": [install_jobs._serialize_receipt(receipt)],
            }
        )


@pytest.mark.parametrize("collision", ["address", "pin", "serial"])
async def test_reload_rejects_duplicate_active_target_identity(
    hass: HomeAssistant, collision: str
) -> None:
    """A forged store cannot authorize two workers for one logical panel."""
    manager = InstallJobManager(hass, now=Clock())
    first, _ = await create(manager)
    second_target = target("other.local", "SERIAL-2", "192.168.1.24")
    replacements = {
        "address": first.target.address,
        "pinned_address": first.target.pinned_address,
        "adb_serial": first.target.adb_serial,
    }
    collision_field = {
        "address": "address",
        "pin": "pinned_address",
        "serial": "adb_serial",
    }[collision]
    second_target = InstallTarget(
        **{
            **asdict(second_target),
            collision_field: replacements[collision_field],
        }
    )
    second = replace(
        first,
        job_id="2" * 32,
        target=second_target,
        plan_sha256=install_plan_sha256(second_target, first.artifact, CREDENTIAL_ID),
    )
    document = {
        "format": "ha-paneld-install-jobs-v1",
        "jobs": [
            install_jobs._serialize_receipt(first),
            install_jobs._serialize_receipt(second),
        ],
    }

    with pytest.raises(InstallJobStoreError):
        install_jobs._parse_document(copy.deepcopy(document))


async def test_missing_job_and_bad_revision_are_distinct(hass: HomeAssistant) -> None:
    """Callers can distinguish missing opaque IDs from stale work."""
    manager = InstallJobManager(hass, now=Clock())
    receipt, _ = await create(manager)
    with pytest.raises(InstallJobNotFoundError):
        await manager.async_get("0" * 32)
    with pytest.raises(InstallJobNotFoundError):
        await manager.async_get("../install_jobs")
    with pytest.raises(InstallJobRevisionError):
        await manager.async_request_cancel(receipt.job_id, receipt.revision + 1)


async def test_terminal_history_is_pruned_by_count_and_age(
    hass: HomeAssistant,
) -> None:
    """Only 32 recent terminal receipts survive; seven-day expiry is enforced."""
    clock = Clock()
    manager = InstallJobManager(hass, now=clock)
    first_id = ""
    for number in range(33):
        receipt, _ = await create(
            manager,
            install_target=target(
                f"panel-{number}.local",
                f"SERIAL-{number}",
                f"192.168.2.{number + 20}",
            ),
        )
        first_id = first_id or receipt.job_id
        receipt = await manager.async_claim(receipt.job_id, receipt.revision)
        receipt = await manager.async_transition(
            receipt.job_id, receipt.revision, InstallPhase.AUTHORIZING
        )
        await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            InstallPhase.FAILED,
            result_code=InstallResultCode.AUTHORIZATION_FAILED,
        )
        clock.advance()
    jobs = await manager.async_list()
    assert len(jobs) == 32
    assert first_id not in {job.job_id for job in jobs}

    clock.advance(timedelta(days=8))
    current, _ = await create(
        manager,
        install_target=target("current.local", "CURRENT-SERIAL", "192.168.3.20"),
    )
    assert await manager.async_list() == (current,)
    assert manager._claimed_jobs == {}


async def test_invalid_descriptor_or_target_never_creates_receipt(
    hass: HomeAssistant,
) -> None:
    """Compatibility and credential bindings are validated before persistence."""
    manager = InstallJobManager(hass, now=Clock())
    invalid_artifact = artifact()
    invalid_artifact = InstallArtifact(
        **{**asdict(invalid_artifact), "apk_size": 64 * 1024 * 1024 + 1}
    )
    with pytest.raises(InstallJobStoreError):
        await create(manager, install_artifact=invalid_artifact)
    with pytest.raises(InstallJobTransitionError):
        incompatible_target = InstallTarget(**{**asdict(target()), "android_sdk": 25})
        await manager.async_create_or_join(
            incompatible_target,
            artifact(),
            install_plan_sha256(incompatible_target, artifact(), CREDENTIAL_ID),
            CREDENTIAL_ID,
        )
    assert await manager.async_list() == ()


async def test_process_wide_getter_reuses_and_guards_manager(
    hass: HomeAssistant,
) -> None:
    """Config flows and executors share one authority in hass.data."""
    first, second = await asyncio.gather(
        async_get_install_job_manager(hass), async_get_install_job_manager(hass)
    )
    assert first is second

    hass.data[f"{DOMAIN}.install_job_manager"] = object()
    with pytest.raises(InstallJobStoreError):
        await async_get_install_job_manager(hass)


def test_store_file_must_be_small_regular_and_owner_only(tmp_path: Path) -> None:
    """Receipt persistence refuses broad modes, links, and excessive files."""
    store_path = tmp_path / "install-jobs"
    store_path.write_text("{}", encoding="utf-8")
    store_path.chmod(0o600)
    assert _REAL_STORE_PRESENCE(str(store_path)) == (True, False)

    store_path.chmod(0o644)
    with pytest.raises(InstallJobStoreError):
        _REAL_STORE_PRESENCE(str(store_path))

    store_path.unlink()
    os.symlink(tmp_path / "missing", store_path)
    with pytest.raises(InstallJobStoreError):
        _REAL_STORE_PRESENCE(str(store_path))

    store_path.unlink()
    store_path.write_bytes(b"x" * (128 * 1024 + 1))
    store_path.chmod(0o600)
    with pytest.raises(InstallJobStoreError):
        _REAL_STORE_PRESENCE(str(store_path))
