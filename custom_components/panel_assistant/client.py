"""Small read-only client for the ha-paneld health and status contracts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .const import (
    APK_COMMIT_PATH,
    APK_DISCARD_PATH,
    APK_STAGE_PATH,
    BACKUP_PATH,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_SECONDS,
    DIAG_PATH,
    HEALTH_PATH,
    INSTALL_COMPONENT_PATH,
    INSTALL_STATUS_PATH,
    MAX_HEALTH_RESPONSE_BYTES,
    MAX_INSTALL_RESPONSE_BYTES,
    MAX_STATUS_RESPONSE_BYTES,
    STATUS_PATH,
    UPDATE_OWNER_HEADER,
)

if TYPE_CHECKING:
    from .status import PanelStatus

_CONFIG_HASH_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_DISCOVERY_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HEALTH_FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_PANEL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_]*[a-z0-9])?$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_INSTALL_TIME_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,18})$")
_STABLE_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_RELEASE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_VERSION_LENGTH = 63
_MAX_ANDROID_LONG = 2**63 - 1
_KNOWN_HEALTH_FIELDS = frozenset(
    {
        "panel",
        "build",
        "cfg",
        "did",
        "ha",
        "ha_src",
        "ha_refused",
        "ha_net",
        "ha_resp",
        "ha_net_p95",
        "ha_net_n",
        "ha_net_miss",
        "ha_net_age",
    }
)
_LIFECYCLE_STATES = frozenset(
    {"normal", "shutting_down", "starting", "back_online", "connection_lost"}
)
_LIFECYCLE_SOURCES = frozenset({"socket", "mqtt"})


class HaPaneldError(Exception):
    """Base exception for ha-paneld client failures."""


class InvalidAddressError(HaPaneldError):
    """Raised when a panel address cannot be normalized."""


class CannotConnectError(HaPaneldError):
    """Raised when a panel cannot be reached."""


class InvalidResponseError(HaPaneldError):
    """Raised when a panel does not return the health contract."""


class UpdateRejectedError(HaPaneldError):
    """Raised when the panel refuses a requested component update."""


class UpdateBusyError(UpdateRejectedError):
    """Raised when another panel-owned destructive operation is active."""


class UploadDisabledError(UpdateRejectedError):
    """Raised when the panel does not accept app uploads."""


class UpdateApprovalRequiredError(UpdateRejectedError):
    """Raised when Hardened mode requires physical panel approval before retry."""


def is_valid_discovery_id(value: str) -> bool:
    """Return whether an mDNS/health discovery token has the Android contract shape."""
    return _DISCOVERY_ID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class PanelHealth:
    """Parsed fields from the stable ha-paneld health line."""

    version: str
    panel_id: str
    build: str
    config_hash: str
    ha_state: str | None = None
    ha_source: str | None = None
    ha_subscription_refused: bool = False
    discovery_id: str | None = None

    def as_dict(self) -> dict[str, str | bool | None]:
        """Return a serializable diagnostics representation."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PanelInstallStatus:
    """Minimal progress facts retained from the panel-owned update operation."""

    running: bool
    component: str


@dataclass(frozen=True, slots=True)
class PanelAddress:
    """Normalized host and port for a panel."""

    host: str
    port: int

    @property
    def stored_value(self) -> str:
        """Return the canonical config-entry representation."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == DEFAULT_PORT:
            return host
        return f"{host}:{self.port}"

    @property
    def base_url(self) -> URL:
        """Return the panel's HTTP root URL."""
        return URL.build(scheme="http", host=self.host, port=self.port)


def normalize_address(value: str) -> PanelAddress:
    """Normalize a hostname or IP address, with an optional port."""
    candidate = value.strip()
    if not candidate or "://" in candidate:
        raise InvalidAddressError

    try:
        parsed = urlsplit(f"//{candidate}")
        host = parsed.hostname
        port = DEFAULT_PORT if parsed.port is None else parsed.port
    except ValueError as err:
        raise InvalidAddressError from err

    if (
        host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or candidate.endswith(":")
        or any(character.isspace() for character in candidate)
        or not 1 <= port <= 65535
    ):
        raise InvalidAddressError

    return PanelAddress(host=host.lower(), port=port)


def parse_health_response(body: str) -> PanelHealth:
    """Parse the stable health line while ignoring future appended tokens."""
    if len(body) > MAX_HEALTH_RESPONSE_BYTES:
        raise InvalidResponseError
    line = body[:-1] if body.endswith("\n") else body
    if any(not " " <= character <= "~" for character in line):
        raise InvalidResponseError
    tokens = line.split(" ")
    if (
        len(tokens) < 5
        or tokens[0] != "ha-paneld"
        or len(tokens[1]) > _MAX_VERSION_LENGTH
        or _VERSION_PATTERN.fullmatch(tokens[1]) is None
    ):
        raise InvalidResponseError

    fields: dict[str, str] = {}
    for token in tokens[2:]:
        key, _, value = token.partition("=")
        if (
            not value
            or _HEALTH_FIELD_KEY_PATTERN.fullmatch(key) is None
            or (key in _KNOWN_HEALTH_FIELDS and key in fields)
        ):
            raise InvalidResponseError
        fields.setdefault(key, value)

    panel_id = fields.get("panel")
    build = fields.get("build")
    config_hash = fields.get("cfg")
    if (
        panel_id is None
        or _PANEL_ID_PATTERN.fullmatch(panel_id) is None
        or build is None
        or not (
            len(build) <= _MAX_VERSION_LENGTH
            and (
                _VERSION_PATTERN.fullmatch(build) is not None
                or (
                    _INSTALL_TIME_PATTERN.fullmatch(build) is not None
                    and int(build) <= _MAX_ANDROID_LONG
                )
            )
        )
        or config_hash is None
        or _CONFIG_HASH_PATTERN.fullmatch(config_hash) is None
    ):
        raise InvalidResponseError

    ha_state = fields.get("ha")
    if ha_state not in _LIFECYCLE_STATES:
        ha_state = None
    ha_source = fields.get("ha_src")
    if ha_source not in _LIFECYCLE_SOURCES:
        ha_source = None
    discovery_id = fields.get("did")
    if discovery_id is not None and not is_valid_discovery_id(discovery_id):
        raise InvalidResponseError

    return PanelHealth(
        version=tokens[1],
        panel_id=panel_id,
        build=build,
        config_hash=config_hash,
        ha_state=ha_state,
        ha_source=ha_source,
        ha_subscription_refused=fields.get("ha_refused") == "1",
        discovery_id=discovery_id,
    )


def _reject_json_constant(_value: str) -> NoReturn:
    raise InvalidResponseError


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponseError
        result[key] = value
    return result


def _load_json_object(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as err:
        raise InvalidResponseError from err
    if not isinstance(document, dict):
        raise InvalidResponseError
    return document


def _stable_version_parts(value: str) -> tuple[int, int, int] | None:
    match = _STABLE_VERSION_PATTERN.fullmatch(value)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer_stable_version(candidate: str, installed: str) -> bool:
    """Compare a stable candidate with current Android SemVer, including RCs."""
    candidate_parts = _stable_version_parts(candidate)
    installed_match = _VERSION_PATTERN.fullmatch(installed)
    if candidate_parts is None or installed_match is None:
        return False
    installed_parts = tuple(int(part) for part in installed_match.groups()[:3])
    if candidate_parts != installed_parts:
        return candidate_parts > installed_parts
    return "-" in installed


def parse_panel_install_status(body: bytes) -> PanelInstallStatus:
    """Parse only the fixed progress fields needed to recover a self-update."""
    document = _load_json_object(body)
    running = document.get("running")
    component = document.get("component")
    if (
        not isinstance(running, bool)
        or not isinstance(component, str)
        or len(component) > _MAX_VERSION_LENGTH
        or any(not " " <= character <= "~" for character in component)
    ):
        raise InvalidResponseError
    return PanelInstallStatus(running=running, component=component)


def parse_update_start_response(body: bytes) -> None:
    """Accept the panel's only successful start outcome without exposing prose."""
    status = _load_json_object(body).get("status")
    if status == "started":
        return
    if status == "busy":
        raise UpdateBusyError
    raise UpdateRejectedError


def parse_update_approval_response(body: bytes) -> NoReturn:
    """Recognize the one non-success response that requires a physical retry."""
    if _load_json_object(body).get("error") == "approval-required":
        raise UpdateApprovalRequiredError
    raise UpdateRejectedError


_MAX_DIAG_BYTES = 256 * 1024
_MAX_BACKUP_BYTES = 64 * 1024 * 1024
_BACKUP_TIMEOUT_SECONDS = 120.0
# The panel allows 600 s to receive an upload; stop just after it gives up.
_UPLOAD_TIMEOUT_SECONDS = 630.0
_DIAG_FIRST_LINE = re.compile(
    r"ha-paneld diagnostics \u2014 ([0-9A-Za-z][0-9A-Za-z._+-]{0,63}) "
    r"\(build ([1-9][0-9]{0,9})\)"
)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")


@dataclass(frozen=True, slots=True)
class StagedApk:
    """What the panel says about an upload it has staged but not installed."""

    token: str
    package: str
    version: str
    signer: str


def parse_diag_version(body: bytes) -> tuple[str, int]:
    """Read the version name and build number from the diagnostics header."""
    first, _, _rest = body.partition(b"\n")
    try:
        line = first.decode("utf-8").rstrip("\r")
    except UnicodeDecodeError as err:
        raise InvalidResponseError from err
    match = _DIAG_FIRST_LINE.fullmatch(line)
    if match is None:
        raise InvalidResponseError
    code = int(match.group(2))
    if code > _MAX_ANDROID_LONG:
        raise InvalidResponseError
    return match.group(1), code


def parse_staged_apk(body: bytes) -> StagedApk:
    """Accept only the fixed preview fields, each short and printable."""
    document = _load_json_object(body)
    if document.get("ok") is not True:
        raise InvalidResponseError
    fields: list[str] = []
    for key in ("token", "package", "version", "signer"):
        value = document.get(key)
        if (
            not isinstance(value, str)
            or not 0 < len(value) <= 256
            or any(not " " < character <= "~" for character in value)
        ):
            raise InvalidResponseError
        fields.append(value)
    token, package, version, signer = fields
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise InvalidResponseError
    return StagedApk(token=token, package=package, version=version, signer=signer)


class HaPaneldClient:
    """Bounded client for one ha-paneld panel's stable control-plane contracts."""

    def __init__(self, session: ClientSession, address: PanelAddress) -> None:
        """Initialize the client with Home Assistant's shared web session."""
        self._session = session
        self.address = address

    @property
    def configuration_url(self) -> str:
        """Return the panel's browser configuration URL."""
        return str(self.address.base_url)

    @property
    def health_url(self) -> URL:
        """Return the canonical health endpoint."""
        return self.address.base_url.with_path(HEALTH_PATH)

    @property
    def status_url(self) -> URL:
        """Return the canonical status endpoint."""
        return self.address.base_url.with_path(STATUS_PATH)

    @property
    def install_status_url(self) -> URL:
        """Return the panel-owned component-operation status endpoint."""
        return self.address.base_url.with_path(INSTALL_STATUS_PATH)

    @property
    def install_component_url(self) -> URL:
        """Return the panel-owned component-update endpoint."""
        return self.address.base_url.with_path(INSTALL_COMPONENT_PATH)

    async def _async_get_bounded(
        self, url: URL, maximum_bytes: int, extra_headers: dict[str, str] | None = None
    ) -> bytes:
        try:
            async with self._session.get(
                url,
                allow_redirects=False,
                headers={"Cache-Control": "no-cache", **(extra_headers or {})},
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    raise CannotConnectError
                body = bytearray()
                async for chunk in response.content.iter_chunked(maximum_bytes + 1):
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        break
        except CannotConnectError:
            raise
        except (ClientError, TimeoutError) as err:
            raise CannotConnectError from err

        if len(body) > maximum_bytes:
            raise InvalidResponseError
        return bytes(body)

    async def _async_post_bounded(
        self,
        url: URL,
        form: dict[str, str] | bytes,
        maximum_bytes: int,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        try:
            async with self._session.post(
                url,
                data=form,
                allow_redirects=False,
                headers={"Cache-Control": "no-cache"},
                timeout=ClientTimeout(total=timeout_seconds),
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(maximum_bytes + 1):
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        break
                status = response.status
        except (ClientError, TimeoutError) as err:
            raise CannotConnectError from err
        if len(body) > maximum_bytes:
            raise InvalidResponseError
        return status, bytes(body)

    async def async_get_health(self) -> PanelHealth:
        """Fetch and parse the bounded health response."""
        body = await self._async_get_bounded(self.health_url, MAX_HEALTH_RESPONSE_BYTES)
        try:
            return parse_health_response(body.decode("utf-8"))
        except UnicodeDecodeError as err:
            raise InvalidResponseError from err

    async def async_get_status(self, *, update_owner: bool = False) -> PanelStatus:
        """Fetch and parse the bounded, privacy-safe status response.

        With ``update_owner`` the request tells the panel this Home Assistant
        shows its ha-paneld update entity.
        """
        from .status import parse_status_response

        body = await self._async_get_bounded(
            self.status_url,
            MAX_STATUS_RESPONSE_BYTES,
            {UPDATE_OWNER_HEADER: "1"} if update_owner else None,
        )
        try:
            return parse_status_response(body.decode("utf-8"))
        except UnicodeDecodeError as err:
            raise InvalidResponseError from err

    async def async_get_panel_install_status(self) -> PanelInstallStatus:
        """Read fixed operation progress while an accepted update restarts the panel."""
        body = await self._async_get_bounded(
            self.install_status_url, MAX_INSTALL_RESPONSE_BYTES
        )
        return parse_panel_install_status(body)

    async def async_start_panel_update(self, tag: str) -> None:
        """Ask the panel to install one exact cached panel-approved release tag."""
        if not isinstance(tag, str) or _RELEASE_TAG_PATTERN.fullmatch(tag) is None:
            raise InvalidResponseError
        status, body = await self._async_post_bounded(
            self.install_component_url,
            {"name": "paneld", "action": "update", "version": tag},
            MAX_INSTALL_RESPONSE_BYTES,
        )
        if status == 200:
            parse_update_start_response(body)
            return
        if status == 202:
            parse_update_approval_response(body)
        if 400 <= status < 500:
            raise UpdateRejectedError
        raise CannotConnectError

    async def async_get_version_code(self) -> tuple[str, int]:
        """Read the running app's version name and build number.

        No JSON contract carries the build number; the diagnostics dump names it
        on its first line, which is the same text the panel's own UI shows.
        """
        body = await self._async_get_bounded(
            self.address.base_url.with_path(DIAG_PATH), _MAX_DIAG_BYTES
        )
        return parse_diag_version(body)

    async def async_backup_panel(self) -> bytes:
        """Take the panel's own settings backup before it is changed."""
        status, body = await self._async_post_bounded(
            self.address.base_url.with_path(BACKUP_PATH),
            {"allow_plaintext": "1", "include_companion": "false"},
            _MAX_BACKUP_BYTES,
            _BACKUP_TIMEOUT_SECONDS,
        )
        if status == 200 and body:
            return body
        if status == 202:
            parse_update_approval_response(body)
        if status == 409:
            raise UpdateBusyError
        if 400 <= status < 500:
            raise UpdateRejectedError
        raise CannotConnectError

    async def async_stage_apk(self, apk: bytes) -> StagedApk:
        """Upload app bytes for the panel to inspect before anything installs."""
        status, body = await self._async_post_bounded(
            self.address.base_url.with_path(APK_STAGE_PATH),
            apk,
            MAX_INSTALL_RESPONSE_BYTES,
            _UPLOAD_TIMEOUT_SECONDS,
        )
        if status == 200:
            return parse_staged_apk(body)
        if status == 403:
            raise UploadDisabledError
        if status == 409:
            raise UpdateBusyError
        # 4xx, plus "no root" (503) and "no space" (507), are the panel saying no.
        if 400 <= status < 500 or status in (503, 507):
            raise UpdateRejectedError
        raise CannotConnectError

    async def async_commit_apk(self, token: str) -> None:
        """Install exactly the staged upload the caller inspected."""
        status, body = await self._async_post_bounded(
            self.address.base_url.with_path(APK_COMMIT_PATH),
            {"token": token},
            MAX_INSTALL_RESPONSE_BYTES,
        )
        if status == 200:
            parse_update_start_response(body)
            return
        if status == 202:
            parse_update_approval_response(body)
        if status == 403:
            raise UploadDisabledError
        if 400 <= status < 500:
            raise UpdateRejectedError
        raise CannotConnectError

    async def async_discard_apk(self, token: str) -> None:
        """Delete a staged upload that must not be installed."""
        await self._async_post_bounded(
            self.address.base_url.with_path(APK_DISCARD_PATH),
            {"token": token},
            MAX_INSTALL_RESPONSE_BYTES,
        )
