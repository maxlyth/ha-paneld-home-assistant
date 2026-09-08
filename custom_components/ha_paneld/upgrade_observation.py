"""Fresh database observations for upgrade checks, not installation authority."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass

from .client import HaPaneldClient, InvalidResponseError
from .const import MAX_STATUS_RESPONSE_BYTES
from .status import parse_status_response

_NONCE = re.compile(r"[0-9a-f]{32}")
_STATES = frozenset({"unchecked", "healthy", "warning", "critical", "database_failure"})
_QUICK_CHECKS = frozenset({"not_run", "ok", "failed"})


@dataclass(frozen=True, slots=True)
class DatabaseObservation:
    """Same-request database facts; no backup, identity or upgrade authorization."""

    schema_version: int
    state: str
    quick_check: str
    failure: str | None


def parse_database_observation(body: bytes, expected_nonce: str) -> DatabaseObservation:
    """Reject missing freshness evidence without falling back to cached status."""
    if (
        not isinstance(body, bytes)
        or len(body) > MAX_STATUS_RESPONSE_BYTES
        or not isinstance(expected_nonce, str)
        or _NONCE.fullmatch(expected_nonce) is None
    ):
        raise InvalidResponseError
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidResponseError from None

    # The shared parser rejects duplicate keys, constants and malformed known
    # fields, while tolerating additive fields. Only then inspect the nonce.
    status = parse_status_response(text)
    document = json.loads(text)
    if document.get("database_observation_nonce") != expected_nonce:
        raise InvalidResponseError
    storage = status.storage_health
    if storage is None:
        raise InvalidResponseError
    schema = storage.get("schema_version")
    state = storage.get("state")
    quick_check = storage.get("quick_check")
    failure = storage.get("failure")
    if (
        type(schema) is not int
        or not 1 <= schema <= 2**31 - 1
        or not isinstance(state, str)
        or state not in _STATES
        or not isinstance(quick_check, str)
        or quick_check not in _QUICK_CHECKS
        or (failure is not None and not isinstance(failure, str))
    ):
        raise InvalidResponseError
    return DatabaseObservation(schema, state, quick_check, failure)


async def async_observe_database(client: HaPaneldClient) -> DatabaseObservation:
    """Request a fresh SQLite observation without refreshing release discovery.

    Every call owns a new nonce and uses the existing bounded, no-redirect
    transport. These facts describe this request only; do not persist or reuse
    them as mutation admission. Package identity and backup custody are separate.
    """
    nonce = secrets.token_hex(16)
    body = await client._async_get_bounded(
        client.status_url.with_query(database_observation_nonce=nonce),
        MAX_STATUS_RESPONSE_BYTES,
    )
    return parse_database_observation(body, nonce)
