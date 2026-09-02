"""Read-only Android Debug Bridge preflight for panel installation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from re import ASCII, fullmatch
from secrets import token_hex

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.exceptions import DeviceAuthError

from .client import PanelAddress

ADB_PORT = 5555
_ADB_BANNER = "ha-paneld-home-assistant"
_CONNECT_TIMEOUT_SECONDS = 5.0
_SHELL_TIMEOUT_SECONDS = 5.0
_CLOSE_TIMEOUT_SECONDS = 2.0
_MAX_SHELL_RESPONSE_BYTES = 16 * 1024
_PACKAGE = "io.github.maxlyth.hapaneld"
_PACKAGE_MANAGER_LIVENESS_PACKAGE = "android"
_MIN_ANDROID_SDK = 26
_SUPPORTED_PRIMARY_ABIS = frozenset({"arm64-v8a", "armeabi-v7a"})


class InstallTargetState(StrEnum):
    """Read-only classification of an Android installation target."""

    ADB_UNREACHABLE = "adb_unreachable"
    ADB_UNAUTHORIZED = "adb_unauthorized"
    CLEAN = "clean"
    INCOMPATIBLE = "incompatible"
    INSTALLED = "installed"
    RETAINED_OR_AMBIGUOUS = "retained_or_ambiguous"


@dataclass(frozen=True, slots=True)
class InstallTargetProbe:
    """Result of the read-only ADB installation-target preflight."""

    state: InstallTargetState
    model: str | None = None
    serial: str | None = None
    primary_abi: str | None = None
    android_sdk: int | None = None


class _MalformedProbeResponse(Exception):
    """Raised when ADB answered but did not prove a safe classification."""


class _PackagePresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class _RetainedPackageData(StrEnum):
    RETAINED = "retained"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class _TargetFacts:
    model: str
    serial: str
    primary_abi: str
    android_sdk: int


def _parse_status_marker(line: str, prefix: str, nonce: str) -> int | None:
    """Parse one nonce-bound child exit-status marker."""
    match = fullmatch(rf"{prefix}:{nonce}:([0-9]+)", line)
    if match is None:
        return None
    return int(match.group(1))


def _is_package_path(line: str) -> bool:
    """Return whether a package-manager path is absolute and unambiguous."""
    return fullmatch(r"package:/[^ \t]+", line) is not None


def _parse_package_presence(output: str, nonce: str) -> _PackagePresence:
    """Classify an installed path only from a complete nonce-bound observation."""
    begin = f"HAPANELD_PKG_BEGIN:{nonce}"
    target_prefix = "HAPANELD_PKG_TARGET"
    live_prefix = "HAPANELD_PKG_LIVE"
    end = f"HAPANELD_PKG_END:{nonce}"
    segment = 0
    target_status: int | None = None
    live_status: int | None = None
    target_path = False
    live_path = False

    for line in output.replace("\r", "").splitlines():
        if line == begin:
            if segment != 0:
                return _PackagePresence.UNKNOWN
            segment = 1
            continue

        status = _parse_status_marker(line, target_prefix, nonce)
        if status is not None:
            if segment != 1:
                return _PackagePresence.UNKNOWN
            target_status = status
            segment = 2
            continue

        status = _parse_status_marker(line, live_prefix, nonce)
        if status is not None:
            if segment != 2:
                return _PackagePresence.UNKNOWN
            live_status = status
            segment = 3
            continue

        if line == end:
            if segment != 3:
                return _PackagePresence.UNKNOWN
            segment = 4
            continue

        if line.startswith("HAPANELD_PKG_"):
            return _PackagePresence.UNKNOWN
        if line.startswith("package:"):
            if not _is_package_path(line):
                return _PackagePresence.UNKNOWN
            if segment == 1:
                target_path = True
            elif segment == 2:
                live_path = True
            else:
                return _PackagePresence.UNKNOWN
            continue
        if line:
            return _PackagePresence.UNKNOWN

    if segment != 4 or live_status != 0 or not live_path:
        return _PackagePresence.UNKNOWN
    if target_path:
        if target_status == 0:
            return _PackagePresence.PRESENT
        return _PackagePresence.UNKNOWN
    if target_status in (0, 1):
        return _PackagePresence.ABSENT
    return _PackagePresence.UNKNOWN


def _parse_retained_package_data(output: str, nonce: str) -> _RetainedPackageData:
    """Classify retained package data from the complete uninstalled-package list."""
    begin = f"HAPANELD_DATA_BEGIN:{nonce}"
    target_prefix = "HAPANELD_DATA_TARGET"
    live_prefix = "HAPANELD_DATA_LIVE"
    end = f"HAPANELD_DATA_END:{nonce}"
    segment = 0
    target_status: int | None = None
    live_status: int | None = None
    retained = False
    live_path = False

    for line in output.replace("\r", "").splitlines():
        if line == begin:
            if segment != 0:
                return _RetainedPackageData.UNKNOWN
            segment = 1
            continue

        status = _parse_status_marker(line, target_prefix, nonce)
        if status is not None:
            if segment != 1:
                return _RetainedPackageData.UNKNOWN
            target_status = status
            segment = 2
            continue

        status = _parse_status_marker(line, live_prefix, nonce)
        if status is not None:
            if segment != 2:
                return _RetainedPackageData.UNKNOWN
            live_status = status
            segment = 3
            continue

        if line == end:
            if segment != 3:
                return _RetainedPackageData.UNKNOWN
            segment = 4
            continue

        if line.startswith("HAPANELD_DATA_"):
            return _RetainedPackageData.UNKNOWN
        if segment == 1:
            if line == f"package:{_PACKAGE}":
                retained = True
            elif line:
                return _RetainedPackageData.UNKNOWN
        elif line.startswith("package:"):
            if segment != 2 or not _is_package_path(line):
                return _RetainedPackageData.UNKNOWN
            live_path = True
        elif line:
            return _RetainedPackageData.UNKNOWN

    if segment != 4 or target_status != 0 or live_status != 0 or not live_path:
        return _RetainedPackageData.UNKNOWN
    if retained:
        return _RetainedPackageData.RETAINED
    return _RetainedPackageData.ABSENT


def _parse_target_facts(output: str, nonce: str) -> _TargetFacts:
    """Parse four bounded properties from an exact nonce-bound response."""
    property_names = ("MODEL", "SERIAL", "ABI", "SDK")
    lines = output.replace("\r", "").splitlines()
    expected_lines = 2 + 3 * len(property_names)
    if (
        len(lines) != expected_lines
        or lines[0] != f"HAPANELD_ID_BEGIN:{nonce}"
        or lines[-1] != f"HAPANELD_ID_END:{nonce}"
    ):
        raise _MalformedProbeResponse

    values: dict[str, str] = {}
    offset = 1
    for name in property_names:
        if lines[offset] != f"HAPANELD_ID_{name}_BEGIN:{nonce}":
            raise _MalformedProbeResponse
        value = lines[offset + 1]
        status = _parse_status_marker(
            lines[offset + 2], f"HAPANELD_ID_{name}_END", nonce
        )
        if status != 0:
            raise _MalformedProbeResponse
        values[name] = value
        offset += 3

    model = values["MODEL"]
    serial = values["SERIAL"]
    primary_abi = values["ABI"]
    sdk_text = values["SDK"]
    if (
        model != model.strip()
        or not 1 <= len(model) <= 128
        or not model.isprintable()
        or fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial, flags=ASCII) is None
        or fullmatch(r"[A-Za-z0-9_.-]{1,64}", primary_abi, flags=ASCII) is None
        or fullmatch(r"[0-9]{1,3}", sdk_text, flags=ASCII) is None
    ):
        raise _MalformedProbeResponse

    android_sdk = int(sdk_text)
    if not 1 <= android_sdk <= 100:
        raise _MalformedProbeResponse
    return _TargetFacts(
        model=model,
        serial=serial,
        primary_abi=primary_abi,
        android_sdk=android_sdk,
    )


def _package_presence_command(nonce: str) -> str:
    """Build the static read-only installed-package observation."""
    return (
        f"echo HAPANELD_PKG_BEGIN:{nonce}; "
        f"pm path {_PACKAGE}; "
        f"echo HAPANELD_PKG_TARGET:{nonce}:$?; "
        f"pm path {_PACKAGE_MANAGER_LIVENESS_PACKAGE}; "
        f"echo HAPANELD_PKG_LIVE:{nonce}:$?; "
        f"echo HAPANELD_PKG_END:{nonce}"
    )


def _retained_package_data_command(nonce: str) -> str:
    """Build the static read-only retained-package observation."""
    return (
        f"echo HAPANELD_DATA_BEGIN:{nonce}; "
        f"pm list packages -u {_PACKAGE}; "
        f"echo HAPANELD_DATA_TARGET:{nonce}:$?; "
        f"pm path {_PACKAGE_MANAGER_LIVENESS_PACKAGE}; "
        f"echo HAPANELD_DATA_LIVE:{nonce}:$?; "
        f"echo HAPANELD_DATA_END:{nonce}"
    )


def _target_facts_command(nonce: str) -> str:
    """Build the static read-only physical-target identification observation."""
    properties = (
        ("MODEL", "ro.product.model"),
        ("SERIAL", "ro.serialno"),
        ("ABI", "ro.product.cpu.abi"),
        ("SDK", "ro.build.version.sdk"),
    )
    commands = [f"echo HAPANELD_ID_BEGIN:{nonce}"]
    for name, android_property in properties:
        commands.extend(
            (
                f"echo HAPANELD_ID_{name}_BEGIN:{nonce}",
                f"getprop {android_property}",
                f"echo HAPANELD_ID_{name}_END:{nonce}:$?",
            )
        )
    commands.append(f"echo HAPANELD_ID_END:{nonce}")
    return "; ".join(commands)


async def _async_bounded_shell(device: AdbDeviceTcpAsync, command: str) -> str:
    """Run a read-only shell observation with time and output bounds."""
    body = bytearray()
    async with asyncio.timeout(_SHELL_TIMEOUT_SECONDS):
        async for chunk in device.streaming_shell(
            command,
            transport_timeout_s=_SHELL_TIMEOUT_SECONDS,
            read_timeout_s=_SHELL_TIMEOUT_SECONDS,
            decode=False,
        ):
            if not isinstance(chunk, bytes):
                raise _MalformedProbeResponse
            if len(body) + len(chunk) > _MAX_SHELL_RESPONSE_BYTES:
                raise _MalformedProbeResponse
            body.extend(chunk)

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as err:
        raise _MalformedProbeResponse from err


async def _async_close(device: AdbDeviceTcpAsync) -> None:
    """Bound cleanup without allowing it to hide the probe result."""
    with suppress(Exception):
        async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
            await device.close()


async def async_probe_install_target(address: PanelAddress) -> InstallTargetProbe:
    """Read-only probe of whether an Android target is safe for first install.

    No key is generated or sent. A target that requests authentication is reported
    separately so a later, explicit flow can own the user's trust decision.
    """
    device = AdbDeviceTcpAsync(
        address.host,
        port=ADB_PORT,
        default_transport_timeout_s=_CONNECT_TIMEOUT_SECONDS,
        banner=_ADB_BANNER,
    )
    state = InstallTargetState.ADB_UNREACHABLE
    facts: _TargetFacts | None = None

    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
            connected = await device.connect(
                rsa_keys=[],
                transport_timeout_s=_CONNECT_TIMEOUT_SECONDS,
                auth_timeout_s=_CONNECT_TIMEOUT_SECONDS,
                read_timeout_s=_CONNECT_TIMEOUT_SECONDS,
            )
        if not connected:
            return InstallTargetProbe(state=state)

        nonce = token_hex(16)
        presence = _parse_package_presence(
            await _async_bounded_shell(device, _package_presence_command(nonce)),
            nonce,
        )
        if presence is _PackagePresence.PRESENT:
            state = InstallTargetState.INSTALLED
        elif presence is _PackagePresence.ABSENT:
            nonce = token_hex(16)
            retained_data = _parse_retained_package_data(
                await _async_bounded_shell(
                    device, _retained_package_data_command(nonce)
                ),
                nonce,
            )
            if retained_data is _RetainedPackageData.ABSENT:
                nonce = token_hex(16)
                facts = _parse_target_facts(
                    await _async_bounded_shell(device, _target_facts_command(nonce)),
                    nonce,
                )
                if (
                    facts.android_sdk < _MIN_ANDROID_SDK
                    or facts.primary_abi not in _SUPPORTED_PRIMARY_ABIS
                ):
                    state = InstallTargetState.INCOMPATIBLE
                else:
                    state = InstallTargetState.CLEAN
            else:
                state = InstallTargetState.RETAINED_OR_AMBIGUOUS
        else:
            state = InstallTargetState.RETAINED_OR_AMBIGUOUS
    except DeviceAuthError:
        state = InstallTargetState.ADB_UNAUTHORIZED
    except _MalformedProbeResponse:
        state = InstallTargetState.RETAINED_OR_AMBIGUOUS
    except Exception:
        state = InstallTargetState.ADB_UNREACHABLE
    finally:
        await _async_close(device)

    if facts is not None:
        return InstallTargetProbe(
            state=state,
            model=facts.model,
            serial=facts.serial,
            primary_abi=facts.primary_abi,
            android_sdk=facts.android_sdk,
        )
    return InstallTargetProbe(state=state)
