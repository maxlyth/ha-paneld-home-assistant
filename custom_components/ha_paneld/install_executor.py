"""Process-wide orchestration for one durable clean-panel installation.

The executor deliberately stops at ``HEALTHY_UNCLAIMED``.  Config-entry
creation remains a config-flow responsibility so a background worker can never
change the integration's endpoint identity contract.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .adb_credentials import (
    AdbCredential,
    AdbCredentialError,
    async_get_durable_adb_credential,
)
from .client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidResponseError,
    PanelHealth,
    normalize_address,
)
from .const import DOMAIN
from .install_adb import (
    AdbInstallTarget,
    AdbPreflight,
    AdbRootMode,
    DefiniteCleanupReason,
    InstallAdbError,
    InstallAdbErrorCode,
    InstallOutcome,
    LaunchOutcome,
    StagedApk,
    async_cleanup_staged_apk,
    async_install_staged_apk,
    async_launch_installed_app,
    async_preflight_install,
    async_stage_apk,
)
from .install_artifacts import (
    ArtifactCustodyError,
    ArtifactErrorCode,
    async_cleanup_install_artifact,
    async_download_install_artifact,
    async_reconcile_install_artifacts,
)
from .install_artifacts import (
    InstallArtifact as CustodiedArtifact,
)
from .install_jobs import (
    InstallArtifact,
    InstallJobCleanupRequiredError,
    InstallJobManager,
    InstallJobReceipt,
    InstallJobRevisionError,
    InstallJobStoreError,
    InstallJobTransitionError,
    InstallPhase,
    InstallResultCode,
    async_get_install_job_manager,
)
from .install_network import (
    InstallNetworkError,
    InstallNetworkErrorCode,
    PinnedPanelTarget,
    async_revalidate_install_target,
)
from .release import InstallDescriptor, ReleaseArtifact

_EXECUTOR_DATA_KEY = f"{DOMAIN}.install_executor"
_EXECUTOR_LOCK_DATA_KEY = f"{DOMAIN}.install_executor_lock"
_EXECUTION_ID_DOMAIN = b"ha-paneld-install-execution-v1\0"
# The path is device-local, so one fixed slot bounds ambiguous remote residue
# without creating a cross-panel collision. Stage proves the slot absent in the
# same ADB connection before writing; a later job refuses rather than overwrites.
_REMOTE_STAGING_SLOT_ID = sha256(
    b"ha-paneld-device-local-staging-slot-v1\0"
).hexdigest()[:32]
_RELEASE_DOWNLOAD_ROOT = "https://github.com/maxlyth/ha-paneld/releases/download"
_REMOTE_STAGING_PREFIX = "/data/local/tmp/ha-paneld-install-"
_HEALTH_ATTEMPTS = 12
_HEALTH_RETRY_SECONDS = 2.0
_FINALIZER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$", flags=re.ASCII)

_SAFE_RESUME_PHASES = frozenset(
    {
        InstallPhase.APPROVED,
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.INSTALLED,
        InstallPhase.HEALTH_CHECK,
    }
)
_RESTART_AMBIGUOUS_PHASES = frozenset(
    {InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING}
)

_AMBIGUOUS_ADB_ERRORS = frozenset(
    {
        InstallAdbErrorCode.STAGE_AMBIGUOUS,
        InstallAdbErrorCode.STAGE_VERIFICATION_FAILED,
        InstallAdbErrorCode.INSTALL_AMBIGUOUS,
        InstallAdbErrorCode.LAUNCH_AMBIGUOUS,
        InstallAdbErrorCode.CLEANUP_AMBIGUOUS,
    }
)
_PREFLIGHT_REJECTIONS = frozenset(
    {
        InstallAdbErrorCode.TARGET_CHANGED,
        InstallAdbErrorCode.TARGET_INCOMPATIBLE,
        InstallAdbErrorCode.TARGET_NOT_CLEAN,
        InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS,
    }
)
_ARTIFACT_REJECTIONS = frozenset(
    {
        ArtifactErrorCode.DESCRIPTOR_REQUIRED,
        ArtifactErrorCode.CONTRACT_INVALID,
        ArtifactErrorCode.JOB_ID_INVALID,
        ArtifactErrorCode.PATH_INVALID,
        ArtifactErrorCode.READY_INVALID,
        ArtifactErrorCode.REDIRECT_INVALID,
        ArtifactErrorCode.HTTP_STATUS,
        ArtifactErrorCode.RESPONSE_INVALID,
        ArtifactErrorCode.TOO_LARGE,
        ArtifactErrorCode.SIZE_MISMATCH,
        ArtifactErrorCode.DIGEST_MISMATCH,
        ArtifactErrorCode.IO_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class _FrozenExecution:
    """Runtime forms reconstructed solely from one authenticated receipt."""

    pinned: PinnedPanelTarget
    adb_target: AdbInstallTarget
    descriptor: InstallDescriptor
    release: ReleaseArtifact
    execution_id: str


class _CancellationObserved(Exception):
    """Carry a freshly read cancellation without losing its receipt revision."""

    def __init__(self, receipt: InstallJobReceipt) -> None:
        self.receipt = receipt
        super().__init__()


class _PauseJob(Exception):
    """Leave a safe durable phase resumable after local cleanup could not finish."""

    def __init__(self, *, nonrestartable: bool = False) -> None:
        self.nonrestartable = nonrestartable
        super().__init__()


class InstallExecutor:
    """Own exactly one detached background worker for each durable job."""

    def __init__(self, hass: HomeAssistant, manager: InstallJobManager) -> None:
        self._hass = hass
        self._manager = manager
        self._lock = asyncio.Lock()
        self._resume_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._finalizers: dict[str, str] = {}
        # A cancelled worker may have been interrupted inside a mutation phase.
        # Never restart it in the same process, whose manager still owns the old
        # in-memory claim. A new HA process will reclaim or quarantine durably.
        self._nonrestartable_workers: set[str] = set()

    async def async_ensure_job(self, job_id: str) -> asyncio.Task[None] | None:
        """Start or join one process-wide worker without duplicating side effects."""
        async with self._lock:
            running = self._tasks.get(job_id)
            if running is not None:
                return running
            if job_id in self._nonrestartable_workers:
                return None
            receipt = await self._manager.async_get(job_id)
            if receipt.is_terminal or receipt.phase is InstallPhase.HEALTHY_UNCLAIMED:
                return None
            task = self._hass.async_create_background_task(
                self._async_run(job_id),
                f"ha-paneld install {job_id}",
                eager_start=False,
            )
            self._tasks[job_id] = task
            task.add_done_callback(partial(self._worker_done, job_id))
            return task

    async def async_wait(self, job_id: str) -> InstallJobReceipt:
        """Wait for current progress without giving the caller worker ownership."""
        task = await self.async_ensure_job(job_id)
        if task is not None:
            await asyncio.shield(task)
        return await self._manager.async_get(job_id)

    async def async_resume_loaded_jobs(self) -> tuple[str, ...]:
        """Resume durable jobs because an existing integration entry loaded us.

        This function does not register an HA startup hook. With zero config
        entries Home Assistant does not import the custom integration, so a
        later config flow must call :meth:`async_ensure_job` to resume the job.
        """
        async with self._resume_lock:
            resumed: list[str] = []
            for receipt in await self._manager.async_list():
                if (
                    receipt.is_terminal
                    or receipt.phase is InstallPhase.HEALTHY_UNCLAIMED
                ):
                    continue
                if receipt.phase in _RESTART_AMBIGUOUS_PHASES:
                    try:
                        await self._async_claim(receipt)
                    except _PauseJob:
                        continue
                    except InstallJobRevisionError:
                        current = await self._manager.async_get(receipt.job_id)
                        if (
                            not current.is_terminal
                            and current.phase in _RESTART_AMBIGUOUS_PHASES
                        ):
                            try:
                                await self._async_claim(current)
                            except _PauseJob, InstallJobRevisionError:
                                continue
                    # An active same-manager worker retains its claim. It must
                    # never be duplicated or have its live artifact cleaned.
                    continue
                if receipt.phase not in _SAFE_RESUME_PHASES:
                    continue
                if await self.async_ensure_job(receipt.job_id) is not None:
                    resumed.append(receipt.job_id)
            return tuple(resumed)

    async def async_acquire_finalizer(self, job_id: str, flow_id: str) -> bool:
        """Grant one flow the exclusive HEALTHY_UNCLAIMED finalization lease."""
        if not isinstance(flow_id, str) or _FINALIZER_ID.fullmatch(flow_id) is None:
            raise InstallJobTransitionError
        async with self._lock:
            receipt = await self._manager.async_get(job_id)
            if receipt.phase is not InstallPhase.HEALTHY_UNCLAIMED:
                return False
            owner = self._finalizers.get(job_id)
            if owner is None:
                self._finalizers[job_id] = flow_id
                return True
            return owner == flow_id

    async def async_release_finalizer(self, job_id: str, flow_id: str) -> None:
        """Release only the calling flow's finalization lease."""
        async with self._lock:
            if self._finalizers.get(job_id) == flow_id:
                self._finalizers.pop(job_id, None)

    async def async_is_finalizer_active(self, job_id: str) -> bool:
        """Tell setup reconciliation whether a flow currently owns finalization."""
        async with self._lock:
            return job_id in self._finalizers

    def _worker_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        if task.cancelled() or task.exception() is not None:
            self._nonrestartable_workers.add(job_id)

    async def _async_run(self, job_id: str) -> None:
        local_artifact: CustodiedArtifact | None = None
        staged: StagedApk | None = None
        stale_partial_removed = False
        try:
            receipt = await self._manager.async_get(job_id)
            try:
                receipt = await self._async_claim(receipt)
            except InstallJobRevisionError:
                receipt = await self._manager.async_get(job_id)
                receipt = await self._async_claim(receipt)
            while (
                not receipt.is_terminal
                and receipt.phase is not InstallPhase.HEALTHY_UNCLAIMED
            ):
                if receipt.cancel_requested:
                    receipt = await self._async_cancel_requested(
                        receipt, local_artifact, staged
                    )
                    continue

                execution = _frozen_execution(receipt)
                phase = receipt.phase
                if phase is InstallPhase.APPROVED:
                    receipt = await self._async_transition(
                        receipt, InstallPhase.AUTHORIZING
                    )
                elif phase is InstallPhase.AUTHORIZING:
                    try:
                        await self._async_current_credential(receipt)
                    except AdbCredentialError:
                        receipt = await self._async_fail(
                            receipt, InstallResultCode.AUTHORIZATION_FAILED
                        )
                    else:
                        receipt = await self._async_transition(
                            receipt, InstallPhase.PREFLIGHT
                        )
                elif phase is InstallPhase.PREFLIGHT:
                    try:
                        observed = await self._async_preflight(receipt, execution)
                    except AdbCredentialError, InstallNetworkError:
                        receipt = await self._async_fail(
                            receipt, InstallResultCode.TRANSPORT_FAILED
                        )
                    except InstallAdbError as err:
                        receipt = await self._async_fail(
                            receipt, _preflight_result(err)
                        )
                    else:
                        receipt = await self._async_transition(
                            receipt,
                            InstallPhase.DOWNLOADING,
                            preflight_root_mode=observed.root_mode.value,
                        )
                elif phase is InstallPhase.DOWNLOADING:
                    try:
                        local_artifact = await async_download_install_artifact(
                            self._hass,
                            async_get_clientsession(self._hass),
                            execution.release,
                            execution.execution_id,
                        )
                        _require_local_artifact(
                            local_artifact, execution.execution_id, receipt.artifact
                        )
                    except ArtifactCustodyError as err:
                        if err.code is ArtifactErrorCode.BUSY:
                            stale_partial_removed = (
                                await self._async_clear_stale_partial(
                                    execution.execution_id,
                                    already_removed=stale_partial_removed,
                                )
                            )
                        else:
                            receipt = await self._async_cleanup_local_then_fail(
                                receipt,
                                execution.execution_id,
                                _artifact_result(err),
                            )
                    else:
                        receipt = await self._async_transition(
                            receipt,
                            InstallPhase.ARTIFACT_READY,
                            actual_apk_bytes=local_artifact.size,
                        )
                elif phase is InstallPhase.ARTIFACT_READY:
                    try:
                        if local_artifact is None:
                            local_artifact = await self._async_ensure_local_artifact(
                                receipt, execution
                            )
                        else:
                            _require_local_artifact(
                                local_artifact,
                                execution.execution_id,
                                receipt.artifact,
                            )
                    except ArtifactCustodyError as err:
                        if err.code is ArtifactErrorCode.BUSY:
                            stale_partial_removed = (
                                await self._async_clear_stale_partial(
                                    execution.execution_id,
                                    already_removed=stale_partial_removed,
                                )
                            )
                        else:
                            receipt = await self._async_cleanup_local_then_fail(
                                receipt,
                                execution.execution_id,
                                _artifact_result(err),
                            )
                    else:
                        receipt = await self._async_transition(
                            receipt, InstallPhase.REVALIDATING
                        )
                elif phase is InstallPhase.REVALIDATING:
                    try:
                        if local_artifact is None:
                            local_artifact = await self._async_ensure_local_artifact(
                                receipt, execution
                            )
                        observed = await self._async_preflight(receipt, execution)
                        if observed.root_mode.value != receipt.preflight_root_mode:
                            raise InstallAdbError(
                                InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS
                            )
                    except ArtifactCustodyError as err:
                        if err.code is ArtifactErrorCode.BUSY:
                            stale_partial_removed = (
                                await self._async_clear_stale_partial(
                                    execution.execution_id,
                                    already_removed=stale_partial_removed,
                                )
                            )
                        else:
                            receipt = await self._async_cleanup_local_then_fail(
                                receipt,
                                execution.execution_id,
                                _artifact_result(err),
                            )
                    except AdbCredentialError, InstallNetworkError:
                        receipt = await self._async_cleanup_local_then_fail(
                            receipt,
                            execution.execution_id,
                            InstallResultCode.TRANSPORT_FAILED,
                        )
                    except InstallAdbError as err:
                        receipt = await self._async_cleanup_local_then_fail(
                            receipt,
                            execution.execution_id,
                            _preflight_result(err),
                        )
                    else:
                        receipt = await self._async_transition(
                            receipt, InstallPhase.STAGING
                        )
                elif phase is InstallPhase.STAGING:
                    if local_artifact is None:
                        receipt = await self._async_recovery(receipt)
                        continue
                    try:
                        credential, receipt = await self._async_mutation_authority(
                            receipt, execution
                        )
                        staged = await async_stage_apk(
                            execution.adb_target,
                            credential.signer,
                            execution.descriptor,
                            _REMOTE_STAGING_SLOT_ID,
                            Path(local_artifact.path),
                            expected_root_mode=_root_mode(receipt),
                        )
                        _require_staged(staged, receipt.artifact)
                    except _CancellationObserved as err:
                        receipt = err.receipt
                    except AdbCredentialError, InstallNetworkError:
                        receipt = await self._async_cleanup_local_then_fail(
                            receipt,
                            execution.execution_id,
                            InstallResultCode.TRANSPORT_FAILED,
                        )
                    except InstallAdbError as err:
                        if err.code in _AMBIGUOUS_ADB_ERRORS:
                            receipt = await self._async_recovery(receipt)
                        else:
                            receipt = await self._async_cleanup_local_then_fail(
                                receipt,
                                execution.execution_id,
                                _stage_result(err),
                            )
                    else:
                        receipt = await self._async_transition(
                            receipt, InstallPhase.INSTALLING
                        )
                elif phase is InstallPhase.INSTALLING:
                    if staged is None:
                        receipt = await self._async_recovery(receipt)
                        continue
                    try:
                        credential, receipt = await self._async_mutation_authority(
                            receipt, execution
                        )
                        outcome = await async_install_staged_apk(
                            execution.adb_target,
                            credential.signer,
                            execution.descriptor,
                            _REMOTE_STAGING_SLOT_ID,
                            expected_root_mode=_root_mode(receipt),
                        )
                    except AdbCredentialError, InstallNetworkError:
                        receipt = await self._async_recovery(
                            receipt, InstallResultCode.VERIFICATION_REQUIRED
                        )
                    except InstallAdbError as err:
                        if err.code in _AMBIGUOUS_ADB_ERRORS:
                            receipt = await self._async_recovery(receipt)
                        else:
                            receipt = await self._async_recovery(
                                receipt, InstallResultCode.VERIFICATION_REQUIRED
                            )
                    else:
                        if outcome is InstallOutcome.REFUSED:
                            receipt = await self._async_install_refused(
                                receipt, execution, staged
                            )
                        elif outcome is InstallOutcome.INSTALLED:
                            receipt = await self._async_transition(
                                receipt, InstallPhase.INSTALLED
                            )
                        else:
                            receipt = await self._async_recovery(receipt)
                elif phase is InstallPhase.INSTALLED:
                    staged = _staged_from_receipt(receipt)
                    try:
                        await self._async_cleanup_local(execution.execution_id)
                    except ArtifactCustodyError:
                        receipt = await self._async_recovery(
                            receipt, InstallResultCode.VERIFICATION_REQUIRED
                        )
                    else:
                        receipt = await self._async_transition(
                            receipt, InstallPhase.LAUNCHING
                        )
                elif phase is InstallPhase.LAUNCHING:
                    if staged is None:
                        receipt = await self._async_recovery(receipt)
                        continue
                    receipt = await self._async_cleanup_and_launch(
                        receipt, execution, staged
                    )
                elif phase is InstallPhase.HEALTH_CHECK:
                    health = await self._async_health(execution)
                    if (
                        health is None
                        or health.version != receipt.artifact.version_name
                    ):
                        receipt = await self._async_recovery(
                            receipt, InstallResultCode.VERIFICATION_REQUIRED
                        )
                    else:
                        receipt = await self._async_transition(
                            receipt,
                            InstallPhase.HEALTHY_UNCLAIMED,
                            health_checked_at=_now_timestamp(),
                        )
                else:
                    raise InstallJobTransitionError
        except _PauseJob as err:
            if err.nonrestartable:
                self._nonrestartable_workers.add(job_id)
            return
        except asyncio.CancelledError:
            self._nonrestartable_workers.add(job_id)
            raise
        except (
            InstallJobRevisionError,
            InstallJobStoreError,
            InstallJobTransitionError,
        ):
            # Fresh durable authority was lost. Some transition failures can
            # occur immediately after an external mutation, so never replay
            # this worker in the same process or invent a definite outcome.
            self._nonrestartable_workers.add(job_id)

    async def _async_claim(
        self,
        receipt: InstallJobReceipt,
    ) -> InstallJobReceipt:
        """Claim once, cleaning exact local custody before terminalization."""
        try:
            return await self._manager.async_claim(receipt.job_id, receipt.revision)
        except InstallJobCleanupRequiredError:
            current = await self._manager.async_get(receipt.job_id)
            try:
                await self._async_cleanup_local(_execution_id(current))
            except ArtifactCustodyError:
                raise _PauseJob(nonrestartable=True) from None
            return await self._manager.async_claim(
                current.job_id,
                current.revision,
                cleanup_confirmed_revision=current.revision,
            )

    async def _async_preflight(
        self, receipt: InstallJobReceipt, execution: _FrozenExecution
    ) -> AdbPreflight:
        await _require_pin(self._hass, execution.pinned)
        credential = await self._async_current_credential(receipt)
        observed = await async_preflight_install(
            execution.adb_target, credential.signer, execution.descriptor
        )
        _require_preflight(observed, execution.adb_target)
        return observed

    async def _async_current_credential(
        self, receipt: InstallJobReceipt
    ) -> AdbCredential:
        credential = await async_get_durable_adb_credential(self._hass)
        if credential.generation_id != receipt.adb_credential_id:
            raise AdbCredentialError
        return credential

    async def _async_mutation_authority(
        self, receipt: InstallJobReceipt, execution: _FrozenExecution
    ) -> tuple[AdbCredential, InstallJobReceipt]:
        await _require_pin(self._hass, execution.pinned)
        current = await self._manager.async_get(receipt.job_id)
        if (
            current.revision != receipt.revision
            or current.phase is not receipt.phase
            or current.executor_generation != receipt.executor_generation
        ):
            if (
                current.phase is receipt.phase
                and current.executor_generation == receipt.executor_generation
                and current.cancel_requested
            ):
                raise _CancellationObserved(current)
            raise InstallJobRevisionError
        current = await self._manager.async_verify_mutation_barrier(
            current.job_id, current.revision, current.phase
        )
        credential = await self._async_current_credential(current)
        return credential, current

    async def _async_cleanup_authority(
        self, receipt: InstallJobReceipt, execution: _FrozenExecution
    ) -> tuple[AdbCredential, InstallJobReceipt]:
        await _require_pin(self._hass, execution.pinned)
        current = await self._manager.async_get(receipt.job_id)
        if (
            current.revision != receipt.revision
            or current.phase is not receipt.phase
            or current.executor_generation != receipt.executor_generation
        ):
            raise InstallJobRevisionError
        current = await self._manager.async_verify_cleanup_barrier(
            current.job_id, current.revision, current.phase
        )
        credential = await self._async_current_credential(current)
        return credential, current

    async def _async_ensure_local_artifact(
        self, receipt: InstallJobReceipt, execution: _FrozenExecution
    ) -> CustodiedArtifact:
        local = await async_download_install_artifact(
            self._hass,
            async_get_clientsession(self._hass),
            execution.release,
            execution.execution_id,
        )
        _require_local_artifact(local, execution.execution_id, receipt.artifact)
        return local

    async def _async_cleanup_local(self, execution_id: str) -> None:
        await async_cleanup_install_artifact(self._hass, execution_id)

    async def _async_clear_stale_partial(
        self, execution_id: str, *, already_removed: bool
    ) -> bool:
        """Remove one exact crash partial, with no same-process cleanup loop."""
        if already_removed:
            raise _PauseJob(nonrestartable=True)
        # The process singleton cannot race another download for this job, so
        # BUSY at a safe durable phase is residue from an earlier worker.
        try:
            await self._async_cleanup_local(execution_id)
        except ArtifactCustodyError:
            raise _PauseJob from None
        return True

    async def _async_cleanup_local_then_fail(
        self,
        receipt: InstallJobReceipt,
        execution_id: str,
        result: InstallResultCode,
    ) -> InstallJobReceipt:
        try:
            await self._async_cleanup_local(execution_id)
        except ArtifactCustodyError:
            if receipt.phase is InstallPhase.STAGING:
                return await self._async_recovery(
                    receipt, InstallResultCode.VERIFICATION_REQUIRED
                )
            raise _PauseJob from None
        return await self._async_fail(receipt, result)

    async def _async_install_refused(
        self,
        receipt: InstallJobReceipt,
        execution: _FrozenExecution,
        staged: StagedApk,
    ) -> InstallJobReceipt:
        try:
            credential, receipt = await self._async_cleanup_authority(
                receipt, execution
            )
            await async_cleanup_staged_apk(
                execution.adb_target,
                credential.signer,
                staged,
                DefiniteCleanupReason.INSTALL_REFUSED,
                expected_root_mode=_root_mode(receipt),
            )
            await self._async_cleanup_local(execution.execution_id)
        except (
            AdbCredentialError,
            ArtifactCustodyError,
            InstallNetworkError,
        ):
            return await self._async_recovery(
                receipt, InstallResultCode.VERIFICATION_REQUIRED
            )
        except InstallAdbError as err:
            return await self._async_recovery(receipt, _cleanup_result(err))
        return await self._async_fail(receipt, InstallResultCode.INSTALL_FAILED)

    async def _async_cleanup_and_launch(
        self,
        receipt: InstallJobReceipt,
        execution: _FrozenExecution,
        staged: StagedApk,
    ) -> InstallJobReceipt:
        try:
            credential, receipt = await self._async_cleanup_authority(
                receipt, execution
            )
            await async_cleanup_staged_apk(
                execution.adb_target,
                credential.signer,
                staged,
                DefiniteCleanupReason.INSTALL_SUCCEEDED,
                expected_root_mode=_root_mode(receipt),
            )
        except (
            AdbCredentialError,
            InstallNetworkError,
        ):
            return await self._async_recovery(
                receipt, InstallResultCode.VERIFICATION_REQUIRED
            )
        except InstallAdbError as err:
            return await self._async_recovery(receipt, _cleanup_result(err))
        try:
            credential, receipt = await self._async_mutation_authority(
                receipt, execution
            )
            outcome = await async_launch_installed_app(
                execution.adb_target,
                credential.signer,
                execution.descriptor,
                expected_root_mode=_root_mode(receipt),
            )
        except AdbCredentialError, InstallNetworkError:
            return await self._async_fail(receipt, InstallResultCode.LAUNCH_FAILED)
        except InstallAdbError as err:
            if err.code in _AMBIGUOUS_ADB_ERRORS:
                return await self._async_recovery(receipt)
            if err.code in {
                InstallAdbErrorCode.TARGET_CHANGED,
                InstallAdbErrorCode.ROOT_MODE_CHANGED,
            }:
                return await self._async_recovery(
                    receipt, InstallResultCode.VERIFICATION_REQUIRED
                )
            return await self._async_fail(receipt, InstallResultCode.LAUNCH_FAILED)
        if outcome is LaunchOutcome.STARTED:
            return await self._async_transition(receipt, InstallPhase.HEALTH_CHECK)
        if outcome is LaunchOutcome.REFUSED:
            return await self._async_fail(receipt, InstallResultCode.LAUNCH_FAILED)
        return await self._async_recovery(receipt)

    async def _async_health(self, execution: _FrozenExecution) -> PanelHealth | None:
        client = HaPaneldClient(
            async_get_clientsession(self._hass), execution.pinned.pinned
        )
        for attempt in range(_HEALTH_ATTEMPTS):
            try:
                await _require_pin(self._hass, execution.pinned)
                return await client.async_get_health()
            except InstallNetworkError:
                return None
            except CannotConnectError, InvalidResponseError:
                if attempt + 1 < _HEALTH_ATTEMPTS:
                    await asyncio.sleep(_HEALTH_RETRY_SECONDS)
        return None

    async def _async_cancel_requested(
        self,
        receipt: InstallJobReceipt,
        local_artifact: CustodiedArtifact | None,
        staged: StagedApk | None,
    ) -> InstallJobReceipt:
        execution = _frozen_execution(receipt)
        if receipt.phase is InstallPhase.STAGING and staged is not None:
            try:
                credential, receipt = await self._async_cleanup_authority(
                    receipt, execution
                )
                await async_cleanup_staged_apk(
                    execution.adb_target,
                    credential.signer,
                    staged,
                    DefiniteCleanupReason.CANCELLED,
                    expected_root_mode=_root_mode(receipt),
                )
            except (
                AdbCredentialError,
                InstallNetworkError,
            ):
                return await self._async_recovery(
                    receipt, InstallResultCode.VERIFICATION_REQUIRED
                )
            except InstallAdbError as err:
                return await self._async_recovery(receipt, _cleanup_result(err))
        if (
            local_artifact is not None
            or receipt.actual_apk_bytes is not None
            or receipt.phase is InstallPhase.DOWNLOADING
        ):
            try:
                await self._async_cleanup_local(execution.execution_id)
            except ArtifactCustodyError:
                if receipt.phase in {
                    InstallPhase.STAGING,
                    InstallPhase.INSTALLING,
                    InstallPhase.INSTALLED,
                    InstallPhase.LAUNCHING,
                    InstallPhase.HEALTH_CHECK,
                    InstallPhase.HEALTHY_UNCLAIMED,
                }:
                    return await self._async_recovery(
                        receipt, InstallResultCode.VERIFICATION_REQUIRED
                    )
                raise _PauseJob from None
        result = (
            InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP
            if receipt.phase is InstallPhase.STAGING
            else InstallResultCode.CANCELLED_BY_USER
        )
        return await self._async_transition(
            receipt, InstallPhase.CANCELLED, result_code=result
        )

    async def _async_transition(
        self,
        receipt: InstallJobReceipt,
        phase: InstallPhase,
        *,
        actual_apk_bytes: int | None = None,
        health_checked_at: str | None = None,
        result_code: InstallResultCode | None = None,
        preflight_root_mode: str | None = None,
    ) -> InstallJobReceipt:
        try:
            root_mode = (
                {}
                if preflight_root_mode is None
                else {"preflight_root_mode": preflight_root_mode}
            )
            return await self._manager.async_transition(
                receipt.job_id,
                receipt.revision,
                phase,
                actual_apk_bytes=actual_apk_bytes,
                health_checked_at=health_checked_at,
                result_code=result_code,
                **root_mode,
            )
        except InstallJobRevisionError:
            current = await self._manager.async_get(receipt.job_id)
            if (
                current.phase is receipt.phase
                and current.executor_generation == receipt.executor_generation
                and current.cancel_requested
            ):
                return current
            raise

    async def _async_fail(
        self, receipt: InstallJobReceipt, result: InstallResultCode
    ) -> InstallJobReceipt:
        current = await self._manager.async_get(receipt.job_id)
        if current.is_terminal:
            return current
        return await self._manager.async_transition(
            current.job_id,
            current.revision,
            InstallPhase.FAILED,
            result_code=result,
        )

    async def _async_recovery(
        self,
        receipt: InstallJobReceipt,
        result: InstallResultCode = InstallResultCode.AMBIGUOUS_MUTATION,
    ) -> InstallJobReceipt:
        current = await self._manager.async_get(receipt.job_id)
        if current.is_terminal:
            return current
        try:
            # These bytes are reproducible from the frozen signed digest and
            # are not evidence of the panel-side mutation outcome. Cleanup is
            # restricted to this receipt's derived private custody ID.
            await self._async_cleanup_local(_execution_id(current))
        except ArtifactCustodyError:
            # The mutation truth remains in its current durable phase.  A new
            # process will remove local custody before it claims an ambiguous
            # phase, so a cleanup failure can never enable same-process replay.
            raise _PauseJob(nonrestartable=True) from None
        current = await self._manager.async_get(receipt.job_id)
        if current.is_terminal:
            return current
        return await self._manager.async_transition(
            current.job_id,
            current.revision,
            InstallPhase.RECOVERY_REQUIRED,
            result_code=result,
        )


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _execution_id(receipt: InstallJobReceipt) -> str:
    digest = sha256()
    digest.update(_EXECUTION_ID_DOMAIN)
    digest.update(bytes.fromhex(receipt.job_id))
    digest.update(bytes.fromhex(receipt.plan_sha256))
    return digest.hexdigest()[:32]


def _descriptor(artifact: InstallArtifact) -> InstallDescriptor:
    return InstallDescriptor(
        schema=artifact.descriptor_schema,
        release_tag=artifact.release_tag,
        version_name=artifact.version_name,
        version_code=artifact.version_code,
        apk_name=artifact.apk_name,
        apk_size=artifact.apk_size,
        apk_sha256=artifact.apk_sha256,
        package_id=artifact.package_id,
        signer_certificate_sha256=artifact.signer_certificate_sha256,
        min_sdk=artifact.min_sdk,
        supported_abis=artifact.supported_abis,
        database_compatibility=artifact.database_compatibility,
        launch_component=artifact.launch_component,
    )


def _frozen_execution(receipt: InstallJobReceipt) -> _FrozenExecution:
    original = normalize_address(receipt.target.address)
    pinned_address = normalize_address(receipt.target.pinned_address)
    pinned = PinnedPanelTarget(original=original, pinned=pinned_address)
    descriptor = _descriptor(receipt.artifact)
    execution_id = _execution_id(receipt)
    return _FrozenExecution(
        pinned=pinned,
        adb_target=AdbInstallTarget(
            address=pinned_address,
            serial=receipt.target.adb_serial,
            model=receipt.target.model,
            primary_abi=receipt.target.primary_abi,
            android_sdk=receipt.target.android_sdk,
        ),
        descriptor=descriptor,
        release=ReleaseArtifact(
            tag=receipt.artifact.release_tag,
            version=receipt.artifact.version_name,
            apk_name=receipt.artifact.apk_name,
            apk_url=(
                f"{_RELEASE_DOWNLOAD_ROOT}/{receipt.artifact.release_tag}/"
                f"{receipt.artifact.apk_name}"
            ),
            sha256=receipt.artifact.apk_sha256,
            descriptor=descriptor,
        ),
        execution_id=execution_id,
    )


async def _require_pin(hass: HomeAssistant, pinned: PinnedPanelTarget) -> None:
    revalidated = await async_revalidate_install_target(hass, pinned)
    if revalidated != pinned:
        raise InstallNetworkError(InstallNetworkErrorCode.PINNED_TARGET_REMOVED)


def _require_preflight(observed: AdbPreflight, target: AdbInstallTarget) -> None:
    if (
        not isinstance(observed, AdbPreflight)
        or observed.serial != target.serial
        or observed.model != target.model
        or observed.primary_abi != target.primary_abi
        or observed.android_sdk != target.android_sdk
        or observed.root_mode not in {AdbRootMode.ROOT_ADBD, AdbRootMode.ROOTLESS}
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_CHANGED)


def _require_local_artifact(
    local: CustodiedArtifact, execution_id: str, artifact: InstallArtifact
) -> None:
    if (
        not isinstance(local, CustodiedArtifact)
        or local.job_id != execution_id
        or local.size != artifact.apk_size
        or local.sha256 != artifact.apk_sha256
        or not isinstance(local.path, str)
        or not local.path
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.READY_INVALID)


def _require_staged(staged: StagedApk, artifact: InstallArtifact) -> None:
    expected = _staged(artifact)
    if staged != expected:
        raise InstallAdbError(InstallAdbErrorCode.STAGE_VERIFICATION_FAILED)


def _staged(artifact: InstallArtifact) -> StagedApk:
    return StagedApk(
        job_id=_REMOTE_STAGING_SLOT_ID,
        remote_path=f"{_REMOTE_STAGING_PREFIX}{_REMOTE_STAGING_SLOT_ID}.apk",
        apk_size=artifact.apk_size,
        apk_sha256=artifact.apk_sha256,
    )


def _staged_from_receipt(receipt: InstallJobReceipt) -> StagedApk:
    if receipt.phase is not InstallPhase.INSTALLED:
        raise InstallJobTransitionError
    return _staged(receipt.artifact)


def _root_mode(receipt: InstallJobReceipt) -> AdbRootMode:
    stored_root_mode = receipt.preflight_root_mode
    if stored_root_mode is None:
        raise InstallJobStoreError
    try:
        return AdbRootMode(stored_root_mode)
    except ValueError:
        raise InstallJobStoreError from None


def _preflight_result(error: InstallAdbError) -> InstallResultCode:
    if error.code in _PREFLIGHT_REJECTIONS:
        return InstallResultCode.PREFLIGHT_REJECTED
    return InstallResultCode.TRANSPORT_FAILED


def _stage_result(error: InstallAdbError) -> InstallResultCode:
    if error.code in {
        InstallAdbErrorCode.INVALID_REQUEST,
        InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID,
        InstallAdbErrorCode.FILESYNC_UNSAFE,
        InstallAdbErrorCode.STAGING_PATH_OCCUPIED,
    }:
        return InstallResultCode.ARTIFACT_REJECTED
    return InstallResultCode.TRANSPORT_FAILED


def _cleanup_result(error: InstallAdbError) -> InstallResultCode:
    if error.code is InstallAdbErrorCode.CLEANUP_AMBIGUOUS:
        return InstallResultCode.AMBIGUOUS_MUTATION
    return InstallResultCode.VERIFICATION_REQUIRED


def _artifact_result(error: ArtifactCustodyError) -> InstallResultCode:
    if error.code in _ARTIFACT_REJECTIONS:
        return InstallResultCode.ARTIFACT_REJECTED
    return InstallResultCode.TRANSPORT_FAILED


async def async_get_install_executor(hass: HomeAssistant) -> InstallExecutor:
    """Return the one installer task owner for this Home Assistant process."""
    lock = hass.data.get(_EXECUTOR_LOCK_DATA_KEY)
    if lock is None:
        lock = asyncio.Lock()
        hass.data[_EXECUTOR_LOCK_DATA_KEY] = lock
    if not isinstance(lock, asyncio.Lock):
        raise InstallJobStoreError
    async with lock:
        executor = hass.data.get(_EXECUTOR_DATA_KEY)
        if executor is None:
            manager = await async_get_install_job_manager(hass)
            receipts = await manager.async_list()
            await async_reconcile_install_artifacts(
                hass,
                frozenset(
                    _execution_id(receipt)
                    for receipt in receipts
                    if not receipt.is_terminal
                ),
            )
            executor = InstallExecutor(hass, manager)
            hass.data[_EXECUTOR_DATA_KEY] = executor
        if not isinstance(executor, InstallExecutor):
            raise InstallJobStoreError
        return executor


async def async_resume_loaded_install_jobs(hass: HomeAssistant) -> tuple[str, ...]:
    """Resume safe jobs only after an existing config entry loaded the domain."""
    return await (await async_get_install_executor(hass)).async_resume_loaded_jobs()
