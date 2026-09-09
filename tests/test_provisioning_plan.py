"""Commissioning guidance is bounded, additive and privacy-safe."""

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.panel_assistant.client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidResponseError,
    normalize_address,
)
from custom_components.panel_assistant.provisioning_plan import (
    MAX_PROVISIONING_BYTES,
    async_get_provisioning_plan,
    parse_provisioning_plan,
)


def document() -> dict:
    return {
        "plan_schema": 1,
        "state": "attention",
        "items": [
            {
                "id": "access.shizuku",
                "importance": "recommended",
                "status": "manual",
                "executor": "local_user",
                "reason_code": "shizuku_permission_required",
                "desired_state": "private-value",
                "observed_state": "private-value",
            }
        ],
        "issues": [],
        "profile": {"display_name": "private-value"},
    }


def test_retains_only_allowlisted_presentation_codes() -> None:
    result = parse_provisioning_plan(json.dumps(document()).encode())
    assert result.state == "attention"
    assert result.items[0].reason_code == "shizuku_permission_required"
    assert not result.needs_updated_client
    assert "private-value" not in repr(asdict(result))


@pytest.mark.parametrize(
    "field", ["id", "importance", "status", "executor", "reason_code"]
)
def test_future_codes_do_not_leak_or_claim_full_support(field: str) -> None:
    doc = document()
    doc["items"][0][field] = "future_private_value"
    result = parse_provisioning_plan(json.dumps(doc).encode())
    assert result.needs_updated_client
    assert "future_private_value" not in repr(result)
    if field == "reason_code":
        assert result.items[0].reason_code == "unknown"
    else:
        assert result.items == ()


@pytest.mark.parametrize(
    "field", ["id", "importance", "status", "executor", "reason_code"]
)
@pytest.mark.parametrize(
    "value", [None, [], 1, "", "a" * 97, "a\nb", "https://private"]
)
def test_malformed_codes_rejected(field: str, value: object) -> None:
    doc = document()
    doc["items"][0][field] = value
    with pytest.raises(InvalidResponseError):
        parse_provisioning_plan(json.dumps(doc).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_schema", True),
        ("plan_schema", 2),
        ("plan_schema", "1"),
        ("state", "future"),
        ("state", []),
        ("items", {}),
        ("items", [None]),
        ("issues", {}),
        ("issues", [None]),
        ("issues", [{}]),
    ],
)
def test_invalid_document(field: str, value: object) -> None:
    doc = document()
    doc[field] = value
    with pytest.raises(InvalidResponseError):
        parse_provisioning_plan(json.dumps(doc).encode())


@pytest.mark.parametrize(
    "body",
    [
        b'{"plan_schema":1,"plan_schema":1}',
        b'{"unknown":NaN}',
        b'{"unknown":{"x":1,"x":2}}',
        b"\xff",
        b"[]",
        b"null",
        b"[" * 2000,
        b" " * (MAX_PROVISIONING_BYTES + 1),
    ],
)
def test_strict_json_and_size(body: bytes) -> None:
    with pytest.raises(InvalidResponseError):
        parse_provisioning_plan(body)


def test_duplicates_and_excessive_items_rejected() -> None:
    doc = document()
    doc["items"] *= 2
    with pytest.raises(InvalidResponseError):
        parse_provisioning_plan(json.dumps(doc).encode())
    doc["items"] *= 17
    with pytest.raises(InvalidResponseError):
        parse_provisioning_plan(json.dumps(doc).encode())


def test_new_issue_marks_incomplete_without_leaking_code() -> None:
    doc = document()
    doc["issues"] = [{"code": "future_private_issue"}]
    result = parse_provisioning_plan(json.dumps(doc).encode())
    assert result.needs_updated_client
    assert "future_private_issue" not in repr(result)


async def test_fetch_uses_existing_bounded_transport() -> None:
    client = HaPaneldClient(MagicMock(), normalize_address("panel.example"))
    client._async_get_bounded = AsyncMock(return_value=json.dumps(document()).encode())
    result = await async_get_provisioning_plan(client)
    assert result.items
    client._async_get_bounded.assert_awaited_once_with(
        client.address.base_url.with_path("/api/v1/provisioning/plan"),
        MAX_PROVISIONING_BYTES,
    )


async def test_old_endpoint_unavailable_is_not_reinterpreted_as_ready() -> None:
    client = HaPaneldClient(MagicMock(), normalize_address("panel.example"))
    client._async_get_bounded = AsyncMock(side_effect=CannotConnectError)
    with pytest.raises(CannotConnectError):
        await async_get_provisioning_plan(client)
