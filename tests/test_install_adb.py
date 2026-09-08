"""Tests for bounded, clean-install ADB primitives."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import stat
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbConnectionError,
    DeviceAuthError,
    InvalidResponseError,
)
from adb_shell.transport.tcp_transport_async import TcpTransportAsync

from custom_components.panel_assistant import install_adb
from custom_components.panel_assistant.client import PanelAddress
from custom_components.panel_assistant.install_adb import (
    AdbInstallTarget,
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
    async_verify_installed_target,
)
from custom_components.panel_assistant.release import InstallDescriptor

NONCES = tuple(f"{number:032x}" for number in range(1, 20))
JOB_ID = "a" * 32
REMOTE_PATH = f"/data/local/tmp/ha-paneld-install-{JOB_ID}.apk"
APK_BYTES = b"signed apk fixture"
APK_SHA256 = hashlib.sha256(APK_BYTES).hexdigest()


def _staged() -> StagedApk:
    return StagedApk(
        job_id=JOB_ID,
        remote_path=REMOTE_PATH,
        apk_size=len(APK_BYTES),
        apk_sha256=APK_SHA256,
    )


def _write_private_apk(path: Path, body: bytes = APK_BYTES) -> None:
    path.write_bytes(body)
    path.chmod(0o600)


@pytest.fixture
def signer() -> PythonRSASigner:
    """Return an identity-only signer; fake ADB never asks it to sign."""
    return object.__new__(PythonRSASigner)


@pytest.fixture
def target() -> AdbInstallTarget:
    return AdbInstallTarget(
        address=PanelAddress(host="192.168.1.23", port=8888),
        serial="SERIAL-1",
        model="Test Panel",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )


@pytest.fixture
def descriptor() -> InstallDescriptor:
    return InstallDescriptor(
        schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v0.9.7",
        version_name="0.9.7",
        version_code=678,
        apk_name="ha-paneld-v0.9.7-manual-setup-required.apk",
        apk_size=len(APK_BYTES),
        apk_sha256=APK_SHA256,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=(
            "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
        ),
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:1:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )


def _section(
    prefix: str, name: str, nonce: str, lines: list[str], status: int
) -> list[str]:
    return [
        f"HAPANELD_{prefix}_{name}_BEGIN:{nonce}",
        *lines,
        f"HAPANELD_{prefix}_{name}_END:{nonce}:{status}",
    ]


def _identity_sections(
    prefix: str,
    nonce: str,
    *,
    model: str = "Test Panel",
    serial: str = "SERIAL-1",
    abi: str = "arm64-v8a",
    sdk: int = 34,
) -> list[str]:
    values = (("MODEL", model), ("SERIAL", serial), ("ABI", abi), ("SDK", str(sdk)))
    lines: list[str] = []
    for name, value in values:
        lines.extend(_section(prefix, name, nonce, [value], 0))
    return lines


def _preflight_output(
    nonce: str,
    *,
    model: str = "Test Panel",
    serial: str = "SERIAL-1",
    abi: str = "arm64-v8a",
    sdk: int = 34,
    uid: str = "2000",
    secure: str = "1",
    debuggable: str = "0",
    su_lines: list[str] | None = None,
    su_status: int = 0,
    package_lines: list[str] | None = None,
    retained_lines: list[str] | None = None,
    unreadable_base: int | None = None,
    residue_index: int | None = None,
) -> bytes:
    prefix = "PREFLIGHT"
    lines = [f"HAPANELD_{prefix}_BEGIN:{nonce}"]
    lines.extend(
        _identity_sections(prefix, nonce, model=model, serial=serial, abi=abi, sdk=sdk)
    )
    lines.extend(_section(prefix, "UID", nonce, [uid], 0))
    lines.extend(_section(prefix, "SECURE", nonce, [secure], 0))
    lines.extend(_section(prefix, "DEBUGGABLE", nonce, [debuggable], 0))
    lines.extend(
        _section(
            prefix,
            "SU",
            nonce,
            ["absent"] if su_lines is None else su_lines,
            su_status,
        )
    )
    lines.extend(_section(prefix, "PACKAGE", nonce, package_lines or [], 1))
    lines.extend(_section(prefix, "RETAINED", nonce, retained_lines or [], 0))
    lines.extend(
        _section(
            prefix,
            "LIVE",
            nonce,
            ["package:/system/framework/framework-res.apk"],
            0,
        )
    )
    for index in range(len(install_adb._ROOT_DATA_BASES)):
        state = "unreadable" if index == unreadable_base else "readable"
        lines.extend(_section(prefix, f"BASE{index}", nonce, [state], 0))
    for index in range(len(install_adb._RESIDUE_PATHS)):
        state = "present" if index == residue_index else "absent"
        lines.extend(_section(prefix, f"RESIDUE{index}", nonce, [state], 0))
    lines.append(f"HAPANELD_{prefix}_END:{nonce}")
    return ("\n".join(lines) + "\n").encode()


def _identity_root_output(
    nonce: str,
    *,
    serial: str = "SERIAL-1",
    model: str = "Test Panel",
    abi: str = "arm64-v8a",
    sdk: int = 34,
    uid: str = "2000",
    secure: str = "1",
    debuggable: str = "0",
    su_lines: list[str] | None = None,
    su_status: int = 0,
) -> bytes:
    prefix = "POSTURE"
    lines = [f"HAPANELD_{prefix}_BEGIN:{nonce}"]
    lines.extend(
        _identity_sections(prefix, nonce, serial=serial, model=model, abi=abi, sdk=sdk)
    )
    lines.extend(_section(prefix, "UID", nonce, [uid], 0))
    lines.extend(_section(prefix, "SECURE", nonce, [secure], 0))
    lines.extend(_section(prefix, "DEBUGGABLE", nonce, [debuggable], 0))
    lines.extend(
        _section(
            prefix,
            "SU",
            nonce,
            ["absent"] if su_lines is None else su_lines,
            su_status,
        )
    )
    lines.append(f"HAPANELD_{prefix}_END:{nonce}")
    return ("\n".join(lines) + "\n").encode()


def _single_output(prefix: str, nonce: str, lines: list[str], status: int) -> bytes:
    return (
        "\n".join(
            [
                f"HAPANELD_{prefix}_BEGIN:{nonce}",
                *lines,
                f"HAPANELD_{prefix}_END:{nonce}:{status}",
            ]
        )
        + "\n"
    ).encode()


def _remote_output(
    nonce: str,
    *,
    size: int = len(APK_BYTES),
    sha256: str = APK_SHA256,
    mode: str = "81a4",
) -> bytes:
    prefix = "ARTIFACT"
    lines = [f"HAPANELD_{prefix}_BEGIN:{nonce}"]
    lines.extend(_section(prefix, "CHMOD", nonce, [], 0))
    lines.extend(_section(prefix, "MODE", nonce, [mode], 0))
    lines.extend(_section(prefix, "SIZE", nonce, [str(size)], 0))
    lines.extend(_section(prefix, "SHA", nonce, [f"{sha256}  {REMOTE_PATH}"], 0))
    lines.append(f"HAPANELD_{prefix}_END:{nonce}")
    return ("\n".join(lines) + "\n").encode()


def _cleanup_output(nonce: str) -> bytes:
    prefix = "CLEANUP"
    lines = [f"HAPANELD_{prefix}_BEGIN:{nonce}"]
    lines.extend(_section(prefix, "RM", nonce, [], 0))
    lines.extend(_section(prefix, "STATE", nonce, ["absent"], 0))
    lines.append(f"HAPANELD_{prefix}_END:{nonce}")
    return ("\n".join(lines) + "\n").encode()


def _su_output(
    nonce: str,
    *,
    clean: bool = True,
    uid: str = "0",
    unreadable_base: int | None = None,
    residue_index: int | None = None,
) -> bytes:
    lines = [f"HAPANELD_DELEGATE_BEGIN:{nonce}", f"HAPANELD_SU_BEGIN:{nonce}"]
    lines.extend(_section("SU", "UID", nonce, [uid], 0))
    if clean:
        for index in range(3):
            value = "unreadable" if index == unreadable_base else "readable"
            lines.extend(_section("SU", f"BASE{index}", nonce, [value], 0))
        for index in range(3):
            value = "present" if index == residue_index else "absent"
            lines.extend(_section("SU", f"RESIDUE{index}", nonce, [value], 0))
    lines.extend([f"HAPANELD_SU_END:{nonce}", f"HAPANELD_DELEGATE_END:{nonce}:0"])
    return ("\n".join(lines) + "\n").encode()


class FakeDevice:
    def __init__(
        self,
        outputs: list[bytes | BaseException],
        *,
        maxdata: object = 64 * 1024,
        connect_error: BaseException | None = None,
        push_error: BaseException | None = None,
        prompt_for_authorization: bool = False,
    ) -> None:
        self.outputs = iter(outputs)
        self._maxdata = maxdata
        self.connect_error = connect_error
        self.push_error = push_error
        self.prompt_for_authorization = prompt_for_authorization
        self.constructor: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self.connect_kwargs: dict[str, Any] | None = None
        self.commands: list[str] = []
        self.shell_kwargs: list[dict[str, Any]] = []
        self.pushes: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    async def connect(self, **kwargs: Any) -> bool:
        self.connect_kwargs = kwargs
        if self.prompt_for_authorization:
            kwargs["auth_callback"](self)
        if self.connect_error is not None:
            raise self.connect_error
        return True

    async def streaming_shell(
        self, command: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        self.commands.append(command)
        self.shell_kwargs.append(kwargs)
        output = next(self.outputs)
        if isinstance(output, BaseException):
            raise output
        yield output

    async def push(self, *args: Any, **kwargs: Any) -> None:
        self.pushes.append((args, kwargs))
        if self.push_error is not None:
            raise self.push_error

    async def close(self) -> None:
        self.closed = True


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, devices: list[FakeDevice]
) -> Iterator[FakeDevice]:
    device_iterator = iter(devices)
    nonces = iter(NONCES)
    monkeypatch.setattr(install_adb, "token_hex", lambda _length: next(nonces))

    def factory(*args: Any, **kwargs: Any) -> FakeDevice:
        device = next(device_iterator)
        device.constructor = (args, kwargs)
        return device

    monkeypatch.setattr(install_adb, "AdbDeviceAsync", factory)
    return device_iterator


async def test_preflight_admits_positive_rootless_target_with_persistent_signer(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0])])
    _install_fakes(monkeypatch, [fake])

    proof = await async_preflight_install(target, signer, descriptor)

    assert proof.root_mode is AdbRootMode.ROOTLESS
    assert proof.serial == target.serial
    assert fake.connect_kwargs is not None
    assert fake.connect_kwargs["rsa_keys"] == [signer]
    assert fake.connect_kwargs["transport_timeout_s"] == 10.0
    assert fake.connect_kwargs["auth_timeout_s"] == 5.0
    assert fake.connect_kwargs["read_timeout_s"] == 10.0
    assert callable(fake.connect_kwargs["auth_callback"])
    assert fake.closed is True
    assert len(fake.commands) == 1
    assert "command -v su >/dev/null 2>&1" in fake.commands[0]
    assert "echo absent" in fake.commands[0]
    assert "echo abnormal" in fake.commands[0]
    assert "su -c" not in fake.commands[0]
    assert "adb root" not in fake.commands[0]
    assert fake.constructor is not None
    constructor_args, constructor_kwargs = fake.constructor
    assert len(constructor_args) == 1
    transport = constructor_args[0]
    assert isinstance(transport, install_adb._BoundedTcpTransportAsync)
    assert transport._host == "192.168.1.23"
    assert transport._port == 5555
    assert constructor_kwargs == {
        "default_transport_timeout_s": 10.0,
        "banner": "ha-paneld-home-assistant",
    }


@pytest.mark.parametrize("dialect", range(5))
async def test_su_preflight_requires_exact_root_inventory_proof(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    dialect: int,
) -> None:
    failures = [
        f"HAPANELD_DELEGATE_BEGIN:{nonce}\nunsupported dialect\n"
        f"HAPANELD_DELEGATE_END:{nonce}:1\n".encode()
        for nonce in NONCES[1 : dialect + 1]
    ]
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0], su_lines=["present"], unreadable_base=0),
            *failures,
            _su_output(NONCES[dialect + 1]),
        ]
    )
    _install_fakes(monkeypatch, [fake])
    proof = await async_preflight_install(target, signer, descriptor)
    assert proof.root_mode is AdbRootMode.ROOT_SU
    assert len(fake.commands) == dialect + 2
    assert fake.closed
    assert not fake.pushes
    command = fake.commands[-1]
    prefix = install_adb._SU_PREFIXES[dialect]
    payload = install_adb._su_inspection_command(NONCES[dialect + 1], clean=True)
    assert f"{prefix} {shlex.quote(payload)};" in command
    assert shlex.split(f"{prefix} {shlex.quote(payload)}")[-1] == payload
    for path in install_adb._ROOT_DATA_BASES:
        assert f"ls -1A {path} >/dev/null" in payload
    for path in install_adb._RESIDUE_PATHS:
        assert f"[ -e {path} ] || [ -L {path} ]" in payload
    assert all(
        "pm install" not in cmd and "adb root" not in cmd for cmd in fake.commands
    )


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("kind", ["unreadable_base", "residue_index"])
async def test_su_cannot_admit_residue_or_unreadable_inventory(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    index: int,
    kind: str,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0], su_lines=["present"]),
            _su_output(NONCES[1], **{kind: index}),
        ]
    )
    _install_fakes(monkeypatch, [fake])
    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)
    expected = (
        InstallAdbErrorCode.TARGET_NOT_CLEAN
        if kind == "residue_index"
        else InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS
    )
    assert caught.value.code is expected
    assert len(fake.commands) == 2
    assert not fake.pushes


@pytest.mark.parametrize(
    "output",
    [
        TimeoutError(),
        b"denied\n",
        _su_output(NONCES[1], uid="2000"),
        _su_output(NONCES[1]).replace(b"\n0\n", b"\nuid=0 noisy\n"),
        _su_output(NONCES[1]).replace(
            b"HAPANELD_SU_BEGIN", b"noise\nHAPANELD_SU_BEGIN"
        ),
        _su_output(NONCES[2]),
    ],
)
async def test_su_timeout_noise_nonroot_or_wrong_nonce_never_admits(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    output: bytes | BaseException,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], su_lines=["present"]), output])
    _install_fakes(monkeypatch, [fake])
    with pytest.raises(InstallAdbError):
        await async_preflight_install(target, signer, descriptor)
    assert len(fake.commands) == 2
    assert fake.closed
    assert not fake.pushes


async def test_existing_package_short_circuits_before_su_invocation(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(
                NONCES[0],
                su_lines=["present"],
                package_lines=["package:/data/app/base.apk"],
            )
        ]
    )
    _install_fakes(monkeypatch, [fake])
    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)
    assert caught.value.code is InstallAdbErrorCode.TARGET_NOT_CLEAN
    assert fake.commands == [install_adb._preflight_command(NONCES[0])]


async def test_su_denial_stops_after_five_bounded_dialects(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0], su_lines=["present"]),
            *[
                _single_output("DELEGATE", nonce, ["denied"], 1)
                for nonce in NONCES[1:6]
            ],
        ]
    )
    _install_fakes(monkeypatch, [fake])
    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)
    assert caught.value.code is InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS
    assert len(fake.commands) == 6
    assert all(kwargs["read_timeout_s"] == 3.0 for kwargs in fake.shell_kwargs[1:])
    assert fake.closed


@pytest.mark.parametrize("operation", ["stage", "install", "launch", "cleanup"])
async def test_su_never_wraps_mutation_commands(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    tmp_path: Path,
    operation: str,
) -> None:
    clean = operation in ("stage", "install")
    initial = _preflight_output if clean else _identity_root_output
    outputs = [
        initial(NONCES[0], su_lines=["present"]),
        _su_output(NONCES[1], clean=clean),
    ]
    if operation == "stage":
        outputs.extend(
            [
                _single_output("PATH", NONCES[2], ["absent"], 0),
                _remote_output(NONCES[3]),
            ]
        )
    elif operation == "install":
        outputs.extend(
            [
                _remote_output(NONCES[2]),
                _single_output("INSTALL", NONCES[3], ["Success"], 0),
            ]
        )
    elif operation == "launch":
        outputs.extend(
            [
                _single_output("PACKAGE", NONCES[2], ["package:/data/app/base.apk"], 0),
                _single_output(
                    "LAUNCH",
                    NONCES[3],
                    [
                        "Starting: Intent { "
                        "cmp=io.github.maxlyth.hapaneld/.MainActivity }"
                    ],
                    0,
                ),
            ]
        )
    else:
        outputs.append(_cleanup_output(NONCES[2]))
    fake = FakeDevice(outputs)
    _install_fakes(monkeypatch, [fake])
    if operation == "stage":
        apk = tmp_path / "fixture.apk"
        _write_private_apk(apk)
        result = await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOT_SU,
        )
        assert result == _staged()
        assert len(fake.pushes) == 1
    elif operation == "install":
        assert (
            await async_install_staged_apk(
                target,
                signer,
                descriptor,
                JOB_ID,
                expected_root_mode=AdbRootMode.ROOT_SU,
            )
            is InstallOutcome.INSTALLED
        )
    elif operation == "launch":
        assert (
            await async_launch_installed_app(
                target, signer, descriptor, expected_root_mode=AdbRootMode.ROOT_SU
            )
            is LaunchOutcome.STARTED
        )
    else:
        await async_cleanup_staged_apk(
            target,
            signer,
            _staged(),
            reason=DefiniteCleanupReason.CANCELLED,
            expected_root_mode=AdbRootMode.ROOT_SU,
        )
    assert "HAPANELD_DELEGATE" in fake.commands[1]
    for command in fake.commands[2:]:
        assert "su " not in command
        assert "HAPANELD_DELEGATE" not in command
    assert fake.closed


async def test_su_posture_barrier_reproves_capability_before_package_query(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0], su_lines=["present"]),
            _su_output(NONCES[1], clean=False, uid="2000"),
        ]
    )
    _install_fakes(monkeypatch, [fake])
    with pytest.raises(InstallAdbError) as caught:
        await async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOT_SU
        )
    assert caught.value.code is InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS
    assert len(fake.commands) == 2
    assert all("pm path" not in command for command in fake.commands)


async def test_bounded_transport_honors_valid_phase_read_and_write_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, float]] = []

    async def bulk_read(
        _self: TcpTransportAsync, _numbytes: int, wait_seconds: float
    ) -> bytes:
        observed.append(("read", wait_seconds))
        return b"x"

    async def bulk_write(
        _self: TcpTransportAsync, data: bytes | bytearray, wait_seconds: float
    ) -> int:
        observed.append(("write", wait_seconds))
        return len(data)

    monkeypatch.setattr(TcpTransportAsync, "bulk_read", bulk_read)
    monkeypatch.setattr(TcpTransportAsync, "bulk_write", bulk_write)
    transport = install_adb._BoundedTcpTransportAsync("192.168.1.23", 5555)

    assert await transport.bulk_read(1, 23.0) == b"x"
    assert await transport.bulk_write(bytearray(b"x"), 179.0) == 1
    assert observed == [("read", 23.0), ("write", 179.0)]


@pytest.mark.parametrize("wait_seconds", [None, 0, -1, 180.1, True, float("inf")])
async def test_bounded_transport_rejects_unbounded_timeout_values(
    wait_seconds: object,
) -> None:
    transport = install_adb._BoundedTcpTransportAsync("192.168.1.23", 5555)

    with pytest.raises(install_adb._UnsafeAdbPacket):
        await transport.bulk_read(1, wait_seconds)  # type: ignore[arg-type]


async def test_root_adbd_requires_all_exact_residue_paths_absent(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], uid="0")])
    _install_fakes(monkeypatch, [fake])

    proof = await async_preflight_install(target, signer, descriptor)

    assert proof.root_mode is AdbRootMode.ROOT_ADBD
    command = fake.commands[0]
    assert all(path in command for path in install_adb._ROOT_DATA_BASES)
    assert all(path in command for path in install_adb._RESIDUE_PATHS)


async def test_root_adbd_refuses_an_unreadable_data_inventory(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], uid="0", unreadable_base=1)])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS


@pytest.mark.parametrize(
    "changes",
    [
        {"uid": "2000", "secure": "0"},
        {"uid": "2000", "debuggable": "1"},
        {"uid": "1000"},
    ],
)
async def test_visible_or_ambiguous_root_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    changes: dict[str, Any],
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], **changes)])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.ROOT_STATE_AMBIGUOUS


@pytest.mark.parametrize(
    ("su_lines", "su_status"),
    [
        (["abnormal"], 0),
        (["absent"], 2),
        (["absent"], 126),
        (["absent"], 127),
        (["absent"], 129),
        (["absent"], 130),
        (["absent"], 137),
        (["absent"], 255),
    ],
)
async def test_abnormal_su_lookup_can_never_prove_rootless(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    su_lines: list[str],
    su_status: int,
) -> None:
    fake = FakeDevice(
        [_preflight_output(NONCES[0], su_lines=su_lines, su_status=su_status)]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.TARGET_UNREACHABLE


@pytest.mark.parametrize(
    "response",
    [
        _preflight_output(NONCES[0], residue_index=2, uid="0"),
        _preflight_output(NONCES[0], residue_index=1),
        _preflight_output(
            NONCES[0],
            package_lines=["package:/data/app/io.github.maxlyth.hapaneld/base.apk"],
        ),
        _preflight_output(
            NONCES[0], retained_lines=["package:io.github.maxlyth.hapaneld"]
        ),
    ],
)
async def test_package_or_root_visible_residue_is_not_clean(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    response: bytes,
) -> None:
    fake = FakeDevice([response])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.TARGET_NOT_CLEAN


@pytest.mark.parametrize(
    ("response", "changed_descriptor", "expected"),
    [
        (
            _preflight_output(NONCES[0], serial="SERIAL-2"),
            None,
            InstallAdbErrorCode.TARGET_CHANGED,
        ),
        (
            _preflight_output(NONCES[0], model="Another Panel"),
            None,
            InstallAdbErrorCode.TARGET_CHANGED,
        ),
        (
            _preflight_output(NONCES[0], abi="armeabi-v7a"),
            None,
            InstallAdbErrorCode.TARGET_CHANGED,
        ),
        (
            _preflight_output(NONCES[0], sdk=33),
            None,
            InstallAdbErrorCode.TARGET_CHANGED,
        ),
        (
            _preflight_output(NONCES[0]),
            {"min_sdk": 35},
            InstallAdbErrorCode.TARGET_INCOMPATIBLE,
        ),
        (
            _preflight_output(NONCES[0]),
            {"supported_abis": ("armeabi-v7a",)},
            InstallAdbErrorCode.INVALID_REQUEST,
        ),
    ],
)
async def test_identity_drift_or_descriptor_incompatibility_blocks_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    response: bytes,
    changed_descriptor: dict[str, Any] | None,
    expected: InstallAdbErrorCode,
) -> None:
    artifact = replace(descriptor, **(changed_descriptor or {}))
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice([response])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            artifact,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is expected
    assert fake.pushes == []


async def test_descriptor_abi_compatibility_is_rechecked_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    x86_target = replace(target, primary_abi="x86_64")
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice([_preflight_output(NONCES[0], abi="x86_64")])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            x86_target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.TARGET_INCOMPATIBLE
    assert fake.pushes == []


async def test_standalone_preflight_rejects_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], serial="SERIAL-2")])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.TARGET_CHANGED


async def test_installed_target_verification_is_fresh_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0]),
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    assert (
        await async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOTLESS
        )
        is None
    )

    assert fake.connect_kwargs is not None
    assert fake.connect_kwargs["rsa_keys"] == [signer]
    assert fake.closed is True
    assert fake.pushes == []
    commands = "\n".join(fake.commands)
    assert len(fake.commands) == 2
    assert "HAPANELD_POSTURE_BEGIN" in fake.commands[0]
    assert "id -u" in fake.commands[0]
    assert "command -v su" in fake.commands[0]
    assert "pm path io.github.maxlyth.hapaneld" in commands
    assert "pm path io.github.maxlyth.hapaneld" in fake.commands[1]
    assert "pm install" not in commands
    assert "am start" not in commands
    assert "rm -f" not in commands
    assert "chmod" not in commands


@pytest.mark.parametrize(
    "identity_changes",
    [
        {"serial": "SERIAL-2"},
        {"model": "Another Panel"},
        {"abi": "armeabi-v7a"},
        {"sdk": 33},
    ],
)
async def test_installed_target_verification_rejects_each_identity_axis_drift(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    identity_changes: dict[str, Any],
) -> None:
    fake = FakeDevice([_identity_root_output(NONCES[0], **identity_changes)])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOTLESS
        )

    assert caught.value.code is InstallAdbErrorCode.TARGET_CHANGED
    assert len(fake.commands) == 1
    assert fake.pushes == []


@pytest.mark.parametrize(
    ("package_response", "expected"),
    [
        (
            _single_output("PACKAGE", NONCES[1], [], 1),
            InstallAdbErrorCode.INSTALLED_PACKAGE_MISSING,
        ),
        (
            _single_output("PACKAGE", NONCES[1], ["malformed"], 0),
            InstallAdbErrorCode.TARGET_RESPONSE_INVALID,
        ),
        (
            _single_output("PACKAGE", NONCES[1], [], 137),
            InstallAdbErrorCode.TARGET_RESPONSE_INVALID,
        ),
    ],
)
async def test_installed_target_verification_distinguishes_missing_from_malformed(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    package_response: bytes,
    expected: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice([_identity_root_output(NONCES[0]), package_response])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOTLESS
        )

    assert caught.value.code is expected
    assert fake.pushes == []


@pytest.mark.parametrize(
    ("connect_error", "expected"),
    [
        (
            DeviceAuthError("private authorization details"),
            InstallAdbErrorCode.AUTHORIZATION_REQUIRED,
        ),
        (
            AdbConnectionError("private network details"),
            InstallAdbErrorCode.TARGET_UNREACHABLE,
        ),
        (
            InvalidResponseError("private malformed peer details"),
            InstallAdbErrorCode.TARGET_RESPONSE_INVALID,
        ),
    ],
)
async def test_installed_target_verification_connection_errors_are_privacy_safe(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    connect_error: BaseException,
    expected: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice([], connect_error=connect_error)
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOTLESS
        )

    assert caught.value.code is expected
    assert str(caught.value) == expected.value
    assert "private" not in str(caught.value)
    assert fake.closed is True


async def test_installed_target_verification_cancellation_closes_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    shell_started = asyncio.Event()

    class CancelledVerifyDevice(FakeDevice):
        async def streaming_shell(
            self, command: str, **kwargs: Any
        ) -> AsyncIterator[bytes]:
            self.commands.append(command)
            self.shell_kwargs.append(kwargs)
            shell_started.set()
            await asyncio.Future()
            yield b"unreachable"

    fake = CancelledVerifyDevice([])
    _install_fakes(monkeypatch, [fake])
    task = asyncio.create_task(
        async_verify_installed_target(
            target, signer, expected_root_mode=AdbRootMode.ROOTLESS
        )
    )
    await shell_started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed is True
    assert fake.pushes == []
    assert len(fake.commands) == 1
    assert "pm install" not in fake.commands[0]
    assert "am start" not in fake.commands[0]
    assert "rm -f" not in fake.commands[0]


async def test_installed_target_verification_accepts_matching_root_adbd_posture(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(
                NONCES[0],
                uid="0",
                secure="0",
                debuggable="1",
                su_lines=["present"],
            ),
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    await async_verify_installed_target(
        target, signer, expected_root_mode=AdbRootMode.ROOT_ADBD
    )

    assert len(fake.commands) == 2
    assert "HAPANELD_POSTURE_BEGIN" in fake.commands[0]
    assert all(path not in fake.commands[0] for path in install_adb._RESIDUE_PATHS)
    assert "pm path io.github.maxlyth.hapaneld" in fake.commands[1]


@pytest.mark.parametrize(
    ("posture", "expected_root_mode", "expected_error"),
    [
        (
            _identity_root_output(NONCES[0]),
            AdbRootMode.ROOT_ADBD,
            InstallAdbErrorCode.ROOT_MODE_CHANGED,
        ),
        (
            _identity_root_output(NONCES[0], su_lines=["abnormal"]),
            AdbRootMode.ROOTLESS,
            InstallAdbErrorCode.TARGET_RESPONSE_INVALID,
        ),
    ],
)
async def test_installed_target_root_drift_or_ambiguity_blocks_package_proof(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    posture: bytes,
    expected_root_mode: AdbRootMode,
    expected_error: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice(
        [
            posture,
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_verify_installed_target(
            target, signer, expected_root_mode=expected_root_mode
        )

    assert caught.value.code is expected_error
    assert len(fake.commands) == 1
    assert "pm path" not in fake.commands[0]
    assert fake.pushes == []


@pytest.mark.parametrize(
    "operation", ["verify", "stage", "install", "launch", "cleanup"]
)
async def test_invalid_expected_root_mode_fails_before_panel_contact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    operation: str,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    device_iterator = _install_fakes(monkeypatch, [])
    invalid_root_mode = "rootless"

    with pytest.raises(InstallAdbError) as caught:
        if operation == "verify":
            await async_verify_installed_target(
                target,
                signer,
                expected_root_mode=invalid_root_mode,  # type: ignore[arg-type]
            )
        elif operation == "stage":
            await async_stage_apk(
                target,
                signer,
                descriptor,
                JOB_ID,
                apk,
                expected_root_mode=invalid_root_mode,  # type: ignore[arg-type]
            )
        elif operation == "install":
            await async_install_staged_apk(
                target,
                signer,
                descriptor,
                JOB_ID,
                expected_root_mode=invalid_root_mode,  # type: ignore[arg-type]
            )
        elif operation == "launch":
            await async_launch_installed_app(
                target,
                signer,
                descriptor,
                expected_root_mode=invalid_root_mode,  # type: ignore[arg-type]
            )
        else:
            await async_cleanup_staged_apk(
                target,
                signer,
                _staged(),
                DefiniteCleanupReason.CANCELLED,
                expected_root_mode=invalid_root_mode,  # type: ignore[arg-type]
            )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST
    with pytest.raises(StopIteration):
        next(device_iterator)


@pytest.mark.parametrize(
    ("preflight", "expected_root_mode", "expected_error"),
    [
        (
            _preflight_output(NONCES[0]),
            AdbRootMode.ROOT_ADBD,
            InstallAdbErrorCode.ROOT_MODE_CHANGED,
        ),
        (
            _preflight_output(NONCES[0], su_lines=["abnormal"]),
            AdbRootMode.ROOTLESS,
            InstallAdbErrorCode.TARGET_UNREACHABLE,
        ),
    ],
)
async def test_stage_root_drift_or_ambiguity_blocks_filesync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    preflight: bytes,
    expected_root_mode: AdbRootMode,
    expected_error: InstallAdbErrorCode,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            preflight,
            _single_output("PATH", NONCES[1], ["absent"], 0),
            _remote_output(NONCES[2]),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=expected_root_mode,
        )

    assert caught.value.code is expected_error
    assert fake.pushes == []
    assert len(fake.commands) == 1
    assert "HAPANELD_PREFLIGHT_BEGIN" in fake.commands[0]


@pytest.mark.parametrize("maxdata", [4095, 1024 * 1024 + 1, True, "65536"])
async def test_peer_maxdata_attack_is_rejected_before_filesync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    maxdata: object,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["absent"], 0),
            _remote_output(NONCES[2]),
        ],
        maxdata=maxdata,
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.FILESYNC_UNSAFE
    assert fake.pushes == []


async def test_stage_pushes_fixed_regular_0644_path_and_verifies_exact_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "name with shell ; metacharacters.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["absent"], 0),
            _remote_output(NONCES[2]),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    staged = await async_stage_apk(
        target,
        signer,
        descriptor,
        JOB_ID,
        apk,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )
    assert staged.remote_path == REMOTE_PATH
    assert len(fake.pushes) == 1
    args, kwargs = fake.pushes[0]
    assert args[1] == REMOTE_PATH
    assert args[0].startswith("/proc/self/fd/")
    assert kwargs["st_mode"] == 0o100644
    assert kwargs["mtime"] == 1
    assert str(apk) not in "\n".join(fake.commands)
    assert len(fake.commands) == 3
    assert "HAPANELD_PREFLIGHT_BEGIN" in fake.commands[0]
    assert "HAPANELD_PATH_BEGIN" in fake.commands[1]
    assert "HAPANELD_ARTIFACT_BEGIN" in fake.commands[2]
    legacy_permissions = kwargs["st_mode"] & 0o777
    legacy_permissions |= (legacy_permissions >> 3) & 0o070
    legacy_permissions |= (legacy_permissions >> 3) & 0o007
    assert legacy_permissions == 0o666
    verification_command = fake.commands[-1]
    assert verification_command.index(f"chmod 0644 {REMOTE_PATH}") < (
        verification_command.index(f"stat -c %f {REMOTE_PATH}")
    )


@pytest.mark.parametrize("unsafe_metadata", ["mode", "hardlink"])
async def test_stage_rejects_unsafe_local_apk_metadata_before_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    unsafe_metadata: str,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    if unsafe_metadata == "mode":
        apk.chmod(0o644)
    else:
        os.link(apk, tmp_path / "artifact-hardlink.apk")
    device_iterator = _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    with pytest.raises(StopIteration):
        next(device_iterator)


def test_verified_apk_identity_requires_current_effective_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    status = apk.lstat()
    monkeypatch.setattr(install_adb.os, "geteuid", lambda: status.st_uid + 1)

    with pytest.raises(InstallAdbError) as caught:
        install_adb._verified_apk_identity(status, len(APK_BYTES))

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID


def test_verified_apk_identity_requires_a_regular_file(tmp_path: Path) -> None:
    fifo = tmp_path / "artifact.apk"
    os.mkfifo(fifo, mode=0o600)
    status = fifo.lstat()
    assert status.st_nlink == 1
    assert status.st_size == 0

    with pytest.raises(InstallAdbError) as caught:
        install_adb._verified_apk_identity(status, 0)

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID


def test_verified_apk_identity_requires_the_descriptor_size(tmp_path: Path) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)

    with pytest.raises(InstallAdbError) as caught:
        install_adb._verified_apk_identity(apk.lstat(), len(APK_BYTES) + 1)

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID


def test_verified_apk_identity_requires_private_mode(tmp_path: Path) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    apk.chmod(0o644)

    with pytest.raises(InstallAdbError) as caught:
        install_adb._verified_apk_identity(apk.lstat(), len(APK_BYTES))

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID


def test_verified_apk_identity_requires_one_filesystem_link(tmp_path: Path) -> None:
    apk = tmp_path / "artifact.apk"
    hardlink = tmp_path / "artifact-hardlink.apk"
    _write_private_apk(apk)
    os.link(apk, hardlink)
    assert apk.lstat().st_nlink == 2

    with pytest.raises(InstallAdbError) as caught:
        install_adb._verified_apk_identity(apk.lstat(), len(APK_BYTES))

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID


def test_verified_apk_identity_includes_nanosecond_timestamps(tmp_path: Path) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    status = apk.lstat()

    assert install_adb._verified_apk_identity(status, len(APK_BYTES)) == (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def test_open_verified_apk_compares_preopen_path_with_open_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    other_apk = tmp_path / "other.apk"
    _write_private_apk(apk)
    _write_private_apk(other_apk)
    original_open = os.open
    original_lstat = os.lstat
    path_stat_calls = 0

    def _open_other(path: Any, flags: int, *args: Any) -> int:
        if Path(path) == apk:
            return original_open(other_apk, flags, *args)
        return original_open(path, flags, *args)

    def _follow_opened_identity(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal path_stat_calls
        if Path(path) == apk:
            path_stat_calls += 1
            if path_stat_calls > 1:
                return original_lstat(other_apk, *args, **kwargs)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(install_adb.os, "open", _open_other)
    monkeypatch.setattr(install_adb.os, "lstat", _follow_opened_identity)

    try:
        file_descriptor = install_adb._open_verified_apk(apk, descriptor)
    except InstallAdbError as caught:
        assert caught.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    else:
        os.close(file_descriptor)
        pytest.fail("mismatched pathname and opened descriptor were accepted")

    assert path_stat_calls == 1


async def test_stage_fifo_swap_at_open_boundary_fails_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    displaced = tmp_path / "artifact-displaced.apk"
    original_open = os.open
    observed_flags: list[int] = []

    def _open_after_fifo_swap(path: Any, flags: int, *args: Any) -> int:
        if Path(path) == apk and not observed_flags:
            observed_flags.append(flags)
            if not flags & os.O_NONBLOCK:
                raise AssertionError("APK open omitted O_NONBLOCK")
            apk.rename(displaced)
            os.mkfifo(apk, mode=0o600)
        return original_open(path, flags, *args)

    monkeypatch.setattr(install_adb.os, "open", _open_after_fifo_swap)
    device_iterator = _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await asyncio.wait_for(
            async_stage_apk(
                target,
                signer,
                descriptor,
                JOB_ID,
                apk,
                expected_root_mode=AdbRootMode.ROOTLESS,
            ),
            timeout=1,
        )

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    assert observed_flags[0] & os.O_NONBLOCK
    assert stat.S_ISFIFO(apk.lstat().st_mode)
    assert displaced.read_bytes() == APK_BYTES
    with pytest.raises(StopIteration):
        next(device_iterator)


def test_open_verified_apk_rejects_post_hash_path_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    replacement = tmp_path / "replacement.apk"
    displaced = tmp_path / "displaced.apk"
    _write_private_apk(apk)
    _write_private_apk(replacement)
    initial_status = apk.lstat()
    original_fstat = os.fstat
    original_read = os.read
    rebound = False

    def _rebind_after_hash(file_descriptor: int, size: int) -> bytes:
        nonlocal rebound
        data = original_read(file_descriptor, size)
        if not data and not rebound:
            apk.rename(displaced)
            replacement.rename(apk)
            rebound = True
        return data

    def _stable_opened_status(file_descriptor: int) -> os.stat_result:
        if rebound:
            return initial_status
        return original_fstat(file_descriptor)

    monkeypatch.setattr(install_adb.os, "read", _rebind_after_hash)
    monkeypatch.setattr(install_adb.os, "fstat", _stable_opened_status)

    try:
        file_descriptor = install_adb._open_verified_apk(apk, descriptor)
    except InstallAdbError as caught:
        assert caught.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    else:
        os.close(file_descriptor)
        pytest.fail("pathname rebound after hashing was accepted")

    assert rebound
    assert apk.lstat().st_ino != initial_status.st_ino


def test_open_verified_apk_rechecks_descriptor_metadata_after_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    initial_status = apk.lstat()
    original_read = os.read
    metadata_changed = False

    def _change_mode_after_hash(file_descriptor: int, size: int) -> bytes:
        nonlocal metadata_changed
        data = original_read(file_descriptor, size)
        if not data and not metadata_changed:
            os.fchmod(file_descriptor, 0o400)
            metadata_changed = True
        return data

    monkeypatch.setattr(install_adb.os, "read", _change_mode_after_hash)
    monkeypatch.setattr(install_adb.os, "lstat", lambda _path: initial_status)

    try:
        file_descriptor = install_adb._open_verified_apk(apk, descriptor)
    except InstallAdbError as caught:
        assert caught.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    else:
        os.close(file_descriptor)
        pytest.fail("opened descriptor metadata change after hashing was accepted")
    finally:
        apk.chmod(0o600)

    assert metadata_changed


async def test_stage_rejects_local_apk_growth_during_bounded_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    original_read = os.read
    grew = False

    def _grow_then_read(file_descriptor: int, size: int) -> bytes:
        nonlocal grew
        if not grew:
            with apk.open("ab") as stream:
                stream.write(b"x")
            grew = True
        return original_read(file_descriptor, size)

    monkeypatch.setattr(install_adb.os, "read", _grow_then_read)
    device_iterator = _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    assert grew
    with pytest.raises(StopIteration):
        next(device_iterator)


def test_open_verified_apk_rejects_oversize_before_a_second_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    read_calls = 0

    def _oversize_then_fail(_file_descriptor: int, _size: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            return APK_BYTES + b"x"
        raise AssertionError("oversize input was read again")

    monkeypatch.setattr(install_adb.os, "read", _oversize_then_fail)

    with pytest.raises(InstallAdbError) as caught:
        install_adb._open_verified_apk(apk, descriptor)

    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    assert read_calls == 1


async def test_preexisting_fixed_staging_path_is_refused_before_filesync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["present"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.STAGING_PATH_OCCUPIED
    assert fake.pushes == []


async def test_interrupted_filesync_is_an_ambiguous_staging_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["absent"], 0),
        ],
        push_error=AdbConnectionError("EOF during push"),
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.STAGE_AMBIGUOUS
    assert len(fake.pushes) == 1


async def test_local_hash_mismatch_never_connects_or_pushes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk, b"same byte count!!!")
    assert apk.stat().st_size == descriptor.apk_size
    device_iterator = _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    with pytest.raises(StopIteration):
        next(device_iterator)


@pytest.mark.parametrize("path", [Path("bad\0path.apk"), Path("missing.apk")])
async def test_invalid_local_path_is_contained_as_a_stable_artifact_error(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    path: Path,
) -> None:
    _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            path,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    assert str(caught.value) == "local_artifact_invalid"


async def test_local_read_error_is_not_misreported_as_a_target_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    _install_fakes(monkeypatch, [])

    def fail_read(_file_descriptor: int, _size: int) -> bytes:
        raise OSError("local storage failed")

    monkeypatch.setattr(install_adb.os, "read", fail_read)

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.LOCAL_ARTIFACT_INVALID
    assert "storage" not in str(caught.value)


async def test_cancellation_drains_delayed_apk_worker_and_closes_returned_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    worker_opened = threading.Event()
    release_worker = threading.Event()
    returned_fds: list[int] = []
    original_open = install_adb._open_verified_apk

    def delayed_open(path: Path, artifact: InstallDescriptor) -> int:
        file_descriptor = original_open(path, artifact)
        returned_fds.append(file_descriptor)
        worker_opened.set()
        release_worker.wait(timeout=5)
        return file_descriptor

    monkeypatch.setattr(install_adb, "_open_verified_apk", delayed_open)
    device_iterator = _install_fakes(monkeypatch, [])
    task = asyncio.create_task(
        async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    )
    assert await asyncio.to_thread(worker_opened.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_worker.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(returned_fds) == 1
    with pytest.raises(OSError):
        os.fstat(returned_fds[0])
    with pytest.raises(StopIteration):
        next(device_iterator)


async def test_repeated_cancellation_during_device_close_cannot_leak_staged_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    push_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    pushed_fds: list[int] = []

    class CancelledStageDevice(FakeDevice):
        async def push(self, *args: Any, **kwargs: Any) -> None:
            self.pushes.append((args, kwargs))
            pushed_fds.append(int(str(args[0]).rsplit("/", maxsplit=1)[-1]))
            push_started.set()
            await asyncio.Future()

        async def close(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True

    fake = CancelledStageDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["absent"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])
    task = asyncio.create_task(
        async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    )
    await push_started.wait()
    assert len(pushed_fds) == 1
    os.fstat(pushed_fds[0])

    task.cancel()
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    os.fstat(pushed_fds[0])
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed is True
    with pytest.raises(OSError):
        os.fstat(pushed_fds[0])


async def test_cancellation_before_stage_task_starts_opens_no_apk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    opened = False
    original_open = install_adb._open_verified_apk

    def observed_open(path: Path, artifact: InstallDescriptor) -> int:
        nonlocal opened
        opened = True
        return original_open(path, artifact)

    monkeypatch.setattr(install_adb, "_open_verified_apk", observed_open)
    task = asyncio.create_task(
        async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    )
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened is False


@pytest.mark.parametrize(
    "remote",
    [
        _remote_output(NONCES[2], size=len(APK_BYTES) - 1),
        _remote_output(NONCES[2], sha256="f" * 64),
        _remote_output(NONCES[2], mode="81ed"),
    ],
)
async def test_remote_size_hash_or_mode_mismatch_never_reports_staged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    remote: bytes,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _single_output("PATH", NONCES[1], ["absent"], 0),
            remote,
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )
    assert caught.value.code is InstallAdbErrorCode.STAGE_VERIFICATION_FAILED
    assert len(fake.pushes) == 1


@pytest.mark.parametrize(
    ("preflight", "expected_error"),
    [
        (
            _preflight_output(NONCES[0], uid="0"),
            InstallAdbErrorCode.ROOT_MODE_CHANGED,
        ),
        (
            _preflight_output(NONCES[0], su_lines=["absent"], su_status=137),
            InstallAdbErrorCode.TARGET_UNREACHABLE,
        ),
    ],
)
async def test_install_root_drift_or_signal_proof_blocks_package_mutation(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    preflight: bytes,
    expected_error: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice(
        [
            preflight,
            _remote_output(NONCES[1]),
            _single_output("INSTALL", NONCES[2], ["Success"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_install_staged_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is expected_error
    assert len(fake.commands) == 1
    assert "sha256sum" not in fake.commands[0]
    assert "pm install" not in fake.commands[0]


async def test_install_uses_no_replacement_or_grant_flags(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _remote_output(NONCES[1]),
            _single_output("INSTALL", NONCES[2], ["Success"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    outcome = await async_install_staged_apk(
        target,
        signer,
        descriptor,
        JOB_ID,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    assert outcome is InstallOutcome.INSTALLED
    install_command = fake.commands[-1]
    assert len(fake.commands) == 3
    assert "HAPANELD_PREFLIGHT_BEGIN" in fake.commands[0]
    assert "HAPANELD_ARTIFACT_BEGIN" in fake.commands[1]
    assert "HAPANELD_INSTALL_BEGIN" in fake.commands[2]
    assert f"pm install -R {REMOTE_PATH}" in install_command
    assert " -r" not in install_command
    assert " -g" not in install_command
    assert "--replace" not in install_command
    assert fake.shell_kwargs[-1]["transport_timeout_s"] == 180.0
    assert fake.shell_kwargs[-1]["read_timeout_s"] == 180.0


async def test_api26_uses_legacy_nonreplacement_default_without_unknown_flag(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    api26_target = replace(target, android_sdk=26)
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0], sdk=26),
            _remote_output(NONCES[1]),
            _single_output("INSTALL", NONCES[2], ["Success"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    outcome = await async_install_staged_apk(
        api26_target,
        signer,
        descriptor,
        JOB_ID,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    assert outcome is InstallOutcome.INSTALLED
    assert f"pm install {REMOTE_PATH}" in fake.commands[-1]
    assert "pm install -R" not in fake.commands[-1]
    assert "pm install -r" not in fake.commands[-1]


async def test_install_revalidates_identity_before_reading_or_mutating_stage(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([_preflight_output(NONCES[0], serial="SERIAL-2")])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_install_staged_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.TARGET_CHANGED
    assert len(fake.commands) == 1
    assert "pm install" not in fake.commands[0]
    assert "sha256sum" not in fake.commands[0]


@pytest.mark.parametrize(
    "install_response",
    [
        AdbConnectionError("peer EOF after mutation"),
        b"Success\n",
        _single_output("INSTALL", NONCES[2], ["unexpected"], 0),
    ],
)
async def test_install_transport_eof_or_malformed_result_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    install_response: bytes | BaseException,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _remote_output(NONCES[1]),
            install_response,
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_install_staged_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INSTALL_AMBIGUOUS


async def test_noncanonical_nonzero_install_exit_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _remote_output(NONCES[1]),
            _single_output("INSTALL", NONCES[2], ["Killed"], 137),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_install_staged_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INSTALL_AMBIGUOUS


@pytest.mark.parametrize("status", [2, 126, 127, 129, 130, 137, 255])
async def test_canonical_failure_with_abnormal_install_exit_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    status: int,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _remote_output(NONCES[1]),
            _single_output(
                "INSTALL",
                NONCES[2],
                ["Failure [INSTALL_FAILED_INVALID_APK: rejected]"],
                status,
            ),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_install_staged_apk(
            target,
            signer,
            descriptor,
            JOB_ID,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INSTALL_AMBIGUOUS


async def test_package_manager_definite_refusal_is_not_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _preflight_output(NONCES[0]),
            _remote_output(NONCES[1]),
            _single_output("INSTALL", NONCES[2], ["Failure [INSTALL_FAILED]"], 1),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    outcome = await async_install_staged_apk(
        target,
        signer,
        descriptor,
        JOB_ID,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    assert outcome is InstallOutcome.REFUSED


@pytest.mark.parametrize(
    ("posture", "expected_root_mode", "expected_error"),
    [
        (
            _identity_root_output(NONCES[0]),
            AdbRootMode.ROOT_ADBD,
            InstallAdbErrorCode.ROOT_MODE_CHANGED,
        ),
        (
            _identity_root_output(NONCES[0], su_lines=["absent"], su_status=137),
            AdbRootMode.ROOTLESS,
            InstallAdbErrorCode.TARGET_UNREACHABLE,
        ),
    ],
)
async def test_launch_root_drift_or_signal_proof_blocks_activity_mutation(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    posture: bytes,
    expected_root_mode: AdbRootMode,
    expected_error: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice(
        [
            posture,
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
            _single_output("LAUNCH", NONCES[2], ["Status: ok"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_launch_installed_app(
            target,
            signer,
            descriptor,
            expected_root_mode=expected_root_mode,
        )

    assert caught.value.code is expected_error
    assert len(fake.commands) == 1
    assert "pm path" not in fake.commands[0]
    assert "am start" not in fake.commands[0]


async def test_launch_uses_exact_descriptor_package_and_component_once(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0]),
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
            _single_output("LAUNCH", NONCES[2], ["Status: ok"], 0),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    outcome = await async_launch_installed_app(
        target,
        signer,
        descriptor,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    assert outcome is LaunchOutcome.STARTED
    assert len(fake.commands) == 3
    assert "HAPANELD_POSTURE_BEGIN" in fake.commands[0]
    assert "HAPANELD_PACKAGE_BEGIN" in fake.commands[1]
    assert "HAPANELD_LAUNCH_BEGIN" in fake.commands[2]
    assert fake.commands.count(fake.commands[-1]) == 1
    assert (
        "am start -W -n io.github.maxlyth.hapaneld/.MainActivity "
        "-p io.github.maxlyth.hapaneld"
    ) in fake.commands[-1]


async def test_launch_returns_refused_only_for_ordinary_am_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0]),
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
            _single_output("LAUNCH", NONCES[2], ["Error: refused"], 1),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    outcome = await async_launch_installed_app(
        target,
        signer,
        descriptor,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    assert outcome is LaunchOutcome.REFUSED


@pytest.mark.parametrize("status", [2, 126, 127, 130, 137, 255])
async def test_abnormal_launch_exit_is_ambiguous_after_start_attempt(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    status: int,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0]),
            _single_output(
                "PACKAGE", NONCES[1], ["package:/data/app/ha-paneld/base.apk"], 0
            ),
            _single_output("LAUNCH", NONCES[2], ["abnormal"], status),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_launch_installed_app(
            target,
            signer,
            descriptor,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.LAUNCH_AMBIGUOUS


@pytest.mark.parametrize(
    ("posture", "expected_root_mode", "expected_error"),
    [
        (
            _identity_root_output(NONCES[0], uid="0", secure="0", debuggable="1"),
            AdbRootMode.ROOTLESS,
            InstallAdbErrorCode.ROOT_MODE_CHANGED,
        ),
        (
            _identity_root_output(NONCES[0], su_lines=["abnormal"]),
            AdbRootMode.ROOTLESS,
            InstallAdbErrorCode.TARGET_UNREACHABLE,
        ),
    ],
)
async def test_cleanup_root_drift_or_ambiguity_blocks_remote_delete(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    posture: bytes,
    expected_root_mode: AdbRootMode,
    expected_error: InstallAdbErrorCode,
) -> None:
    fake = FakeDevice([posture, _cleanup_output(NONCES[1])])
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_cleanup_staged_apk(
            target,
            signer,
            _staged(),
            DefiniteCleanupReason.INSTALL_SUCCEEDED,
            expected_root_mode=expected_root_mode,
        )

    assert caught.value.code is expected_error
    assert len(fake.commands) == 1
    assert "rm -f" not in fake.commands[0]


async def test_cleanup_wrong_identity_never_reaches_rm(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    fake = FakeDevice(
        [
            _identity_root_output(NONCES[0], serial="OTHER-SERIAL"),
            _cleanup_output(NONCES[1]),
        ]
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_cleanup_staged_apk(
            target,
            signer,
            _staged(),
            DefiniteCleanupReason.INSTALL_SUCCEEDED,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.TARGET_CHANGED
    assert len(fake.commands) == 1
    assert "rm -f" not in fake.commands[0]


async def test_cleanup_deletes_only_exact_job_path_after_definite_outcome(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    fake = FakeDevice([_identity_root_output(NONCES[0]), _cleanup_output(NONCES[1])])
    _install_fakes(monkeypatch, [fake])

    await async_cleanup_staged_apk(
        target,
        signer,
        _staged(),
        DefiniteCleanupReason.INSTALL_REFUSED,
        expected_root_mode=AdbRootMode.ROOTLESS,
    )

    cleanup_command = fake.commands[-1]
    assert len(fake.commands) == 2
    assert "HAPANELD_POSTURE_BEGIN" in fake.commands[0]
    assert "HAPANELD_CLEANUP_BEGIN" in fake.commands[1]
    assert cleanup_command.count(REMOTE_PATH) == 3
    assert f"rm -f {REMOTE_PATH}" in cleanup_command
    assert "/data/local/tmp" in cleanup_command
    assert "*" not in cleanup_command


@pytest.mark.parametrize(
    "job_id",
    [
        "../escape",
        "a" * 31,
        "A" * 32,
        "a" * 31 + ";",
        "a" * 32 + " --grant-all",
    ],
)
async def test_invalid_job_id_cannot_reach_shell_or_filesync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
    job_id: str,
) -> None:
    apk = tmp_path / "artifact.apk"
    _write_private_apk(apk)
    _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_stage_apk(
            target,
            signer,
            descriptor,
            job_id,
            apk,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST


async def test_non_private_or_unpinned_target_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(
            replace(target, address=PanelAddress(host="panel.example", port=8888)),
            signer,
            descriptor,
        )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST


async def test_cleanup_requires_a_typed_definite_outcome(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    _install_fakes(monkeypatch, [])

    with pytest.raises(InstallAdbError) as caught:
        await async_cleanup_staged_apk(
            target,
            signer,
            _staged(),
            "install_succeeded",  # type: ignore[arg-type]
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST


async def test_cleanup_rejects_a_forged_or_nonowned_staging_receipt(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
) -> None:
    _install_fakes(monkeypatch, [])
    forged = replace(_staged(), remote_path="/data/local/tmp/foreign.apk")

    with pytest.raises(InstallAdbError) as caught:
        await async_cleanup_staged_apk(
            target,
            signer,
            forged,
            DefiniteCleanupReason.CANCELLED,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST


async def test_forged_descriptor_command_fields_fail_before_connection(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    _install_fakes(monkeypatch, [])
    forged = replace(
        descriptor,
        launch_component="io.github.maxlyth.hapaneld/.MainActivity; id",
    )

    with pytest.raises(InstallAdbError) as caught:
        await async_launch_installed_app(
            target,
            signer,
            forged,
            expected_root_mode=AdbRootMode.ROOTLESS,
        )

    assert caught.value.code is InstallAdbErrorCode.INVALID_REQUEST


async def test_each_phase_gets_a_new_connection_and_same_signer(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    first = FakeDevice([_preflight_output(NONCES[0])])
    second = FakeDevice([_preflight_output(NONCES[1])])
    _install_fakes(monkeypatch, [first, second])

    await async_preflight_install(target, signer, descriptor)
    await async_preflight_install(target, signer, descriptor)

    assert first is not second
    assert first.connect_kwargs is not None
    assert second.connect_kwargs is not None
    assert first.connect_kwargs["rsa_keys"] == [signer]
    assert second.connect_kwargs["rsa_keys"] == [signer]
    assert first.closed is True
    assert second.closed is True


async def test_authorization_error_is_stable_and_contains_no_peer_text(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice([], connect_error=DeviceAuthError("secret peer details"))
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.AUTHORIZATION_REQUIRED
    assert str(caught.value) == "authorization_required"
    assert "secret" not in str(caught.value)
    assert fake.closed is True


async def test_adb_shell_public_key_prompt_timeout_means_authorization_required(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    fake = FakeDevice(
        [],
        connect_error=TimeoutError("panel approval did not arrive"),
        prompt_for_authorization=True,
    )
    _install_fakes(monkeypatch, [fake])

    with pytest.raises(InstallAdbError) as caught:
        await async_preflight_install(target, signer, descriptor)

    assert caught.value.code is InstallAdbErrorCode.AUTHORIZATION_REQUIRED
    assert str(caught.value) == "authorization_required"
    assert "approval" not in str(caught.value)
    assert fake.closed is True


async def test_cancellation_during_connect_drains_close_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    signer: PythonRSASigner,
    target: AdbInstallTarget,
    descriptor: InstallDescriptor,
) -> None:
    connect_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class CancelledConnectDevice(FakeDevice):
        async def connect(self, **kwargs: Any) -> bool:
            self.connect_kwargs = kwargs
            connect_started.set()
            await asyncio.Future()
            return True

        async def close(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True

    fake = CancelledConnectDevice([])
    _install_fakes(monkeypatch, [fake])
    task = asyncio.create_task(async_preflight_install(target, signer, descriptor))
    await connect_started.wait()

    task.cancel()
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed is True
