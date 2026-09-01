"""Tests for the read-only ha-paneld client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientConnectionError

from custom_components.ha_paneld.client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidAddressError,
    InvalidResponseError,
    normalize_address,
    parse_health_response,
)

FIXTURE = Path(__file__).parent / "fixtures" / "health.txt"


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, limit: int) -> AsyncIterator[bytes]:
        """Yield bounded chunks like aiohttp's stream reader."""
        for offset in range(0, len(self._body), limit):
            yield self._body[offset : offset + limit]


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _FakeContent(body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSession:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        error: Exception | None = None,
    ) -> None:
        self._response = _FakeResponse(status, body)
        self._error = error
        self.request: tuple[Any, dict[str, Any]] | None = None

    def get(self, url: Any, **kwargs: Any) -> _FakeResponse:
        self.request = (url, kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def test_normalize_address() -> None:
    """Addresses are stored without schemes and with explicit non-default ports."""
    assert normalize_address(" PANEL.local ").stored_value == "panel.local"
    assert normalize_address("192.0.2.1:9999").stored_value == "192.0.2.1:9999"
    assert normalize_address("[fd00::1]").stored_value == "[fd00::1]"
    assert normalize_address("[fd00::1]:9999").stored_value == "[fd00::1]:9999"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://panel.local",
        "panel.local/path",
        "user@panel.local",
        "panel local",
        "panel.local:",
        "panel.local:0",
    ],
)
def test_reject_invalid_address(value: str) -> None:
    """URLs, paths, credentials and whitespace are not panel addresses."""
    with pytest.raises(InvalidAddressError):
        normalize_address(value)


def test_parse_health_fixture() -> None:
    """The replay fixture follows the exact Android health contract."""
    health = parse_health_response(FIXTURE.read_text(encoding="utf-8"))
    assert health.version == "0.9.0"
    assert health.panel_id == "alpha"
    assert health.build == "1000"
    assert health.config_hash == "1a2b3c4d"
    assert health.ha_state == "normal"
    assert health.ha_source == "mqtt"
    assert health.ha_subscription_refused is False


def test_parser_ignores_future_tokens_and_values() -> None:
    """Additive health tokens do not invalidate an otherwise stable response."""
    health = parse_health_response(
        "ha-paneld 1.2.3 panel=test build=abc cfg=0123abcd "
        "ha=future_state future=value ha_refused=1\n"
    )
    assert health.panel_id == "test"
    assert health.ha_state is None
    assert health.ha_subscription_refused is True


@pytest.mark.parametrize(
    "body",
    [
        "ok",
        "other 1.0 panel=test build=abc cfg=0123abcd",
        "ha-paneld 1.0 panel=test build=abc",
        "ha-paneld 1.0 panel=test build=abc cfg=not-a-hash",
    ],
)
def test_reject_invalid_health(body: str) -> None:
    """Malformed or non-ha-paneld bodies do not establish identity."""
    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


async def test_client_fetches_canonical_endpoint() -> None:
    """The client uses the versioned health route without redirects."""
    session = _FakeSession(body=FIXTURE.read_bytes())
    client = HaPaneldClient(session, normalize_address("panel.local"))  # type: ignore[arg-type]

    health = await client.async_get_health()

    assert health.panel_id == "alpha"
    assert session.request is not None
    url, kwargs = session.request
    assert str(url) == "http://panel.local:8888/api/v1/health"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"Cache-Control": "no-cache"}


@pytest.mark.parametrize("status", [301, 403, 500])
async def test_client_rejects_non_success(status: int) -> None:
    """Only an HTTP 200 response validates a panel."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(status=status), normalize_address("panel.local")
    )
    with pytest.raises(CannotConnectError):
        await client.async_get_health()


async def test_client_maps_network_failure() -> None:
    """Network failures use the expected config-flow exception."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(error=ClientConnectionError()),
        normalize_address("panel.local"),
    )
    with pytest.raises(CannotConnectError):
        await client.async_get_health()


async def test_client_rejects_oversized_response() -> None:
    """The client refuses responses beyond the Android readiness bound."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(body=b"x" * 513), normalize_address("panel.local")
    )
    with pytest.raises(InvalidResponseError):
        await client.async_get_health()
