"""Pure binding of authenticated installer inputs into one durable plan."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from enum import StrEnum

from .client import InvalidAddressError, PanelAddress, normalize_address
from .install_jobs import (
    InstallArtifact,
    InstallJobStoreError,
    InstallTarget,
    install_plan_sha256,
)
from .install_network import PinnedPanelTarget, is_allowed_install_address
from .provisioning import InstallTargetProbe, InstallTargetState
from .release import (
    InstallDescriptor,
    ReleaseArtifact,
    is_install_release_tag,
    is_rc_release_tag,
)

_DESCRIPTOR_SCHEMA = "io.github.maxlyth.hapaneld.install.v1"
_PACKAGE_ID = "io.github.maxlyth.hapaneld"
_LAUNCH_COMPONENT = f"{_PACKAGE_ID}/.MainActivity"
_RELEASE_SIGNER_SHA256 = (
    "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
)
_SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a")
_MAX_APK_BYTES = 64 * 1024 * 1024
_MAX_SDK = 100
_MAX_ADDRESS_LENGTH = 255
_MAX_MODEL_LENGTH = 128
_MAX_APK_NAME_LENGTH = 255
_MAX_RELEASE_TEXT_LENGTH = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADB_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", flags=re.ASCII)
_ABI = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", flags=re.ASCII)
_DATABASE_COMPATIBILITY = re.compile(
    r"^hapaneld-db:v1:ha-paneld\.db:([1-9][0-9]*):([1-9][0-9]*)$"
)
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class InstallPlanErrorCode(StrEnum):
    """Stable, privacy-safe failures while binding an installation plan."""

    INVALID_TARGET = "invalid_target"
    PROBE_NOT_INSTALL_CANDIDATE = "probe_not_install_candidate"
    INCOMPLETE_PROBE = "incomplete_probe"
    PREVIEW_ONLY_RELEASE = "preview_only_release"
    INVALID_RELEASE = "invalid_release"
    INCOMPATIBLE_RELEASE = "incompatible_release"
    INVALID_CREDENTIAL = "invalid_credential"
    INVALID_PLAN = "invalid_plan"


class InstallPlanError(Exception):
    """Raised with only a stable code, never input or exception details."""

    def __init__(self, code: InstallPlanErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Frozen job inputs and their deterministic authorization digest."""

    target: InstallTarget
    artifact: InstallArtifact
    plan_sha256: str
    adb_credential_id: str


def _bounded_integer(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _safe_text(value: object, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        return None
    return value


def _canonical_address(address: object) -> PanelAddress | None:
    if not isinstance(address, PanelAddress):
        return None
    if (
        not isinstance(address.host, str)
        or isinstance(address.port, bool)
        or not isinstance(address.port, int)
        or not 1 <= address.port <= 65535
    ):
        return None
    stored = address.stored_value
    if len(stored) > _MAX_ADDRESS_LENGTH:
        return None
    try:
        normalized = normalize_address(stored)
    except InvalidAddressError:
        return None
    return address if normalized == address else None


def _valid_dns_name(host: str) -> bool:
    if len(host) > 253 or not host.isascii() or "%" in host:
        return False
    try:
        socket.inet_aton(host)
    except OSError, ValueError:
        pass
    else:
        return False
    labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
    return bool(labels) and all(_DNS_LABEL.fullmatch(label) for label in labels)


def _build_target(
    pinned_target: PinnedPanelTarget, probe: InstallTargetProbe
) -> InstallTarget:
    if not isinstance(pinned_target, PinnedPanelTarget):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET)
    original = _canonical_address(pinned_target.original)
    pinned = _canonical_address(pinned_target.pinned)
    if original is None or pinned is None or original.port != pinned.port:
        raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET)

    try:
        pinned_ip = ipaddress.ip_address(pinned.host)
    except ValueError:
        raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET) from None
    if str(pinned_ip) != pinned.host or not is_allowed_install_address(pinned_ip):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET)

    try:
        original_ip = ipaddress.ip_address(original.host)
    except ValueError:
        if not _valid_dns_name(original.host):
            raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET) from None
    else:
        if original_ip != pinned_ip:
            raise InstallPlanError(InstallPlanErrorCode.INVALID_TARGET)

    if not isinstance(probe, InstallTargetProbe):
        raise InstallPlanError(InstallPlanErrorCode.INCOMPLETE_PROBE)
    if probe.state is not InstallTargetState.INSTALL_CANDIDATE:
        raise InstallPlanError(InstallPlanErrorCode.PROBE_NOT_INSTALL_CANDIDATE)

    model = _safe_text(probe.model, _MAX_MODEL_LENGTH)
    serial = _safe_text(probe.serial, 128)
    primary_abi = _safe_text(probe.primary_abi, 64)
    android_sdk = _bounded_integer(probe.android_sdk, 1, _MAX_SDK)
    if (
        model is None
        or serial is None
        or _ADB_SERIAL.fullmatch(serial) is None
        or primary_abi is None
        or _ABI.fullmatch(primary_abi) is None
        or android_sdk is None
    ):
        raise InstallPlanError(InstallPlanErrorCode.INCOMPLETE_PROBE)

    return InstallTarget(
        address=original.stored_value,
        pinned_address=pinned.stored_value,
        adb_serial=serial,
        model=model,
        primary_abi=primary_abi,
        android_sdk=android_sdk,
    )


def _build_artifact(
    release: ReleaseArtifact, expected_rc_tag: str | None
) -> InstallArtifact:
    if not isinstance(release, ReleaseArtifact):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_RELEASE)
    descriptor = release.descriptor
    if descriptor is None:
        raise InstallPlanError(InstallPlanErrorCode.PREVIEW_ONLY_RELEASE)
    if not isinstance(descriptor, InstallDescriptor):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_RELEASE)

    release_tag = _safe_text(descriptor.release_tag, _MAX_RELEASE_TEXT_LENGTH)
    version_name = _safe_text(descriptor.version_name, _MAX_RELEASE_TEXT_LENGTH)
    apk_name = _safe_text(descriptor.apk_name, _MAX_APK_NAME_LENGTH)
    apk_sha256 = _safe_text(descriptor.apk_sha256, 64)
    signer = _safe_text(descriptor.signer_certificate_sha256, 64)
    database = _safe_text(descriptor.database_compatibility, 128)
    version_code = _bounded_integer(descriptor.version_code, 1, 2**31 - 1)
    apk_size = _bounded_integer(descriptor.apk_size, 1, _MAX_APK_BYTES)
    min_sdk = _bounded_integer(descriptor.min_sdk, 1, _MAX_SDK)
    database_match = (
        _DATABASE_COMPATIBILITY.fullmatch(database) if database is not None else None
    )
    database_bounds: tuple[int, int] | None = None
    if database_match is not None and all(
        len(group) <= 10 for group in database_match.groups()
    ):
        database_bounds = (
            int(database_match.group(1)),
            int(database_match.group(2)),
        )

    if (
        descriptor.schema != _DESCRIPTOR_SCHEMA
        or release_tag is None
        or not is_install_release_tag(release_tag)
        or (
            is_rc_release_tag(release_tag)
            if expected_rc_tag is None
            else not is_rc_release_tag(expected_rc_tag)
            or release_tag != expected_rc_tag
        )
        or version_name != release_tag.removeprefix("v")
        or apk_name != f"ha-paneld-{release_tag}-manual-setup-required.apk"
        or apk_sha256 is None
        or _SHA256.fullmatch(apk_sha256) is None
        or signer != _RELEASE_SIGNER_SHA256
        or descriptor.package_id != _PACKAGE_ID
        or descriptor.supported_abis != _SUPPORTED_ABIS
        or database_bounds is None
        or not 1 <= database_bounds[0] <= database_bounds[1] <= 2**31 - 1
        or descriptor.launch_component != _LAUNCH_COMPONENT
        or version_code is None
        or apk_size is None
        or min_sdk is None
        or release.tag != release_tag
        or release.version != version_name
        or release.apk_name != apk_name
        or release.sha256 != apk_sha256
    ):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_RELEASE)

    return InstallArtifact(
        descriptor_schema=descriptor.schema,
        release_tag=descriptor.release_tag,
        version_name=descriptor.version_name,
        version_code=descriptor.version_code,
        apk_name=descriptor.apk_name,
        apk_sha256=descriptor.apk_sha256,
        apk_size=descriptor.apk_size,
        package_id=descriptor.package_id,
        signer_certificate_sha256=descriptor.signer_certificate_sha256,
        min_sdk=descriptor.min_sdk,
        supported_abis=descriptor.supported_abis,
        database_compatibility=descriptor.database_compatibility,
        launch_component=descriptor.launch_component,
    )


def build_install_plan(
    pinned_target: PinnedPanelTarget,
    probe: InstallTargetProbe,
    release: ReleaseArtifact,
    adb_credential_id: str,
    *,
    expected_rc_tag: str | None = None,
) -> InstallPlan:
    """Validate and bind one exact target, release, and ADB key generation."""
    target = _build_target(pinned_target, probe)
    artifact = _build_artifact(release, expected_rc_tag)
    if (
        not isinstance(adb_credential_id, str)
        or _SHA256.fullmatch(adb_credential_id) is None
    ):
        raise InstallPlanError(InstallPlanErrorCode.INVALID_CREDENTIAL)
    if (
        target.android_sdk < artifact.min_sdk
        or target.primary_abi not in artifact.supported_abis
    ):
        raise InstallPlanError(InstallPlanErrorCode.INCOMPATIBLE_RELEASE)
    try:
        plan_sha256 = install_plan_sha256(target, artifact, adb_credential_id)
    except InstallJobStoreError:
        raise InstallPlanError(InstallPlanErrorCode.INVALID_PLAN) from None

    return InstallPlan(
        target=target,
        artifact=artifact,
        plan_sha256=plan_sha256,
        adb_credential_id=adb_credential_id,
    )
