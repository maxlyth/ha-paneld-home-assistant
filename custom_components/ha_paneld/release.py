"""Resolve an authenticated stable ha-paneld release without downloading its APK."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, NoReturn

from aiohttp import ClientError, ClientSession, ClientTimeout
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from yarl import URL

_LATEST_RELEASE_URL = URL(
    "https://api.github.com/repos/maxlyth/ha-paneld/releases/latest"
)
_REPOSITORY_RELEASE_ROOT = "https://github.com/maxlyth/ha-paneld/releases/download"
_STABLE_TAG_PATTERN = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_MAX_TAG_LENGTH = 64
_MAX_RELEASE_RESPONSE_BYTES = 256 * 1024
_MAX_CHECKSUM_RESPONSE_BYTES = 512
_MAX_SIGNATURE_RESPONSE_BYTES = 512
_MAX_RELEASE_ASSETS = 128
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REQUEST_TIMEOUT_SECONDS = 10.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 5.0
_RSA_SIGNATURE_BYTES = 256
_TRUSTED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
    }
)
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Cache-Control": "no-cache",
    "X-GitHub-Api-Version": "2022-11-28",
}
_ASSET_HEADERS = {
    "Accept": "application/octet-stream",
    "Cache-Control": "no-cache",
}
_RELEASE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3LH+db6kzNld/ERP612x
UOOG6TINFvuKJKinQAWi6Gfm2jCmW4plhw+w4vXgP8B8FpY0SLatUVo3EeAi+f1K
EHj0syPi7Sx781o1oc9LicQG4LjWVZPe+m4AkPl9ByopobQwYTXOjaq6ZFpFgAZe
NwQ44hg5o9iVKtxpnnjHEc/m6o9TBySQvxDWF3RxCDyPLNBqhrsgKsDlAyh+dtA8
aJpQsDUJoX42xsRvA1hkRCpnWdEs1Bwfyv0ztlOxj7MxeFrFxWc3mnUyGhsn6rCT
O+ygQ2m7FHp3D5t1+wFIendluEzUC+y9MpUHmoyq/lFrVuA8EOiy1U+z7Lr1vBWf
LQIDAQAB
-----END PUBLIC KEY-----
"""


class ReleaseResolutionError(Exception):
    """Raised when a stable release cannot be authenticated exactly."""


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """Authenticated metadata for one stable release APK."""

    tag: str
    version: str
    apk_name: str
    apk_url: str
    sha256: str


def _request_timeout() -> ClientTimeout:
    """Return explicit total, connection and read bounds for each request."""
    return ClientTimeout(
        total=_REQUEST_TIMEOUT_SECONDS,
        connect=_CONNECT_TIMEOUT_SECONDS,
        sock_read=_READ_TIMEOUT_SECONDS,
    )


def _reject_json_constant(_value: str) -> NoReturn:
    """Reject non-standard NaN and infinity values."""
    raise ReleaseResolutionError


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseResolutionError
        result[key] = value
    return result


def _is_trusted_download_url(url: URL) -> bool:
    """Return whether a release redirect remains on GitHub's HTTPS asset hosts."""
    return (
        url.scheme == "https"
        and url.user is None
        and url.password is None
        and url.host in _TRUSTED_DOWNLOAD_HOSTS
        and url.port == 443
        and not url.fragment
    )


async def _async_fetch_bounded(
    session: ClientSession,
    url: URL,
    maximum_bytes: int,
    *,
    allow_release_redirects: bool,
    headers: dict[str, str],
) -> bytes:
    """Fetch one response under fixed status, redirect, time and byte bounds."""
    current_url = url
    redirects = 0
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            while True:
                if allow_release_redirects and not _is_trusted_download_url(
                    current_url
                ):
                    raise ReleaseResolutionError

                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers=headers,
                    timeout=_request_timeout(),
                ) as response:
                    if response.history or response.url != current_url:
                        raise ReleaseResolutionError

                    if response.status in _REDIRECT_STATUSES:
                        if not allow_release_redirects or redirects >= _MAX_REDIRECTS:
                            raise ReleaseResolutionError
                        locations = response.headers.getall("Location", ())
                        if len(locations) != 1:
                            raise ReleaseResolutionError
                        location = locations[0]
                        if (
                            not isinstance(location, str)
                            or not location
                            or location != location.strip()
                            or any(ord(character) < 32 for character in location)
                            or "\x7f" in location
                        ):
                            raise ReleaseResolutionError
                        next_url = current_url.join(URL(location))
                        if not _is_trusted_download_url(next_url):
                            raise ReleaseResolutionError
                        current_url = next_url
                        redirects += 1
                        continue

                    if response.status != 200:
                        raise ReleaseResolutionError

                    content_length = response.content_length
                    if content_length is not None and content_length > maximum_bytes:
                        raise ReleaseResolutionError

                    body = bytearray()
                    async for chunk in response.content.iter_chunked(maximum_bytes + 1):
                        if not isinstance(chunk, bytes):
                            raise ReleaseResolutionError
                        body.extend(chunk)
                        if len(body) > maximum_bytes:
                            raise ReleaseResolutionError
                    return bytes(body)
    except ReleaseResolutionError:
        raise
    except (ClientError, TimeoutError, ValueError) as err:
        raise ReleaseResolutionError from err


def _parse_release_metadata(body: bytes) -> tuple[str, str, dict[str, URL]]:
    """Select the one exact APK and proof triplet from a stable GitHub release."""
    try:
        document: Any = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ReleaseResolutionError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as err:
        raise ReleaseResolutionError from err

    if not isinstance(document, dict):
        raise ReleaseResolutionError

    tag = document.get("tag_name")
    assets = document.get("assets")
    if (
        not isinstance(tag, str)
        or len(tag) > _MAX_TAG_LENGTH
        or _STABLE_TAG_PATTERN.fullmatch(tag) is None
        or document.get("draft") is not False
        or document.get("prerelease") is not False
        or not isinstance(assets, list)
        or len(assets) > _MAX_RELEASE_ASSETS
    ):
        raise ReleaseResolutionError

    apk_name = f"ha-paneld-{tag}-manual-setup-required.apk"
    expected_names = frozenset(
        {
            apk_name,
            f"{apk_name}.sha256",
            f"{apk_name}.sha256.sig",
        }
    )
    selected: dict[str, URL] = {}

    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseResolutionError
        name = asset.get("name")
        if not isinstance(name, str):
            raise ReleaseResolutionError
        if name not in expected_names:
            continue
        raw_url = asset.get("browser_download_url")
        expected_url = f"{_REPOSITORY_RELEASE_ROOT}/{tag}/{name}"
        if not isinstance(raw_url, str) or raw_url != expected_url or name in selected:
            raise ReleaseResolutionError
        selected[name] = URL(raw_url)

    if selected.keys() != expected_names:
        raise ReleaseResolutionError

    return tag, apk_name, selected


def _verify_checksum_signature(checksum: bytes, signature: bytes) -> None:
    """Authenticate the checksum record with the key used by the public installer."""
    if len(signature) != _RSA_SIGNATURE_BYTES:
        raise ReleaseResolutionError
    try:
        public_key = serialization.load_pem_public_key(_RELEASE_PUBLIC_KEY_PEM)
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise ReleaseResolutionError
        public_key.verify(
            signature,
            checksum,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except ReleaseResolutionError:
        raise
    except (InvalidSignature, TypeError, ValueError) as err:
        raise ReleaseResolutionError from err


def _parse_checksum_record(checksum: bytes, apk_name: str) -> str:
    """Parse one canonical lowercase GNU sha256sum record for the exact APK."""
    pattern = re.compile(
        rb"([0-9a-f]{64})  " + re.escape(apk_name.encode("ascii")) + rb"\n"
    )
    match = pattern.fullmatch(checksum)
    if match is None:
        raise ReleaseResolutionError
    return match.group(1).decode("ascii")


async def async_resolve_stable_release(session: ClientSession) -> ReleaseArtifact:
    """Resolve and authenticate the latest stable ha-paneld APK metadata.

    The APK itself is deliberately not downloaded. The returned digest is trusted
    only after the exact checksum bytes have passed detached RSA verification.
    """
    release_body = await _async_fetch_bounded(
        session,
        _LATEST_RELEASE_URL,
        _MAX_RELEASE_RESPONSE_BYTES,
        allow_release_redirects=False,
        headers=_API_HEADERS,
    )
    tag, apk_name, assets = _parse_release_metadata(release_body)

    checksum_name = f"{apk_name}.sha256"
    signature_name = f"{checksum_name}.sig"
    checksum = await _async_fetch_bounded(
        session,
        assets[checksum_name],
        _MAX_CHECKSUM_RESPONSE_BYTES,
        allow_release_redirects=True,
        headers=_ASSET_HEADERS,
    )
    signature = await _async_fetch_bounded(
        session,
        assets[signature_name],
        _MAX_SIGNATURE_RESPONSE_BYTES,
        allow_release_redirects=True,
        headers=_ASSET_HEADERS,
    )
    _verify_checksum_signature(checksum, signature)
    sha256 = _parse_checksum_record(checksum, apk_name)

    return ReleaseArtifact(
        tag=tag,
        version=tag.removeprefix("v"),
        apk_name=apk_name,
        apk_url=str(assets[apk_name]),
        sha256=sha256,
    )
