"""Adversarial tests for the process-wide clean-install executor."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from homeassistant.core import HomeAssistant

from custom_components.ha_paneld import install_executor, install_jobs
from custom_components.ha_paneld.adb_credentials import (
    AdbCredential,
    AdbCredentialError,
)
from custom_components.ha_paneld.client import (
    CannotConnectError,
    PanelAddress,
    PanelHealth,
)
from custom_components.ha_paneld.install_adb import (
    AdbInstallTarget,
    AdbPreflight,
    AdbRootMode,
    DefiniteCleanupReason,
    InstallAdbError,
    InstallAdbErrorCode,
    InstallOutcome,
    LaunchOutcome,
    StagedApk,
)
from custom_components.ha_paneld.install_artifacts import (
    ArtifactCustodyError,
    ArtifactErrorCode,
)
from custom_components.ha_paneld.install_artifacts import (
    InstallArtifact as CustodiedArtifact,
)
from custom_components.ha_paneld.install_executor import (
    InstallExecutor,
    async_get_install_executor,
)
from custom_components.ha_paneld.install_jobs import (
    InstallArtifact,
    InstallJobManager,
    InstallJobReceipt,
    InstallJobTransitionError,
    InstallPhase,
    InstallResultCode,
    InstallTarget,
    install_plan_sha256,
)
from custom_components.ha_paneld.install_network import PinnedPanelTarget
from custom_components.ha_paneld.release import InstallDescriptor, ReleaseArtifact

APK_SHA256 = "a" * 64
CREDENTIAL_ID = "b" * 64
OTHER_CREDENTIAL_ID = "c" * 64
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


def target() -> InstallTarget:
    """Return one literal-address target accepted by the durable authority."""
    return InstallTarget(
        address="panel-one.local",
        pinned_address="192.168.250.23",
        adb_serial="SERIAL-1",
        model="Test Panel",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )


def artifact() -> InstallArtifact:
    """Return one fully authenticated frozen release contract."""
    return InstallArtifact(
        descriptor_schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v0.1.0",
        version_name="0.1.0",
        version_code=100,
        apk_name="ha-paneld-v0.1.0-manual-setup-required.apk",
        apk_sha256=APK_SHA256,
        apk_size=12_345,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=(
            "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
        ),
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:1:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )


def seed_crash_partial(path: Path) -> None:
    """Create one production-shaped private partial outside the event loop."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(b"interrupted download")
    path.chmod(0o600)


async def create_job(manager: InstallJobManager) -> InstallJobReceipt:
    """Create one durable approved receipt."""
    selected_target = target()
    selected_artifact = artifact()
    receipt, created = await manager.async_create_or_join(
        selected_target,
        selected_artifact,
        install_plan_sha256(selected_target, selected_artifact, CREDENTIAL_ID),
        CREDENTIAL_ID,
    )
    assert created
    return receipt


async def seed_phase(
    hass: HomeAssistant, phase: InstallPhase
) -> tuple[InstallJobReceipt, InstallJobManager]:
    """Persist a phase, then return a new manager which has no memory claim."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    if phase is InstallPhase.APPROVED:
        return receipt, InstallJobManager(hass)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    phases = (
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    )
    for next_phase in phases:
        transition_fields: dict[str, object] = {}
        if next_phase is InstallPhase.ARTIFACT_READY:
            transition_fields["actual_apk_bytes"] = artifact().apk_size
        if next_phase is InstallPhase.DOWNLOADING:
            transition_fields["preflight_root_mode"] = AdbRootMode.ROOTLESS.value
        receipt = await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            next_phase,
            **transition_fields,
        )
        if next_phase is phase:
            return receipt, InstallJobManager(hass)
    raise AssertionError


@dataclass
class Harness:
    """Deterministic stand-ins with a complete side-effect call log."""

    monkeypatch: pytest.MonkeyPatch
    expected_version: str = "0.1.0"

    def __post_init__(self) -> None:
        self.events: list[str] = []
        self.credentials: list[AdbCredential] = []
        self.pin_arguments: list[PinnedPanelTarget] = []
        self.preflight_arguments: list[
            tuple[AdbInstallTarget, PythonRSASigner, InstallDescriptor]
        ] = []
        self.download_arguments: list[tuple[ReleaseArtifact, str]] = []
        self.local_cleanup_job_ids: list[str] = []
        self.stage_arguments: list[
            tuple[
                AdbInstallTarget,
                PythonRSASigner,
                InstallDescriptor,
                str,
                Path,
                AdbRootMode,
            ]
        ] = []
        self.install_arguments: list[
            tuple[
                AdbInstallTarget,
                PythonRSASigner,
                InstallDescriptor,
                str,
                AdbRootMode,
            ]
        ] = []
        self.remote_cleanup_arguments: list[
            tuple[
                AdbInstallTarget,
                PythonRSASigner,
                StagedApk,
                DefiniteCleanupReason,
                AdbRootMode,
            ]
        ] = []
        self.launch_arguments: list[
            tuple[
                AdbInstallTarget,
                PythonRSASigner,
                InstallDescriptor,
                AdbRootMode,
            ]
        ] = []
        self.health_client_addresses: list[object] = []
        self.mutation_barrier_arguments: list[tuple[str, int, InstallPhase]] = []
        self.cleanup_barrier_arguments: list[tuple[str, int, InstallPhase]] = []
        self.credential_calls = 0
        self.credential_wrong_at: int | None = None
        self.credential_error_at: int | None = None
        self.pin_error_at: int | None = None
        self.pin_wrong_at: int | None = None
        self.pin_calls = 0
        self.preflight_calls = 0
        self.preflight_wrong_at: int | None = None
        self.preflight_wrong_field = "serial"
        self.preflight_root_modes: list[AdbRootMode] = []
        self.download_error: ArtifactCustodyError | None = None
        self.download_mutation: str | None = None
        self.local_cleanup_error: ArtifactCustodyError | None = None
        self.stage_error: Exception | None = None
        self.stage_mismatch = False
        self.install_error: Exception | None = None
        self.install_outcome = InstallOutcome.INSTALLED
        self.cleanup_error: Exception | None = None
        self.launch_error: Exception | None = None
        self.launch_outcome = LaunchOutcome.STARTED
        self.health_error: Exception | None = None
        self.health_calls = 0
        self.stage_entered: asyncio.Event | None = None
        self.stage_release: asyncio.Event | None = None
        self.stage_callback: Any = None
        self._install()

    def _install(self) -> None:
        self.monkeypatch.setattr(
            install_executor,
            "async_get_clientsession",
            lambda _hass: object(),
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_get_durable_adb_credential",
            self.async_credential,
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_revalidate_install_target",
            self.async_pin,
        )
        self.monkeypatch.setattr(
            install_executor, "async_preflight_install", self.async_preflight
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_download_install_artifact",
            self.async_download,
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_cleanup_install_artifact",
            self.async_cleanup_local,
        )
        self.monkeypatch.setattr(install_executor, "async_stage_apk", self.async_stage)
        self.monkeypatch.setattr(
            install_executor,
            "async_install_staged_apk",
            self.async_install,
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_cleanup_staged_apk",
            self.async_cleanup_remote,
        )
        self.monkeypatch.setattr(
            install_executor,
            "async_launch_installed_app",
            self.async_launch,
        )
        harness = self

        class FakeClient:
            """Return the harness's bounded health observation."""

            def __init__(self, _session: object, address: object) -> None:
                harness.events.append("health_client")
                harness.health_client_addresses.append(address)
                self.address = address

            async def async_get_health(self) -> PanelHealth:
                harness.events.append("health")
                harness.health_calls += 1
                if harness.health_error is not None:
                    raise harness.health_error
                return PanelHealth(
                    version=harness.expected_version,
                    panel_id="panel",
                    build="release",
                    config_hash="01234567",
                )

        self.monkeypatch.setattr(install_executor, "HaPaneldClient", FakeClient)

    async def async_credential(self, _hass: HomeAssistant) -> AdbCredential:
        self.events.append("credential")
        self.credential_calls += 1
        if self.credential_calls == self.credential_error_at:
            raise AdbCredentialError
        generation = (
            OTHER_CREDENTIAL_ID
            if self.credential_calls == self.credential_wrong_at
            else CREDENTIAL_ID
        )
        credential = AdbCredential(
            signer=cast(PythonRSASigner, object()),
            generation_id=generation,
        )
        self.credentials.append(credential)
        return credential

    async def async_pin(
        self, _hass: HomeAssistant, pinned: PinnedPanelTarget
    ) -> PinnedPanelTarget:
        self.events.append("pin")
        self.pin_arguments.append(pinned)
        self.pin_calls += 1
        if self.pin_calls == self.pin_error_at:
            raise install_executor.InstallNetworkError(
                install_executor.InstallNetworkErrorCode.PINNED_TARGET_REMOVED
            )
        if self.pin_calls == self.pin_wrong_at:
            return PinnedPanelTarget(
                original=pinned.original,
                pinned=PanelAddress(host="192.168.250.99", port=8888),
            )
        return pinned

    async def async_preflight(
        self,
        target: AdbInstallTarget,
        signer: PythonRSASigner,
        descriptor: InstallDescriptor,
    ) -> AdbPreflight:
        self.events.append("preflight")
        self.preflight_arguments.append((target, signer, descriptor))
        self.preflight_calls += 1
        root_mode = (
            self.preflight_root_modes.pop(0)
            if self.preflight_root_modes
            else AdbRootMode.ROOTLESS
        )
        wrong = self.preflight_calls == self.preflight_wrong_at
        return AdbPreflight(
            serial="OTHER"
            if wrong and self.preflight_wrong_field == "serial"
            else target.serial,
            model="Other"
            if wrong and self.preflight_wrong_field == "model"
            else target.model,
            primary_abi=(
                "armeabi-v7a"
                if wrong and self.preflight_wrong_field == "primary_abi"
                else target.primary_abi
            ),
            android_sdk=(
                target.android_sdk - 1
                if wrong and self.preflight_wrong_field == "android_sdk"
                else target.android_sdk
            ),
            root_mode=root_mode,
        )

    async def async_download(
        self,
        _hass: HomeAssistant,
        _session: object,
        release: ReleaseArtifact,
        job_id: str,
    ) -> CustodiedArtifact:
        self.events.append("download")
        self.download_arguments.append((release, job_id))
        if self.download_error is not None:
            raise self.download_error
        return CustodiedArtifact(
            job_id="d" * 32 if self.download_mutation == "job_id" else job_id,
            path=(
                cast(str, Path(f"/private/{job_id}.apk"))
                if self.download_mutation == "path_type"
                else (
                    ""
                    if self.download_mutation == "path_empty"
                    else f"/private/{job_id}.apk"
                )
            ),
            size=(
                artifact().apk_size + 1
                if self.download_mutation == "size"
                else artifact().apk_size
            ),
            sha256=(
                OTHER_CREDENTIAL_ID
                if self.download_mutation == "sha256"
                else APK_SHA256
            ),
        )

    async def async_cleanup_local(self, _hass: HomeAssistant, job_id: str) -> None:
        self.events.append("local_cleanup")
        self.local_cleanup_job_ids.append(job_id)
        if self.local_cleanup_error is not None:
            raise self.local_cleanup_error

    async def async_stage(
        self,
        target: AdbInstallTarget,
        signer: PythonRSASigner,
        descriptor: InstallDescriptor,
        job_id: str,
        path: Path,
        *,
        expected_root_mode: AdbRootMode,
    ) -> StagedApk:
        self.events.append("stage")
        self.stage_arguments.append(
            (target, signer, descriptor, job_id, path, expected_root_mode)
        )
        if self.stage_entered is not None:
            self.stage_entered.set()
        if self.stage_callback is not None:
            await self.stage_callback()
        if self.stage_release is not None:
            await self.stage_release.wait()
        if self.stage_error is not None:
            raise self.stage_error
        return StagedApk(
            job_id=job_id,
            remote_path=(
                "/data/local/tmp/ha-paneld-install-wrong.apk"
                if self.stage_mismatch
                else f"/data/local/tmp/ha-paneld-install-{job_id}.apk"
            ),
            apk_size=descriptor.apk_size,
            apk_sha256=descriptor.apk_sha256,
        )

    async def async_install(
        self,
        target: AdbInstallTarget,
        signer: PythonRSASigner,
        descriptor: InstallDescriptor,
        job_id: str,
        *,
        expected_root_mode: AdbRootMode,
    ) -> InstallOutcome:
        self.events.append("install")
        self.install_arguments.append(
            (target, signer, descriptor, job_id, expected_root_mode)
        )
        if self.install_error is not None:
            raise self.install_error
        return self.install_outcome

    async def async_cleanup_remote(
        self,
        target: AdbInstallTarget,
        signer: PythonRSASigner,
        staged: StagedApk,
        reason: DefiniteCleanupReason,
        *,
        expected_root_mode: AdbRootMode,
    ) -> None:
        self.events.append(f"remote_cleanup:{reason.value}")
        self.remote_cleanup_arguments.append(
            (target, signer, staged, reason, expected_root_mode)
        )
        if self.cleanup_error is not None:
            raise self.cleanup_error

    async def async_launch(
        self,
        target: AdbInstallTarget,
        signer: PythonRSASigner,
        descriptor: InstallDescriptor,
        *,
        expected_root_mode: AdbRootMode,
    ) -> LaunchOutcome:
        self.events.append("launch")
        self.launch_arguments.append((target, signer, descriptor, expected_root_mode))
        if self.launch_error is not None:
            raise self.launch_error
        return self.launch_outcome


async def run_job(
    hass: HomeAssistant,
    manager: InstallJobManager,
) -> InstallJobReceipt:
    """Run one job until the detached worker stops."""
    receipt = (await manager.async_list())[0]
    executor = InstallExecutor(hass, manager)
    return await executor.async_wait(receipt.job_id)


async def test_happy_path_stops_unclaimed_and_binds_every_operation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One worker performs the bounded phase path but never creates an entry."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    add_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_add", add_entry)
    verify_mutation = manager.async_verify_mutation_barrier
    verify_cleanup = manager.async_verify_cleanup_barrier

    async def mutation_barrier(
        job_id: str, revision: int, phase: InstallPhase
    ) -> InstallJobReceipt:
        harness.events.append(f"mutation_barrier:{phase.value}")
        harness.mutation_barrier_arguments.append((job_id, revision, phase))
        return await verify_mutation(job_id, revision, phase)

    async def cleanup_barrier(
        job_id: str, revision: int, phase: InstallPhase
    ) -> InstallJobReceipt:
        harness.events.append(f"cleanup_barrier:{phase.value}")
        harness.cleanup_barrier_arguments.append((job_id, revision, phase))
        return await verify_cleanup(job_id, revision, phase)

    monkeypatch.setattr(manager, "async_verify_mutation_barrier", mutation_barrier)
    monkeypatch.setattr(manager, "async_verify_cleanup_barrier", cleanup_barrier)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)
    execution_digest = sha256()
    execution_digest.update(b"ha-paneld-install-execution-v1\0")
    execution_digest.update(bytes.fromhex(receipt.job_id))
    execution_digest.update(bytes.fromhex(receipt.plan_sha256))
    expected_execution_id = execution_digest.hexdigest()[:32]
    expected_pinned = PinnedPanelTarget(
        original=PanelAddress(host="panel-one.local", port=8888),
        pinned=PanelAddress(host="192.168.250.23", port=8888),
    )
    expected_adb_target = AdbInstallTarget(
        address=PanelAddress(host="192.168.250.23", port=8888),
        serial="SERIAL-1",
        model="Test Panel",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )
    expected_descriptor = InstallDescriptor(
        schema=receipt.artifact.descriptor_schema,
        release_tag=receipt.artifact.release_tag,
        version_name=receipt.artifact.version_name,
        version_code=receipt.artifact.version_code,
        apk_name=receipt.artifact.apk_name,
        apk_size=receipt.artifact.apk_size,
        apk_sha256=receipt.artifact.apk_sha256,
        package_id=receipt.artifact.package_id,
        signer_certificate_sha256=receipt.artifact.signer_certificate_sha256,
        min_sdk=receipt.artifact.min_sdk,
        supported_abis=receipt.artifact.supported_abis,
        database_compatibility=receipt.artifact.database_compatibility,
        launch_component=receipt.artifact.launch_component,
    )
    expected_release = ReleaseArtifact(
        tag=receipt.artifact.release_tag,
        version=receipt.artifact.version_name,
        apk_name=receipt.artifact.apk_name,
        apk_url=(
            "https://github.com/maxlyth/ha-paneld/releases/download/"
            "v0.1.0/ha-paneld-v0.1.0-manual-setup-required.apk"
        ),
        sha256=APK_SHA256,
        descriptor=expected_descriptor,
    )
    expected_staged = StagedApk(
        job_id=expected_execution_id,
        remote_path=(f"/data/local/tmp/ha-paneld-install-{expected_execution_id}.apk"),
        apk_size=12_345,
        apk_sha256=APK_SHA256,
    )

    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED
    assert completed.health_checked_at is not None
    assert completed.result_code is None
    assert completed.consumed_entry_id is None
    assert harness.events.count("stage") == 1
    assert harness.events.count("install") == 1
    assert harness.events.count("launch") == 1
    assert harness.events.count("remote_cleanup:install_succeeded") == 1
    assert harness.events == [
        "credential",
        "pin",
        "credential",
        "preflight",
        "download",
        "pin",
        "credential",
        "preflight",
        "pin",
        "mutation_barrier:staging",
        "credential",
        "stage",
        "pin",
        "mutation_barrier:installing",
        "credential",
        "install",
        "local_cleanup",
        "pin",
        "cleanup_barrier:launching",
        "credential",
        "remote_cleanup:install_succeeded",
        "pin",
        "mutation_barrier:launching",
        "credential",
        "launch",
        "health_client",
        "pin",
        "health",
    ]
    assert harness.pin_arguments == [expected_pinned] * 7
    assert all(pin.original.host == "panel-one.local" for pin in harness.pin_arguments)
    assert all(pin.pinned.host == "192.168.250.23" for pin in harness.pin_arguments)
    assert harness.preflight_arguments == [
        (expected_adb_target, harness.credentials[1].signer, expected_descriptor),
        (expected_adb_target, harness.credentials[2].signer, expected_descriptor),
    ]
    assert harness.download_arguments == [(expected_release, expected_execution_id)]
    downloaded_release = harness.download_arguments[0][0]
    assert downloaded_release.tag == receipt.artifact.release_tag
    assert downloaded_release.version == receipt.artifact.version_name
    assert downloaded_release.apk_name == receipt.artifact.apk_name
    assert downloaded_release.apk_url == (
        "https://github.com/maxlyth/ha-paneld/releases/download/"
        f"{receipt.artifact.release_tag}/{receipt.artifact.apk_name}"
    )
    assert downloaded_release.sha256 == receipt.artifact.apk_sha256
    assert downloaded_release.descriptor == expected_descriptor
    assert harness.stage_arguments == [
        (
            expected_adb_target,
            harness.credentials[3].signer,
            expected_descriptor,
            expected_execution_id,
            Path(f"/private/{expected_execution_id}.apk"),
            AdbRootMode.ROOTLESS,
        )
    ]
    assert harness.install_arguments == [
        (
            expected_adb_target,
            harness.credentials[4].signer,
            expected_descriptor,
            expected_execution_id,
            AdbRootMode.ROOTLESS,
        )
    ]
    assert harness.local_cleanup_job_ids == [expected_execution_id]
    assert harness.remote_cleanup_arguments == [
        (
            expected_adb_target,
            harness.credentials[5].signer,
            expected_staged,
            DefiniteCleanupReason.INSTALL_SUCCEEDED,
            AdbRootMode.ROOTLESS,
        )
    ]
    assert harness.launch_arguments == [
        (
            expected_adb_target,
            harness.credentials[6].signer,
            expected_descriptor,
            AdbRootMode.ROOTLESS,
        )
    ]
    assert harness.health_client_addresses == [expected_pinned.pinned]
    assert harness.mutation_barrier_arguments == [
        (receipt.job_id, 7, InstallPhase.STAGING),
        (receipt.job_id, 8, InstallPhase.INSTALLING),
        (receipt.job_id, 10, InstallPhase.LAUNCHING),
    ]
    assert harness.cleanup_barrier_arguments == [
        (receipt.job_id, 10, InstallPhase.LAUNCHING)
    ]
    add_entry.assert_not_awaited()


async def test_original_hostname_is_never_given_to_an_adb_operation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS is revalidated separately while ADB receives only the frozen IP."""
    manager = InstallJobManager(hass)
    selected_target = InstallTarget(
        address="panel-one.local",
        pinned_address="192.168.250.23",
        adb_serial="SERIAL-1",
        model="Test Panel",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )
    selected_artifact = artifact()
    receipt, created = await manager.async_create_or_join(
        selected_target,
        selected_artifact,
        install_plan_sha256(selected_target, selected_artifact, CREDENTIAL_ID),
        CREDENTIAL_ID,
    )
    assert created
    harness = Harness(monkeypatch)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    adb_targets = [arguments[0] for arguments in harness.preflight_arguments]
    adb_targets.extend(arguments[0] for arguments in harness.stage_arguments)
    adb_targets.extend(arguments[0] for arguments in harness.install_arguments)
    adb_targets.extend(arguments[0] for arguments in harness.remote_cleanup_arguments)
    adb_targets.extend(arguments[0] for arguments in harness.launch_arguments)
    assert adb_targets
    assert all(
        adb_target.address.host == "192.168.250.23" for adb_target in adb_targets
    )
    assert all(
        pinned.original.host == "panel-one.local"
        and pinned.pinned.host == "192.168.250.23"
        for pinned in harness.pin_arguments
    )
    assert harness.health_client_addresses[0].host == "192.168.250.23"
    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED


async def test_concurrent_ensure_owns_exactly_one_worker_and_actuator_call(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent config flows cannot duplicate the staged or installed mutation."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_entered = asyncio.Event()
    harness.stage_release = asyncio.Event()
    executor = InstallExecutor(hass, manager)

    tasks = await asyncio.gather(
        *(executor.async_ensure_job(receipt.job_id) for _index in range(40))
    )
    assert tasks[0] is not None
    assert all(task is tasks[0] for task in tasks)
    await harness.stage_entered.wait()
    assert harness.events.count("stage") == 1

    harness.stage_release.set()
    await tasks[0]
    assert harness.events.count("stage") == 1
    assert harness.events.count("install") == 1
    assert harness.events.count("launch") == 1


@pytest.mark.parametrize(
    ("rejected_phase", "stage_calls", "install_calls"),
    [
        (InstallPhase.STAGING, 0, 0),
        (InstallPhase.INSTALLING, 1, 0),
        (InstallPhase.LAUNCHING, 1, 1),
    ],
)
async def test_rejected_mutation_barrier_precedes_and_blocks_exact_actuator(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    rejected_phase: InstallPhase,
    stage_calls: int,
    install_calls: int,
) -> None:
    """A durable barrier rejection occurs before its one external mutation."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    verify = manager.async_verify_mutation_barrier

    async def rejecting_barrier(
        job_id: str, revision: int, phase: InstallPhase
    ) -> InstallJobReceipt:
        harness.events.append(f"mutation_barrier:{phase.value}")
        if phase is rejected_phase:
            raise InstallJobTransitionError
        return await verify(job_id, revision, phase)

    monkeypatch.setattr(manager, "async_verify_mutation_barrier", rejecting_barrier)
    executor = InstallExecutor(hass, manager)

    paused = await executor.async_wait(receipt.job_id)

    assert paused.phase is rejected_phase
    assert harness.events.count("stage") == stage_calls
    assert harness.events.count("install") == install_calls
    assert "launch" not in harness.events
    assert (
        harness.events.index(f"mutation_barrier:{rejected_phase.value}")
        == len(harness.events) - 1
    )
    assert await executor.async_ensure_job(receipt.job_id) is None


async def test_rejected_cleanup_barrier_blocks_remote_rm_and_launch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cleanup side effect occurs unless its fresh narrow barrier succeeds."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)

    async def rejecting_cleanup(
        _job_id: str, _revision: int, phase: InstallPhase
    ) -> InstallJobReceipt:
        harness.events.append(f"cleanup_barrier:{phase.value}")
        raise InstallJobTransitionError

    monkeypatch.setattr(manager, "async_verify_cleanup_barrier", rejecting_cleanup)
    executor = InstallExecutor(hass, manager)

    paused = await executor.async_wait(receipt.job_id)

    assert paused.phase is InstallPhase.LAUNCHING
    assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    assert "launch" not in harness.events
    assert harness.events[-1] == "cleanup_barrier:launching"
    assert await executor.async_ensure_job(receipt.job_id) is None


@pytest.mark.parametrize(
    ("operation", "durable_phase", "event"),
    [
        ("stage", InstallPhase.STAGING, "stage"),
        ("install", InstallPhase.INSTALLING, "install"),
        (
            "cleanup",
            InstallPhase.LAUNCHING,
            "remote_cleanup:install_succeeded",
        ),
        ("launch", InstallPhase.LAUNCHING, "launch"),
    ],
)
async def test_unexpected_post_actuator_fault_cannot_replay_in_same_process(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    durable_phase: InstallPhase,
    event: str,
) -> None:
    """Untyped faults preserve ambiguity and permanently stop this worker owner."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    fault = OSError("post-actuator close failed")
    if operation == "stage":
        harness.stage_error = fault
    elif operation == "install":
        harness.install_error = fault
    elif operation == "cleanup":
        harness.cleanup_error = fault
    else:
        harness.launch_error = fault
    executor = InstallExecutor(hass, manager)

    with pytest.raises(OSError, match="post-actuator close failed"):
        await executor.async_wait(receipt.job_id)

    persisted = await manager.async_get(receipt.job_id)
    assert persisted.phase is durable_phase
    assert persisted.result_code is None
    assert harness.events.count(event) == 1
    assert await executor.async_ensure_job(receipt.job_id) is None
    assert harness.events.count(event) == 1

    restarted = InstallJobManager(hass)
    quarantined = await restarted.async_claim(receipt.job_id, persisted.revision)
    assert quarantined.phase is InstallPhase.RECOVERY_REQUIRED
    assert quarantined.result_code is InstallResultCode.VERIFICATION_REQUIRED


async def test_cancelled_waiter_does_not_cancel_detached_worker(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing config flow progress leaves its process-wide worker alive."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_entered = asyncio.Event()
    harness.stage_release = asyncio.Event()
    executor = InstallExecutor(hass, manager)
    waiter = asyncio.create_task(executor.async_wait(receipt.job_id))
    await harness.stage_entered.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    running = await executor.async_ensure_job(receipt.job_id)
    assert running is not None and not running.cancelled()

    harness.stage_release.set()
    await running
    current = await manager.async_get(receipt.job_id)
    assert current.phase is InstallPhase.HEALTHY_UNCLAIMED


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.APPROVED,
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.INSTALLED,
        InstallPhase.HEALTH_CHECK,
    ],
)
async def test_new_process_resumes_every_safe_phase(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """Every replay-safe durable phase reaches the unclaimed health boundary."""
    receipt, restarted = await seed_phase(hass, phase)
    Harness(monkeypatch)

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED


@pytest.mark.parametrize(
    "phase", [InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING]
)
async def test_new_process_quarantines_every_ambiguous_phase_without_adb(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """A crash boundary is never replayed by the new process worker."""
    receipt, restarted = await seed_phase(hass, phase)
    harness = Harness(monkeypatch)

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.VERIFICATION_REQUIRED
    assert "stage" not in harness.events
    assert "install" not in harness.events
    assert "launch" not in harness.events


async def test_worker_cancellation_leaves_durable_phase_for_new_process_claim(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA shutdown is not persisted as a definite failure or cancellation."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_entered = asyncio.Event()
    harness.stage_release = asyncio.Event()
    executor = InstallExecutor(hass, manager)
    worker = await executor.async_ensure_job(receipt.job_id)
    assert worker is not None
    await harness.stage_entered.wait()

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    persisted = await manager.async_get(receipt.job_id)
    assert persisted.phase is InstallPhase.STAGING
    assert persisted.result_code is None
    assert await executor.async_ensure_job(receipt.job_id) is None

    restarted = InstallJobManager(hass)
    quarantined = await restarted.async_claim(receipt.job_id, persisted.revision)
    assert quarantined.phase is InstallPhase.RECOVERY_REQUIRED


@pytest.mark.parametrize("failure", ["missing", "generation"])
async def test_authorizing_requires_the_exact_durable_credential(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """No network or ADB work starts before durable authorization succeeds."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    if failure == "missing":
        harness.credential_error_at = 1
    else:
        harness.credential_wrong_at = 1

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events == ["credential"]
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.AUTHORIZATION_FAILED


@pytest.mark.parametrize(
    ("credential_call", "blocked_event", "prior_count", "result"),
    [
        (2, "preflight", 0, InstallResultCode.TRANSPORT_FAILED),
        (3, "preflight", 1, InstallResultCode.TRANSPORT_FAILED),
        (4, "stage", 0, InstallResultCode.TRANSPORT_FAILED),
        (5, "install", 0, InstallResultCode.VERIFICATION_REQUIRED),
        (
            6,
            "remote_cleanup:install_succeeded",
            0,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
        (7, "launch", 0, InstallResultCode.LAUNCH_FAILED),
    ],
)
async def test_credential_generation_is_reloaded_before_every_adb_action(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    credential_call: int,
    blocked_event: str,
    prior_count: int,
    result: InstallResultCode,
) -> None:
    """A rotated durable key closes the next ADB boundary before the operation."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.credential_wrong_at = credential_call

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count(blocked_event) == prior_count
    assert completed.result_code is result
    if result in {
        InstallResultCode.AMBIGUOUS_MUTATION,
        InstallResultCode.VERIFICATION_REQUIRED,
    }:
        assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    else:
        assert completed.phase is InstallPhase.FAILED


@pytest.mark.parametrize(
    ("credential_call", "blocked_event", "prior_count", "phase", "result"),
    [
        (
            2,
            "preflight",
            0,
            InstallPhase.FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        ),
        (
            3,
            "preflight",
            1,
            InstallPhase.FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        ),
        (
            4,
            "stage",
            0,
            InstallPhase.FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        ),
        (
            5,
            "install",
            0,
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
        (
            6,
            "remote_cleanup:install_succeeded",
            0,
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
        (
            7,
            "launch",
            0,
            InstallPhase.FAILED,
            InstallResultCode.LAUNCH_FAILED,
        ),
    ],
)
async def test_missing_or_nonprivate_credential_never_reaches_next_adb_action(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    credential_call: int,
    blocked_event: str,
    prior_count: int,
    phase: InstallPhase,
    result: InstallResultCode,
) -> None:
    """Deletion and permission failures share the fail-closed durable authority."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.credential_error_at = credential_call

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count(blocked_event) == prior_count
    assert completed.phase is phase
    assert completed.result_code is result


async def test_pin_drift_before_stage_prevents_the_mutation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Original-name rebinding is caught again immediately before staging."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.pin_error_at = 3

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.TRANSPORT_FAILED


async def test_changed_pin_value_before_stage_prevents_the_mutation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver returning any pin other than the frozen target fails closed."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.pin_wrong_at = 3

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.TRANSPORT_FAILED


async def test_root_posture_drift_fails_before_staging(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh serial and root posture must match the earlier clean admission."""
    manager = InstallJobManager(hass)
    first = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.preflight_root_modes = [AdbRootMode.ROOTLESS, AdbRootMode.ROOT_ADBD]

    completed = await InstallExecutor(hass, manager).async_wait(first.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.PREFLIGHT_REJECTED


async def test_admitted_root_posture_is_bound_to_every_mutation_primitive(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same exact root admission is passed into every later ADB connection."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.preflight_root_modes = [
        AdbRootMode.ROOT_ADBD,
        AdbRootMode.ROOT_ADBD,
    ]

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.stage_arguments[0][-1] is AdbRootMode.ROOT_ADBD
    assert harness.install_arguments[0][-1] is AdbRootMode.ROOT_ADBD
    assert harness.remote_cleanup_arguments[0][-1] is AdbRootMode.ROOT_ADBD
    assert harness.launch_arguments[0][-1] is AdbRootMode.ROOT_ADBD
    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED


@pytest.mark.parametrize(
    ("operation", "phase", "result"),
    [
        ("stage", InstallPhase.FAILED, InstallResultCode.TRANSPORT_FAILED),
        (
            "install",
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
        (
            "cleanup",
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
        (
            "launch",
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
    ],
)
async def test_same_connection_root_drift_fails_closed_before_mutation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    phase: InstallPhase,
    result: InstallResultCode,
) -> None:
    """A mutation primitive's own fresh root mismatch is never ignored."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    error = InstallAdbError(InstallAdbErrorCode.ROOT_MODE_CHANGED)
    if operation == "stage":
        harness.stage_error = error
    elif operation == "install":
        harness.install_error = error
    elif operation == "cleanup":
        harness.cleanup_error = error
    else:
        harness.launch_error = error

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert completed.phase is phase
    assert completed.result_code is result
    if operation in {"stage", "install"}:
        assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    if operation != "launch":
        assert "launch" not in harness.events


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
    ],
)
async def test_safe_resume_compares_fresh_root_posture_with_durable_admission(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """A restart cannot forget the root posture admitted before download."""
    receipt, restarted = await seed_phase(hass, phase)
    assert receipt.preflight_root_mode == AdbRootMode.ROOTLESS.value
    harness = Harness(monkeypatch)
    harness.preflight_root_modes = [AdbRootMode.ROOT_ADBD]

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.PREFLIGHT_REJECTED


async def test_new_process_cleans_one_stale_partial_then_resumes_download(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process-death partial is exact-owned cleanup, not a terminal BUSY."""
    receipt, restarted = await seed_phase(hass, InstallPhase.DOWNLOADING)
    execution_digest = sha256()
    execution_digest.update(b"ha-paneld-install-execution-v1\0")
    execution_digest.update(bytes.fromhex(receipt.job_id))
    execution_digest.update(bytes.fromhex(receipt.plan_sha256))
    execution_id = execution_digest.hexdigest()[:32]
    artifact_directory = Path(
        hass.config.path(".storage", "ha_paneld.install_artifacts")
    )
    partial = artifact_directory / f"{execution_id}.apk.part"
    await hass.async_add_executor_job(seed_crash_partial, partial)
    real_download = install_executor.async_download_install_artifact
    real_cleanup = install_executor.async_cleanup_install_artifact
    harness = Harness(monkeypatch)
    download_calls = 0
    cleanup_observations: list[bool] = []

    async def crash_shaped_download(
        selected_hass: HomeAssistant,
        session: object,
        release: ReleaseArtifact,
        job_id: str,
    ) -> CustodiedArtifact:
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            return await real_download(selected_hass, session, release, job_id)
        return await harness.async_download(selected_hass, session, release, job_id)

    async def exact_cleanup(selected_hass: HomeAssistant, job_id: str) -> None:
        cleanup_observations.append(
            await selected_hass.async_add_executor_job(partial.exists)
        )
        await real_cleanup(selected_hass, job_id)

    monkeypatch.setattr(
        install_executor, "async_download_install_artifact", crash_shaped_download
    )
    monkeypatch.setattr(
        install_executor, "async_cleanup_install_artifact", exact_cleanup
    )

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert download_calls == 2
    assert cleanup_observations == [True, False]
    assert not await hass.async_add_executor_job(partial.exists)
    assert harness.events.count("stage") == 1
    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
    ],
)
async def test_one_stale_busy_is_cleaned_and_resumed_from_every_safe_phase(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """A crash partial is recoverable even after artifact-ready persistence."""
    receipt, restarted = await seed_phase(hass, phase)
    harness = Harness(monkeypatch)
    attempts = 0

    async def busy_once(
        selected_hass: HomeAssistant,
        session: object,
        release: ReleaseArtifact,
        job_id: str,
    ) -> CustodiedArtifact:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ArtifactCustodyError(ArtifactErrorCode.BUSY)
        return await harness.async_download(selected_hass, session, release, job_id)

    monkeypatch.setattr(install_executor, "async_download_install_artifact", busy_once)

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert attempts == 2
    assert harness.events.count("local_cleanup") == 2
    assert harness.events.count("stage") == 1
    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
    ],
)
async def test_repeated_download_busy_is_bounded_and_paused(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """One worker removes at most one exact stale partial before stopping."""
    receipt, restarted = await seed_phase(hass, phase)
    harness = Harness(monkeypatch)
    harness.download_error = ArtifactCustodyError(ArtifactErrorCode.BUSY)
    executor = InstallExecutor(hass, restarted)

    paused = await executor.async_wait(receipt.job_id)

    assert harness.events.count("download") == 2
    assert harness.events.count("local_cleanup") == 1
    assert paused.phase is phase
    assert paused.result_code is None
    assert await executor.async_ensure_job(receipt.job_id) is None


@pytest.mark.parametrize("preflight_call", [1, 2])
async def test_identity_drift_at_either_clean_preflight_fails_closed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    preflight_call: int,
) -> None:
    """A changed serial is rejected both initially and before the first mutation."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.preflight_wrong_at = preflight_call

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == (preflight_call == 2)
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.PREFLIGHT_REJECTED


@pytest.mark.parametrize("field", ["model", "primary_abi", "android_sdk"])
async def test_each_target_fact_drift_at_revalidation_fails_closed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Model, primary ABI, and SDK are all exact pre-mutation bindings."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.preflight_wrong_at = 2
    harness.preflight_wrong_field = field

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.PREFLIGHT_REJECTED


@pytest.mark.parametrize(
    "mutation", ["job_id", "size", "sha256", "path_type", "path_empty"]
)
async def test_artifact_mismatch_is_rejected_without_staging(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """The executor cross-checks custody output against frozen signed facts."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.download_mutation = mutation

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "stage" not in harness.events
    assert harness.events.count("local_cleanup") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.ARTIFACT_REJECTED


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
    ],
)
@pytest.mark.parametrize(
    ("error_code", "result"),
    [
        (ArtifactErrorCode.DIGEST_MISMATCH, InstallResultCode.ARTIFACT_REJECTED),
        (ArtifactErrorCode.TIMEOUT, InstallResultCode.TRANSPORT_FAILED),
    ],
)
async def test_artifact_error_taxonomy_is_stable_across_safe_resume_phases(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
    error_code: ArtifactErrorCode,
    result: InstallResultCode,
) -> None:
    """A custody failure cannot change meaning merely because HA restarted."""
    receipt, restarted = await seed_phase(hass, phase)
    harness = Harness(monkeypatch)
    harness.download_error = ArtifactCustodyError(error_code)

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert harness.events.count("local_cleanup") == 1
    assert "stage" not in harness.events
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is result


@pytest.mark.parametrize("failure", ["pin", "credential", "identity"])
async def test_revalidation_failure_removes_the_authenticated_local_artifact(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Every definite rejection after download cleans only this job's file."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    if failure == "pin":
        harness.pin_error_at = 2
    elif failure == "credential":
        harness.credential_error_at = 3
    else:
        harness.preflight_wrong_at = 2

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)
    frozen = install_executor._frozen_execution(receipt)

    assert harness.local_cleanup_job_ids == [frozen.execution_id]
    assert "stage" not in harness.events
    assert completed.phase is InstallPhase.FAILED


async def test_failed_local_cleanup_pauses_safe_phase_for_later_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local residue is neither hidden by failure nor made crash-ambiguous."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.download_mutation = "sha256"
    harness.local_cleanup_error = ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
    executor = InstallExecutor(hass, manager)

    paused = await executor.async_wait(receipt.job_id)

    assert paused.phase is InstallPhase.DOWNLOADING
    assert paused.result_code is None
    assert "stage" not in harness.events
    await asyncio.sleep(0)
    harness.download_mutation = None
    harness.local_cleanup_error = None

    completed = await executor.async_wait(receipt.job_id)

    assert completed.phase is InstallPhase.HEALTHY_UNCLAIMED
    assert harness.events.count("stage") == 1


async def test_definite_install_refusal_cleans_exact_stage_then_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definite package-manager refusal permits only exact job-owned cleanup."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.install_outcome = InstallOutcome.REFUSED

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)
    frozen = install_executor._frozen_execution(receipt)
    staged = install_executor._staged(frozen.execution_id, receipt.artifact)

    assert harness.events.count("remote_cleanup:install_refused") == 1
    assert harness.remote_cleanup_arguments == [
        (
            frozen.adb_target,
            harness.credentials[5].signer,
            staged,
            DefiniteCleanupReason.INSTALL_REFUSED,
            AdbRootMode.ROOTLESS,
        )
    ]
    assert harness.local_cleanup_job_ids == [frozen.execution_id]
    assert "launch" not in harness.events
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.INSTALL_FAILED


@pytest.mark.parametrize(
    ("cleanup_error", "result"),
    [
        (
            InstallAdbError(InstallAdbErrorCode.CLEANUP_AMBIGUOUS),
            InstallResultCode.AMBIGUOUS_MUTATION,
        ),
        (
            InstallAdbError(InstallAdbErrorCode.TARGET_UNREACHABLE),
            InstallResultCode.VERIFICATION_REQUIRED,
        ),
    ],
)
async def test_incomplete_remote_cleanup_quarantines_instead_of_launching(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: InstallAdbError,
    result: InstallResultCode,
) -> None:
    """Unknown or retained remote staging state is never hidden by a launch."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.cleanup_error = cleanup_error

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "launch" not in harness.events
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is result


@pytest.mark.parametrize(
    "stage_error",
    [
        InstallAdbError(InstallAdbErrorCode.STAGE_AMBIGUOUS),
        InstallAdbError(InstallAdbErrorCode.STAGE_VERIFICATION_FAILED),
    ],
)
async def test_ambiguous_stage_is_quarantined_without_remote_cleanup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    stage_error: InstallAdbError,
) -> None:
    """A push that might have started is never followed by an unsafe rm guess."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_error = stage_error

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "install" not in harness.events
    assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.AMBIGUOUS_MUTATION


async def test_definite_pre_push_stage_refusal_cleans_local_and_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definite occupied staging path is not mislabeled as an ambiguous push."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_error = InstallAdbError(InstallAdbErrorCode.STAGING_PATH_OCCUPIED)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("local_cleanup") == 1
    assert "install" not in harness.events
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.ARTIFACT_REJECTED


async def test_mismatched_staged_receipt_is_quarantined_without_cleanup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected stage result cannot authorize install or guessed cleanup."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.stage_mismatch = True

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("stage") == 1
    assert "install" not in harness.events
    assert "local_cleanup" not in harness.events
    assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.AMBIGUOUS_MUTATION


async def test_ambiguous_install_is_quarantined_without_cleanup_or_launch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown package-manager outcome retains all evidence for recovery."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.install_error = InstallAdbError(InstallAdbErrorCode.INSTALL_AMBIGUOUS)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    assert "launch" not in harness.events
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.AMBIGUOUS_MUTATION


@pytest.mark.parametrize("failure", ["credential", "adb"])
async def test_install_pre_mutation_failure_preserves_staged_evidence(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """No terminal result discards residue once a verified stage exists."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    if failure == "credential":
        harness.credential_wrong_at = 5
    else:
        harness.install_error = InstallAdbError(InstallAdbErrorCode.TARGET_UNREACHABLE)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("stage") == 1
    assert not any(event.startswith("remote_cleanup:") for event in harness.events)
    assert "local_cleanup" not in harness.events
    assert "launch" not in harness.events
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.VERIFICATION_REQUIRED


async def test_ambiguous_launch_is_quarantined_after_exact_cleanup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown activity-manager result remains explicitly recoverable."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.launch_error = InstallAdbError(InstallAdbErrorCode.LAUNCH_AMBIGUOUS)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("remote_cleanup:install_succeeded") == 1
    assert harness.events.count("launch") == 1
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.AMBIGUOUS_MUTATION


async def test_definite_launch_refusal_fails_after_exact_cleanup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical activity-manager refusal is a definite launch failure."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.launch_outcome = LaunchOutcome.REFUSED

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("remote_cleanup:install_succeeded") == 1
    assert harness.events.count("launch") == 1
    assert completed.phase is InstallPhase.FAILED
    assert completed.result_code is InstallResultCode.LAUNCH_FAILED


async def test_local_custody_cleanup_failure_after_install_requires_recovery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definitely installed package does not hide a failed private-file cleanup."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.local_cleanup_error = ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert "launch" not in harness.events
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.VERIFICATION_REQUIRED


async def test_health_poll_is_bounded_and_never_claims_success_on_transport_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated absent health stops after the fixed attempt budget."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    harness.health_error = CannotConnectError()
    sleep = AsyncMock()
    monkeypatch.setattr(install_executor.asyncio, "sleep", sleep)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.health_calls == install_executor._HEALTH_ATTEMPTS
    assert sleep.await_count == install_executor._HEALTH_ATTEMPTS - 1
    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.VERIFICATION_REQUIRED


async def test_version_mismatch_never_becomes_healthy_or_creates_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health from any other version remains a verification-required install."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    Harness(monkeypatch, expected_version="9.9.9")
    add_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_add", add_entry)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert completed.phase is InstallPhase.RECOVERY_REQUIRED
    assert completed.result_code is InstallResultCode.VERIFICATION_REQUIRED
    add_entry.assert_not_awaited()


async def test_receipt_reconstruction_always_supplies_authenticated_descriptor(
    hass: HomeAssistant,
) -> None:
    """The executor accepts only an already authenticated durable receipt."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)

    frozen = install_executor._frozen_execution(receipt)

    assert frozen.release.descriptor is not None


async def test_durable_cancel_after_stage_uses_cleanup_barrier_then_cancels(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent cancel cleans a definitely staged path under fresh authority."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)

    async def request_cancel_after_push() -> None:
        current = await manager.async_get(receipt.job_id)
        await manager.async_request_cancel(current.job_id, current.revision)

    harness.stage_callback = request_cancel_after_push
    cleanup_barrier = AsyncMock(wraps=manager.async_verify_cleanup_barrier)
    monkeypatch.setattr(manager, "async_verify_cleanup_barrier", cleanup_barrier)

    completed = await InstallExecutor(hass, manager).async_wait(receipt.job_id)

    assert harness.events.count("stage") == 1
    assert harness.events.count("remote_cleanup:cancelled") == 1
    assert "install" not in harness.events
    assert cleanup_barrier.await_count == 1
    assert completed.phase is InstallPhase.CANCELLED
    assert completed.result_code is InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP


async def test_cancelled_downloading_receipt_cleans_possible_partial_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash-era partial is not orphaned when cancellation wins the restart."""
    receipt, restarted = await seed_phase(hass, InstallPhase.DOWNLOADING)
    receipt = await restarted.async_request_cancel(receipt.job_id, receipt.revision)
    harness = Harness(monkeypatch)

    completed = await InstallExecutor(hass, restarted).async_wait(receipt.job_id)

    assert harness.events == ["local_cleanup"]
    assert completed.phase is InstallPhase.CANCELLED
    assert completed.result_code is InstallResultCode.CANCELLED_BY_USER


async def test_cancel_cleanup_failure_keeps_safe_request_retryable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed local cleanup cannot turn a requested cancel into definite failure."""
    receipt, restarted = await seed_phase(hass, InstallPhase.DOWNLOADING)
    receipt = await restarted.async_request_cancel(receipt.job_id, receipt.revision)
    harness = Harness(monkeypatch)
    harness.local_cleanup_error = ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
    executor = InstallExecutor(hass, restarted)

    paused = await executor.async_wait(receipt.job_id)

    assert paused.phase is InstallPhase.DOWNLOADING
    assert paused.cancel_requested
    assert paused.result_code is None
    await asyncio.sleep(0)
    harness.local_cleanup_error = None

    completed = await executor.async_wait(receipt.job_id)

    assert completed.phase is InstallPhase.CANCELLED
    assert completed.result_code is InstallResultCode.CANCELLED_BY_USER


async def test_resume_hook_is_explicit_and_getter_is_process_wide(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an importing loaded integration can invoke process-wide safe resume."""
    manager = InstallJobManager(hass)
    receipt = await create_job(manager)
    harness = Harness(monkeypatch)
    manager_getter = AsyncMock()

    async def delayed_manager(_hass: HomeAssistant) -> InstallJobManager:
        await asyncio.sleep(0)
        return manager

    manager_getter.side_effect = delayed_manager
    monkeypatch.setattr(
        install_executor,
        "async_get_install_job_manager",
        manager_getter,
    )

    first, second = await asyncio.gather(
        async_get_install_executor(hass), async_get_install_executor(hass)
    )
    assert first is second
    assert manager_getter.await_count == 1
    assert not first._tasks

    resumed = await first.async_resume_loaded_jobs()
    assert resumed == (receipt.job_id,)
    worker = await first.async_ensure_job(receipt.job_id)
    assert worker is not None
    await worker
    assert harness.events.count("stage") == 1


@pytest.mark.parametrize(
    "phase", [InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING]
)
async def test_resume_hook_quarantines_but_does_not_resume_ambiguous_phase(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    phase: InstallPhase,
) -> None:
    """Loaded-domain recovery never reports crash-ambiguous work as resumed."""
    receipt, restarted = await seed_phase(hass, phase)
    harness = Harness(monkeypatch)
    executor = InstallExecutor(hass, restarted)

    resumed = await executor.async_resume_loaded_jobs()
    quarantined = await restarted.async_get(receipt.job_id)

    assert resumed == ()
    assert quarantined.phase is InstallPhase.RECOVERY_REQUIRED
    assert quarantined.result_code is InstallResultCode.VERIFICATION_REQUIRED
    assert "stage" not in harness.events
    assert "install" not in harness.events
    assert "launch" not in harness.events


async def test_finalizer_lease_grants_exactly_one_flow_and_releases_by_owner(
    hass: HomeAssistant,
) -> None:
    """Two completed config flows cannot both create an entry for one job."""
    receipt, manager = await seed_phase(hass, InstallPhase.HEALTH_CHECK)
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    receipt = await manager.async_transition(
        receipt.job_id,
        receipt.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    executor = InstallExecutor(hass, manager)

    owners = await asyncio.gather(
        *(
            executor.async_acquire_finalizer(receipt.job_id, f"flow_{index}")
            for index in range(40)
        )
    )
    assert owners.count(True) == 1
    owner = f"flow_{owners.index(True)}"
    assert await executor.async_is_finalizer_active(receipt.job_id)
    assert await executor.async_acquire_finalizer(receipt.job_id, owner)

    await executor.async_release_finalizer(receipt.job_id, "flow_loser")
    assert await executor.async_is_finalizer_active(receipt.job_id)
    await executor.async_release_finalizer(receipt.job_id, owner)
    assert not await executor.async_is_finalizer_active(receipt.job_id)
    assert await executor.async_acquire_finalizer(receipt.job_id, "flow_next")


async def test_finalizer_lease_is_unavailable_before_health_or_after_consumption(
    hass: HomeAssistant,
) -> None:
    """The in-memory lease never substitutes for the durable receipt phase."""
    manager = InstallJobManager(hass)
    approved = await create_job(manager)
    executor = InstallExecutor(hass, manager)
    assert not await executor.async_acquire_finalizer(approved.job_id, "flow_one")
    approved = await manager.async_claim(approved.job_id, approved.revision)
    approved = await manager.async_request_cancel(approved.job_id, approved.revision)
    await manager.async_transition(
        approved.job_id,
        approved.revision,
        InstallPhase.CANCELLED,
        result_code=InstallResultCode.CANCELLED_BY_USER,
    )

    healthy, manager = await seed_phase(hass, InstallPhase.HEALTH_CHECK)
    healthy = await manager.async_claim(healthy.job_id, healthy.revision)
    healthy = await manager.async_transition(
        healthy.job_id,
        healthy.revision,
        InstallPhase.HEALTHY_UNCLAIMED,
        health_checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    executor = InstallExecutor(hass, manager)
    assert await executor.async_acquire_finalizer(healthy.job_id, "flow_one")
    consumed = await manager.async_transition(
        healthy.job_id,
        healthy.revision,
        InstallPhase.CONSUMED,
        result_code=InstallResultCode.ENTRY_CREATED,
        consumed_entry_id="01M1J723MDQ69QDQVCRXKYZBJV",
    )
    assert consumed.phase is InstallPhase.CONSUMED
    assert not await executor.async_acquire_finalizer(healthy.job_id, "flow_two")
    assert await executor.async_is_finalizer_active(healthy.job_id)
    await executor.async_release_finalizer(healthy.job_id, "flow_one")
    assert not await executor.async_is_finalizer_active(healthy.job_id)
