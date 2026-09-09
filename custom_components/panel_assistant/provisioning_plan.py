"""Privacy-safe, read-only commissioning guidance from the panel."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .client import HaPaneldClient, InvalidResponseError

MAX_PROVISIONING_BYTES = 32768
_IDS = frozenset({"access.helper", "access.shizuku", "software.webview"})
_IMPORTANCE = frozenset({"required", "recommended", "optional"})
_STATUS = frozenset(
    {"satisfied", "actionable", "manual", "blocked", "degraded", "not_applicable"}
)
_EXECUTORS = frozenset({"app", "host", "local_user", "none"})
_REASONS = frozenset(
    {
        "helper_compatible",
        "daemon_driver_without_helper",
        "helper_incompatible",
        "shizuku_ready",
        "shizuku_consent_disabled",
        "shizuku_permission_required",
        "shizuku_service_not_running",
        "profile_recommended",
        "profile_optional",
        "webview_version_unreadable",
        "webview_recommendation_satisfied",
        "webview_outdated",
        "webview_missing",
    }
    | {
        f"{component}_{reason}"
        for component in ("helper", "shizuku", "webview")
        for reason in (
            "identity_unavailable",
            "observation_not_ready",
            "probe_failed",
            "probe_unsupported",
        )
    }
)


@dataclass(frozen=True, slots=True)
class ProvisioningItem:
    """Allowlisted presentation codes, never commands or panel-authored prose."""

    id: str
    importance: str
    status: str
    executor: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    """Guidance is not proof of complete commissioning or mutation authority."""

    state: str
    items: tuple[ProvisioningItem, ...]
    needs_updated_client: bool


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponseError
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise InvalidResponseError


def _code(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z][a-z0-9_.]{0,95}", value) is None
    ):
        raise InvalidResponseError
    return value


def parse_provisioning_plan(body: bytes) -> ProvisioningPlan:
    """Read schema one; ignore additive fields without retaining private content.

    Android's planner emits at most three items today. The 32-item and 32 KiB
    client limits leave room for additive items without accepting unbounded data.
    Enum values and reason codes follow ProvisioningContracts/ProvisioningPlanner.
    """
    if not isinstance(body, bytes) or len(body) > MAX_PROVISIONING_BYTES:
        raise InvalidResponseError
    try:
        doc = json.loads(
            body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except ValueError, UnicodeError, RecursionError:
        raise InvalidResponseError from None
    if (
        not isinstance(doc, dict)
        or type(doc.get("plan_schema")) is not int
        or doc["plan_schema"] != 1
        or doc.get("state") not in ("satisfied", "attention")
        or not isinstance(doc.get("items"), list)
        or len(doc["items"]) > 32
    ):
        raise InvalidResponseError
    items: list[ProvisioningItem] = []
    seen: set[str] = set()
    updated = False
    for item in doc["items"]:
        if not isinstance(item, dict):
            raise InvalidResponseError
        identity = _code(item.get("id"))
        importance = _code(item.get("importance"))
        status = _code(item.get("status"))
        executor = _code(item.get("executor"))
        reason = _code(item.get("reason_code"))
        if identity in seen:
            raise InvalidResponseError
        seen.add(identity)
        if identity not in _IDS:
            updated = True
            continue
        if (
            importance not in _IMPORTANCE
            or status not in _STATUS
            or executor not in _EXECUTORS
        ):
            updated = True
            continue
        if reason not in _REASONS:
            reason = "unknown"
            updated = True
        items.append(ProvisioningItem(identity, importance, status, executor, reason))
    issues = doc.get("issues", [])
    if not isinstance(issues, list) or len(issues) > 32:
        raise InvalidResponseError
    for issue in issues:
        if not isinstance(issue, dict):
            raise InvalidResponseError
        _code(issue.get("code"))
        updated = True
    return ProvisioningPlan(doc["state"], tuple(items), updated)


async def async_get_provisioning_plan(client: HaPaneldClient) -> ProvisioningPlan:
    """Use the existing deadline, size limit and no-redirect client transport."""
    body = await client._async_get_bounded(
        client.address.base_url.with_path("/api/v1/provisioning/plan"),
        MAX_PROVISIONING_BYTES,
    )
    return parse_provisioning_plan(body)
