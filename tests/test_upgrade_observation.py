"""Fresh database collection refuses stale, malformed and oversized responses."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_paneld.client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidResponseError,
    PanelAddress,
)
from custom_components.ha_paneld.const import MAX_STATUS_RESPONSE_BYTES
from custom_components.ha_paneld.upgrade_observation import (
    async_observe_database,
    parse_database_observation,
)

NONCE = "a" * 32


def body(**storage_changes: object) -> bytes:
    return json.dumps(
        {
            "warnings": [],
            "capabilities": [],
            "database_observation_nonce": NONCE,
            "storage_health": {
                "state": "healthy",
                "schema_version": 14,
                "quick_check": "ok",
                "failure": None,
                **storage_changes,
            },
            "future": {"private": "do-not-retain"},
        }
    ).encode()


def test_collects_only_database_facts() -> None:
    observed = parse_database_observation(body(future="ignored"), NONCE)
    assert observed.schema_version == 14
    assert observed.state == "healthy"
    assert observed.quick_check == "ok"
    assert observed.failure is None
    assert "do-not-retain" not in repr(observed)
    with pytest.raises(FrozenInstanceError):
        observed.schema_version = 1


@pytest.mark.parametrize("schema", [1, 2**31 - 1])
def test_android_schema_bounds(schema: int) -> None:
    assert (
        parse_database_observation(body(schema_version=schema), NONCE).schema_version
        == schema
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": None},
        {"schema_version": True},
        {"schema_version": 0},
        {"schema_version": -1},
        {"schema_version": 2**31},
        {"schema_version": "14"},
        {"schema_version": 14.0},
        {"state": "future"},
        {"state": None},
        {"quick_check": "future"},
        {"quick_check": True},
        {"failure": 0},
    ],
)
def test_invalid_storage(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidResponseError):
        parse_database_observation(body(**changes), NONCE)


@pytest.mark.parametrize("field", ["schema_version", "quick_check", "state"])
def test_missing_storage_field(field: str) -> None:
    document = json.loads(body())
    del document["storage_health"][field]
    with pytest.raises(InvalidResponseError):
        parse_database_observation(json.dumps(document).encode(), NONCE)


def test_android_omits_absent_failure() -> None:
    document = json.loads(body())
    del document["storage_health"]["failure"]
    assert (
        parse_database_observation(json.dumps(document).encode(), NONCE).failure is None
    )


@pytest.mark.parametrize(
    "state", ["unchecked", "warning", "critical", "database_failure"]
)
@pytest.mark.parametrize("check", ["not_run", "failed"])
def test_unhealthy_observations_are_not_relabelled(state: str, check: str) -> None:
    observed = parse_database_observation(
        body(state=state, quick_check=check, failure="corruption"), NONCE
    )
    assert (observed.state, observed.quick_check, observed.failure) == (
        state,
        check,
        "corruption",
    )


@pytest.mark.parametrize(
    "nonce", ["", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", None, 3]
)
def test_invalid_request_nonce(nonce: object) -> None:
    with pytest.raises(InvalidResponseError):
        parse_database_observation(body(), nonce)


@pytest.mark.parametrize("field", ["database_observation_nonce", "storage_health"])
@pytest.mark.parametrize("value", [None, {}, "wrong"])
def test_missing_or_invalid_response_proof(field: str, value: object) -> None:
    document = json.loads(body())
    document[field] = value
    with pytest.raises(InvalidResponseError):
        parse_database_observation(json.dumps(document).encode(), NONCE)
    del document[field]
    with pytest.raises(InvalidResponseError):
        parse_database_observation(json.dumps(document).encode(), NONCE)


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"[]",
        b"null",
        b"\xff",
        b"{" * 2000,
        b" " * (MAX_STATUS_RESPONSE_BYTES + 1),
        body().replace(b'"schema_version": 14', b'"schema_version": NaN'),
        body().replace(
            b'"schema_version": 14', b'"schema_version": 14, "schema_version": 15'
        ),
        body().replace(b'"warnings": []', b'"warnings": [], "warnings": []'),
    ],
)
def test_malformed_response(response: bytes) -> None:
    with pytest.raises(InvalidResponseError):
        parse_database_observation(response, NONCE)


async def test_request_is_bounded_fresh_and_does_not_refresh_updates() -> None:
    client = HaPaneldClient(MagicMock(), PanelAddress("panel.local", 8888))
    second = "b" * 32
    transport = AsyncMock(
        side_effect=[body(), body().replace(NONCE.encode(), second.encode())]
    )
    with (
        patch.object(client, "_async_get_bounded", transport),
        patch(
            "custom_components.ha_paneld.upgrade_observation.secrets.token_hex",
            side_effect=[NONCE, second],
        ) as generate,
    ):
        assert (await async_observe_database(client)).schema_version == 14
        assert (await async_observe_database(client)).schema_version == 14
    assert generate.call_count == 2
    for call, nonce in zip(transport.await_args_list, [NONCE, second], strict=True):
        url, maximum = call.args
        assert (
            str(url)
            == f"http://panel.local:8888/api/v1/status?database_observation_nonce={nonce}"
        )
        assert maximum == MAX_STATUS_RESPONSE_BYTES


@pytest.mark.parametrize(
    "error", [CannotConnectError, InvalidResponseError, asyncio.CancelledError]
)
async def test_transport_failure_does_not_return_cached_evidence(
    error: type[BaseException],
) -> None:
    client = HaPaneldClient(MagicMock(), PanelAddress("panel.local", 8888))
    with (
        patch.object(client, "_async_get_bounded", AsyncMock(side_effect=error)),
        pytest.raises(error),
    ):
        await async_observe_database(client)
