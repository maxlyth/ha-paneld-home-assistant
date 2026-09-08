"""Tests for installer mutation target pinning."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from typing import Any

import pytest

from custom_components.panel_assistant.client import PanelAddress, normalize_address
from custom_components.panel_assistant.install_network import (
    MAX_INSTALL_RESOLVER_RESULTS,
    InstallNetworkError,
    InstallNetworkErrorCode,
    PinnedPanelTarget,
    async_pin_install_target,
    async_revalidate_install_target,
)


def _result(address: str) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 8888, 0, 0) if family == socket.AF_INET6 else (address, 8888)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class _FakeHass:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []

    async def async_add_executor_job(
        self, target: Callable[..., Any], *args: Any
    ) -> Any:
        self.calls.append((target, args))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response()
        return response


@pytest.mark.parametrize(
    "host",
    [
        "10.1.0.1",
        "10.255.255.254",
        "172.16.0.1",
        "172.30.255.254",
        "192.168.0.1",
        "192.168.255.254",
        "fc00::1",
        "fdff:ffff::1",
    ],
)
async def test_private_literal_is_pinned_without_dns(host: str) -> None:
    """Explicit RFC1918 and ULA literals do not consult DNS."""
    hass = _FakeHass()
    original = PanelAddress(host=host, port=9999)

    target = await async_pin_install_target(hass, original)  # type: ignore[arg-type]

    assert target.original is original
    assert target.pinned == PanelAddress(host=host, port=9999)
    assert hass.calls == []


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "8.8.8.8",
        "::",
        "::1",
        "::ffff:192.168.1.1",
        "fe80::1",
        "fd00::1%eth0",
        "ff02::1",
        "2001:4860:4860::8888",
    ],
)
async def test_unsafe_literal_is_rejected(host: str) -> None:
    """Non-RFC1918 IPv4 and non-ULA IPv6 literals cannot be mutation targets."""
    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(_FakeHass(), PanelAddress(host, 8888))  # type: ignore[arg-type]

    assert raised.value.code is InstallNetworkErrorCode.UNSAFE_TARGET
    assert str(raised.value) == "unsafe_target"


async def test_dns_prefers_lowest_ipv4_deterministically() -> None:
    """Result ordering and duplicates do not affect the selected address."""
    records = [
        _result("fd00::2"),
        _result("192.168.1.20"),
        _result("10.1.0.9"),
        _result("10.1.0.9"),
        _result("fd00::1"),
    ]
    hass = _FakeHass(records)
    original = normalize_address("Panel.Local:9999")

    target = await async_pin_install_target(hass, original)  # type: ignore[arg-type]

    assert target.original is original
    assert target.pinned == PanelAddress("10.1.0.9", 9999)
    assert hass.calls[0][0] is socket.getaddrinfo
    assert hass.calls[0][1] == (
        "panel.local",
        9999,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        0,
    )


async def test_dns_uses_lowest_ula_when_no_ipv4_exists() -> None:
    """ULA-only results are ordered numerically."""
    hass = _FakeHass([_result("fd00::20"), _result("fc00::10")])

    target = await async_pin_install_target(  # type: ignore[arg-type]
        hass, normalize_address("panel.local")
    )

    assert target.pinned.host == "fc00::10"


@pytest.mark.parametrize(
    "records",
    [
        [_result("192.168.1.20"), _result("8.8.8.8")],
        [_result("192.168.1.20"), _result("fe80::1")],
    ],
)
async def test_mixed_safe_and_unsafe_dns_results_are_rejected(
    records: list[Any],
) -> None:
    """A mixed DNS answer fails closed instead of filtering unsafe records."""
    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(  # type: ignore[arg-type]
            _FakeHass(records), normalize_address("panel.local")
        )

    assert raised.value.code is InstallNetworkErrorCode.UNSAFE_TARGET


@pytest.mark.parametrize(
    "host",
    [
        "panel_local",
        "-panel.local",
        "panel-.local",
        "panel..local",
        "%bad.local",
        "pänel.local",
        "faß.local",
        "panel\u00ad.local",
        "192.168.1",
        "012.0.0.1",
        "0x0a000001",
        "167772161",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 63}",
        f"{'a' * 64}.local",
    ],
)
async def test_malformed_or_oversized_hostname_is_rejected(host: str) -> None:
    """Malformed DNS syntax is rejected before calling the resolver."""
    hass = _FakeHass()

    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(hass, PanelAddress(host, 8888))  # type: ignore[arg-type]

    assert raised.value.code is InstallNetworkErrorCode.INVALID_HOST
    assert hass.calls == []


async def test_excessive_resolver_results_are_rejected() -> None:
    """Resolver output is capped before any target is selected."""
    records = [_result(f"10.1.0.{index}") for index in range(1, 18)]
    assert len(records) == MAX_INSTALL_RESOLVER_RESULTS + 1

    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(  # type: ignore[arg-type]
            _FakeHass(records), normalize_address("panel.local")
        )

    assert raised.value.code is InstallNetworkErrorCode.TOO_MANY_RESULTS


@pytest.mark.parametrize(
    "records",
    [
        [],
        "not-a-list",
        [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", "path")],
        [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ())],
        [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("bad-ip", 8888))],
        [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("fd00::1", 8888))],
        [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("10.1.0.1", 8888))],
        [(socket.AF_INET, socket.SOCK_STREAM, 0, "")],
    ],
)
async def test_empty_or_malformed_resolver_results_are_rejected(records: Any) -> None:
    """Unexpected resolver shapes cannot become mutation targets."""
    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(  # type: ignore[arg-type]
            _FakeHass(records), normalize_address("panel.local")
        )

    assert raised.value.code is InstallNetworkErrorCode.RESOLUTION_FAILED


async def test_resolver_error_is_redacted() -> None:
    """Resolver details are not copied into the stable public error."""
    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(  # type: ignore[arg-type]
            _FakeHass(socket.gaierror("secret resolver detail")),
            normalize_address("panel.local"),
        )

    assert raised.value.code is InstallNetworkErrorCode.RESOLUTION_FAILED
    assert str(raised.value) == "resolution_failed"
    assert raised.value.__cause__ is None


async def test_resolver_timeout_is_fixed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled resolver is bounded by the installer timeout."""

    async def _stall() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(
        "custom_components.panel_assistant.install_network.INSTALL_RESOLVE_TIMEOUT_SECONDS",
        0.001,
    )
    with pytest.raises(InstallNetworkError) as raised:
        await async_pin_install_target(  # type: ignore[arg-type]
            _FakeHass(_stall), normalize_address("panel.local")
        )

    assert raised.value.code is InstallNetworkErrorCode.RESOLUTION_TIMEOUT
    assert str(raised.value) == "resolution_timeout"
    assert raised.value.__cause__ is None


async def test_revalidation_accepts_exact_pin_without_switching() -> None:
    """Revalidation returns the original object when its exact pin remains."""
    original = normalize_address("panel.local:9999")
    target = PinnedPanelTarget(original, PanelAddress("192.168.1.20", 9999))
    hass = _FakeHass([_result("192.168.1.30"), _result("192.168.1.20")])

    result = await async_revalidate_install_target(hass, target)  # type: ignore[arg-type]

    assert result is target
    assert result.pinned.host == "192.168.1.20"


async def test_revalidation_rejects_removed_pin() -> None:
    """DNS rebinding cannot silently replace the approved pinned address."""
    original = normalize_address("panel.local")
    target = PinnedPanelTarget(original, PanelAddress("192.168.1.20", 8888))

    with pytest.raises(InstallNetworkError) as raised:
        await async_revalidate_install_target(  # type: ignore[arg-type]
            _FakeHass([_result("192.168.1.21")]), target
        )

    assert raised.value.code is InstallNetworkErrorCode.PINNED_TARGET_REMOVED
    assert target.pinned.host == "192.168.1.20"


async def test_revalidation_rejects_port_change() -> None:
    """The pinned endpoint includes the original port, not only an IP address."""
    target = PinnedPanelTarget(
        normalize_address("panel.local:9999"), PanelAddress("192.168.1.20", 8888)
    )
    hass = _FakeHass()

    with pytest.raises(InstallNetworkError) as raised:
        await async_revalidate_install_target(hass, target)  # type: ignore[arg-type]

    assert raised.value.code is InstallNetworkErrorCode.PINNED_TARGET_REMOVED
    assert hass.calls == []


@pytest.mark.parametrize("pinned_host", ["not-an-ip", "127.0.0.1"])
async def test_revalidation_rejects_invalid_or_unsafe_pin(pinned_host: str) -> None:
    """Only a safe numeric address can be carried as an installer pin."""
    target = PinnedPanelTarget(
        normalize_address("panel.local"), PanelAddress(pinned_host, 8888)
    )
    hass = _FakeHass()

    with pytest.raises(InstallNetworkError) as raised:
        await async_revalidate_install_target(hass, target)  # type: ignore[arg-type]

    assert raised.value.code is InstallNetworkErrorCode.UNSAFE_TARGET
    assert hass.calls == []


async def test_literal_revalidation_does_not_consult_dns() -> None:
    """A literal identity is revalidated by exact numeric equality."""
    original = PanelAddress("fd00:0:0::1", 8888)
    target = PinnedPanelTarget(original, PanelAddress("fd00::1", 8888))
    hass = _FakeHass()

    assert await async_revalidate_install_target(hass, target) is target  # type: ignore[arg-type]
    assert hass.calls == []
