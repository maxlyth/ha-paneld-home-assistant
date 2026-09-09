"""Tests for the read-only ha-paneld client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientConnectionError

from custom_components.panel_assistant.client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidAddressError,
    InvalidResponseError,
    PanelHealth,
    UpdateApprovalRequiredError,
    UpdateBusyError,
    UpdateRejectedError,
    is_newer_stable_version,
    normalize_address,
    parse_health_response,
    parse_panel_install_status,
)
from custom_components.panel_assistant.const import (
    MAX_STATUS_CAPABILITIES,
    MAX_STATUS_RESPONSE_BYTES,
    MAX_STATUS_WARNING_LENGTH,
    MAX_STATUS_WARNINGS,
)
from custom_components.panel_assistant.status import parse_status_response

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"
HEALTH_FIXTURE = FIXTURE_DIRECTORY / "health.txt"
STATUS_FIXTURE = FIXTURE_DIRECTORY / "status.json"


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

    def post(self, url: Any, **kwargs: Any) -> _FakeResponse:
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
    health = parse_health_response(HEALTH_FIXTURE.read_text(encoding="utf-8"))
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
        "ha-paneld 1.2.3 panel=test build=1234 cfg=0123abcd "
        "ha=future_state ha_src=future_source future=value ha_refused=1\n"
    )
    assert health.panel_id == "test"
    assert health.ha_state is None
    assert health.ha_source is None
    assert health.ha_subscription_refused is True


def test_parser_accepts_current_android_network_suffix() -> None:
    """Current unconsumed Android health fields remain additive and harmless."""
    health = parse_health_response(
        "ha-paneld 0.9.7-rc3 panel=test_panel build=1725312345678 "
        "cfg=0123abcd ha=normal ha_net=healthy ha_resp=warning "
        "ha_net_p95=4200 ha_net_n=30 ha_net_miss=3 ha_net_age=4000\n"
    )

    assert health == PanelHealth(
        version="0.9.7-rc3",
        panel_id="test_panel",
        build="1725312345678",
        config_hash="0123abcd",
        ha_state="normal",
    )


def test_parser_accepts_the_bounded_panel_assistant_discovery_id() -> None:
    """The new Android identity is consumed only after its strict grammar passes."""
    discovery_id = "a" * 64

    health = parse_health_response(
        "ha-paneld 0.9.7-rc4 panel=test_panel build=1725312345678 "
        f"cfg=0123abcd did={discovery_id}\n"
    )

    assert health.discovery_id == discovery_id


@pytest.mark.parametrize("discovery_id", ["A" * 64, "a" * 63, "a" * 65, "not-a-id"])
def test_parser_rejects_malformed_panel_assistant_discovery_id(
    discovery_id: str,
) -> None:
    """Malformed identity tokens cannot reach the config flow or diagnostics."""
    with pytest.raises(InvalidResponseError):
        parse_health_response(
            "ha-paneld 0.9.7-rc4 panel=test_panel build=1725312345678 "
            f"cfg=0123abcd did={discovery_id}\n"
        )


def test_parser_accepts_metadata_boundaries_and_build_fallback() -> None:
    """The response envelope, version limit and Android fallback remain valid."""
    version = f"1.2.3-{'a' * 57}"
    health = parse_health_response(
        f"ha-paneld {version} panel=test build={version} cfg=0123abcd"
    )
    assert health == PanelHealth(version, "test", version, "0123abcd")

    legacy_panel_id = "a" * 469
    legacy_health = parse_health_response(
        f"ha-paneld 0.0.0 panel={legacy_panel_id} build=0 cfg=0123abcd"
    )
    assert legacy_health == PanelHealth("0.0.0", legacy_panel_id, "0", "0123abcd")

    maximum_install_time = str(2**63 - 1)
    timestamp_health = parse_health_response(
        f"ha-paneld 1.2.3 panel=test build={maximum_install_time} cfg=0123abcd"
    )
    assert timestamp_health.build == maximum_install_time


@pytest.mark.parametrize(
    "duplicate",
    [
        "panel=other",
        "build=5678",
        "cfg=89abcdef",
        f"did={'a' * 64} did={'a' * 64}",
        "ha=normal ha=starting",
        "ha_src=mqtt ha_src=socket",
        "ha_refused=1 ha_refused=1",
        "ha_net=healthy ha_net=warning",
    ],
)
def test_parser_rejects_duplicate_known_fields(duplicate: str) -> None:
    """Consumed fields cannot carry an order-dependent second value."""
    body = f"ha-paneld 1.2.3 panel=test build=1234 cfg=0123abcd {duplicate}"

    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


@pytest.mark.parametrize("suffix", ["future", "future=", "=value"])
def test_parser_rejects_malformed_suffix_tokens(suffix: str) -> None:
    """Every additive suffix remains an explicit key-value token."""
    body = f"ha-paneld 1.2.3 panel=test build=1234 cfg=0123abcd {suffix}"

    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


@pytest.mark.parametrize(
    "body",
    [
        "ha-paneld 1.2 panel=test build=1234 cfg=0123abcd",
        "ha-paneld 01.2.3 panel=test build=1234 cfg=0123abcd",
        f"ha-paneld 1.2.3-{'a' * 58} panel=test build=1234 cfg=0123abcd",
        "ha-paneld 1.2.3 panel=Test-Panel build=1234 cfg=0123abcd",
        f"ha-paneld 1.2.3 panel={'a' * 470} build=1234 cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test build=arbitrary cfg=0123abcd",
        f"ha-paneld 1.2.3 panel=test build=1.2.3-{'a' * 58} cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test build=9223372036854775808 cfg=0123abcd",
    ],
)
def test_parser_rejects_metadata_outside_android_bounds(body: str) -> None:
    """Registry-facing metadata must match the current Android producer bounds."""
    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


@pytest.mark.parametrize(
    "body",
    [
        "ha-paneld\t1.2.3 panel=test build=1234 cfg=0123abcd",
        "ha-paneld  1.2.3 panel=test build=1234 cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test\x7f build=1234 cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test build=1234\x1b cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test build=1234 cfg=0123abcd future=value\x00",
        "ha-paneld 1.2.3 panel=test build=1234 cfg=0123abcd\nsecond=line",
    ],
)
def test_parser_rejects_control_bearing_lines(body: str) -> None:
    """Control characters cannot reach HA diagnostics or registry metadata."""
    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


@pytest.mark.parametrize(
    "body",
    [
        "ok",
        "other 1.2.3 panel=test build=1234 cfg=0123abcd",
        "ha-paneld 1.2.3 panel=test build=1234",
        "ha-paneld 1.2.3 panel=test build=1234 cfg=not-a-hash",
    ],
)
def test_reject_invalid_health(body: str) -> None:
    """Malformed or non-ha-paneld bodies do not establish identity."""
    with pytest.raises(InvalidResponseError):
        parse_health_response(body)


async def test_client_fetches_canonical_endpoint() -> None:
    """The client uses the versioned health route without redirects."""
    session = _FakeSession(body=HEALTH_FIXTURE.read_bytes())
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


@pytest.mark.parametrize(
    ("candidate", "installed", "expected"),
    [
        ("0.9.10", "0.9.9", True),
        ("0.9.10", "0.9.10-rc3", True),
        ("0.9.10", "0.9.10", False),
        ("0.9.9", "0.9.10", False),
        ("0.9.10", "invalid", False),
    ],
)
def test_stable_update_comparison_never_offers_a_downgrade(
    candidate: str, installed: str, expected: bool
) -> None:
    """A stable target can replace the equivalent release candidate only."""
    assert is_newer_stable_version(candidate, installed) is expected


async def test_client_uses_the_cached_exact_tag_in_the_panel_update_request() -> None:
    """The integration sends only a cached Android-owned tag to the updater."""
    session = _FakeSession(body=b'{"status":"started"}')
    client = HaPaneldClient(session, normalize_address("panel.local"))  # type: ignore[arg-type]

    await client.async_start_panel_update("v0.9.10")

    assert session.request is not None
    url, kwargs = session.request
    assert str(url) == "http://panel.local:8888/api/v1/install/component"
    assert kwargs["data"] == {
        "name": "paneld",
        "action": "update",
        "version": "v0.9.10",
    }


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (200, b'{"status":"busy"}', UpdateBusyError),
        (200, b'{"status":"other"}', UpdateRejectedError),
        (
            202,
            b'{"error":"approval-required","approval_id":"opaque"}',
            UpdateApprovalRequiredError,
        ),
        (202, b'{"error":"other"}', UpdateRejectedError),
        (403, b'{"status":"denied"}', UpdateRejectedError),
    ],
)
async def test_client_rejects_nonstarted_panel_update(
    status: int, body: bytes, error: type[Exception]
) -> None:
    """Hardened-mode and busy outcomes never become a successful update start."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(status=status, body=body), normalize_address("panel.local")
    )

    with pytest.raises(error):
        await client.async_start_panel_update("v0.9.10")


@pytest.mark.parametrize("tag", ["", "tag/escape", "tag space", "x" * 65])
async def test_client_refuses_noncanonical_update_tags(tag: str) -> None:
    """Only an Android-validated release tag can reach the destructive endpoint."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(), normalize_address("panel.local")
    )

    with pytest.raises(InvalidResponseError):
        await client.async_start_panel_update(tag)


def test_parse_panel_install_status_keeps_only_progress_ownership() -> None:
    """Operation prose remains on the panel, outside Home Assistant state."""
    status = parse_panel_install_status(
        b'{"running":true,"component":"ha-paneld","message":"untrusted prose"}'
    )

    assert status.running is True
    assert status.component == "ha-paneld"


def test_status_parser_projects_only_the_cached_panel_update_target() -> None:
    """The additive status contract does not request or expose a release catalogue."""
    status = parse_status_response(
        '{"warnings":[],"capabilities":[],"panel_assistant_update":'
        '{"state":"available","current_version":"0.9.10-rc3",'
        '"target_version":"0.9.10","tag":"v0.9.10",'
        '"future":"ignored"}}'
    )

    assert status.panel_assistant_update is not None
    assert status.panel_assistant_update.current_version == "0.9.10-rc3"
    assert status.panel_assistant_update.target_version == "0.9.10"
    assert status.panel_assistant_update.tag == "v0.9.10"


@pytest.mark.parametrize(
    "cached_update",
    [
        {"state": "available"},
        {
            "state": "available",
            "current_version": "0.9.9",
            "target_version": "0.9.10-rc1",
            "tag": "v0.9.10-rc1",
        },
        {
            "state": "available",
            "current_version": "0.9.9",
            "target_version": "0.9.10",
            "tag": "bad/tag",
        },
        {
            "state": "available",
            "current_version": "0.9.9",
            "target_version": "0.9.10",
            "tag": "v0.9.11",
        },
        {"state": "none", "tag": "v0.9.10"},
        {"state": "future"},
    ],
)
def test_status_parser_rejects_malformed_cached_panel_update(
    cached_update: dict[str, str],
) -> None:
    """Malformed cached release facts never turn into an HA update action."""
    with pytest.raises(InvalidResponseError):
        parse_status_response(
            json.dumps(
                {
                    "warnings": [],
                    "capabilities": [],
                    "panel_assistant_update": cached_update,
                }
            )
        )


def test_status_parser_accepts_an_explicit_empty_cached_panel_update() -> None:
    """No cached target is a valid local-only outcome, not an unavailable panel."""
    status = parse_status_response(
        '{"warnings":[],"capabilities":[],"panel_assistant_update":{"state":"none"}}'
    )

    assert status.panel_assistant_update is None


@pytest.mark.parametrize(
    ("current_version", "target_version"),
    [
        ("123456789.9.9", "0.9.10"),
        ("0.9.9-preview1", "0.9.10"),
        ("0.9.9-rc123456789", "0.9.10"),
        ("0.9.9", "123456789.9.10"),
        ("0.9.9", "0.9.10-rc1"),
    ],
)
def test_status_parser_matches_android_cached_update_version_bounds(
    current_version: str, target_version: str
) -> None:
    """HA rejects every version shape the Android producer grammar rejects."""
    with pytest.raises(InvalidResponseError):
        parse_status_response(
            json.dumps(
                {
                    "warnings": [],
                    "capabilities": [],
                    "panel_assistant_update": {
                        "state": "available",
                        "current_version": current_version,
                        "target_version": target_version,
                        "tag": f"v{target_version}",
                    },
                }
            )
        )


@pytest.mark.parametrize(
    "current_version",
    [
        "12345678.0.99999999",
        "0.9.9-alpha0",
        "0.9.9-beta12345678",
        "0.9.9-rc1",
    ],
)
def test_status_parser_accepts_android_cached_update_version_boundaries(
    current_version: str,
) -> None:
    """HA accepts the producer's stable and bounded prerelease edge cases."""
    status = parse_status_response(
        json.dumps(
            {
                "warnings": [],
                "capabilities": [],
                "panel_assistant_update": {
                    "state": "available",
                    "current_version": current_version,
                    "target_version": "12345678.0.99999999",
                    "tag": "v12345678.0.99999999",
                },
            }
        )
    )

    assert status.panel_assistant_update is not None


def test_parse_current_status_fixture_projects_only_safe_fields() -> None:
    """The current Android shape is retained without free-form or opaque data."""
    document = json.loads(STATUS_FIXTURE.read_text(encoding="utf-8"))
    document["database_observation_nonce"] = "0123456789abcdef0123456789abcdef"
    document["future_section"] = {"nested": {"value": "ignored"}}
    status = parse_status_response(json.dumps(document))
    diagnostics = status.as_dict()

    assert diagnostics["warning_count"] == 1
    assert diagnostics["capability_count"] == 2
    assert diagnostics["renderer"]["state"] == "rendered"  # type: ignore[index]
    assert diagnostics["storage_health"]["used_percent"] == 50.0  # type: ignore[index]
    assert diagnostics["camera"]["stream_port"] == 8554  # type: ignore[index]
    assert diagnostics["power_safety"]["reason_codes"] == [  # type: ignore[index]
        "doze_not_exempt",
        "stay_on_disabled",
    ]

    serialized = json.dumps(diagnostics)
    assert "192.0.2.10" not in serialized
    assert "192.0.2.11" not in serialized
    assert "rtsp://" not in serialized
    acknowledgement = "a" * 64
    assert acknowledgement not in serialized
    assert "0123456789abcdef0123456789abcdef" not in serialized
    assert "future_section" not in serialized
    for component in (
        "renderer",
        "storage_health",
        "camera",
        "power_safety",
    ):
        assert "summary" not in diagnostics[component]  # type: ignore[operator]
        assert "action" not in diagnostics[component]  # type: ignore[operator]


@pytest.mark.parametrize(
    "addition",
    [
        {},
        {"zigbee_gateway": None},
        {"zigbee_gateway": {"state": "healthy", "future": "ignored"}},
        {"storage_health": {"state": "healthy"}},
        {"power_safety": {"state": "safe"}},
        {"renderer": {"mode": "builtin", "state": "rendered"}},
        {"camera": {"state": "absent"}},
    ],
)
def test_status_parser_accepts_historical_additive_shapes(
    addition: dict[str, object],
) -> None:
    """Sections added after 0.9.0 remain optional and unknown fields are ignored."""
    document: dict[str, object] = {"warnings": [], "capabilities": []}
    document.update(addition)

    status = parse_status_response(json.dumps(document))

    assert status.warning_count == 0
    assert status.capability_count == 0


def test_status_parser_accepts_android_zigbee_and_signed_setting_bounds() -> None:
    """Current multicore CPU and signed OEM settings remain valid evidence."""
    document = {
        "warnings": [],
        "capabilities": [],
        "zigbee_gateway": {
            "state": "healthy",
            "role": "End Device",
            "gateway_cpu_percent": 1000,
            "guard_cpu_percent": 101,
        },
        "power_safety": {
            "state": "unknown",
            "screen_off_timeout_ms": -1,
            "stay_on_while_plugged_in": -1,
        },
    }

    diagnostics = parse_status_response(json.dumps(document)).as_dict()

    assert diagnostics["zigbee_gateway"]["gateway_cpu_percent"] == 1000  # type: ignore[index]
    assert diagnostics["zigbee_gateway"]["role"] == "End Device"  # type: ignore[index]
    assert diagnostics["power_safety"]["screen_off_timeout_ms"] == -1  # type: ignore[index]


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("camera", "delivered_fps"),
        ("storage_health", "used_percent"),
    ],
)
def test_status_parser_rejects_huge_integer_numbers(component: str, field: str) -> None:
    """Bounded JSON integers cannot escape the invalid-response contract."""
    huge_integer = 10**400
    body = json.dumps(
        {
            "warnings": [],
            "capabilities": [],
            component: {"state": "ok", field: huge_integer},
        }
    )
    assert len(str(huge_integer)) == 401
    assert len(body.encode()) <= MAX_STATUS_RESPONSE_BYTES

    with pytest.raises(InvalidResponseError):
        parse_status_response(body)


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "{}",
        '{"warnings":{},"capabilities":[]}',
        '{"warnings":[],"capabilities":{}}',
        '{"warnings":[],"warnings":[],"capabilities":[]}',
        '{"warnings":[],"capabilities":[],"future":NaN}',
        '{"warnings":[],"capabilities":[],"storage_health":{"state":"healthy","used_percent":1e999}}',
        '{"warnings":[],"capabilities":[],"camera":{"state":"live","clients":true}}',
        '{"warnings":[],"capabilities":[],"camera":{"state":"live","stream_port":70000}}',
        '{"warnings":[],"capabilities":[],"storage_health":{"state":"ok","used_percent":101}}',
        '{"warnings":[],"capabilities":[],"zigbee_gateway":{"state":"healthy","gateway_cpu_percent":1001}}',
        '{"warnings":[],"capabilities":[],"renderer":{"mode":"builtin","state":"https://panel.local"}}',
        '{"warnings":[],"capabilities":[],"power_safety":{"state":"ok","reason_codes":[false]}}',
        json.dumps(
            {
                "warnings": ["x" * (MAX_STATUS_WARNING_LENGTH + 1)],
                "capabilities": [],
            }
        ),
    ],
)
def test_status_parser_rejects_malformed_known_data(body: str) -> None:
    """Malformed JSON and invalid known fields never reach diagnostics."""
    with pytest.raises(InvalidResponseError):
        parse_status_response(body)


@pytest.mark.parametrize(
    ("field", "count"),
    [
        ("warnings", MAX_STATUS_WARNINGS + 1),
        ("capabilities", MAX_STATUS_CAPABILITIES + 1),
    ],
)
def test_status_parser_rejects_excessive_original_arrays(
    field: str, count: int
) -> None:
    """The original variable-length arrays have explicit entry limits."""
    document: dict[str, object] = {"warnings": [], "capabilities": []}
    value: object = "warning" if field == "warnings" else {"name": "capability"}
    document[field] = [value] * count

    with pytest.raises(InvalidResponseError):
        parse_status_response(json.dumps(document))


def test_status_parser_ignores_large_or_deep_unknown_fields() -> None:
    """Only the body cap and known-field bounds constrain additive data."""
    wide = {
        "warnings": [],
        "capabilities": [],
        "future": [None] * 512,
    }
    deep: dict[str, object] = {"value": "x" * 4096}
    for _index in range(50):
        deep = {"nested": deep}

    for document in (
        wide,
        {"warnings": [], "capabilities": [], "future": deep},
    ):
        status = parse_status_response(json.dumps(document))
        assert status.as_dict()["warning_count"] == 0


async def test_client_fetches_canonical_status_endpoint() -> None:
    """Status polling is a plain read with no refresh or nonce query."""
    session = _FakeSession(body=STATUS_FIXTURE.read_bytes())
    client = HaPaneldClient(session, normalize_address("panel.local"))  # type: ignore[arg-type]

    status = await client.async_get_status()

    assert status.warning_count == 1
    assert session.request is not None
    url, kwargs = session.request
    assert str(url) == "http://panel.local:8888/api/v1/status"
    assert url.query_string == ""
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"Cache-Control": "no-cache"}


async def test_client_rejects_invalid_status_utf8() -> None:
    """Invalid UTF-8 is rejected before status parsing."""
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(body=b"\xff"), normalize_address("panel.local")
    )

    with pytest.raises(InvalidResponseError):
        await client.async_get_status()


async def test_client_rejects_oversized_valid_status_document() -> None:
    """A valid additive document beyond 64 KiB is rejected by the body guard."""
    assert MAX_STATUS_RESPONSE_BYTES == 64 * 1024
    body = json.dumps(
        {
            "warnings": [],
            "capabilities": [],
            "future": "x" * MAX_STATUS_RESPONSE_BYTES,
        }
    ).encode()
    assert len(body) > MAX_STATUS_RESPONSE_BYTES
    client = HaPaneldClient(  # type: ignore[arg-type]
        _FakeSession(body=body), normalize_address("panel.local")
    )

    with pytest.raises(InvalidResponseError):
        await client.async_get_status()
