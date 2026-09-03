"""Small read-only client for the ha-paneld health and status contracts."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .const import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_SECONDS,
    HEALTH_PATH,
    MAX_HEALTH_RESPONSE_BYTES,
    MAX_STATUS_RESPONSE_BYTES,
    STATUS_PATH,
)

if TYPE_CHECKING:
    from .status import PanelStatus

_CONFIG_HASH_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_HEALTH_FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_PANEL_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_]*[a-z0-9])?$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_INSTALL_TIME_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,18})$")
_MAX_VERSION_LENGTH = 63
_MAX_ANDROID_LONG = 2**63 - 1
_KNOWN_HEALTH_FIELDS = frozenset(
    {
        "panel",
        "build",
        "cfg",
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

    def as_dict(self) -> dict[str, str | bool | None]:
        """Return a serializable diagnostics representation."""
        return asdict(self)


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

    return PanelHealth(
        version=tokens[1],
        panel_id=panel_id,
        build=build,
        config_hash=config_hash,
        ha_state=ha_state,
        ha_source=ha_source,
        ha_subscription_refused=fields.get("ha_refused") == "1",
    )


class HaPaneldClient:
    """Read-only client for one ha-paneld panel."""

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

    async def _async_get_bounded(self, url: URL, maximum_bytes: int) -> bytes:
        try:
            async with self._session.get(
                url,
                allow_redirects=False,
                headers={"Cache-Control": "no-cache"},
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

    async def async_get_health(self) -> PanelHealth:
        """Fetch and parse the bounded health response."""
        body = await self._async_get_bounded(self.health_url, MAX_HEALTH_RESPONSE_BYTES)
        try:
            return parse_health_response(body.decode("utf-8"))
        except UnicodeDecodeError as err:
            raise InvalidResponseError from err

    async def async_get_status(self) -> PanelStatus:
        """Fetch and parse the bounded, privacy-safe status response."""
        from .status import parse_status_response

        body = await self._async_get_bounded(self.status_url, MAX_STATUS_RESPONSE_BYTES)
        try:
            return parse_status_response(body.decode("utf-8"))
        except UnicodeDecodeError as err:
            raise InvalidResponseError from err
