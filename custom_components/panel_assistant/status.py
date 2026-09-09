"""Bounded, privacy-safe projection of the additive panel status contract."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from .client import InvalidResponseError
from .const import (
    MAX_ANDROID_INTEGER,
    MAX_STATUS_CAPABILITIES,
    MAX_STATUS_FAULT_DETAIL_LENGTH,
    MAX_STATUS_INTEGER,
    MAX_STATUS_REASON_CODES,
    MAX_STATUS_TOKEN_LENGTH,
    MAX_STATUS_WARNING_LENGTH,
    MAX_STATUS_WARNINGS,
    MAX_ZIGBEE_CPU_PERCENT,
    MIN_ANDROID_INTEGER,
)

type StatusValue = str | bool | int | float | list[str] | None
type ComponentStatus = Mapping[str, StatusValue]
type FieldValidator = Callable[[object], StatusValue]

_TOKEN_PATTERN = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.-]{{0,{MAX_STATUS_TOKEN_LENGTH - 1}}}$"
)
_FAULT_DETAIL_PATTERN = re.compile(
    rf"^[A-Za-z0-9_]{{1,{MAX_STATUS_FAULT_DETAIL_LENGTH}}}$"
)
_PANEL_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_STABLE_PANEL_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_RELEASE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class PanelStatus:
    """Sanitized fields retained from one status response."""

    warning_count: int
    capability_count: int
    zigbee_gateway: ComponentStatus | None = None
    storage_health: ComponentStatus | None = None
    renderer: ComponentStatus | None = None
    camera: ComponentStatus | None = None
    power_safety: ComponentStatus | None = None
    panel_assistant_update: PanelCachedUpdate | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a serializable copy suitable for diagnostics."""
        return {
            "warning_count": self.warning_count,
            "capability_count": self.capability_count,
            "zigbee_gateway": _copy_component(self.zigbee_gateway),
            "storage_health": _copy_component(self.storage_health),
            "renderer": _copy_component(self.renderer),
            "camera": _copy_component(self.camera),
            "power_safety": _copy_component(self.power_safety),
            "panel_assistant_update": (
                self.panel_assistant_update.as_dict()
                if self.panel_assistant_update is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class PanelCachedUpdate:
    """One cached panel-owned stable update target, without release metadata."""

    current_version: str
    target_version: str
    tag: str

    def as_dict(self) -> dict[str, str]:
        """Return the small privacy-safe projection used by diagnostics."""
        return {
            "current_version": self.current_version,
            "target_version": self.target_version,
            "tag": self.tag,
        }


def _copy_component(component: ComponentStatus | None) -> dict[str, StatusValue] | None:
    if component is None:
        return None
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in component.items()
    }


def _reject_json_constant(_value: str) -> NoReturn:
    raise InvalidResponseError


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponseError
        result[key] = value
    return result


def _token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise InvalidResponseError
    return value


def _nullable_token(value: object) -> str | None:
    return None if value is None else _token(value)


def _fault_detail(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _FAULT_DETAIL_PATTERN.fullmatch(value) is None:
        raise InvalidResponseError
    return value


def _short_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_STATUS_TOKEN_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvalidResponseError
    return value


def _nullable_short_text(value: object) -> str | None:
    return None if value is None else _short_text(value)


def _nullable_zigbee_role(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvalidResponseError
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise InvalidResponseError
    return value


def _nullable_boolean(value: object) -> bool | None:
    return None if value is None else _boolean(value)


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidResponseError
    if not 0 <= value <= MAX_STATUS_INTEGER:
        raise InvalidResponseError
    return value


def _nullable_nonnegative_integer(value: object) -> int | None:
    return None if value is None else _nonnegative_integer(value)


def _nullable_android_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidResponseError
    if not MIN_ANDROID_INTEGER <= value <= MAX_ANDROID_INTEGER:
        raise InvalidResponseError
    return value


def _nonnegative_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidResponseError
    if not 0 <= value <= MAX_STATUS_INTEGER or not math.isfinite(value):
        raise InvalidResponseError
    return value


def _nullable_nonnegative_number(value: object) -> int | float | None:
    return None if value is None else _nonnegative_number(value)


def _nullable_percent(value: object) -> int | float | None:
    result = _nullable_nonnegative_number(value)
    if result is not None and result > 100:
        raise InvalidResponseError
    return result


def _nullable_zigbee_cpu_percent(value: object) -> int | None:
    result = _nullable_nonnegative_integer(value)
    if result is not None and result > MAX_ZIGBEE_CPU_PERCENT:
        raise InvalidResponseError
    return result


def _nullable_port(value: object) -> int | None:
    if value is None:
        return None
    result = _nonnegative_integer(value)
    if not 1 <= result <= 65535:
        raise InvalidResponseError
    return result


def _reason_codes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_STATUS_REASON_CODES:
        raise InvalidResponseError
    return [_token(item) for item in value]


def _panel_cached_update(root: Mapping[str, object]) -> PanelCachedUpdate | None:
    """Parse the additive Android cache without causing a release lookup."""
    if "panel_assistant_update" not in root:
        return None
    value = root["panel_assistant_update"]
    if not isinstance(value, dict):
        raise InvalidResponseError
    state = value.get("state")
    if state == "none":
        if any(
            field in value for field in ("current_version", "target_version", "tag")
        ):
            raise InvalidResponseError
        return None
    if state != "available":
        raise InvalidResponseError
    current_version = value.get("current_version")
    target_version = value.get("target_version")
    tag = value.get("tag")
    if (
        not isinstance(current_version, str)
        or len(current_version) > 64
        or _PANEL_VERSION_PATTERN.fullmatch(current_version) is None
        or not isinstance(target_version, str)
        or len(target_version) > 64
        or _STABLE_PANEL_VERSION_PATTERN.fullmatch(target_version) is None
        or not isinstance(tag, str)
        or _RELEASE_TAG_PATTERN.fullmatch(tag) is None
        or tag.removeprefix("v") != target_version
    ):
        raise InvalidResponseError
    return PanelCachedUpdate(
        current_version=current_version,
        target_version=target_version,
        tag=tag,
    )


_ZIGBEE_FIELDS: dict[str, FieldValidator] = {
    "state": _token,
    "firmware": _nullable_short_text,
    "product_version": _nullable_short_text,
    "gateway_layout": _token,
    "gateway_package_version": _nullable_short_text,
    "joined": _nullable_boolean,
    "role": _nullable_zigbee_role,
    "gateway_cpu_percent": _nullable_zigbee_cpu_percent,
    "guard_cpu_percent": _nullable_zigbee_cpu_percent,
    "restart_count_10m": _nonnegative_integer,
    "containment_result": _token,
    "recursive_watchdog_assignment": _boolean,
}

_STORAGE_FIELDS: dict[str, FieldValidator] = {
    "state": _token,
    "pressure_state": _token,
    "usable_bytes": _nullable_nonnegative_integer,
    "total_bytes": _nullable_nonnegative_integer,
    "used_percent": _nullable_percent,
    "database_bytes": _nullable_nonnegative_integer,
    "wal_bytes": _nullable_nonnegative_integer,
    "page_size_bytes": _nullable_nonnegative_integer,
    "page_count": _nullable_nonnegative_integer,
    "freelist_count": _nullable_nonnegative_integer,
    "schema_version": _nullable_nonnegative_integer,
    "quick_check": _token,
    "checked_at": _nullable_nonnegative_integer,
    "failure": _nullable_token,
}

_RENDERER_FIELDS: dict[str, FieldValidator] = {
    "mode": _token,
    "state": _token,
    "outcome": _token,
    "fault": _token,
    "fault_detail": _fault_detail,
    "recovery": _token,
    "transport": _token,
    "address_family_policy": _token,
    "observed_age_ms": _nullable_nonnegative_integer,
    "process_age_ms": _nonnegative_integer,
    "package_updated_age_ms": _nullable_nonnegative_integer,
    "rendered": _boolean,
    "theme_policy": _token,
    "theme_effective": _nullable_token,
    "theme_overridden": _boolean,
}

_CAMERA_FIELDS: dict[str, FieldValidator] = {
    "state": _token,
    "outcome": _token,
    "fault": _token,
    "fault_detail": _fault_detail,
    "recovery": _token,
    "clients": _nonnegative_integer,
    "last_frame_age_ms": _nullable_nonnegative_integer,
    "consecutive_failures": _nonnegative_integer,
    "indication": _token,
    "live": _boolean,
    "stream_clients": _nonnegative_integer,
    "stream_port": _nullable_port,
    "encoder": _nullable_short_text,
    "encode_width": _nullable_nonnegative_integer,
    "encode_height": _nullable_nonnegative_integer,
    "encode_fps": _nullable_nonnegative_integer,
    "encode_kbps": _nullable_nonnegative_integer,
    "delivered_fps": _nullable_nonnegative_number,
    "delivered_kbps": _nullable_nonnegative_integer,
}

_POWER_FIELDS: dict[str, FieldValidator] = {
    "state": _token,
    "warning": _boolean,
    "repair_capability": _token,
    "repair_available": _boolean,
    "manual_only": _boolean,
    "acknowledge_available": _boolean,
    "acknowledged": _boolean,
    "reason_codes": _reason_codes,
    "keep_awake_configured": _boolean,
    "wake_lock_held": _boolean,
    "wifi_lock_required": _boolean,
    "wifi_lock_held": _boolean,
    "prevent_idle_dim_configured": _boolean,
    "screen_off_timeout_ms": _nullable_android_integer,
    "interactive": _nullable_boolean,
    "power_source": _token,
    "plugged_mask": _nullable_nonnegative_integer,
    "stay_on_while_plugged_in": _nullable_android_integer,
    "stay_on_effective": _nullable_boolean,
    "device_idle": _nullable_boolean,
    "doze_exempt": _nullable_boolean,
    "screen_off_mechanism": _token,
}


def _sanitize_component(
    root: Mapping[str, object],
    name: str,
    fields: Mapping[str, FieldValidator],
    required: frozenset[str],
    *,
    nullable: bool = False,
) -> dict[str, StatusValue] | None:
    if name not in root:
        return None
    value = root[name]
    if value is None and nullable:
        return None
    if not isinstance(value, dict) or not required.issubset(value):
        raise InvalidResponseError
    return {
        field: validator(value[field])
        for field, validator in fields.items()
        if field in value
    }


def _validate_original_arrays(root: Mapping[str, object]) -> tuple[int, int]:
    warnings = root.get("warnings")
    capabilities = root.get("capabilities")
    if not isinstance(warnings, list) or len(warnings) > MAX_STATUS_WARNINGS:
        raise InvalidResponseError
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > MAX_STATUS_CAPABILITIES
    ):
        raise InvalidResponseError
    for warning in warnings:
        if not isinstance(warning, str) or len(warning) > MAX_STATUS_WARNING_LENGTH:
            raise InvalidResponseError
    for capability in capabilities:
        if not isinstance(capability, dict):
            raise InvalidResponseError
        for field, limit in (("name", 128), ("note", 512), ("color", 32)):
            value = capability.get(field)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > limit
            ):
                raise InvalidResponseError
    return len(warnings), len(capabilities)


def parse_status_response(body: str) -> PanelStatus:
    """Parse status JSON and retain only bounded, privacy-safe known fields."""
    try:
        parsed = json.loads(
            body,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except InvalidResponseError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as err:
        raise InvalidResponseError from err

    if not isinstance(parsed, dict):
        raise InvalidResponseError
    warning_count, capability_count = _validate_original_arrays(parsed)

    return PanelStatus(
        warning_count=warning_count,
        capability_count=capability_count,
        zigbee_gateway=_sanitize_component(
            parsed,
            "zigbee_gateway",
            _ZIGBEE_FIELDS,
            frozenset({"state"}),
            nullable=True,
        ),
        storage_health=_sanitize_component(
            parsed, "storage_health", _STORAGE_FIELDS, frozenset({"state"})
        ),
        renderer=_sanitize_component(
            parsed, "renderer", _RENDERER_FIELDS, frozenset({"mode", "state"})
        ),
        camera=_sanitize_component(
            parsed, "camera", _CAMERA_FIELDS, frozenset({"state"})
        ),
        power_safety=_sanitize_component(
            parsed, "power_safety", _POWER_FIELDS, frozenset({"state"})
        ),
        panel_assistant_update=_panel_cached_update(parsed),
    )
