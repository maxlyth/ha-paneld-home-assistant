"""Tests for authenticated stable ha-paneld release resolution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientConnectionError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from yarl import URL

from custom_components.ha_paneld import release
from custom_components.ha_paneld.release import (
    ReleaseResolutionError,
    async_resolve_stable_release,
)

_TAG = "v1.2.3"
_VERSION = "1.2.3"
_APK_NAME = f"ha-paneld-{_TAG}-manual-setup-required.apk"
_APK_URL = f"https://github.com/maxlyth/ha-paneld/releases/download/{_TAG}/{_APK_NAME}"
_CHECKSUM_URL = f"{_APK_URL}.sha256"
_SIGNATURE_URL = f"{_CHECKSUM_URL}.sig"
_SHA256 = "0123456789abcdef" * 4
_CHECKSUM = f"{_SHA256}  {_APK_NAME}\n".encode()


class _FakeContent:
    def __init__(self, body: bytes | list[bytes | str]) -> None:
        self._chunks = [body] if isinstance(body, bytes) else body

    async def iter_chunked(self, _limit: int) -> AsyncIterator[bytes | str]:
        for chunk in self._chunks:
            yield chunk


@dataclass
class _FakeResponse:
    status: int
    body: bytes | list[bytes | str]
    url: URL
    history: tuple[Any, ...] = ()
    declared_length: int | None = None

    def __post_init__(self) -> None:
        self.content = _FakeContent(self.body)

    @property
    def content_length(self) -> int | None:
        return self.declared_length

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSession:
    def __init__(
        self,
        responses: dict[str, _FakeResponse],
        *,
        error: Exception | None = None,
    ) -> None:
        self._responses = responses
        self._error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: URL, **kwargs: Any) -> _FakeResponse:
        raw_url = str(url)
        self.requests.append((raw_url, kwargs))
        if self._error is not None:
            raise self._error
        return self._responses[raw_url]


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """Create a disposable release key for deterministic offline proof tests."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _release_document(
    *,
    tag: Any = _TAG,
    draft: Any = False,
    prerelease: Any = False,
    assets: Any = None,
) -> dict[str, Any]:
    if assets is None:
        assets = [
            {
                "name": _APK_NAME,
                "browser_download_url": _APK_URL,
                "size": 12_345,
                "future_asset_field": {"ignored": True},
            },
            {
                "name": f"{_APK_NAME}.sha256",
                "browser_download_url": _CHECKSUM_URL,
            },
            {
                "name": f"{_APK_NAME}.sha256.sig",
                "browser_download_url": _SIGNATURE_URL,
            },
            {
                "name": "release-notes.txt",
                "browser_download_url": (
                    f"https://github.com/maxlyth/ha-paneld/releases/download/"
                    f"{_TAG}/release-notes.txt"
                ),
            },
        ]
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
        "future_release_field": {"nested": [1, 2, 3]},
    }


def _metadata_response(document: Any) -> _FakeResponse:
    return _FakeResponse(
        status=200,
        body=json.dumps(document).encode(),
        url=release._LATEST_RELEASE_URL,
    )


def _signature(signing_key: rsa.RSAPrivateKey, checksum: bytes) -> bytes:
    return signing_key.sign(checksum, padding.PKCS1v15(), hashes.SHA256())


def _install_test_key(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(release, "_RELEASE_PUBLIC_KEY_PEM", public_key)


def _successful_session(
    signing_key: rsa.RSAPrivateKey,
    *,
    checksum: bytes = _CHECKSUM,
    signature: bytes | None = None,
) -> _FakeSession:
    if signature is None:
        signature = _signature(signing_key, checksum)
    return _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(_release_document()),
            _CHECKSUM_URL: _FakeResponse(200, checksum, URL(_CHECKSUM_URL)),
            _SIGNATURE_URL: _FakeResponse(200, signature, URL(_SIGNATURE_URL)),
        }
    )


async def test_resolves_signed_stable_release_without_downloading_apk(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """Only metadata and the signed proof are read; additive fields are ignored."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)

    artifact = await async_resolve_stable_release(session)  # type: ignore[arg-type]

    assert artifact.tag == _TAG
    assert artifact.version == _VERSION
    assert artifact.apk_name == _APK_NAME
    assert artifact.apk_url == _APK_URL
    assert artifact.sha256 == _SHA256
    requested_urls = [url for url, _kwargs in session.requests]
    assert requested_urls == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
        _SIGNATURE_URL,
    ]
    assert _APK_URL not in requested_urls

    metadata_kwargs = session.requests[0][1]
    assert metadata_kwargs["allow_redirects"] is False
    assert metadata_kwargs["max_redirects"] == 3
    assert metadata_kwargs["headers"]["Accept"] == "application/vnd.github+json"
    for _url, kwargs in session.requests:
        timeout = kwargs["timeout"]
        assert timeout.total == 10.0
        assert timeout.connect == 5.0
        assert timeout.sock_read == 5.0
    for _url, kwargs in session.requests[1:]:
        assert kwargs["allow_redirects"] is True
        assert kwargs["max_redirects"] == 3
        assert kwargs["headers"]["Accept"] == "application/octet-stream"


def test_embedded_public_key_matches_installer_key_fingerprint() -> None:
    """The resolver pins the same RSA public key as scripts/install.sh."""
    public_key = serialization.load_pem_public_key(release._RELEASE_PUBLIC_KEY_PEM)
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.key_size == 2048
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert hashlib.sha256(der).hexdigest() == (
        "502bf38874682ff337f187022b904adbc3ab0b387fd7ceb4043ce722997273f3"
    )


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v1.2.3.4",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-rc1",
        "v1.2.3+build.1",
        "v\N{ARABIC-INDIC DIGIT ONE}.2.3",
        "v" + "1" * 65 + ".2.3",
    ],
)
async def test_rejects_noncanonical_or_nonstable_tags(tag: str) -> None:
    """Only a bounded v-prefixed stable SemVer tag can select an artifact."""
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(tag=tag)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert len(session.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"\xff",
        b"[]",
        json.dumps({"draft": False, "prerelease": False, "assets": []}).encode(),
        json.dumps({"tag_name": _TAG, "prerelease": False, "assets": []}).encode(),
        json.dumps({"tag_name": _TAG, "draft": False, "assets": []}).encode(),
        json.dumps(
            {
                "tag_name": _TAG,
                "draft": False,
                "prerelease": False,
                "assets": {},
            }
        ).encode(),
        json.dumps(
            _release_document(assets=[None, *_release_document()["assets"]])
        ).encode(),
        json.dumps(
            _release_document(assets=[{}, *_release_document()["assets"]])
        ).encode(),
        json.dumps(
            _release_document(assets=[{}] * (release._MAX_RELEASE_ASSETS + 1))
        ).encode(),
    ],
)
async def test_rejects_malformed_release_documents(body: bytes) -> None:
    """Malformed known GitHub fields cannot influence release selection."""
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _FakeResponse(
                200, body, release._LATEST_RELEASE_URL
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(("draft", "prerelease"), [(True, False), (False, True)])
async def test_rejects_release_flags_that_are_not_stable(
    draft: bool, prerelease: bool
) -> None:
    """GitHub draft and prerelease flags fail closed even with a stable tag shape."""
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(draft=draft, prerelease=prerelease)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing_name",
    [_APK_NAME, f"{_APK_NAME}.sha256", f"{_APK_NAME}.sha256.sig"],
)
async def test_rejects_release_with_any_required_asset_missing(
    missing_name: str,
) -> None:
    """APK, checksum and signature are one indivisible release triplet."""
    assets = [
        asset
        for asset in _release_document()["assets"]
        if asset["name"] != missing_name
    ]
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_wrongly_named_asset_triplet() -> None:
    """A plausible APK for another tag is not a substitute for the exact asset."""
    wrong_apk = "ha-paneld-v1.2.4-manual-setup-required.apk"
    assets = [
        {
            "name": wrong_apk + suffix,
            "browser_download_url": (
                f"https://github.com/maxlyth/ha-paneld/releases/download/"
                f"{_TAG}/{wrong_apk}{suffix}"
            ),
        }
        for suffix in ("", ".sha256", ".sha256.sig")
    ]
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "replacement_url",
    [
        "http://github.com/maxlyth/ha-paneld/releases/download/v1.2.3/asset",
        "https://example.com/asset",
        (f"https://github.com/maxlyth/ha-paneld/releases/download/v1.2.4/{_APK_NAME}"),
    ],
)
async def test_rejects_required_asset_with_noncanonical_url(
    replacement_url: str,
) -> None:
    """GitHub metadata cannot redirect initial artifact selection elsewhere."""
    assets = _release_document()["assets"]
    assets[0] = {**assets[0], "browser_download_url": replacement_url}
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_duplicate_required_asset() -> None:
    """Two assets with the canonical name are ambiguous and fail closed."""
    assets = _release_document()["assets"]
    assets.append(dict(assets[0]))
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_invalid_checksum_signature(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A correctly sized but invalid signature cannot authenticate the digest."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, signature=b"x" * 256)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "checksum",
    [
        f"{'A' * 64}  {_APK_NAME}\n".encode(),
        f"{_SHA256} {_APK_NAME}\n".encode(),
        f"{_SHA256}  wrong.apk\n".encode(),
        f"{_SHA256}  {_APK_NAME}".encode(),
        f"{_SHA256}  {_APK_NAME}\r\n".encode(),
        f"{_SHA256}  {_APK_NAME}\nextra\n".encode(),
        f"{_SHA256[:-1]}  {_APK_NAME}\n".encode(),
    ],
)
async def test_rejects_signed_but_malformed_checksum_record(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    checksum: bytes,
) -> None:
    """A valid signature does not relax exact lowercase checksum-record syntax."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, checksum=checksum)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("target", "body"),
    [
        ("metadata", b"x" * (release._MAX_RELEASE_RESPONSE_BYTES + 1)),
        ("checksum", b"x" * (release._MAX_CHECKSUM_RESPONSE_BYTES + 1)),
        ("signature", b"x" * (release._MAX_SIGNATURE_RESPONSE_BYTES + 1)),
    ],
)
async def test_rejects_oversized_responses(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    target: str,
    body: bytes,
) -> None:
    """Metadata and both proof assets have independent hard byte limits."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    request_url = {
        "metadata": str(release._LATEST_RELEASE_URL),
        "checksum": _CHECKSUM_URL,
        "signature": _SIGNATURE_URL,
    }[target]
    session._responses[request_url].body = body
    session._responses[request_url].content = _FakeContent(body)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_excessive_declared_content_length() -> None:
    """An excessive Content-Length fails before the response body is consumed."""
    response = _metadata_response(_release_document())
    response.declared_length = release._MAX_RELEASE_RESPONSE_BYTES + 1
    response.content = _FakeContent(["must not be consumed"])
    session = _FakeSession({str(release._LATEST_RELEASE_URL): response})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_nonbyte_response_chunks() -> None:
    """The resolver never coerces an unexpected stream payload type."""
    response = _metadata_response(_release_document())
    response.content = _FakeContent(["not bytes"])
    session = _FakeSession({str(release._LATEST_RELEASE_URL): response})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_wrong_signature_size(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A detached signature must have the pinned 2048-bit RSA width."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, signature=b"short")

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_non_rsa_embedded_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the pinned trust key to another algorithm fails closed."""
    ec_public_key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(release, "_RELEASE_PUBLIC_KEY_PEM", ec_public_key)
    session = _successful_session(
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "final_url",
    [
        URL("http://release-assets.githubusercontent.com/proof"),
        URL("https://example.com/proof"),
        URL("https://github.com:444/proof"),
        URL("https://user@github.com/proof"),
    ],
)
async def test_rejects_untrusted_release_asset_redirect(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    final_url: URL,
) -> None:
    """Proof redirects stay on a narrow HTTPS GitHub asset-host allow-list."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    checksum_response = session._responses[_CHECKSUM_URL]
    checksum_response.url = final_url
    checksum_response.history = (SimpleNamespace(url=URL(_CHECKSUM_URL)),)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_accepts_bounded_github_release_asset_redirect(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """The normal GitHub-to-release-assets redirect remains usable."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    checksum_response = session._responses[_CHECKSUM_URL]
    checksum_response.url = URL(
        "https://release-assets.githubusercontent.com/github-production-release-asset/proof"
    )
    checksum_response.history = (SimpleNamespace(url=URL(_CHECKSUM_URL)),)

    artifact = await async_resolve_stable_release(session)  # type: ignore[arg-type]

    assert artifact.sha256 == _SHA256


async def test_rejects_more_than_three_redirects(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A response cannot evade aiohttp's configured redirect cap."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    session._responses[_CHECKSUM_URL].history = tuple(
        SimpleNamespace(url=URL(_CHECKSUM_URL)) for _index in range(4)
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_metadata_redirect() -> None:
    """The GitHub API lookup itself never follows or accepts a redirect."""
    response = _metadata_response(_release_document())
    response.history = (SimpleNamespace(url=release._LATEST_RELEASE_URL),)
    session = _FakeSession({str(release._LATEST_RELEASE_URL): response})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "body",
    [
        (
            b'{"tag_name":"v1.2.3","tag_name":"v9.9.9",'
            b'"draft":false,"prerelease":false,"assets":[]}'
        ),
        (
            b'{"tag_name":"v1.2.3","draft":false,"prerelease":false,'
            b'"assets":[],"future":{"value":1,"value":2}}'
        ),
        (
            b'{"tag_name":"v1.2.3","draft":false,"prerelease":false,'
            b'"assets":[],"future":NaN}'
        ),
    ],
)
async def test_rejects_duplicate_keys_and_nonstandard_numbers(body: bytes) -> None:
    """Duplicate keys are not resolved by last-wins JSON behavior."""
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _FakeResponse(
                200, body, release._LATEST_RELEASE_URL
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [201, 301, 403, 404, 500])
async def test_rejects_non_success_status(status: int) -> None:
    """Only one complete HTTP 200 metadata response is accepted."""
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _FakeResponse(
                status, b"", release._LATEST_RELEASE_URL
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize("error", [TimeoutError(), ClientConnectionError()])
async def test_maps_bounded_transport_failures(error: Exception) -> None:
    """Timeout and aiohttp transport failures fail closed as resolution errors."""
    session = _FakeSession({}, error=error)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
