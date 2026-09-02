"""Tests for the read-only ADB installation-target preflight."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from adb_shell.exceptions import DeviceAuthError

from custom_components.ha_paneld import provisioning
from custom_components.ha_paneld.client import normalize_address
from custom_components.ha_paneld.provisioning import (
    InstallTargetState,
    async_probe_install_target,
)

_FIRST_NONCE = "1" * 32
_SECOND_NONCE = "2" * 32
_THIRD_NONCE = "3" * 32


def _presence_output(
    *,
    present: bool = False,
    target_status: int = 0,
    live_status: int = 0,
    live_path: str = "package:/system/framework/framework-res.apk",
) -> bytes:
    target_path = "package:/data/app/ha-paneld/base.apk\n" if present else ""
    return (
        f"HAPANELD_PKG_BEGIN:{_FIRST_NONCE}\n"
        f"{target_path}"
        f"HAPANELD_PKG_TARGET:{_FIRST_NONCE}:{target_status}\n"
        f"{live_path}\n"
        f"HAPANELD_PKG_LIVE:{_FIRST_NONCE}:{live_status}\n"
        f"HAPANELD_PKG_END:{_FIRST_NONCE}\n"
    ).encode()


def _retained_output(*, retained: bool = False) -> bytes:
    package = "package:io.github.maxlyth.hapaneld\n" if retained else ""
    return (
        f"HAPANELD_DATA_BEGIN:{_SECOND_NONCE}\n"
        f"{package}"
        f"HAPANELD_DATA_TARGET:{_SECOND_NONCE}:0\n"
        "package:/system/framework/framework-res.apk\n"
        f"HAPANELD_DATA_LIVE:{_SECOND_NONCE}:0\n"
        f"HAPANELD_DATA_END:{_SECOND_NONCE}\n"
    ).encode()


def _target_facts_output(
    *,
    model: str = "Electron WF1589T",
    serial: str = "WF1589T-0123",
    primary_abi: str = "arm64-v8a",
    android_sdk: str = "30",
) -> bytes:
    values = {
        "MODEL": model,
        "SERIAL": serial,
        "ABI": primary_abi,
        "SDK": android_sdk,
    }
    lines = [f"HAPANELD_ID_BEGIN:{_THIRD_NONCE}"]
    for name, value in values.items():
        lines.extend(
            (
                f"HAPANELD_ID_{name}_BEGIN:{_THIRD_NONCE}",
                value,
                f"HAPANELD_ID_{name}_END:{_THIRD_NONCE}:0",
            )
        )
    lines.append(f"HAPANELD_ID_END:{_THIRD_NONCE}")
    return ("\n".join(lines) + "\n").encode()


class _FakeAdbDevice:
    def __init__(
        self,
        outputs: list[bytes | BaseException],
        *,
        connect_result: bool = True,
        connect_error: Exception | None = None,
    ) -> None:
        self.outputs = outputs
        self.connect_result = connect_result
        self.connect_error = connect_error
        self.constructor: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self.connect_kwargs: dict[str, Any] | None = None
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def connect(self, **kwargs: Any) -> bool:
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        return self.connect_result

    async def streaming_shell(
        self, command: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        self.commands.append((command, kwargs))
        output = self.outputs[len(self.commands) - 1]
        if isinstance(output, BaseException):
            raise output
        yield output

    async def close(self) -> None:
        self.closed = True


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeAdbDevice) -> None:
    nonces = iter((_FIRST_NONCE, _SECOND_NONCE, _THIRD_NONCE))
    monkeypatch.setattr(provisioning, "token_hex", lambda _bytes: next(nonces))

    def _factory(*args: Any, **kwargs: Any) -> _FakeAdbDevice:
        fake.constructor = (args, kwargs)
        return fake

    monkeypatch.setattr(provisioning, "AdbDeviceTcpAsync", _factory)


async def test_clean_target_requires_two_complete_package_manager_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean verdict proves both no path and no retained package record."""
    fake = _FakeAdbDevice(
        [
            _presence_output(target_status=1),
            _retained_output(),
            _target_facts_output(),
        ]
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local:9999"))

    assert probe.state is InstallTargetState.CLEAN
    assert probe.model == "Electron WF1589T"
    assert probe.serial == "WF1589T-0123"
    assert probe.primary_abi == "arm64-v8a"
    assert probe.android_sdk == 30
    assert fake.constructor == (
        ("panel.local",),
        {
            "port": 5555,
            "default_transport_timeout_s": 5.0,
            "banner": "ha-paneld-home-assistant",
        },
    )
    assert fake.connect_kwargs == {
        "rsa_keys": [],
        "transport_timeout_s": 5.0,
        "auth_timeout_s": 5.0,
        "read_timeout_s": 5.0,
    }
    assert len(fake.commands) == 3
    assert "pm path io.github.maxlyth.hapaneld" in fake.commands[0][0]
    assert "pm list packages -u io.github.maxlyth.hapaneld" in fake.commands[1][0]
    assert "pm path android" in fake.commands[0][0]
    assert "pm path android" in fake.commands[1][0]
    assert "getprop ro.product.model" in fake.commands[2][0]
    assert "getprop ro.serialno" in fake.commands[2][0]
    assert "getprop ro.product.cpu.abi" in fake.commands[2][0]
    assert "getprop ro.build.version.sdk" in fake.commands[2][0]
    assert all(kwargs["decode"] is False for _command, kwargs in fake.commands)
    assert fake.closed is True


@pytest.mark.parametrize(
    "facts",
    [
        _target_facts_output(serial=""),
        _target_facts_output(serial="not a serial"),
        _target_facts_output(primary_abi=""),
        _target_facts_output(android_sdk="unknown"),
        _target_facts_output(android_sdk="101"),
        _target_facts_output().replace(b"HAPANELD_ID_END", b"HAPANELD_ID_BEGIN"),
    ],
)
async def test_clean_candidate_requires_valid_physical_target_facts(
    monkeypatch: pytest.MonkeyPatch, facts: bytes
) -> None:
    """A package-clean answer is not presented without bounded target identity."""
    fake = _FakeAdbDevice(
        [_presence_output(target_status=1), _retained_output(), facts]
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.RETAINED_OR_AMBIGUOUS
    assert probe.model is None
    assert probe.serial is None
    assert probe.primary_abi is None
    assert probe.android_sdk is None
    assert fake.closed is True


@pytest.mark.parametrize(
    ("facts", "expected_abi", "expected_sdk"),
    [
        (_target_facts_output(android_sdk="25"), "arm64-v8a", 25),
        (_target_facts_output(primary_abi="x86_64"), "x86_64", 30),
    ],
)
async def test_clean_but_unsupported_target_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
    facts: bytes,
    expected_abi: str,
    expected_sdk: int,
) -> None:
    """Current Android minSdk and production ABIs bound install compatibility."""
    fake = _FakeAdbDevice(
        [_presence_output(target_status=1), _retained_output(), facts]
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.INCOMPATIBLE
    assert probe.model == "Electron WF1589T"
    assert probe.serial == "WF1589T-0123"
    assert probe.primary_abi == expected_abi
    assert probe.android_sdk == expected_sdk
    assert fake.closed is True


async def test_installed_target_does_not_query_retained_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid installed path is sufficient to prevent first-install admission."""
    fake = _FakeAdbDevice([_presence_output(present=True)])
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("192.0.2.10"))

    assert probe.state is InstallTargetState.INSTALLED
    assert len(fake.commands) == 1
    assert fake.closed is True


async def test_retained_uninstalled_package_is_not_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uninstall-with-data record is retained or ambiguous, never fresh."""
    fake = _FakeAdbDevice(
        [_presence_output(target_status=0), _retained_output(retained=True)]
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.RETAINED_OR_AMBIGUOUS
    assert fake.closed is True


@pytest.mark.parametrize(
    "presence",
    [
        b"",
        _presence_output(present=True, target_status=1),
        _presence_output(target_status=137),
        _presence_output(live_status=1),
        _presence_output(live_path="package:relative.apk"),
        _presence_output().replace(b"HAPANELD_PKG_END", b"HAPANELD_PKG_BEGIN"),
        b"unexpected\n" + _presence_output(),
        _presence_output() + b"unexpected\n",
        _presence_output().replace(
            b"HAPANELD_PKG_LIVE", b"unexpected\nHAPANELD_PKG_LIVE"
        ),
        b"\xff",
        b"x" * (16 * 1024 + 1),
    ],
)
async def test_malformed_or_excessive_presence_fails_closed(
    monkeypatch: pytest.MonkeyPatch, presence: bytes
) -> None:
    """Partial, contradictory, malformed and excessive replies are never clean."""
    fake = _FakeAdbDevice([presence])
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.RETAINED_OR_AMBIGUOUS
    assert len(fake.commands) == 1
    assert fake.closed is True


@pytest.mark.parametrize(
    "retained",
    [
        b"",
        _retained_output().replace(
            b"DATA_TARGET:" + _SECOND_NONCE.encode() + b":0",
            b"DATA_TARGET:" + _SECOND_NONCE.encode() + b":1",
        ),
        _retained_output().replace(b"HAPANELD_DATA_END", b"HAPANELD_DATA_LIVE"),
        _retained_output().replace(
            b"package:/system/framework/framework-res.apk",
            b"unexpected\npackage:/system/framework/framework-res.apk",
        ),
        _retained_output() + b"unexpected\n",
        _retained_output().replace(
            b"HAPANELD_DATA_TARGET",
            b"package:io.github.maxlyth.hapaneld.debug\nHAPANELD_DATA_TARGET",
        ),
    ],
)
async def test_ambiguous_retained_data_fails_closed(
    monkeypatch: pytest.MonkeyPatch, retained: bytes
) -> None:
    """A second observation must prove absence exactly before clean admission."""
    fake = _FakeAdbDevice([_presence_output(target_status=1), retained])
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.RETAINED_OR_AMBIGUOUS
    assert fake.closed is True


async def test_authentication_request_is_reported_without_a_shell_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-key preflight distinguishes a target that requires user authorization."""
    fake = _FakeAdbDevice(
        [], connect_error=DeviceAuthError("Device authentication required")
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.ADB_UNAUTHORIZED
    assert fake.connect_kwargs is not None
    assert fake.connect_kwargs["rsa_keys"] == []
    assert fake.commands == []
    assert fake.closed is True


@pytest.mark.parametrize(
    ("connect_result", "connect_error"),
    [(False, None), (True, ConnectionRefusedError())],
)
async def test_unreachable_adb_is_distinct_from_authorization(
    monkeypatch: pytest.MonkeyPatch,
    connect_result: bool,
    connect_error: Exception | None,
) -> None:
    """No listener and failed negotiation remain an ADB reachability outcome."""
    fake = _FakeAdbDevice(
        [], connect_result=connect_result, connect_error=connect_error
    )
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.ADB_UNREACHABLE
    assert fake.commands == []
    assert fake.closed is True


async def test_shell_transport_failure_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection lost before proof completes is not mistaken for clean."""
    fake = _FakeAdbDevice([ConnectionResetError()])
    _install_fake(monkeypatch, fake)

    probe = await async_probe_install_target(normalize_address("panel.local"))

    assert probe.state is InstallTargetState.ADB_UNREACHABLE
    assert fake.closed is True


async def test_cancellation_propagates_after_bounded_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant task cancellation is not converted into device failure."""
    fake = _FakeAdbDevice([asyncio.CancelledError()])
    _install_fake(monkeypatch, fake)

    with pytest.raises(asyncio.CancelledError):
        await async_probe_install_target(normalize_address("panel.local"))

    assert fake.closed is True
