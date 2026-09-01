"""Small read-only client for the ha-paneld health contract."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .const import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_SECONDS,
    HEALTH_PATH,
    MAX_HEALTH_RESPONSE_BYTES,
)

_CONFIG_HASH_PATTERN = re.compile(r"^[0-9a-f]{8}$")
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
    tokens = body.strip().split()
    if len(tokens) < 5 or tokens[0] != "ha-paneld" or not tokens[1]:
        raise InvalidResponseError

    fields: dict[str, str] = {}
    for token in tokens[2:]:
        key, separator, value = token.partition("=")
        if separator and key and value:
            fields.setdefault(key, value)

    panel_id = fields.get("panel")
    build = fields.get("build")
    config_hash = fields.get("cfg")
    if (
        not panel_id
        or not build
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

    async def async_get_health(self) -> PanelHealth:
        """Fetch and parse the bounded health response."""
        try:
            async with self._session.get(
                self.health_url,
                allow_redirects=False,
                headers={"Cache-Control": "no-cache"},
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    raise CannotConnectError
                body = bytearray()
                async for chunk in response.content.iter_chunked(
                    MAX_HEALTH_RESPONSE_BYTES + 1
                ):
                    body.extend(chunk)
                    if len(body) > MAX_HEALTH_RESPONSE_BYTES:
                        break
        except CannotConnectError:
            raise
        except (ClientError, TimeoutError) as err:
            raise CannotConnectError from err

        if len(body) > MAX_HEALTH_RESPONSE_BYTES:
            raise InvalidResponseError
        try:
            return parse_health_response(bytes(body).decode("utf-8"))
        except UnicodeDecodeError as err:
            raise InvalidResponseError from err
