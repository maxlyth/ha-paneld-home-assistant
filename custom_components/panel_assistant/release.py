"""Authenticate stable or explicitly selected RC metadata without downloading an APK."""

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
_RC_TAG_PATTERN = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$"
)
_MAX_TAG_LENGTH = 64
_MAX_RELEASE_RESPONSE_BYTES = 256 * 1024
_MAX_CHECKSUM_RESPONSE_BYTES = 512
_MAX_SIGNATURE_RESPONSE_BYTES = 512
# The closed v1 descriptor is currently well below 1 KiB.  Four KiB leaves room
# for bounded schema evolution without accepting an arbitrary release payload.
_MAX_INSTALL_DESCRIPTOR_BYTES = 4 * 1024
_MAX_RELEASE_ASSETS = 128
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REQUEST_TIMEOUT_SECONDS = 10.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 5.0
_RSA_SIGNATURE_BYTES = 256
_MAX_APK_BYTES = 64 * 1024 * 1024
_MAX_ANDROID_SDK = 100
_MAX_ANDROID_VERSION_CODE = 2**31 - 1
_INSTALL_DESCRIPTOR_SCHEMA = "io.github.maxlyth.hapaneld.install.v1"
_PACKAGE_ID = "io.github.maxlyth.hapaneld"
_LAUNCH_COMPONENT = f"{_PACKAGE_ID}/.MainActivity"
_RELEASE_SIGNER_CERTIFICATE_SHA256 = (
    "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
)
_SUPPORTED_ABIS = ("arm64-v8a", "armeabi-v7a")
_INSTALL_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema",
        "releaseTag",
        "versionName",
        "versionCode",
        "apkName",
        "apkSize",
        "apkSha256",
        "packageId",
        "signerCertificateSha256",
        "minSdk",
        "supportedAbis",
        "databaseCompatibility",
        "launchComponent",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_COMPATIBILITY_PATTERN = re.compile(
    r"^hapaneld-db:v1:ha-paneld\.db:([1-9][0-9]*):([1-9][0-9]*)$"
)
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
    """Raised when a selected release cannot be authenticated exactly."""


@dataclass(frozen=True, slots=True)
class InstallDescriptor:
    """Signed installation and compatibility facts for one exact APK."""

    schema: str
    release_tag: str
    version_name: str
    version_code: int
    apk_name: str
    apk_size: int
    apk_sha256: str
    package_id: str
    signer_certificate_sha256: str
    min_sdk: int
    supported_abis: tuple[str, ...]
    database_compatibility: str
    launch_component: str


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """Authenticated metadata for one exact release APK."""

    tag: str
    version: str
    apk_name: str
    apk_url: str
    sha256: str
    descriptor: InstallDescriptor | None = None


@dataclass(frozen=True, slots=True, repr=False)
class SignedReleaseMetadata:
    """Original authenticated bytes for independent browser verification."""

    checksum: bytes
    checksum_signature: bytes
    descriptor_bytes: bytes
    descriptor_signature: bytes


@dataclass(frozen=True, slots=True, repr=False)
class InstallReleaseBundle:
    """A descriptor-bearing release and its exact signed metadata, not APK bytes."""

    artifact: ReleaseArtifact
    metadata: SignedReleaseMetadata


def is_rc_release_tag(value: object) -> bool:
    """Accept only a bounded canonical exact release-candidate tag."""
    return (
        isinstance(value, str)
        and len(value) <= _MAX_TAG_LENGTH
        and _RC_TAG_PATTERN.fullmatch(value) is not None
    )


def is_install_release_tag(value: object) -> bool:
    """Keep durable release identity restricted to stable or canonical RC tags."""
    return (
        isinstance(value, str)
        and len(value) <= _MAX_TAG_LENGTH
        and (
            _STABLE_TAG_PATTERN.fullmatch(value) is not None or is_rc_release_tag(value)
        )
    )


# A dev build from the signed build feed is named by its version code. It is
# never a GitHub tag, so the two identities can never be confused.
_FEED_BUILD_TAG_PATTERN = re.compile(r"^build-([1-9][0-9]{0,9})$")
_VERSION_NAME_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


def feed_build_tag(version_code: int) -> str:
    """Name a feed build."""
    return f"build-{version_code}"


def feed_build_code(value: object) -> int | None:
    """Return the version code a feed build tag names, or None."""
    match = _FEED_BUILD_TAG_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    code = int(match.group(1))
    return code if code <= _MAX_ANDROID_VERSION_CODE else None


def is_feed_build_tag(value: object) -> bool:
    """Accept only a bounded feed build tag."""
    return feed_build_code(value) is not None


def artifact_identity_matches(
    release_tag: object,
    version_name: object,
    version_code: object,
    apk_name: object,
    apk_sha256: object,
) -> bool:
    """The one rule binding an artifact's tag, version and file name together.

    A GitHub release is named by its tag; a feed build by its version code and
    its content-addressed file name.
    """
    if is_install_release_tag(release_tag):
        assert isinstance(release_tag, str)
        return (
            version_name == release_tag.removeprefix("v")
            and apk_name == f"ha-paneld-{release_tag}-manual-setup-required.apk"
        )
    code = feed_build_code(release_tag)
    return (
        code is not None
        and version_code == code
        and isinstance(version_name, str)
        and _VERSION_NAME_PATTERN.fullmatch(version_name) is not None
        and isinstance(apk_sha256, str)
        and _SHA256_PATTERN.fullmatch(apk_sha256) is not None
        and apk_name == f"{apk_sha256}.apk"
    )


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


def _parse_release_metadata(
    body: bytes, *, expected_rc_tag: str | None = None
) -> tuple[str, str, dict[str, URL]]:
    """Select exact assets without allowing a channel or requested-tag substitution."""
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
        or (
            _STABLE_TAG_PATTERN.fullmatch(tag) is None
            if expected_rc_tag is None
            else not is_rc_release_tag(expected_rc_tag) or tag != expected_rc_tag
        )
        or document.get("draft") is not False
        or document.get("prerelease") is not (expected_rc_tag is not None)
        or not isinstance(assets, list)
        or len(assets) > _MAX_RELEASE_ASSETS
    ):
        raise ReleaseResolutionError

    apk_name = f"ha-paneld-{tag}-manual-setup-required.apk"
    required_names = frozenset(
        {
            apk_name,
            f"{apk_name}.sha256",
            f"{apk_name}.sha256.sig",
        }
    )
    descriptor_name = f"ha-paneld-{tag}-install.json"
    descriptor_names = frozenset({descriptor_name, f"{descriptor_name}.sig"})
    relevant_names = required_names | descriptor_names
    selected: dict[str, URL] = {}

    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseResolutionError
        name = asset.get("name")
        if not isinstance(name, str):
            raise ReleaseResolutionError
        if name not in relevant_names:
            continue
        raw_url = asset.get("browser_download_url")
        expected_url = f"{_REPOSITORY_RELEASE_ROOT}/{tag}/{name}"
        if not isinstance(raw_url, str) or raw_url != expected_url or name in selected:
            raise ReleaseResolutionError
        selected[name] = URL(raw_url)

    if not required_names.issubset(selected):
        raise ReleaseResolutionError
    selected_descriptor_names = descriptor_names.intersection(selected)
    if selected_descriptor_names and selected_descriptor_names != descriptor_names:
        raise ReleaseResolutionError

    return tag, apk_name, selected


def _verify_detached_signature(payload: bytes, signature: bytes) -> None:
    """Authenticate exact release bytes with the public installer key."""
    if len(signature) != _RSA_SIGNATURE_BYTES:
        raise ReleaseResolutionError
    try:
        public_key = serialization.load_pem_public_key(_RELEASE_PUBLIC_KEY_PEM)
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise ReleaseResolutionError
        public_key.verify(
            signature,
            payload,
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


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    """Return a genuine JSON integer within the closed descriptor bound."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseResolutionError
    if not minimum <= value <= maximum:
        raise ReleaseResolutionError
    return value


def _parse_install_descriptor(
    body: bytes,
    *,
    tag: str,
    apk_name: str,
    apk_sha256: str,
) -> InstallDescriptor:
    """Parse one canonical, closed and cross-bound signed v1 descriptor."""
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

    if not isinstance(document, dict) or document.keys() != _INSTALL_DESCRIPTOR_FIELDS:
        raise ReleaseResolutionError
    try:
        canonical = (
            json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as err:
        raise ReleaseResolutionError from err
    if body != canonical:
        raise ReleaseResolutionError

    version = tag.removeprefix("v")
    apk_size = _bounded_integer(document["apkSize"], 1, _MAX_APK_BYTES)
    version_code = _bounded_integer(
        document["versionCode"], 1, _MAX_ANDROID_VERSION_CODE
    )
    min_sdk = _bounded_integer(document["minSdk"], 1, _MAX_ANDROID_SDK)
    database_compatibility = document["databaseCompatibility"]
    database_match = (
        _DATABASE_COMPATIBILITY_PATTERN.fullmatch(database_compatibility)
        if isinstance(database_compatibility, str)
        else None
    )
    database_bounds: tuple[int, int] | None = None
    if database_match is not None and all(
        len(group) <= 10 for group in database_match.groups()
    ):
        database_bounds = (
            int(database_match.group(1)),
            int(database_match.group(2)),
        )
    supported_abis = document["supportedAbis"]
    apk_sha256_value = document["apkSha256"]
    if (
        document["schema"] != _INSTALL_DESCRIPTOR_SCHEMA
        or document["releaseTag"] != tag
        or document["versionName"] != version
        or document["apkName"] != apk_name
        or not isinstance(apk_sha256_value, str)
        or apk_sha256_value != apk_sha256
        or _SHA256_PATTERN.fullmatch(apk_sha256_value) is None
        or document["packageId"] != _PACKAGE_ID
        or document["signerCertificateSha256"] != _RELEASE_SIGNER_CERTIFICATE_SHA256
        or not isinstance(supported_abis, list)
        or tuple(supported_abis) != _SUPPORTED_ABIS
        or database_bounds is None
        or not 1
        <= database_bounds[0]
        <= database_bounds[1]
        <= _MAX_ANDROID_VERSION_CODE
        or document["launchComponent"] != _LAUNCH_COMPONENT
    ):
        raise ReleaseResolutionError

    return InstallDescriptor(
        schema=_INSTALL_DESCRIPTOR_SCHEMA,
        release_tag=tag,
        version_name=version,
        version_code=version_code,
        apk_name=apk_name,
        apk_size=apk_size,
        apk_sha256=apk_sha256,
        package_id=_PACKAGE_ID,
        signer_certificate_sha256=_RELEASE_SIGNER_CERTIFICATE_SHA256,
        min_sdk=min_sdk,
        supported_abis=_SUPPORTED_ABIS,
        database_compatibility=database_compatibility,
        launch_component=_LAUNCH_COMPONENT,
    )


async def async_resolve_stable_release(session: ClientSession) -> ReleaseArtifact:
    """Resolve and authenticate the latest stable ha-paneld APK metadata.

    The APK itself is deliberately not downloaded. The returned digest is trusted
    only after the exact checksum bytes have passed detached RSA verification.
    """
    artifact, _ = await _async_resolve_release(session, _LATEST_RELEASE_URL)
    return artifact


async def async_resolve_rc_release(session: ClientSession, tag: str) -> ReleaseArtifact:
    """Authenticate one explicitly requested RC; never fall back or select latest."""
    if not is_rc_release_tag(tag):
        raise ReleaseResolutionError
    url = URL(f"https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/{tag}")
    artifact, _ = await _async_resolve_release(session, url, expected_rc_tag=tag)
    return artifact


async def async_resolve_install_bundle(
    session: ClientSession, *, rc_tag: str | None = None
) -> InstallReleaseBundle:
    """Retain signed bytes for a stable or explicitly selected RC installation.

    This only resolves metadata; it neither downloads the APK nor creates a job.
    Unlike legacy release inspection, browser installation requires a descriptor.
    """
    url = _LATEST_RELEASE_URL
    if rc_tag is not None:
        if not is_rc_release_tag(rc_tag):
            raise ReleaseResolutionError
        url = URL(
            f"https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/{rc_tag}"
        )
    artifact, metadata = await _async_resolve_release(
        session, url, expected_rc_tag=rc_tag
    )
    if metadata is None:
        raise ReleaseResolutionError
    return InstallReleaseBundle(artifact=artifact, metadata=metadata)


async def _async_resolve_release(
    session: ClientSession, url: URL, *, expected_rc_tag: str | None = None
) -> tuple[ReleaseArtifact, SignedReleaseMetadata | None]:
    """Share identical byte bounds, signatures and descriptor binding for both paths."""
    release_body = await _async_fetch_bounded(
        session,
        url,
        _MAX_RELEASE_RESPONSE_BYTES,
        allow_release_redirects=False,
        headers=_API_HEADERS,
    )
    tag, apk_name, assets = _parse_release_metadata(
        release_body, expected_rc_tag=expected_rc_tag
    )

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
    _verify_detached_signature(checksum, signature)
    sha256 = _parse_checksum_record(checksum, apk_name)

    descriptor_name = f"ha-paneld-{tag}-install.json"
    descriptor: InstallDescriptor | None = None
    metadata: SignedReleaseMetadata | None = None
    if descriptor_name in assets:
        descriptor_body = await _async_fetch_bounded(
            session,
            assets[descriptor_name],
            _MAX_INSTALL_DESCRIPTOR_BYTES,
            allow_release_redirects=True,
            headers=_ASSET_HEADERS,
        )
        descriptor_signature = await _async_fetch_bounded(
            session,
            assets[f"{descriptor_name}.sig"],
            _MAX_SIGNATURE_RESPONSE_BYTES,
            allow_release_redirects=True,
            headers=_ASSET_HEADERS,
        )
        _verify_detached_signature(descriptor_body, descriptor_signature)
        descriptor = _parse_install_descriptor(
            descriptor_body,
            tag=tag,
            apk_name=apk_name,
            apk_sha256=sha256,
        )
        metadata = SignedReleaseMetadata(
            checksum=checksum,
            checksum_signature=signature,
            descriptor_bytes=descriptor_body,
            descriptor_signature=descriptor_signature,
        )

    artifact = ReleaseArtifact(
        tag=tag,
        version=tag.removeprefix("v"),
        apk_name=apk_name,
        apk_url=str(assets[apk_name]),
        sha256=sha256,
        descriptor=descriptor,
    )
    return artifact, metadata
