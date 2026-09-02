"""Network-target pinning for installer mutations."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .client import PanelAddress

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

MAX_INSTALL_HOST_LENGTH = 253
MAX_INSTALL_RESOLVER_RESULTS = 16
INSTALL_RESOLVE_TIMEOUT_SECONDS = 5

_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
_RFC1918_NETWORKS = (
    ipaddress.IPv4Network((0x0A000000, 8)),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_ULA_NETWORK = ipaddress.IPv6Network("fc00::/7")


class InstallNetworkErrorCode(StrEnum):
    """Stable installer network failure codes."""

    INVALID_HOST = "invalid_host"
    RESOLUTION_FAILED = "resolution_failed"
    RESOLUTION_TIMEOUT = "resolution_timeout"
    TOO_MANY_RESULTS = "too_many_results"
    UNSAFE_TARGET = "unsafe_target"
    PINNED_TARGET_REMOVED = "pinned_target_removed"


class InstallNetworkError(Exception):
    """A safe, user-presentable installer network failure."""

    def __init__(self, code: InstallNetworkErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class PinnedPanelTarget:
    """Original panel identity plus the exact address allowed for mutation."""

    original: PanelAddress
    pinned: PanelAddress


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_allowed_install_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return whether an address is in the installer's explicit LAN ranges."""
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _RFC1918_NETWORKS)
    return address.scope_id is None and address in _ULA_NETWORK


def _resolver_hostname(host: str) -> str:
    """Return a bounded ASCII DNS name suitable for getaddrinfo."""
    if not host or "%" in host:
        raise InstallNetworkError(InstallNetworkErrorCode.INVALID_HOST)

    try:
        ascii_host = host.encode("ascii").decode("ascii")
    except UnicodeError:
        raise InstallNetworkError(InstallNetworkErrorCode.INVALID_HOST) from None

    # libc resolvers accept several legacy numeric IPv4 spellings which Python's
    # strict ipaddress parser correctly rejects. Refuse them instead of allowing
    # a displayed hostname such as ``192.168.1`` to mutate ``192.168.0.1``.
    try:
        socket.inet_aton(ascii_host)
    except OSError:
        pass
    else:
        raise InstallNetworkError(InstallNetworkErrorCode.INVALID_HOST)

    if len(ascii_host) > MAX_INSTALL_HOST_LENGTH:
        raise InstallNetworkError(InstallNetworkErrorCode.INVALID_HOST)

    labels = (
        ascii_host[:-1].split(".")
        if ascii_host.endswith(".")
        else ascii_host.split(".")
    )
    if not labels or any(
        _DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels
    ):
        raise InstallNetworkError(InstallNetworkErrorCode.INVALID_HOST)
    return ascii_host.lower()


def _parse_resolver_results(
    results: Any,
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if not isinstance(results, (list, tuple)):
        raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
    if len(results) > MAX_INSTALL_RESOLVER_RESULTS:
        raise InstallNetworkError(InstallNetworkErrorCode.TOO_MANY_RESULTS)

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for result in results:
        if not isinstance(result, tuple) or len(result) != 5:
            raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
        family, _socket_type, _protocol, _canonical_name, sockaddr = result
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
        if not isinstance(sockaddr, tuple) or not sockaddr:
            raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except TypeError, ValueError:
            raise InstallNetworkError(
                InstallNetworkErrorCode.RESOLUTION_FAILED
            ) from None
        if family == socket.AF_INET and not isinstance(address, ipaddress.IPv4Address):
            raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
        if family == socket.AF_INET6 and not isinstance(address, ipaddress.IPv6Address):
            raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
        if not is_allowed_install_address(address):
            raise InstallNetworkError(InstallNetworkErrorCode.UNSAFE_TARGET)
        addresses.add(address)

    if not addresses:
        raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
    return frozenset(addresses)


async def _async_resolve(
    hass: HomeAssistant, address: PanelAddress
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    hostname = _resolver_hostname(address.host)
    try:
        async with asyncio.timeout(INSTALL_RESOLVE_TIMEOUT_SECONDS):
            results = await hass.async_add_executor_job(
                socket.getaddrinfo,
                hostname,
                address.port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                0,
            )
    except TimeoutError:
        raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_TIMEOUT) from None
    except OSError:
        raise InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED) from None
    return _parse_resolver_results(results)


def _preferred_address(
    addresses: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return min(
        addresses,
        key=lambda address: (
            0 if isinstance(address, ipaddress.IPv4Address) else 1,
            int(address),
        ),
    )


async def async_pin_install_target(
    hass: HomeAssistant, address: PanelAddress
) -> PinnedPanelTarget:
    """Resolve and pin one safe LAN address for an installer mutation."""
    literal = _literal_address(address.host)
    if literal is not None:
        if not is_allowed_install_address(literal):
            raise InstallNetworkError(InstallNetworkErrorCode.UNSAFE_TARGET)
        selected = literal
    else:
        selected = _preferred_address(await _async_resolve(hass, address))

    return PinnedPanelTarget(
        original=address,
        pinned=PanelAddress(host=str(selected), port=address.port),
    )


async def async_revalidate_install_target(
    hass: HomeAssistant, target: PinnedPanelTarget
) -> PinnedPanelTarget:
    """Require the original name to retain the exact pinned address."""
    pinned_address = _literal_address(target.pinned.host)
    if pinned_address is None or not is_allowed_install_address(pinned_address):
        raise InstallNetworkError(InstallNetworkErrorCode.UNSAFE_TARGET)
    if target.pinned.port != target.original.port:
        raise InstallNetworkError(InstallNetworkErrorCode.PINNED_TARGET_REMOVED)

    original_literal = _literal_address(target.original.host)
    if original_literal is not None:
        current_addresses = frozenset({original_literal})
    else:
        current_addresses = await _async_resolve(hass, target.original)

    if pinned_address not in current_addresses:
        raise InstallNetworkError(InstallNetworkErrorCode.PINNED_TARGET_REMOVED)
    return target
