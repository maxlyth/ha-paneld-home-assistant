"""Bounded ADB primitives for a clean first installation.

This module deliberately does not own orchestration.  Every public operation
opens a fresh authenticated ADB connection and either returns a small,
privacy-safe result or raises :class:`InstallAdbError` with a stable code.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from secrets import token_hex

from adb_shell.adb_device_async import AdbDeviceAsync
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    DevicePathInvalidError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    InvalidTransportError,
    PushFailedError,
    TcpTimeoutException,
)
from adb_shell.transport.tcp_transport_async import TcpTransportAsync

from .client import PanelAddress
from .install_network import is_allowed_install_address
from .release import InstallDescriptor

ADB_PORT = 5555
_ADB_BANNER = "ha-paneld-home-assistant"
_PACKAGE_ID = "io.github.maxlyth.hapaneld"
_PACKAGE_MANAGER_LIVENESS_PACKAGE = "android"
_LAUNCH_COMPONENT = f"{_PACKAGE_ID}/.MainActivity"
_DESCRIPTOR_SCHEMA = "io.github.maxlyth.hapaneld.install.v1"
_RELEASE_SIGNER_SHA256 = (
    "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
)
_SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a")
_REMOTE_PREFIX = "/data/local/tmp/ha-paneld-install-"
_MAX_APK_BYTES = 64 * 1024 * 1024
_MIN_ADB_MAXDATA = 4 * 1024
_MAX_ADB_MAXDATA = 1024 * 1024
_MAX_ADB_PACKET_BYTES = _MAX_ADB_MAXDATA
_MAX_CONNECTION_READ_BYTES = 2 * 1024 * 1024
_MAX_CONNECTION_WRITE_BYTES = _MAX_APK_BYTES + 4 * 1024 * 1024
_MAX_SHELL_RESPONSE_BYTES = 32 * 1024
_CONNECT_TIMEOUT_SECONDS = 5.0
_TRANSPORT_TIMEOUT_SECONDS = 10.0
_MAX_TRANSPORT_TIMEOUT_SECONDS = 180.0
_READ_TIMEOUT_SECONDS = 10.0
_INSTALL_READ_TIMEOUT_SECONDS = 30.0
_CLOSE_TIMEOUT_SECONDS = 2.0
_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_STAGE_TIMEOUT_SECONDS = 180.0
_INSTALL_TIMEOUT_SECONDS = 180.0
_LAUNCH_TIMEOUT_SECONDS = 30.0
_CLEANUP_TIMEOUT_SECONDS = 30.0
_REMOTE_MODE = stat.S_IFREG | 0o644
_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$", flags=re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", flags=re.ASCII)
_ABI_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", flags=re.ASCII)
_APK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.apk$", re.ASCII)
_ROOT_DATA_BASES = ("/data/user/0", "/data/data", "/data/user_de/0")
_RESIDUE_PATHS = tuple(f"{base}/{_PACKAGE_ID}" for base in _ROOT_DATA_BASES)

_ADB_EXCEPTIONS = (
    AdbConnectionError,
    AdbTimeoutError,
    DevicePathInvalidError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    InvalidTransportError,
    OSError,
    PushFailedError,
    TcpTimeoutException,
)


class InstallAdbErrorCode(StrEnum):
    """Stable, privacy-safe failure codes for the installer executor."""

    INVALID_REQUEST = "invalid_request"
    TARGET_UNREACHABLE = "target_unreachable"
    AUTHORIZATION_REQUIRED = "authorization_required"
    TARGET_CHANGED = "target_changed"
    ROOT_MODE_CHANGED = "root_mode_changed"
    TARGET_RESPONSE_INVALID = "target_response_invalid"
    TARGET_INCOMPATIBLE = "target_incompatible"
    TARGET_NOT_CLEAN = "target_not_clean"
    INSTALLED_PACKAGE_MISSING = "installed_package_missing"
    ROOT_STATE_AMBIGUOUS = "root_state_ambiguous"
    LOCAL_ARTIFACT_INVALID = "local_artifact_invalid"
    FILESYNC_UNSAFE = "filesync_unsafe"
    STAGING_PATH_OCCUPIED = "staging_path_occupied"
    STAGE_AMBIGUOUS = "stage_ambiguous"
    STAGE_VERIFICATION_FAILED = "stage_verification_failed"
    INSTALL_AMBIGUOUS = "install_ambiguous"
    LAUNCH_AMBIGUOUS = "launch_ambiguous"
    CLEANUP_AMBIGUOUS = "cleanup_ambiguous"


class InstallAdbError(Exception):
    """An ADB install failure which never includes peer or filesystem text."""

    def __init__(self, code: InstallAdbErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class AdbRootMode(StrEnum):
    """The two positively admitted ADB privilege postures."""

    ROOT_ADBD = "root_adbd"
    ROOTLESS = "rootless"


class InstallOutcome(StrEnum):
    """Definite package-manager outcomes."""

    INSTALLED = "installed"
    REFUSED = "refused"


class LaunchOutcome(StrEnum):
    """Definite activity-manager outcomes."""

    STARTED = "started"
    REFUSED = "refused"


class DefiniteCleanupReason(StrEnum):
    """States which allow deletion of the one job-owned staging path."""

    CANCELLED = "cancelled"
    INSTALL_SUCCEEDED = "install_succeeded"
    INSTALL_REFUSED = "install_refused"


@dataclass(frozen=True, slots=True)
class AdbInstallTarget:
    """One pre-pinned private address and its previously observed identity."""

    address: PanelAddress
    serial: str
    model: str
    primary_abi: str
    android_sdk: int


@dataclass(frozen=True, slots=True)
class AdbPreflight:
    """Fresh clean-install admission facts from one ADB connection."""

    serial: str
    model: str
    primary_abi: str
    android_sdk: int
    root_mode: AdbRootMode


@dataclass(frozen=True, slots=True)
class StagedApk:
    """Exact remote artifact verified after a completed FileSync push."""

    job_id: str
    remote_path: str
    apk_size: int
    apk_sha256: str


class _MalformedAdbResponse(Exception):
    """The peer did not return a complete bounded protocol frame."""


class _UnsafeAdbPacket(Exception):
    """The peer or library requested an unsafe ADB transport operation."""


@dataclass(frozen=True, slots=True)
class _ObservedTarget:
    model: str
    serial: str
    primary_abi: str
    android_sdk: int


class _BoundedTcpTransportAsync(TcpTransportAsync):
    """Enforce finite per-call and per-connection ADB byte bounds."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(host, port)
        self._received_bytes = 0
        self._written_bytes = 0

    async def bulk_read(
        self, numbytes: int, transport_timeout_s: float | None
    ) -> bytes:
        if (
            isinstance(numbytes, bool)
            or not isinstance(numbytes, int)
            or not 1 <= numbytes <= _MAX_ADB_PACKET_BYTES
        ):
            raise _UnsafeAdbPacket
        remaining = _MAX_CONNECTION_READ_BYTES - self._received_bytes
        if remaining < 1:
            raise _UnsafeAdbPacket
        timeout = _bounded_transport_timeout(transport_timeout_s)
        data = await super().bulk_read(min(numbytes, remaining), timeout)
        if not data:
            raise AdbConnectionError("bounded ADB connection closed")
        self._received_bytes += len(data)
        return data

    async def bulk_write(
        self, data: bytes | bytearray, transport_timeout_s: float | None
    ) -> int:
        if (
            not isinstance(data, (bytes, bytearray))
            or not data
            or len(data) > _MAX_ADB_PACKET_BYTES + 24
            or self._written_bytes + len(data) > _MAX_CONNECTION_WRITE_BYTES
        ):
            raise _UnsafeAdbPacket
        timeout = _bounded_transport_timeout(transport_timeout_s)
        written = await super().bulk_write(data, timeout)
        if written != len(data):
            raise _UnsafeAdbPacket
        self._written_bytes += written
        return written


def _bounded_transport_timeout(value: float | None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= _MAX_TRANSPORT_TIMEOUT_SECONDS
    ):
        raise _UnsafeAdbPacket
    return float(value)


def _remote_path(job_id: str) -> str:
    if not isinstance(job_id, str) or _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    return f"{_REMOTE_PREFIX}{job_id}.apk"


def _validate_target(target: AdbInstallTarget) -> None:
    if (
        not isinstance(target.address, PanelAddress)
        or not isinstance(target.address.host, str)
        or isinstance(target.address.port, bool)
        or not isinstance(target.address.port, int)
        or not 1 <= target.address.port <= 65535
        or not isinstance(target.serial, str)
        or not isinstance(target.model, str)
        or not isinstance(target.primary_abi, str)
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    try:
        address = ipaddress.ip_address(target.address.host)
    except ValueError:
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST) from None
    if (
        not is_allowed_install_address(address)
        or _SERIAL_PATTERN.fullmatch(target.serial) is None
        or target.model != target.model.strip()
        or not 1 <= len(target.model) <= 128
        or not target.model.isprintable()
        or _ABI_PATTERN.fullmatch(target.primary_abi) is None
        or isinstance(target.android_sdk, bool)
        or not isinstance(target.android_sdk, int)
        or not 1 <= target.android_sdk <= 100
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)


def _validate_descriptor(descriptor: InstallDescriptor) -> None:
    if (
        not isinstance(descriptor.schema, str)
        or not isinstance(descriptor.package_id, str)
        or not isinstance(descriptor.launch_component, str)
        or not isinstance(descriptor.signer_certificate_sha256, str)
        or not isinstance(descriptor.apk_name, str)
        or not isinstance(descriptor.apk_sha256, str)
        or descriptor.schema != _DESCRIPTOR_SCHEMA
        or descriptor.package_id != _PACKAGE_ID
        or descriptor.launch_component != _LAUNCH_COMPONENT
        or descriptor.signer_certificate_sha256 != _RELEASE_SIGNER_SHA256
        or _APK_NAME_PATTERN.fullmatch(descriptor.apk_name) is None
        or _SHA256_PATTERN.fullmatch(descriptor.apk_sha256) is None
        or isinstance(descriptor.apk_size, bool)
        or not isinstance(descriptor.apk_size, int)
        or not 1 <= descriptor.apk_size <= _MAX_APK_BYTES
        or isinstance(descriptor.min_sdk, bool)
        or not isinstance(descriptor.min_sdk, int)
        or not 1 <= descriptor.min_sdk <= 100
        or not isinstance(descriptor.supported_abis, tuple)
        or descriptor.supported_abis != _SUPPORTED_ABIS
        or any(
            not isinstance(abi, str) or _ABI_PATTERN.fullmatch(abi) is None
            for abi in descriptor.supported_abis
        )
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)


def _validate_request(target: AdbInstallTarget, descriptor: InstallDescriptor) -> None:
    if not isinstance(target, AdbInstallTarget) or not isinstance(
        descriptor, InstallDescriptor
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    _validate_target(target)
    _validate_descriptor(descriptor)


def _property_sections() -> tuple[tuple[str, str], ...]:
    return (
        ("MODEL", "ro.product.model"),
        ("SERIAL", "ro.serialno"),
        ("ABI", "ro.product.cpu.abi"),
        ("SDK", "ro.build.version.sdk"),
    )


def _framed_value_commands(prefix: str, nonce: str) -> list[str]:
    commands = [f"echo HAPANELD_{prefix}_BEGIN:{nonce}"]
    for name, android_property in _property_sections():
        commands.extend(
            (
                f"echo HAPANELD_{prefix}_{name}_BEGIN:{nonce}",
                f"getprop {android_property}",
                f"echo HAPANELD_{prefix}_{name}_END:{nonce}:$?",
            )
        )
    commands.append(f"echo HAPANELD_{prefix}_END:{nonce}")
    return commands


def _preflight_command(nonce: str) -> str:
    commands = _framed_value_commands("PREFLIGHT", nonce)
    sections = (
        ("UID", "id -u"),
        ("SECURE", "getprop ro.secure"),
        ("DEBUGGABLE", "getprop ro.debuggable"),
        (
            "SU",
            _su_observation_command(),
        ),
        ("PACKAGE", f"pm path {_PACKAGE_ID}"),
        ("RETAINED", f"pm list packages -u {_PACKAGE_ID}"),
        ("LIVE", f"pm path {_PACKAGE_MANAGER_LIVENESS_PACKAGE}"),
    )
    commands.pop()
    for name, command in sections:
        commands.extend(
            (
                f"echo HAPANELD_PREFLIGHT_{name}_BEGIN:{nonce}",
                command,
                f"echo HAPANELD_PREFLIGHT_{name}_END:{nonce}:$?",
            )
        )
    for index, path in enumerate(_ROOT_DATA_BASES):
        commands.extend(
            (
                f"echo HAPANELD_PREFLIGHT_BASE{index}_BEGIN:{nonce}",
                f"if [ -L {path} ] || [ -d {path} ]; then "
                f"if ls -1A {path} >/dev/null 2>&1; then echo readable; "
                "else echo unreadable; fi; else echo unreadable; fi",
                f"echo HAPANELD_PREFLIGHT_BASE{index}_END:{nonce}:$?",
            )
        )
    for index, path in enumerate(_RESIDUE_PATHS):
        commands.extend(
            (
                f"echo HAPANELD_PREFLIGHT_RESIDUE{index}_BEGIN:{nonce}",
                f"if [ -e {path} ] || [ -L {path} ]; then "
                "echo present; else echo absent; fi",
                f"echo HAPANELD_PREFLIGHT_RESIDUE{index}_END:{nonce}:$?",
            )
        )
    commands.append(f"echo HAPANELD_PREFLIGHT_END:{nonce}")
    return "; ".join(commands)


def _su_observation_command() -> str:
    return (
        "if command -v su >/dev/null 2>&1; then echo present; "
        "else hapaneld_su_status=$?; "
        'if [ "$hapaneld_su_status" -eq 1 ]; then echo absent; '
        "else echo abnormal; fi; fi"
    )


def _identity_root_command(nonce: str) -> str:
    commands = _framed_value_commands("POSTURE", nonce)
    commands.pop()
    for name, command in (
        ("UID", "id -u"),
        ("SECURE", "getprop ro.secure"),
        ("DEBUGGABLE", "getprop ro.debuggable"),
        ("SU", _su_observation_command()),
    ):
        commands.extend(
            (
                f"echo HAPANELD_POSTURE_{name}_BEGIN:{nonce}",
                command,
                f"echo HAPANELD_POSTURE_{name}_END:{nonce}:$?",
            )
        )
    commands.append(f"echo HAPANELD_POSTURE_END:{nonce}")
    return "; ".join(commands)


def _path_state_command(nonce: str, remote_path: str) -> str:
    return "; ".join(
        (
            f"echo HAPANELD_PATH_BEGIN:{nonce}",
            f"if [ -e {remote_path} ] || [ -L {remote_path} ]; then "
            "echo present; else echo absent; fi",
            f"echo HAPANELD_PATH_END:{nonce}:$?",
        )
    )


def _remote_artifact_command(nonce: str, remote_path: str) -> str:
    return "; ".join(
        (
            f"echo HAPANELD_ARTIFACT_BEGIN:{nonce}",
            f"echo HAPANELD_ARTIFACT_CHMOD_BEGIN:{nonce}",
            f"chmod 0644 {remote_path}",
            f"echo HAPANELD_ARTIFACT_CHMOD_END:{nonce}:$?",
            f"echo HAPANELD_ARTIFACT_MODE_BEGIN:{nonce}",
            f"stat -c %f {remote_path}",
            f"echo HAPANELD_ARTIFACT_MODE_END:{nonce}:$?",
            f"echo HAPANELD_ARTIFACT_SIZE_BEGIN:{nonce}",
            f"wc -c < {remote_path}",
            f"echo HAPANELD_ARTIFACT_SIZE_END:{nonce}:$?",
            f"echo HAPANELD_ARTIFACT_SHA_BEGIN:{nonce}",
            f"sha256sum {remote_path}",
            f"echo HAPANELD_ARTIFACT_SHA_END:{nonce}:$?",
            f"echo HAPANELD_ARTIFACT_END:{nonce}",
        )
    )


def _package_command(nonce: str) -> str:
    return "; ".join(
        (
            f"echo HAPANELD_PACKAGE_BEGIN:{nonce}",
            f"pm path {_PACKAGE_ID}",
            f"echo HAPANELD_PACKAGE_END:{nonce}:$?",
        )
    )


def _install_command(nonce: str, remote_path: str, android_sdk: int) -> str:
    no_replace = "-R " if android_sdk >= 28 else ""
    return "; ".join(
        (
            f"echo HAPANELD_INSTALL_BEGIN:{nonce}",
            f"pm install {no_replace}{remote_path}",
            f"echo HAPANELD_INSTALL_END:{nonce}:$?",
        )
    )


def _launch_command(nonce: str) -> str:
    return "; ".join(
        (
            f"echo HAPANELD_LAUNCH_BEGIN:{nonce}",
            f"am start -W -n {_LAUNCH_COMPONENT} -p {_PACKAGE_ID}",
            f"echo HAPANELD_LAUNCH_END:{nonce}:$?",
        )
    )


def _cleanup_command(nonce: str, remote_path: str) -> str:
    return "; ".join(
        (
            f"echo HAPANELD_CLEANUP_BEGIN:{nonce}",
            f"echo HAPANELD_CLEANUP_RM_BEGIN:{nonce}",
            f"rm -f {remote_path}",
            f"echo HAPANELD_CLEANUP_RM_END:{nonce}:$?",
            f"echo HAPANELD_CLEANUP_STATE_BEGIN:{nonce}",
            f"if [ -e {remote_path} ] || [ -L {remote_path} ]; then "
            "echo present; else echo absent; fi",
            f"echo HAPANELD_CLEANUP_STATE_END:{nonce}:$?",
            f"echo HAPANELD_CLEANUP_END:{nonce}",
        )
    )


def _decode_lines(body: bytes) -> list[str]:
    if not body or len(body) > _MAX_SHELL_RESPONSE_BYTES:
        raise _MalformedAdbResponse
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as err:
        raise _MalformedAdbResponse from err
    text = text.replace("\r\n", "\n")
    if "\r" in text or not text.endswith("\n"):
        raise _MalformedAdbResponse
    return text.splitlines()


def _parse_status(line: str, prefix: str, nonce: str) -> int | None:
    match = re.fullmatch(rf"HAPANELD_{prefix}_END:{nonce}:([0-9]{{1,3}})", line)
    return None if match is None else int(match.group(1))


def _parse_sections(
    body: bytes,
    *,
    prefix: str,
    nonce: str,
    names: tuple[str, ...],
) -> dict[str, tuple[list[str], int]]:
    lines = _decode_lines(body)
    if (
        lines[0] != f"HAPANELD_{prefix}_BEGIN:{nonce}"
        or lines[-1] != f"HAPANELD_{prefix}_END:{nonce}"
    ):
        raise _MalformedAdbResponse
    offset = 1
    parsed: dict[str, tuple[list[str], int]] = {}
    for name in names:
        begin = f"HAPANELD_{prefix}_{name}_BEGIN:{nonce}"
        if offset >= len(lines) - 1 or lines[offset] != begin:
            raise _MalformedAdbResponse
        offset += 1
        values: list[str] = []
        status: int | None = None
        while offset < len(lines) - 1:
            status = _parse_status(lines[offset], f"{prefix}_{name}", nonce)
            if status is not None:
                offset += 1
                break
            if lines[offset].startswith("HAPANELD_"):
                raise _MalformedAdbResponse
            values.append(lines[offset])
            offset += 1
        if status is None:
            raise _MalformedAdbResponse
        parsed[name] = (values, status)
    if offset != len(lines) - 1:
        raise _MalformedAdbResponse
    return parsed


def _parse_identity(body: bytes, nonce: str, prefix: str) -> _ObservedTarget:
    names = tuple(name for name, _property in _property_sections())
    sections = _parse_sections(body, prefix=prefix, nonce=nonce, names=names)
    values: dict[str, str] = {}
    for name in names:
        lines, status_code = sections[name]
        if status_code != 0 or len(lines) != 1:
            raise _MalformedAdbResponse
        values[name] = lines[0]
    model = values["MODEL"]
    serial = values["SERIAL"]
    primary_abi = values["ABI"]
    sdk = values["SDK"]
    if (
        model != model.strip()
        or not 1 <= len(model) <= 128
        or not model.isprintable()
        or _SERIAL_PATTERN.fullmatch(serial) is None
        or _ABI_PATTERN.fullmatch(primary_abi) is None
        or re.fullmatch(r"[0-9]{1,3}", sdk, flags=re.ASCII) is None
    ):
        raise _MalformedAdbResponse
    android_sdk = int(sdk)
    if not 1 <= android_sdk <= 100:
        raise _MalformedAdbResponse
    return _ObservedTarget(model, serial, primary_abi, android_sdk)


def _is_package_path(line: str) -> bool:
    return re.fullmatch(r"package:/[^ \t]+", line) is not None


def _require_same_target(observed: _ObservedTarget, expected: AdbInstallTarget) -> None:
    if (
        observed.model != expected.model
        or observed.serial != expected.serial
        or observed.primary_abi != expected.primary_abi
        or observed.android_sdk != expected.android_sdk
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_CHANGED)


def _parse_root_mode(
    sections: dict[str, tuple[list[str], int]],
) -> AdbRootMode:
    uid_lines, uid_status = sections["UID"]
    secure_lines, secure_status = sections["SECURE"]
    debuggable_lines, debuggable_status = sections["DEBUGGABLE"]
    su_lines, su_status = sections["SU"]
    if (
        uid_status != 0
        or len(uid_lines) != 1
        or secure_status != 0
        or secure_lines not in (["0"], ["1"])
        or debuggable_status != 0
        or debuggable_lines not in (["0"], ["1"])
        or su_status != 0
        or su_lines not in (["absent"], ["present"], ["abnormal"])
    ):
        raise _MalformedAdbResponse
    if su_lines == ["abnormal"]:
        raise _MalformedAdbResponse
    if uid_lines == ["0"]:
        return AdbRootMode.ROOT_ADBD
    if (
        uid_lines == ["2000"]
        and secure_lines == ["1"]
        and debuggable_lines == ["0"]
        and su_lines == ["absent"]
    ):
        return AdbRootMode.ROOTLESS
    raise InstallAdbError(InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS)


def _require_expected_root_mode(observed: AdbRootMode, expected: AdbRootMode) -> None:
    if observed is not expected:
        raise InstallAdbError(InstallAdbErrorCode.ROOT_MODE_CHANGED)


def _validate_expected_root_mode(expected: AdbRootMode) -> None:
    if not isinstance(expected, AdbRootMode):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)


def _parse_preflight(
    body: bytes,
    nonce: str,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> AdbPreflight:
    property_names = tuple(name for name, _property in _property_sections())
    other_names = (
        "UID",
        "SECURE",
        "DEBUGGABLE",
        "SU",
        "PACKAGE",
        "RETAINED",
        "LIVE",
    )
    residue_names = tuple(f"RESIDUE{index}" for index in range(len(_RESIDUE_PATHS)))
    base_names = tuple(f"BASE{index}" for index in range(len(_ROOT_DATA_BASES)))
    sections = _parse_sections(
        body,
        prefix="PREFLIGHT",
        nonce=nonce,
        names=property_names + other_names + base_names + residue_names,
    )

    identity_body = _sections_as_frame(
        sections, prefix="PREFLIGHT", nonce=nonce, names=property_names
    )
    observed = _parse_identity(identity_body, nonce, "PREFLIGHT")
    _require_same_target(observed, target)

    package_lines, package_status = sections["PACKAGE"]
    retained_lines, retained_status = sections["RETAINED"]
    live_lines, live_status = sections["LIVE"]
    if (
        package_status not in (0, 1)
        or package_lines
        or retained_status != 0
        or retained_lines
        or live_status != 0
        or not live_lines
        or any(not _is_package_path(line) for line in live_lines)
    ):
        if any(line == f"package:{_PACKAGE_ID}" for line in retained_lines) or any(
            _is_package_path(line) for line in package_lines
        ):
            raise InstallAdbError(InstallAdbErrorCode.TARGET_NOT_CLEAN)
        raise _MalformedAdbResponse

    bases_readable = True
    for name in base_names:
        lines, status_code = sections[name]
        if status_code != 0 or lines not in (["readable"], ["unreadable"]):
            raise _MalformedAdbResponse
        bases_readable = bases_readable and lines == ["readable"]

    residue_present = False
    for name in residue_names:
        lines, status_code = sections[name]
        if status_code != 0 or lines not in (["absent"], ["present"]):
            raise _MalformedAdbResponse
        residue_present = residue_present or lines == ["present"]

    if residue_present:
        raise InstallAdbError(InstallAdbErrorCode.TARGET_NOT_CLEAN)

    root_mode = _parse_root_mode(sections)
    if root_mode is AdbRootMode.ROOT_ADBD and not bases_readable:
        raise InstallAdbError(InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS)

    if (
        observed.android_sdk < descriptor.min_sdk
        or observed.primary_abi not in descriptor.supported_abis
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_INCOMPATIBLE)
    return AdbPreflight(
        serial=observed.serial,
        model=observed.model,
        primary_abi=observed.primary_abi,
        android_sdk=observed.android_sdk,
        root_mode=root_mode,
    )


def _sections_as_frame(
    sections: dict[str, tuple[list[str], int]],
    *,
    prefix: str,
    nonce: str,
    names: tuple[str, ...],
) -> bytes:
    lines = [f"HAPANELD_{prefix}_BEGIN:{nonce}"]
    for name in names:
        values, status_code = sections[name]
        lines.append(f"HAPANELD_{prefix}_{name}_BEGIN:{nonce}")
        lines.extend(values)
        lines.append(f"HAPANELD_{prefix}_{name}_END:{nonce}:{status_code}")
    lines.append(f"HAPANELD_{prefix}_END:{nonce}")
    return ("\n".join(lines) + "\n").encode()


def _parse_identity_root(
    body: bytes,
    nonce: str,
    target: AdbInstallTarget,
) -> AdbRootMode:
    property_names = tuple(name for name, _property in _property_sections())
    root_names = ("UID", "SECURE", "DEBUGGABLE", "SU")
    sections = _parse_sections(
        body,
        prefix="POSTURE",
        nonce=nonce,
        names=property_names + root_names,
    )
    identity_body = _sections_as_frame(
        sections, prefix="POSTURE", nonce=nonce, names=property_names
    )
    observed = _parse_identity(identity_body, nonce, "POSTURE")
    _require_same_target(observed, target)
    return _parse_root_mode(sections)


def _parse_single_section(
    body: bytes, *, prefix: str, nonce: str
) -> tuple[list[str], int]:
    lines = _decode_lines(body)
    begin = f"HAPANELD_{prefix}_BEGIN:{nonce}"
    if not lines or lines[0] != begin:
        raise _MalformedAdbResponse
    status = _parse_status(lines[-1], prefix, nonce)
    if status is None or any(line.startswith("HAPANELD_") for line in lines[1:-1]):
        raise _MalformedAdbResponse
    return lines[1:-1], status


def _parse_path_state(body: bytes, nonce: str) -> bool:
    lines, status_code = _parse_single_section(body, prefix="PATH", nonce=nonce)
    if status_code != 0 or lines not in (["absent"], ["present"]):
        raise _MalformedAdbResponse
    return lines == ["present"]


def _parse_remote_artifact(
    body: bytes, nonce: str, remote_path: str
) -> tuple[int, int, str]:
    sections = _parse_sections(
        body,
        prefix="ARTIFACT",
        nonce=nonce,
        names=("CHMOD", "MODE", "SIZE", "SHA"),
    )
    chmod_lines, chmod_status = sections["CHMOD"]
    mode_lines, mode_status = sections["MODE"]
    size_lines, size_status = sections["SIZE"]
    sha_lines, sha_status = sections["SHA"]
    if (
        chmod_lines
        or chmod_status != 0
        or mode_status != 0
        or size_status != 0
        or sha_status != 0
        or len(mode_lines) != 1
        or re.fullmatch(r"[0-9a-fA-F]{1,8}", mode_lines[0]) is None
        or len(size_lines) != 1
        or re.fullmatch(r"[ \t]*[0-9]{1,10}[ \t]*", size_lines[0], flags=re.ASCII)
        is None
        or len(sha_lines) != 1
    ):
        raise _MalformedAdbResponse
    mode = int(mode_lines[0], 16)
    size = int(size_lines[0].strip())
    sha_match = re.fullmatch(
        rf"([0-9a-f]{{64}})[ \t]+{re.escape(remote_path)}", sha_lines[0]
    )
    if sha_match is None or not 0 <= size <= _MAX_APK_BYTES:
        raise _MalformedAdbResponse
    return mode, size, sha_match.group(1)


def _parse_package_present(body: bytes, nonce: str) -> None:
    lines, status_code = _parse_single_section(body, prefix="PACKAGE", nonce=nonce)
    if status_code == 1 and not lines:
        raise InstallAdbError(InstallAdbErrorCode.INSTALLED_PACKAGE_MISSING)
    if (
        status_code != 0
        or not lines
        or any(not _is_package_path(line) for line in lines)
    ):
        raise _MalformedAdbResponse


def _parse_install_outcome(body: bytes, nonce: str) -> InstallOutcome:
    lines, status_code = _parse_single_section(body, prefix="INSTALL", nonce=nonce)
    if status_code == 0 and lines == ["Success"]:
        return InstallOutcome.INSTALLED
    if (
        status_code == 1
        and len(lines) == 1
        and re.fullmatch(
            r"Failure \[INSTALL_[A-Z0-9_]+(?:: [ -~]{1,1024})?\]", lines[0]
        )
        is not None
    ):
        return InstallOutcome.REFUSED
    raise _MalformedAdbResponse


def _validate_staged_apk(staged: StagedApk) -> str:
    if (
        not isinstance(staged, StagedApk)
        or not isinstance(staged.job_id, str)
        or not isinstance(staged.remote_path, str)
        or isinstance(staged.apk_size, bool)
        or not isinstance(staged.apk_size, int)
        or not isinstance(staged.apk_sha256, str)
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    remote_path = _remote_path(staged.job_id)
    if (
        staged.remote_path != remote_path
        or not 1 <= staged.apk_size <= _MAX_APK_BYTES
        or _SHA256_PATTERN.fullmatch(staged.apk_sha256) is None
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    return remote_path


def _parse_launch_outcome(body: bytes, nonce: str) -> LaunchOutcome:
    _lines, status_code = _parse_single_section(body, prefix="LAUNCH", nonce=nonce)
    if status_code == 0:
        return LaunchOutcome.STARTED
    if status_code == 1:
        return LaunchOutcome.REFUSED
    raise _MalformedAdbResponse


def _parse_cleanup(body: bytes, nonce: str) -> None:
    sections = _parse_sections(
        body,
        prefix="CLEANUP",
        nonce=nonce,
        names=("RM", "STATE"),
    )
    rm_lines, rm_status = sections["RM"]
    state_lines, state_status = sections["STATE"]
    if rm_lines or rm_status != 0 or state_status != 0 or state_lines != ["absent"]:
        raise _MalformedAdbResponse


async def _async_shell(
    device: AdbDeviceAsync,
    command: str,
    *,
    read_timeout: float,
    transport_timeout: float = _TRANSPORT_TIMEOUT_SECONDS,
) -> bytes:
    body = bytearray()
    async for chunk in device.streaming_shell(
        command,
        transport_timeout_s=transport_timeout,
        read_timeout_s=read_timeout,
        decode=False,
    ):
        if (
            not isinstance(chunk, bytes)
            or len(body) + len(chunk) > _MAX_SHELL_RESPONSE_BYTES
        ):
            raise _MalformedAdbResponse
        body.extend(chunk)
    return bytes(body)


async def _async_connect(
    target: AdbInstallTarget, signer: PythonRSASigner
) -> AdbDeviceAsync:
    if not isinstance(signer, PythonRSASigner):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    device = AdbDeviceAsync(
        _BoundedTcpTransportAsync(target.address.host, ADB_PORT),
        default_transport_timeout_s=_TRANSPORT_TIMEOUT_SECONDS,
        banner=_ADB_BANNER,
    )
    authorization_prompted = False

    def _authorization_prompted(_device: AdbDeviceAsync) -> None:
        nonlocal authorization_prompted
        authorization_prompted = True

    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
            connected = await device.connect(
                rsa_keys=[signer],
                transport_timeout_s=_TRANSPORT_TIMEOUT_SECONDS,
                auth_timeout_s=_CONNECT_TIMEOUT_SECONDS,
                read_timeout_s=_READ_TIMEOUT_SECONDS,
                auth_callback=_authorization_prompted,
            )
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await _async_close(device)
        raise
    except DeviceAuthError:
        await _async_close(device)
        raise InstallAdbError(InstallAdbErrorCode.AUTHORIZATION_REQUIRED) from None
    except (
        TimeoutError,
        AdbConnectionError,
        AdbTimeoutError,
        OSError,
        TcpTimeoutException,
    ):
        await _async_close(device)
        code = (
            InstallAdbErrorCode.AUTHORIZATION_REQUIRED
            if authorization_prompted
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code) from None
    except (
        _UnsafeAdbPacket,
        DevicePathInvalidError,
        InvalidChecksumError,
        InvalidCommandError,
        InvalidResponseError,
        InvalidTransportError,
        PushFailedError,
    ):
        await _async_close(device)
        raise InstallAdbError(InstallAdbErrorCode.TARGET_RESPONSE_INVALID) from None
    if not connected:
        await _async_close(device)
        code = (
            InstallAdbErrorCode.AUTHORIZATION_REQUIRED
            if authorization_prompted
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code)
    return device


async def _async_wait_for_completion[T](task: asyncio.Task[T]) -> bool:
    """Drain one owned task while recording every caller cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def _async_close_once(device: AdbDeviceAsync) -> None:
    with suppress(Exception):
        async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
            await device.close()


async def _async_close(device: AdbDeviceAsync | None) -> None:
    if device is None:
        return
    close_task = asyncio.create_task(_async_close_once(device))
    cancelled = await _async_wait_for_completion(close_task)
    close_task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _async_preflight_on_device(
    device: AdbDeviceAsync,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> AdbPreflight:
    nonce = token_hex(16)
    body = await _async_shell(
        device, _preflight_command(nonce), read_timeout=_READ_TIMEOUT_SECONDS
    )
    return _parse_preflight(body, nonce, target, descriptor)


async def _async_require_identity_root(
    device: AdbDeviceAsync,
    target: AdbInstallTarget,
    expected_root_mode: AdbRootMode,
) -> None:
    nonce = token_hex(16)
    observed_root_mode = _parse_identity_root(
        await _async_shell(
            device,
            _identity_root_command(nonce),
            read_timeout=_READ_TIMEOUT_SECONDS,
        ),
        nonce,
        target,
    )
    _require_expected_root_mode(observed_root_mode, expected_root_mode)


def _validate_filesync_maxdata(device: AdbDeviceAsync) -> None:
    maxdata = getattr(device, "_maxdata", None)
    if (
        isinstance(maxdata, bool)
        or not isinstance(maxdata, int)
        or not _MIN_ADB_MAXDATA <= maxdata <= _MAX_ADB_MAXDATA
    ):
        raise InstallAdbError(InstallAdbErrorCode.FILESYNC_UNSAFE)


def _verified_apk_identity(
    status: os.stat_result, expected_size: int
) -> tuple[int, int, int, int, int, int, int, int]:
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or status.st_size != expected_size
    ):
        raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID)
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _open_verified_apk(path: Path, descriptor: InstallDescriptor) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        path_stat = os.lstat(path)
        file_descriptor = os.open(path, flags)
    except OSError, TypeError, ValueError:
        raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID) from None
    try:
        path_identity = _verified_apk_identity(path_stat, descriptor.apk_size)
        opened_identity = _verified_apk_identity(
            os.fstat(file_descriptor), descriptor.apk_size
        )
        if opened_identity != path_identity:
            raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID)
        digest = hashlib.sha256()
        actual_size = 0
        while chunk := os.read(file_descriptor, 1024 * 1024):
            actual_size += len(chunk)
            if actual_size > descriptor.apk_size:
                raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID)
            digest.update(chunk)
        if (
            actual_size != descriptor.apk_size
            or digest.hexdigest() != descriptor.apk_sha256
            or _verified_apk_identity(os.fstat(file_descriptor), descriptor.apk_size)
            != opened_identity
            or _verified_apk_identity(os.lstat(path), descriptor.apk_size)
            != opened_identity
        ):
            raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID)
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        return file_descriptor
    except InstallAdbError:
        os.close(file_descriptor)
        raise
    except OSError, TypeError, ValueError:
        os.close(file_descriptor)
        raise InstallAdbError(InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID) from None
    except BaseException:
        os.close(file_descriptor)
        raise


async def _async_open_verified_apk(path: Path, descriptor: InstallDescriptor) -> int:
    """Retain custody of a raw descriptor across caller cancellation."""
    worker = asyncio.create_task(
        asyncio.to_thread(_open_verified_apk, path, descriptor)
    )
    cancelled = await _async_wait_for_completion(worker)
    if cancelled:
        try:
            file_descriptor = worker.result()
        except BaseException:
            pass
        else:
            with suppress(OSError):
                os.close(file_descriptor)
        raise asyncio.CancelledError
    return worker.result()


async def _async_verify_remote_artifact(
    device: AdbDeviceAsync, remote_path: str, descriptor: InstallDescriptor
) -> None:
    nonce = token_hex(16)
    body = await _async_shell(
        device,
        _remote_artifact_command(nonce, remote_path),
        read_timeout=_INSTALL_READ_TIMEOUT_SECONDS,
        transport_timeout=_INSTALL_READ_TIMEOUT_SECONDS,
    )
    try:
        mode, size, sha256 = _parse_remote_artifact(body, nonce, remote_path)
    except _MalformedAdbResponse:
        raise InstallAdbError(InstallAdbErrorCode.STAGE_VERIFICATION_FAILED) from None
    if (
        not stat.S_ISREG(mode)
        or stat.S_IMODE(mode) != 0o644
        or size != descriptor.apk_size
        or sha256 != descriptor.apk_sha256
    ):
        raise InstallAdbError(InstallAdbErrorCode.STAGE_VERIFICATION_FAILED)


async def async_preflight_install(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    descriptor: InstallDescriptor,
) -> AdbPreflight:
    """Re-prove clean admission without mutating the panel."""
    _validate_request(target, descriptor)
    device: AdbDeviceAsync | None = None
    try:
        async with asyncio.timeout(_PREFLIGHT_TIMEOUT_SECONDS):
            device = await _async_connect(target, signer)
            return await _async_preflight_on_device(device, target, descriptor)
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        *_ADB_EXCEPTIONS,
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_UNREACHABLE) from None
    finally:
        await _async_close(device)


async def async_verify_installed_target(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    *,
    expected_root_mode: AdbRootMode,
) -> None:
    """Re-prove the exact target and installed package without mutation."""
    if not isinstance(target, AdbInstallTarget):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    _validate_target(target)
    _validate_expected_root_mode(expected_root_mode)
    device: AdbDeviceAsync | None = None
    try:
        async with asyncio.timeout(_PREFLIGHT_TIMEOUT_SECONDS):
            device = await _async_connect(target, signer)
            await _async_require_identity_root(device, target, expected_root_mode)
            nonce = token_hex(16)
            _parse_package_present(
                await _async_shell(
                    device,
                    _package_command(nonce),
                    read_timeout=_READ_TIMEOUT_SECONDS,
                ),
                nonce,
            )
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        AdbConnectionError,
        AdbTimeoutError,
        OSError,
        TcpTimeoutException,
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_UNREACHABLE) from None
    except (
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        DevicePathInvalidError,
        InvalidChecksumError,
        InvalidCommandError,
        InvalidResponseError,
        InvalidTransportError,
        PushFailedError,
    ):
        raise InstallAdbError(InstallAdbErrorCode.TARGET_RESPONSE_INVALID) from None
    finally:
        await _async_close(device)


async def async_stage_apk(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    descriptor: InstallDescriptor,
    job_id: str,
    local_apk: Path,
    *,
    expected_root_mode: AdbRootMode,
) -> StagedApk:
    """Revalidate, push and authenticate one exact job-owned APK."""
    _validate_request(target, descriptor)
    _validate_expected_root_mode(expected_root_mode)
    remote_path = _remote_path(job_id)
    if not isinstance(local_apk, Path):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    device: AdbDeviceAsync | None = None
    file_descriptor: int | None = None
    mutation_started = False
    try:
        async with asyncio.timeout(_STAGE_TIMEOUT_SECONDS):
            file_descriptor = await _async_open_verified_apk(local_apk, descriptor)
            device = await _async_connect(target, signer)
            preflight = await _async_preflight_on_device(device, target, descriptor)
            _require_expected_root_mode(preflight.root_mode, expected_root_mode)
            nonce = token_hex(16)
            occupied = _parse_path_state(
                await _async_shell(
                    device,
                    _path_state_command(nonce, remote_path),
                    read_timeout=_READ_TIMEOUT_SECONDS,
                ),
                nonce,
            )
            if occupied:
                raise InstallAdbError(InstallAdbErrorCode.STAGING_PATH_OCCUPIED)
            _validate_filesync_maxdata(device)
            mutation_started = True
            await device.push(
                f"/proc/self/fd/{file_descriptor}",
                remote_path,
                st_mode=_REMOTE_MODE,
                mtime=1,
                transport_timeout_s=_TRANSPORT_TIMEOUT_SECONDS,
                read_timeout_s=_INSTALL_READ_TIMEOUT_SECONDS,
            )
            await _async_verify_remote_artifact(device, remote_path, descriptor)
            return StagedApk(
                job_id=job_id,
                remote_path=remote_path,
                apk_size=descriptor.apk_size,
                apk_sha256=descriptor.apk_sha256,
            )
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        *_ADB_EXCEPTIONS,
    ):
        code = (
            InstallAdbErrorCode.STAGE_AMBIGUOUS
            if mutation_started
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code) from None
    finally:
        try:
            await _async_close(device)
        finally:
            if file_descriptor is not None:
                descriptor_to_close = file_descriptor
                file_descriptor = None
                os.close(descriptor_to_close)


async def async_install_staged_apk(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    descriptor: InstallDescriptor,
    job_id: str,
    *,
    expected_root_mode: AdbRootMode,
) -> InstallOutcome:
    """Revalidate and run exactly one non-replacing package installation."""
    _validate_request(target, descriptor)
    _validate_expected_root_mode(expected_root_mode)
    remote_path = _remote_path(job_id)
    device: AdbDeviceAsync | None = None
    mutation_started = False
    try:
        async with asyncio.timeout(_INSTALL_TIMEOUT_SECONDS):
            device = await _async_connect(target, signer)
            preflight = await _async_preflight_on_device(device, target, descriptor)
            _require_expected_root_mode(preflight.root_mode, expected_root_mode)
            await _async_verify_remote_artifact(device, remote_path, descriptor)
            nonce = token_hex(16)
            mutation_started = True
            body = await _async_shell(
                device,
                _install_command(nonce, remote_path, target.android_sdk),
                read_timeout=_INSTALL_TIMEOUT_SECONDS,
                transport_timeout=_INSTALL_TIMEOUT_SECONDS,
            )
            return _parse_install_outcome(body, nonce)
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        *_ADB_EXCEPTIONS,
    ):
        code = (
            InstallAdbErrorCode.INSTALL_AMBIGUOUS
            if mutation_started
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code) from None
    finally:
        await _async_close(device)


async def async_launch_installed_app(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    descriptor: InstallDescriptor,
    *,
    expected_root_mode: AdbRootMode,
) -> LaunchOutcome:
    """Launch the descriptor's one fixed package component exactly once."""
    _validate_request(target, descriptor)
    _validate_expected_root_mode(expected_root_mode)
    device: AdbDeviceAsync | None = None
    mutation_started = False
    try:
        async with asyncio.timeout(_LAUNCH_TIMEOUT_SECONDS):
            device = await _async_connect(target, signer)
            await _async_require_identity_root(device, target, expected_root_mode)
            nonce = token_hex(16)
            _parse_package_present(
                await _async_shell(
                    device,
                    _package_command(nonce),
                    read_timeout=_READ_TIMEOUT_SECONDS,
                ),
                nonce,
            )
            nonce = token_hex(16)
            mutation_started = True
            return _parse_launch_outcome(
                await _async_shell(
                    device,
                    _launch_command(nonce),
                    read_timeout=_INSTALL_READ_TIMEOUT_SECONDS,
                    transport_timeout=_INSTALL_READ_TIMEOUT_SECONDS,
                ),
                nonce,
            )
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        *_ADB_EXCEPTIONS,
    ):
        code = (
            InstallAdbErrorCode.LAUNCH_AMBIGUOUS
            if mutation_started
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code) from None
    finally:
        await _async_close(device)


async def async_cleanup_staged_apk(
    target: AdbInstallTarget,
    signer: PythonRSASigner,
    staged: StagedApk,
    reason: DefiniteCleanupReason,
    *,
    expected_root_mode: AdbRootMode,
) -> None:
    """Delete one verified job-owned stage after a definite disposition."""
    if not isinstance(target, AdbInstallTarget) or not isinstance(
        reason, DefiniteCleanupReason
    ):
        raise InstallAdbError(InstallAdbErrorCode.INVALID_REQUEST)
    _validate_target(target)
    _validate_expected_root_mode(expected_root_mode)
    remote_path = _validate_staged_apk(staged)
    device: AdbDeviceAsync | None = None
    mutation_started = False
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
            device = await _async_connect(target, signer)
            await _async_require_identity_root(device, target, expected_root_mode)
            nonce = token_hex(16)
            mutation_started = True
            _parse_cleanup(
                await _async_shell(
                    device,
                    _cleanup_command(nonce, remote_path),
                    read_timeout=_READ_TIMEOUT_SECONDS,
                ),
                nonce,
            )
    except InstallAdbError:
        raise
    except (
        TimeoutError,
        _MalformedAdbResponse,
        _UnsafeAdbPacket,
        *_ADB_EXCEPTIONS,
    ):
        code = (
            InstallAdbErrorCode.CLEANUP_AMBIGUOUS
            if mutation_started
            else InstallAdbErrorCode.TARGET_UNREACHABLE
        )
        raise InstallAdbError(code) from None
    finally:
        await _async_close(device)
