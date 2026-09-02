"""Durable, bounded receipts for first-install work.

This module deliberately owns state only.  Network, ADB, download, and package
installation work belongs to an executor which must advance these receipts with
revision compare-and-swap operations.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.ulid import bytes_to_ulid, ulid_to_bytes_or_none

from .client import InvalidAddressError, normalize_address
from .const import DOMAIN
from .install_network import (
    InstallNetworkError,
    _resolver_hostname,
    is_allowed_install_address,
)

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}.install_jobs"
_MANAGER_DATA_KEY = f"{DOMAIN}.install_job_manager"
_LOCK_DATA_KEY = f"{DOMAIN}.install_job_store_lock"
_FORMAT = "ha-paneld-install-jobs-v1"
_PLAN_SCHEMA = "io.github.maxlyth.hapaneld.install-plan.v1"
_MAX_STORE_BYTES = 128 * 1024
_MAX_ACTIVE_JOBS = 4
_MAX_TERMINAL_JOBS = 32
_TERMINAL_RETENTION = timedelta(days=7)
_MAX_ATTEMPTS = 32
_MAX_EXECUTOR_GENERATION = 2**31 - 1
_MAX_APK_BYTES = 64 * 1024 * 1024
_MAX_SDK = 100
_MAX_ADDRESS_LENGTH = 255
_MAX_MODEL_LENGTH = 128
_MAX_RELEASE_TEXT_LENGTH = 128
_MAX_RELEASE_TAG_LENGTH = 64
_MAX_APK_NAME_LENGTH = 255
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADB_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", flags=re.ASCII)
_ABI = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", flags=re.ASCII)
_APK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.apk$")
_RELEASE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DATABASE_COMPATIBILITY = re.compile(
    r"^hapaneld-db:v1:ha-paneld\.db:([1-9][0-9]*):([1-9][0-9]*)$"
)
_DESCRIPTOR_SCHEMA = "io.github.maxlyth.hapaneld.install.v1"
_PACKAGE_ID = "io.github.maxlyth.hapaneld"
_LAUNCH_COMPONENT = f"{_PACKAGE_ID}/.MainActivity"
_RELEASE_SIGNER_SHA256 = (
    "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
)
_SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a")


class InstallJobError(Exception):
    """Base error for durable install-job state."""


class InstallJobStoreError(InstallJobError):
    """The receipt store was absent, corrupt, or could not be verified."""


class InstallJobConflictError(InstallJobError):
    """A target already has different active frozen installation facts."""


class InstallJobCapacityError(InstallJobError):
    """The bounded active-job capacity has been reached."""


class InstallJobNotFoundError(InstallJobError):
    """No receipt exists for the supplied opaque identifier."""


class InstallJobRevisionError(InstallJobError):
    """The receipt revision did not match the caller's expected revision."""


class InstallJobTransitionError(InstallJobError):
    """A requested receipt transition or field update is invalid."""


class InstallPhase(StrEnum):
    """Durable phases for the first-install workflow."""

    APPROVED = "approved"
    AUTHORIZING = "authorizing"
    PREFLIGHT = "preflight"
    DOWNLOADING = "downloading"
    ARTIFACT_READY = "artifact_ready"
    REVALIDATING = "revalidating"
    STAGING = "staging"
    INSTALLING = "installing"
    INSTALLED = "installed"
    LAUNCHING = "launching"
    HEALTH_CHECK = "health_check"
    HEALTHY_UNCLAIMED = "healthy_unclaimed"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class InstallResultCode(StrEnum):
    """Privacy-safe terminal outcomes; raw exception text is never persisted."""

    ENTRY_CREATED = "entry_created"
    CANCELLED_BY_USER = "cancelled_by_user"
    CANCELLED_AFTER_STAGING_CLEANUP = "cancelled_after_staging_cleanup"
    AUTHORIZATION_FAILED = "authorization_failed"
    PREFLIGHT_REJECTED = "preflight_rejected"
    ARTIFACT_REJECTED = "artifact_rejected"
    TRANSPORT_FAILED = "transport_failed"
    INSTALL_FAILED = "install_failed"
    LAUNCH_FAILED = "launch_failed"
    HEALTH_CHECK_FAILED = "health_check_failed"
    AMBIGUOUS_MUTATION = "ambiguous_mutation"
    VERIFICATION_REQUIRED = "verification_required"


_TERMINAL_PHASES = frozenset(
    {
        InstallPhase.CONSUMED,
        InstallPhase.CANCELLED,
        InstallPhase.FAILED,
        InstallPhase.RECOVERY_REQUIRED,
    }
)
_MUTATION_BARRIERS = frozenset(
    {InstallPhase.STAGING, InstallPhase.INSTALLING, InstallPhase.LAUNCHING}
)
_CLEANUP_BARRIER_CANCEL_POLARITY: Mapping[InstallPhase, bool] = {
    InstallPhase.STAGING: True,
    InstallPhase.INSTALLING: False,
    InstallPhase.LAUNCHING: False,
}
_ARTIFACT_REQUIRED_PHASES = frozenset(
    {
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
        InstallPhase.HEALTHY_UNCLAIMED,
        InstallPhase.CONSUMED,
    }
)
_CANCELLABLE_PHASES = frozenset(
    {
        InstallPhase.APPROVED,
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
    }
)
_FAILURE_CODES = frozenset(
    {
        InstallResultCode.AUTHORIZATION_FAILED,
        InstallResultCode.PREFLIGHT_REJECTED,
        InstallResultCode.ARTIFACT_REJECTED,
        InstallResultCode.TRANSPORT_FAILED,
        InstallResultCode.INSTALL_FAILED,
        InstallResultCode.LAUNCH_FAILED,
        InstallResultCode.HEALTH_CHECK_FAILED,
    }
)
_RECOVERY_CODES = frozenset(
    {
        InstallResultCode.AMBIGUOUS_MUTATION,
        InstallResultCode.VERIFICATION_REQUIRED,
    }
)
_FAILURE_CODES_BY_PHASE: Mapping[InstallPhase, frozenset[InstallResultCode]] = {
    InstallPhase.AUTHORIZING: frozenset(
        {
            InstallResultCode.AUTHORIZATION_FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.PREFLIGHT: frozenset(
        {
            InstallResultCode.PREFLIGHT_REJECTED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.DOWNLOADING: frozenset(
        {
            InstallResultCode.ARTIFACT_REJECTED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.ARTIFACT_READY: frozenset({InstallResultCode.ARTIFACT_REJECTED}),
    InstallPhase.REVALIDATING: frozenset(
        {
            InstallResultCode.PREFLIGHT_REJECTED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.STAGING: frozenset(
        {
            InstallResultCode.ARTIFACT_REJECTED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.INSTALLING: frozenset({InstallResultCode.INSTALL_FAILED}),
    InstallPhase.INSTALLED: frozenset({InstallResultCode.INSTALL_FAILED}),
    InstallPhase.LAUNCHING: frozenset(
        {
            InstallResultCode.LAUNCH_FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
    InstallPhase.HEALTH_CHECK: frozenset(
        {
            InstallResultCode.HEALTH_CHECK_FAILED,
            InstallResultCode.TRANSPORT_FAILED,
        }
    ),
}
_RECOVERY_SOURCE_PHASES = frozenset(
    {
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
        InstallPhase.HEALTHY_UNCLAIMED,
    }
)
_RESTART_AMBIGUOUS_PHASES = frozenset(
    {
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.LAUNCHING,
    }
)
_NEXT_PHASE: Mapping[InstallPhase, frozenset[InstallPhase]] = {
    InstallPhase.APPROVED: frozenset(
        {InstallPhase.AUTHORIZING, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.AUTHORIZING: frozenset(
        {InstallPhase.PREFLIGHT, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.PREFLIGHT: frozenset(
        {InstallPhase.DOWNLOADING, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.DOWNLOADING: frozenset(
        {InstallPhase.ARTIFACT_READY, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.ARTIFACT_READY: frozenset(
        {InstallPhase.REVALIDATING, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.REVALIDATING: frozenset(
        {InstallPhase.STAGING, InstallPhase.CANCELLED, InstallPhase.FAILED}
    ),
    InstallPhase.STAGING: frozenset(
        {
            InstallPhase.INSTALLING,
            InstallPhase.CANCELLED,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }
    ),
    InstallPhase.INSTALLING: frozenset(
        {
            InstallPhase.INSTALLED,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }
    ),
    InstallPhase.INSTALLED: frozenset(
        {
            InstallPhase.LAUNCHING,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }
    ),
    InstallPhase.LAUNCHING: frozenset(
        {
            InstallPhase.HEALTH_CHECK,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }
    ),
    InstallPhase.HEALTH_CHECK: frozenset(
        {
            InstallPhase.HEALTHY_UNCLAIMED,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }
    ),
    InstallPhase.HEALTHY_UNCLAIMED: frozenset(
        {InstallPhase.CONSUMED, InstallPhase.RECOVERY_REQUIRED}
    ),
}


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """Frozen identity and compatibility facts for one Android target."""

    address: str
    pinned_address: str
    adb_serial: str
    model: str
    primary_abi: str
    android_sdk: int


@dataclass(frozen=True, slots=True)
class InstallArtifact:
    """Authenticated install descriptor retained without a download URL."""

    descriptor_schema: str
    release_tag: str
    version_name: str
    version_code: int
    apk_name: str
    apk_sha256: str
    apk_size: int
    package_id: str
    signer_certificate_sha256: str
    min_sdk: int
    supported_abis: tuple[str, ...]
    database_compatibility: str
    launch_component: str


@dataclass(frozen=True, slots=True)
class InstallJobReceipt:
    """One immutable snapshot of a durable first-install job."""

    job_id: str
    revision: int
    executor_generation: int
    created_at: str
    updated_at: str
    phase: InstallPhase
    cancel_requested: bool
    attempt: int
    target: InstallTarget
    artifact: InstallArtifact
    plan_sha256: str
    adb_credential_id: str
    actual_apk_bytes: int | None = None
    health_checked_at: str | None = None
    result_code: InstallResultCode | None = None
    consumed_entry_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Return whether no executor may advance this receipt."""
        return self.phase in _TERMINAL_PHASES


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_text(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InstallJobStoreError
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstallJobStoreError
    if not minimum <= value <= maximum:
        raise InstallJobStoreError
    return value


def _timestamp(value: object) -> str:
    text = _safe_text(value, 40)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise InstallJobStoreError from err
    if parsed.tzinfo != UTC or parsed.isoformat(timespec="seconds") != text:
        raise InstallJobStoreError
    return text


def _parse_target(value: object) -> InstallTarget:
    if not isinstance(value, dict) or value.keys() != {
        "address",
        "pinned_address",
        "adb_serial",
        "model",
        "primary_abi",
        "android_sdk",
    }:
        raise InstallJobStoreError
    address = _safe_text(value["address"], _MAX_ADDRESS_LENGTH)
    pinned_address = _safe_text(value["pinned_address"], _MAX_ADDRESS_LENGTH)
    try:
        original = normalize_address(address)
        pinned = normalize_address(pinned_address)
        pinned_ip = ipaddress.ip_address(pinned.host)
        if (
            original.stored_value != address
            or pinned.stored_value != pinned_address
            or pinned.host != str(pinned_ip)
            or pinned.port != original.port
            or not is_allowed_install_address(pinned_ip)
        ):
            raise InstallJobStoreError
        try:
            original_ip = ipaddress.ip_address(original.host)
        except ValueError:
            _resolver_hostname(original.host)
        else:
            if original_ip != pinned_ip:
                raise InstallJobStoreError
    except (InstallNetworkError, InvalidAddressError, ValueError) as err:
        raise InstallJobStoreError from err
    model = _safe_text(value["model"], _MAX_MODEL_LENGTH)
    adb_serial = _safe_text(value["adb_serial"], 128)
    primary_abi = _safe_text(value["primary_abi"], 64)
    if (
        not model.isprintable()
        or _ADB_SERIAL.fullmatch(adb_serial) is None
        or _ABI.fullmatch(primary_abi) is None
    ):
        raise InstallJobStoreError
    return InstallTarget(
        address=address,
        pinned_address=pinned_address,
        adb_serial=adb_serial,
        model=model,
        primary_abi=primary_abi,
        android_sdk=_integer(value["android_sdk"], 1, _MAX_SDK),
    )


def _parse_artifact(value: object) -> InstallArtifact:
    if not isinstance(value, dict) or value.keys() != {
        "descriptor_schema",
        "release_tag",
        "version_name",
        "version_code",
        "apk_name",
        "apk_sha256",
        "apk_size",
        "package_id",
        "signer_certificate_sha256",
        "min_sdk",
        "supported_abis",
        "database_compatibility",
        "launch_component",
    }:
        raise InstallJobStoreError
    release_tag = _safe_text(value["release_tag"], _MAX_RELEASE_TEXT_LENGTH)
    version_name = _safe_text(value["version_name"], _MAX_RELEASE_TEXT_LENGTH)
    apk_name = _safe_text(value["apk_name"], _MAX_APK_NAME_LENGTH)
    apk_sha256 = _safe_text(value["apk_sha256"], 64)
    signer = _safe_text(value["signer_certificate_sha256"], 64)
    abis = value["supported_abis"]
    database = _safe_text(value["database_compatibility"], 128)
    database_match = _DATABASE_COMPATIBILITY.fullmatch(database)
    if (
        value["descriptor_schema"] != _DESCRIPTOR_SCHEMA
        or value["package_id"] != _PACKAGE_ID
        or value["launch_component"] != _LAUNCH_COMPONENT
        or signer != _RELEASE_SIGNER_SHA256
        or not isinstance(abis, (list, tuple))
        or tuple(abis) != _SUPPORTED_ABIS
        or len(release_tag) > _MAX_RELEASE_TAG_LENGTH
        or _RELEASE_TAG.fullmatch(release_tag) is None
        or version_name != release_tag.removeprefix("v")
        or apk_name != f"ha-paneld-{release_tag}-manual-setup-required.apk"
        or _APK_NAME.fullmatch(apk_name) is None
        or _SHA256.fullmatch(apk_sha256) is None
        or database_match is None
        or any(len(group) > 10 for group in database_match.groups())
        or int(database_match.group(1)) > int(database_match.group(2))
        or int(database_match.group(2)) > 2**31 - 1
    ):
        raise InstallJobStoreError
    return InstallArtifact(
        descriptor_schema=_DESCRIPTOR_SCHEMA,
        release_tag=release_tag,
        version_name=version_name,
        version_code=_integer(value["version_code"], 1, 2**31 - 1),
        apk_name=apk_name,
        apk_sha256=apk_sha256,
        apk_size=_integer(value["apk_size"], 1, _MAX_APK_BYTES),
        package_id=_PACKAGE_ID,
        signer_certificate_sha256=signer,
        min_sdk=_integer(value["min_sdk"], 1, _MAX_SDK),
        supported_abis=_SUPPORTED_ABIS,
        database_compatibility=database,
        launch_component=_LAUNCH_COMPONENT,
    )


def install_plan_sha256(
    target: InstallTarget,
    artifact: InstallArtifact,
    adb_credential_id: str,
) -> str:
    """Bind every frozen mutation input into one canonical plan digest."""
    parsed_target = _parse_target(asdict(target))
    parsed_artifact = _parse_artifact(asdict(artifact))
    credential_id = _safe_text(adb_credential_id, 64)
    if _SHA256.fullmatch(credential_id) is None:
        raise InstallJobStoreError
    document = {
        "schema": _PLAN_SCHEMA,
        "target": asdict(parsed_target),
        "artifact": asdict(parsed_artifact),
        "adb_credential_id": credential_id,
    }
    canonical = (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    return sha256(canonical).hexdigest()


def _is_config_entry_id(value: str) -> bool:
    """Accept both legacy hexadecimal IDs and canonical current HA ULIDs."""
    if _HEX_32.fullmatch(value) is not None:
        return True
    if len(value) != 26:
        return False
    decoded = ulid_to_bytes_or_none(value)
    return decoded is not None and bytes_to_ulid(decoded) == value


def _parse_receipt(value: object) -> InstallJobReceipt:
    if not isinstance(value, dict) or value.keys() != {
        "job_id",
        "revision",
        "executor_generation",
        "created_at",
        "updated_at",
        "phase",
        "cancel_requested",
        "attempt",
        "target",
        "artifact",
        "plan_sha256",
        "adb_credential_id",
        "actual_apk_bytes",
        "health_checked_at",
        "result_code",
        "consumed_entry_id",
    }:
        raise InstallJobStoreError
    job_id = _safe_text(value["job_id"], 32)
    plan_sha256 = _safe_text(value["plan_sha256"], 64)
    credential_id = _safe_text(value["adb_credential_id"], 64)
    if (
        _HEX_32.fullmatch(job_id) is None
        or _SHA256.fullmatch(plan_sha256) is None
        or _SHA256.fullmatch(credential_id) is None
    ):
        raise InstallJobStoreError
    try:
        phase = InstallPhase(value["phase"])
        result = (
            None
            if value["result_code"] is None
            else InstallResultCode(value["result_code"])
        )
    except (TypeError, ValueError) as err:
        raise InstallJobStoreError from err
    cancel_requested = value["cancel_requested"]
    if not isinstance(cancel_requested, bool):
        raise InstallJobStoreError
    actual = value["actual_apk_bytes"]
    if actual is not None:
        actual = _integer(actual, 1, _MAX_APK_BYTES)
    health = value["health_checked_at"]
    if health is not None:
        health = _timestamp(health)
    entry_id = value["consumed_entry_id"]
    if entry_id is not None:
        entry_id = _safe_text(entry_id, 32)
        if not _is_config_entry_id(entry_id):
            raise InstallJobStoreError
    receipt = InstallJobReceipt(
        job_id=job_id,
        revision=_integer(value["revision"], 0, 2**63 - 1),
        executor_generation=_integer(
            value["executor_generation"], 0, _MAX_EXECUTOR_GENERATION
        ),
        created_at=_timestamp(value["created_at"]),
        updated_at=_timestamp(value["updated_at"]),
        phase=phase,
        cancel_requested=cancel_requested,
        attempt=_integer(value["attempt"], 0, _MAX_ATTEMPTS),
        target=_parse_target(value["target"]),
        artifact=_parse_artifact(value["artifact"]),
        plan_sha256=plan_sha256,
        adb_credential_id=credential_id,
        actual_apk_bytes=actual,
        health_checked_at=health,
        result_code=result,
        consumed_entry_id=entry_id,
    )
    _validate_receipt_invariants(receipt)
    return receipt


def _validate_receipt_invariants(receipt: InstallJobReceipt) -> None:
    if receipt.updated_at < receipt.created_at:
        raise InstallJobStoreError
    if (
        receipt.actual_apk_bytes is not None
        and receipt.actual_apk_bytes != receipt.artifact.apk_size
    ):
        raise InstallJobStoreError
    if receipt.plan_sha256 != install_plan_sha256(
        receipt.target,
        receipt.artifact,
        receipt.adb_credential_id,
    ):
        raise InstallJobStoreError
    if (
        receipt.target.primary_abi not in receipt.artifact.supported_abis
        or receipt.target.android_sdk < receipt.artifact.min_sdk
    ):
        raise InstallJobStoreError
    if (
        receipt.attempt != receipt.executor_generation
        or receipt.revision < receipt.executor_generation
    ):
        raise InstallJobStoreError
    if receipt.phase in _ARTIFACT_REQUIRED_PHASES and receipt.actual_apk_bytes is None:
        raise InstallJobStoreError
    if (
        receipt.phase not in _ARTIFACT_REQUIRED_PHASES
        and receipt.phase not in _TERMINAL_PHASES
        and receipt.actual_apk_bytes is not None
    ):
        raise InstallJobStoreError
    if receipt.phase == InstallPhase.CONSUMED:
        if (
            receipt.result_code != InstallResultCode.ENTRY_CREATED
            or receipt.consumed_entry_id is None
            or receipt.health_checked_at is None
        ):
            raise InstallJobStoreError
    elif receipt.consumed_entry_id is not None:
        raise InstallJobStoreError
    if receipt.phase == InstallPhase.CANCELLED:
        if (
            receipt.result_code
            not in {
                InstallResultCode.CANCELLED_BY_USER,
                InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP,
            }
            or not receipt.cancel_requested
        ):
            raise InstallJobStoreError
    elif receipt.phase == InstallPhase.FAILED:
        if receipt.result_code not in _FAILURE_CODES:
            raise InstallJobStoreError
    elif receipt.phase == InstallPhase.RECOVERY_REQUIRED:
        if receipt.result_code not in _RECOVERY_CODES:
            raise InstallJobStoreError
    elif receipt.phase != InstallPhase.CONSUMED and receipt.result_code is not None:
        raise InstallJobStoreError
    if receipt.phase in {
        InstallPhase.HEALTHY_UNCLAIMED,
        InstallPhase.CONSUMED,
    }:
        if receipt.health_checked_at is None:
            raise InstallJobStoreError
    elif receipt.health_checked_at is not None:
        raise InstallJobStoreError
    if (
        receipt.health_checked_at is not None
        and not receipt.created_at <= receipt.health_checked_at <= receipt.updated_at
    ):
        raise InstallJobStoreError


def _serialize_receipt(receipt: InstallJobReceipt) -> dict[str, Any]:
    data = asdict(receipt)
    data["phase"] = receipt.phase.value
    data["artifact"]["supported_abis"] = list(receipt.artifact.supported_abis)
    data["result_code"] = (
        None if receipt.result_code is None else receipt.result_code.value
    )
    return data


def _parse_document(value: object) -> dict[str, InstallJobReceipt]:
    if not isinstance(value, dict) or value.keys() != {"format", "jobs"}:
        raise InstallJobStoreError
    jobs = value["jobs"]
    if value["format"] != _FORMAT or not isinstance(jobs, list):
        raise InstallJobStoreError
    if len(jobs) > _MAX_ACTIVE_JOBS + _MAX_TERMINAL_JOBS:
        raise InstallJobStoreError
    parsed: dict[str, InstallJobReceipt] = {}
    active_count = 0
    terminal_count = 0
    active_addresses: set[str] = set()
    active_pins: set[str] = set()
    active_serials: set[str] = set()
    for raw_job in jobs:
        receipt = _parse_receipt(raw_job)
        if receipt.job_id in parsed:
            raise InstallJobStoreError
        parsed[receipt.job_id] = receipt
        if receipt.is_terminal:
            terminal_count += 1
        else:
            active_count += 1
            if (
                receipt.target.address in active_addresses
                or receipt.target.pinned_address in active_pins
                or receipt.target.adb_serial in active_serials
            ):
                raise InstallJobStoreError
            active_addresses.add(receipt.target.address)
            active_pins.add(receipt.target.pinned_address)
            active_serials.add(receipt.target.adb_serial)
    if active_count > _MAX_ACTIVE_JOBS or terminal_count > _MAX_TERMINAL_JOBS:
        raise InstallJobStoreError
    return parsed


def _serialize_document(
    jobs: Mapping[str, InstallJobReceipt],
) -> dict[str, Any]:
    return {
        "format": _FORMAT,
        "jobs": [
            _serialize_receipt(receipt)
            for receipt in sorted(
                jobs.values(), key=lambda item: (item.created_at, item.job_id)
            )
        ],
    }


def _store_presence(path_text: str) -> tuple[bool, bool]:
    path = Path(path_text)
    try:
        exists = os.path.lexists(path)
        if exists:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o777 != 0o600
                or metadata.st_size > _MAX_STORE_BYTES
            ):
                raise InstallJobStoreError
        corrupt = path.parent.exists() and any(
            candidate.name.startswith(f"{path.name}.corrupt.")
            for candidate in path.parent.iterdir()
        )
    except OSError as err:
        raise InstallJobStoreError from err
    return exists, corrupt


class InstallJobManager:
    """Serialize and verify all changes to bounded durable install receipts."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        now: Callable[[], str] = _now_timestamp,
    ) -> None:
        self._hass = hass
        self._now = now
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _STORE_VERSION,
            _STORE_KEY,
            private=True,
            atomic_writes=True,
        )
        lock = hass.data.get(_LOCK_DATA_KEY)
        if lock is None:
            lock = asyncio.Lock()
            hass.data[_LOCK_DATA_KEY] = lock
        if not isinstance(lock, asyncio.Lock):
            raise InstallJobStoreError
        self._lock = lock
        self._jobs: dict[str, InstallJobReceipt] | None = None
        self._claimed_jobs: dict[str, int] = {}

    def _invalidate_cache(self) -> None:
        self._jobs = None
        self._claimed_jobs.clear()

    def _reconcile_claims(self) -> None:
        if self._jobs is None:
            self._claimed_jobs.clear()
            return
        self._claimed_jobs = {
            job_id: generation
            for job_id, generation in self._claimed_jobs.items()
            if (receipt := self._jobs.get(job_id)) is not None
            and not receipt.is_terminal
            and receipt.phase != InstallPhase.HEALTHY_UNCLAIMED
            and receipt.executor_generation == generation
        }

    async def _async_load_locked(
        self, *, refresh: bool = False
    ) -> dict[str, InstallJobReceipt]:
        if self._jobs is not None and not refresh:
            return self._jobs
        try:
            existed, corrupt = await self._hass.async_add_executor_job(
                _store_presence, self._store.path
            )
            reader: Store[dict[str, Any]] = Store(
                self._hass,
                _STORE_VERSION,
                _STORE_KEY,
                private=True,
                atomic_writes=True,
            )
            stored = await reader.async_load()
            if stored is None:
                if existed or corrupt:
                    raise InstallJobStoreError
                self._jobs = {}
            else:
                self._jobs = _parse_document(stored)
        except asyncio.CancelledError:
            self._invalidate_cache()
            raise
        except Exception as err:
            self._invalidate_cache()
            raise InstallJobStoreError from err
        self._reconcile_claims()
        return self._jobs

    async def _async_save_locked(self, jobs: dict[str, InstallJobReceipt]) -> None:
        document = _serialize_document(jobs)
        try:
            await self._store.async_save(document)
            existed, _corrupt = await self._hass.async_add_executor_job(
                _store_presence, self._store.path
            )
            if not existed:
                raise InstallJobStoreError
            verifier: Store[dict[str, Any]] = Store(
                self._hass,
                _STORE_VERSION,
                _STORE_KEY,
                private=True,
                atomic_writes=True,
            )
            persisted = await verifier.async_load()
            if persisted != document:
                raise InstallJobStoreError
            verified = _parse_document(persisted)
            if verified != jobs:
                raise InstallJobStoreError
        except asyncio.CancelledError:
            self._invalidate_cache()
            raise
        except Exception as err:
            self._invalidate_cache()
            raise InstallJobStoreError from err
        self._jobs = verified
        self._reconcile_claims()

    def _prune(self, jobs: dict[str, InstallJobReceipt], now: str) -> None:
        cutoff = datetime.fromisoformat(now) - _TERMINAL_RETENTION
        terminal = sorted(
            (receipt for receipt in jobs.values() if receipt.is_terminal),
            key=lambda item: (item.updated_at, item.job_id),
            reverse=True,
        )
        retained_ids = {
            receipt.job_id
            for receipt in terminal[:_MAX_TERMINAL_JOBS]
            if datetime.fromisoformat(receipt.updated_at) >= cutoff
        }
        for receipt in terminal:
            if receipt.job_id not in retained_ids:
                jobs.pop(receipt.job_id, None)

    async def async_create_or_join(
        self,
        target: InstallTarget,
        artifact: InstallArtifact,
        plan_sha256: str,
        adb_credential_id: str,
    ) -> tuple[InstallJobReceipt, bool]:
        """Create a receipt, or join an exactly matching active target job.

        The boolean is true only when this call created and durably verified a
        new receipt.
        """
        target = _parse_target(asdict(target))
        artifact = _parse_artifact(asdict(artifact))
        expected_plan_sha256 = install_plan_sha256(target, artifact, adb_credential_id)
        if (
            plan_sha256 != expected_plan_sha256
            or _SHA256.fullmatch(adb_credential_id) is None
            or target.primary_abi not in artifact.supported_abis
            or target.android_sdk < artifact.min_sdk
        ):
            raise InstallJobTransitionError
        async with self._lock:
            jobs = (await self._async_load_locked(refresh=True)).copy()
            now = _timestamp(self._now())
            self._prune(jobs, now)
            for receipt in jobs.values():
                if receipt.is_terminal:
                    continue
                if (
                    receipt.target.address == target.address
                    or receipt.target.pinned_address == target.pinned_address
                    or receipt.target.adb_serial == target.adb_serial
                ):
                    if (
                        receipt.target == target
                        and receipt.artifact == artifact
                        and receipt.plan_sha256 == plan_sha256
                        and receipt.adb_credential_id == adb_credential_id
                    ):
                        return receipt, False
                    raise InstallJobConflictError
            if sum(not receipt.is_terminal for receipt in jobs.values()) >= (
                _MAX_ACTIVE_JOBS
            ):
                raise InstallJobCapacityError
            job_id = token_hex(16)
            while job_id in jobs:
                job_id = token_hex(16)
            receipt = InstallJobReceipt(
                job_id=job_id,
                revision=0,
                executor_generation=0,
                created_at=now,
                updated_at=now,
                phase=InstallPhase.APPROVED,
                cancel_requested=False,
                attempt=0,
                target=target,
                artifact=artifact,
                plan_sha256=plan_sha256,
                adb_credential_id=adb_credential_id,
            )
            jobs[job_id] = receipt
            await self._async_save_locked(jobs)
            return self._jobs[job_id], True  # type: ignore[index]

    async def async_get(self, job_id: str) -> InstallJobReceipt:
        """Return one immutable receipt snapshot."""
        if _HEX_32.fullmatch(job_id) is None:
            raise InstallJobNotFoundError
        async with self._lock:
            receipt = (await self._async_load_locked(refresh=True)).get(job_id)
            if receipt is None:
                raise InstallJobNotFoundError
            return receipt

    async def async_list(self) -> tuple[InstallJobReceipt, ...]:
        """Return bounded receipt snapshots in deterministic order."""
        async with self._lock:
            jobs = await self._async_load_locked(refresh=True)
            return tuple(
                sorted(jobs.values(), key=lambda item: (item.created_at, item.job_id))
            )

    async def async_find_active(
        self, address: str, pinned_address: str
    ) -> InstallJobReceipt | None:
        """Find an exact active target without contacting the panel."""
        try:
            original = normalize_address(address)
            pinned = normalize_address(pinned_address)
            pinned_ip = ipaddress.ip_address(pinned.host)
            if (
                original.stored_value != address
                or pinned.stored_value != pinned_address
                or pinned.host != str(pinned_ip)
                or pinned.port != original.port
                or not is_allowed_install_address(pinned_ip)
            ):
                raise InstallJobStoreError
            try:
                original_ip = ipaddress.ip_address(original.host)
            except ValueError:
                _resolver_hostname(original.host)
            else:
                if original_ip != pinned_ip:
                    raise InstallJobStoreError
        except (InstallNetworkError, InvalidAddressError, ValueError) as err:
            raise InstallJobStoreError from err
        async with self._lock:
            jobs = await self._async_load_locked(refresh=True)
            for receipt in jobs.values():
                if receipt.is_terminal:
                    continue
                if (
                    receipt.target.address == address
                    or receipt.target.pinned_address == pinned_address
                ):
                    if (
                        receipt.target.address == address
                        and receipt.target.pinned_address == pinned_address
                    ):
                        return receipt
                    raise InstallJobConflictError
            return None

    async def async_request_cancel(
        self, job_id: str, expected_revision: int
    ) -> InstallJobReceipt:
        """Durably request cancellation without claiming that it has happened."""
        async with self._lock:
            jobs = (await self._async_load_locked(refresh=True)).copy()
            current = self._checked_current(jobs, job_id, expected_revision)
            if current.phase not in _CANCELLABLE_PHASES:
                raise InstallJobTransitionError
            if current.cancel_requested:
                return current
            updated = replace(
                current,
                revision=current.revision + 1,
                updated_at=_timestamp(self._now()),
                cancel_requested=True,
            )
            jobs[job_id] = updated
            await self._async_save_locked(jobs)
            return self._jobs[job_id]  # type: ignore[index]

    async def async_claim(
        self, job_id: str, expected_revision: int
    ) -> InstallJobReceipt:
        """Durably claim safe work for this manager generation.

        A new manager has no in-memory claims. Encountering a phase from which
        an earlier process may already have mutated the panel therefore records
        recovery-required instead of replaying the operation.
        """
        async with self._lock:
            jobs = (await self._async_load_locked(refresh=True)).copy()
            current = self._checked_current(jobs, job_id, expected_revision)
            if current.is_terminal or current.phase == InstallPhase.HEALTHY_UNCLAIMED:
                raise InstallJobTransitionError
            if self._claimed_jobs.get(job_id) == current.executor_generation:
                return current
            if (
                current.executor_generation >= _MAX_EXECUTOR_GENERATION
                or current.attempt >= _MAX_ATTEMPTS
            ):
                exhausted = replace(
                    current,
                    revision=current.revision + 1,
                    updated_at=_timestamp(self._now()),
                    phase=InstallPhase.RECOVERY_REQUIRED,
                    result_code=InstallResultCode.VERIFICATION_REQUIRED,
                )
                jobs[job_id] = exhausted
                self._prune(jobs, exhausted.updated_at)
                await self._async_save_locked(jobs)
                return self._jobs[job_id]  # type: ignore[index]
            phase = current.phase
            result_code = None
            if phase in _RESTART_AMBIGUOUS_PHASES:
                phase = InstallPhase.RECOVERY_REQUIRED
                result_code = InstallResultCode.VERIFICATION_REQUIRED
            updated = replace(
                current,
                revision=current.revision + 1,
                executor_generation=current.executor_generation + 1,
                updated_at=_timestamp(self._now()),
                phase=phase,
                attempt=current.attempt + 1,
                result_code=result_code,
            )
            jobs[job_id] = updated
            self._prune(jobs, updated.updated_at)
            await self._async_save_locked(jobs)
            persisted = self._jobs[job_id]  # type: ignore[index]
            if not persisted.is_terminal:
                self._claimed_jobs[job_id] = persisted.executor_generation
            return persisted

    async def async_transition(
        self,
        job_id: str,
        expected_revision: int,
        phase: InstallPhase,
        *,
        actual_apk_bytes: int | None = None,
        health_checked_at: str | None = None,
        result_code: InstallResultCode | None = None,
        consumed_entry_id: str | None = None,
    ) -> InstallJobReceipt:
        """Advance one receipt with an explicit revision compare-and-swap."""
        if not isinstance(phase, InstallPhase):
            raise InstallJobTransitionError
        if result_code is not None and not isinstance(result_code, InstallResultCode):
            raise InstallJobTransitionError
        try:
            parsed_actual = (
                None
                if actual_apk_bytes is None
                else _integer(actual_apk_bytes, 1, _MAX_APK_BYTES)
            )
            parsed_health = (
                None if health_checked_at is None else _timestamp(health_checked_at)
            )
            parsed_entry_id = (
                None if consumed_entry_id is None else _safe_text(consumed_entry_id, 32)
            )
            if parsed_entry_id is not None and not _is_config_entry_id(parsed_entry_id):
                raise InstallJobStoreError
        except InstallJobStoreError as err:
            raise InstallJobTransitionError from err
        async with self._lock:
            jobs = (await self._async_load_locked(refresh=True)).copy()
            current = self._checked_current(jobs, job_id, expected_revision)
            if phase not in _NEXT_PHASE.get(current.phase, frozenset()):
                raise InstallJobTransitionError
            if current.cancel_requested and phase not in {
                InstallPhase.CANCELLED,
                InstallPhase.FAILED,
                InstallPhase.RECOVERY_REQUIRED,
            }:
                raise InstallJobTransitionError
            if (
                not (
                    current.phase == InstallPhase.HEALTHY_UNCLAIMED
                    and phase in {InstallPhase.CONSUMED, InstallPhase.RECOVERY_REQUIRED}
                )
                and self._claimed_jobs.get(job_id) != current.executor_generation
            ):
                raise InstallJobTransitionError

            if phase == InstallPhase.ARTIFACT_READY:
                if parsed_actual is None or current.actual_apk_bytes is not None:
                    raise InstallJobTransitionError
            elif parsed_actual is not None:
                raise InstallJobTransitionError

            if phase == InstallPhase.HEALTHY_UNCLAIMED:
                if parsed_health is None or current.health_checked_at is not None:
                    raise InstallJobTransitionError
            elif parsed_health is not None:
                raise InstallJobTransitionError

            if phase == InstallPhase.CANCELLED:
                expected_cancel_result = (
                    InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP
                    if current.phase == InstallPhase.STAGING
                    else InstallResultCode.CANCELLED_BY_USER
                )
                if (
                    not current.cancel_requested
                    or result_code != expected_cancel_result
                    or parsed_entry_id is not None
                ):
                    raise InstallJobTransitionError
            elif phase == InstallPhase.FAILED:
                if (
                    result_code
                    not in _FAILURE_CODES_BY_PHASE.get(current.phase, frozenset())
                    or parsed_entry_id is not None
                ):
                    raise InstallJobTransitionError
            elif phase == InstallPhase.RECOVERY_REQUIRED:
                if (
                    current.phase not in _RECOVERY_SOURCE_PHASES
                    or result_code not in _RECOVERY_CODES
                    or parsed_entry_id is not None
                ):
                    raise InstallJobTransitionError
            elif phase == InstallPhase.CONSUMED:
                if (
                    result_code != InstallResultCode.ENTRY_CREATED
                    or parsed_entry_id is None
                ):
                    raise InstallJobTransitionError
            elif result_code is not None or parsed_entry_id is not None:
                raise InstallJobTransitionError

            updated = replace(
                current,
                revision=current.revision + 1,
                updated_at=_timestamp(self._now()),
                phase=phase,
                actual_apk_bytes=(
                    current.actual_apk_bytes if parsed_actual is None else parsed_actual
                ),
                health_checked_at=(
                    None
                    if current.phase == InstallPhase.HEALTHY_UNCLAIMED
                    and phase == InstallPhase.RECOVERY_REQUIRED
                    else (
                        current.health_checked_at
                        if parsed_health is None
                        else parsed_health
                    )
                ),
                result_code=result_code,
                consumed_entry_id=parsed_entry_id,
            )
            try:
                _validate_receipt_invariants(updated)
            except InstallJobStoreError as err:
                raise InstallJobTransitionError from err
            jobs[job_id] = updated
            self._prune(jobs, updated.updated_at)
            await self._async_save_locked(jobs)
            return self._jobs[job_id]  # type: ignore[index]

    async def async_verify_mutation_barrier(
        self,
        job_id: str,
        expected_revision: int,
        phase: InstallPhase,
    ) -> InstallJobReceipt:
        """Freshly re-read a persisted mutation barrier before external action."""
        if phase not in _MUTATION_BARRIERS:
            raise InstallJobTransitionError
        async with self._lock:
            in_memory = self._checked_current(
                await self._async_load_locked(refresh=True), job_id, expected_revision
            )
            verifier: Store[dict[str, Any]] = Store(
                self._hass,
                _STORE_VERSION,
                _STORE_KEY,
                private=True,
                atomic_writes=True,
            )
            try:
                persisted = await verifier.async_load()
                fresh = _parse_document(persisted)
            except (InstallJobStoreError, OSError) as err:
                raise InstallJobStoreError from err
            receipt = self._checked_current(fresh, job_id, expected_revision)
            if (
                receipt != in_memory
                or receipt.phase != phase
                or receipt.cancel_requested
                or self._claimed_jobs.get(job_id) != receipt.executor_generation
            ):
                raise InstallJobTransitionError
            return receipt

    async def async_verify_cleanup_barrier(
        self,
        job_id: str,
        expected_revision: int,
        phase: InstallPhase,
    ) -> InstallJobReceipt:
        """Freshly verify the narrowly allowed remote-cleanup authority."""
        expected_cancel_requested = _CLEANUP_BARRIER_CANCEL_POLARITY.get(phase)
        if expected_cancel_requested is None:
            raise InstallJobTransitionError
        async with self._lock:
            in_memory = self._checked_current(
                await self._async_load_locked(refresh=True), job_id, expected_revision
            )
            verifier: Store[dict[str, Any]] = Store(
                self._hass,
                _STORE_VERSION,
                _STORE_KEY,
                private=True,
                atomic_writes=True,
            )
            try:
                persisted = await verifier.async_load()
                fresh = _parse_document(persisted)
            except (InstallJobStoreError, OSError) as err:
                raise InstallJobStoreError from err
            receipt = self._checked_current(fresh, job_id, expected_revision)
            if (
                receipt != in_memory
                or receipt.phase != phase
                or receipt.cancel_requested is not expected_cancel_requested
                or self._claimed_jobs.get(job_id) != receipt.executor_generation
            ):
                raise InstallJobTransitionError
            return receipt

    @staticmethod
    def _checked_current(
        jobs: Mapping[str, InstallJobReceipt], job_id: str, expected_revision: int
    ) -> InstallJobReceipt:
        if _HEX_32.fullmatch(job_id) is None:
            raise InstallJobNotFoundError
        current = jobs.get(job_id)
        if current is None:
            raise InstallJobNotFoundError
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or current.revision != expected_revision
        ):
            raise InstallJobRevisionError
        return current


async def async_get_install_job_manager(hass: HomeAssistant) -> InstallJobManager:
    """Return the config-flow-independent process-wide receipt authority."""
    manager = hass.data.get(_MANAGER_DATA_KEY)
    if manager is None:
        manager = InstallJobManager(hass)
        hass.data[_MANAGER_DATA_KEY] = manager
    if not isinstance(manager, InstallJobManager):
        raise InstallJobStoreError
    return manager
