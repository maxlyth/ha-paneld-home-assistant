"""Tests for authenticated stable ha-paneld release resolution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import ClientConnectionError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from multidict import CIMultiDict
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
_DESCRIPTOR_NAME = f"ha-paneld-{_TAG}-install.json"
_DESCRIPTOR_URL = (
    f"https://github.com/maxlyth/ha-paneld/releases/download/{_TAG}/{_DESCRIPTOR_NAME}"
)
_DESCRIPTOR_SIGNATURE_URL = f"{_DESCRIPTOR_URL}.sig"
_SHA256 = "0123456789abcdef" * 4
_CHECKSUM = f"{_SHA256}  {_APK_NAME}\n".encode()
_RELEASE_SIGNER = "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"


def _descriptor_document(**replacements: Any) -> dict[str, Any]:
    document = {
        "schema": "io.github.maxlyth.hapaneld.install.v1",
        "releaseTag": _TAG,
        "versionName": _VERSION,
        "versionCode": 701,
        "apkName": _APK_NAME,
        "apkSize": 12_345,
        "apkSha256": _SHA256,
        "packageId": "io.github.maxlyth.hapaneld",
        "signerCertificateSha256": _RELEASE_SIGNER,
        "minSdk": 26,
        "supportedAbis": ["arm64-v8a", "armeabi-v7a"],
        "databaseCompatibility": "hapaneld-db:v1:ha-paneld.db:11:14",
        "launchComponent": "io.github.maxlyth.hapaneld/.MainActivity",
    }
    document.update(replacements)
    return document


def _canonical_descriptor(document: dict[str, Any] | None = None) -> bytes:
    if document is None:
        document = _descriptor_document()
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


class _FakeContent:
    def __init__(self, body: bytes | list[bytes | str]) -> None:
        self._chunks = [body] if isinstance(body, bytes) else body
        self.yielded_chunks = 0

    async def iter_chunked(self, _limit: int) -> AsyncIterator[bytes | str]:
        for chunk in self._chunks:
            self.yielded_chunks += 1
            yield chunk


@dataclass
class _FakeResponse:
    status: int
    body: bytes | list[bytes | str]
    url: URL
    history: tuple[Any, ...] = ()
    headers: CIMultiDict[str] = field(default_factory=CIMultiDict)
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
    include_descriptor: bool = False,
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
        if include_descriptor:
            assets.extend(
                [
                    {
                        "name": _DESCRIPTOR_NAME,
                        "browser_download_url": _DESCRIPTOR_URL,
                    },
                    {
                        "name": f"{_DESCRIPTOR_NAME}.sig",
                        "browser_download_url": _DESCRIPTOR_SIGNATURE_URL,
                    },
                ]
            )
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
        "future_release_field": {"nested": [1, 2, 3]},
    }


def _required_assets_for_tag(tag: str) -> list[dict[str, str]]:
    """Return a self-consistent required triplet for tag-validation tests."""
    apk_name = f"ha-paneld-{tag}-manual-setup-required.apk"
    root = f"https://github.com/maxlyth/ha-paneld/releases/download/{tag}"
    return [
        {
            "name": apk_name + suffix,
            "browser_download_url": f"{root}/{apk_name}{suffix}",
        }
        for suffix in ("", ".sha256", ".sha256.sig")
    ]


def _rc_session(signing_key: rsa.RSAPrivateKey) -> _FakeSession:
    """Build real signed RC metadata without weakening any production verifier."""
    tag = "v0.9.7-rc3"
    root = f"https://github.com/maxlyth/ha-paneld/releases/download/{tag}"
    api = f"https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/{tag}"
    apk = f"ha-paneld-{tag}-manual-setup-required.apk"
    descriptor_name = f"ha-paneld-{tag}-install.json"
    checksum = f"{_SHA256}  {apk}\n".encode("ascii")
    descriptor = _canonical_descriptor(
        _descriptor_document(releaseTag=tag, versionName=tag[1:], apkName=apk)
    )
    assets = _required_assets_for_tag(tag) + [
        {"name": name, "browser_download_url": f"{root}/{name}"}
        for name in (descriptor_name, f"{descriptor_name}.sig")
    ]
    payloads = {
        f"{root}/{apk}.sha256": checksum,
        f"{root}/{apk}.sha256.sig": _signature(signing_key, checksum),
        f"{root}/{descriptor_name}": descriptor,
        f"{root}/{descriptor_name}.sig": _signature(signing_key, descriptor),
        api: json.dumps(
            _release_document(tag=tag, prerelease=True, assets=assets)
        ).encode(),
    }
    # A wrong latest-endpoint implementation must reach the exact URL assertion,
    # not fail because this fake happens to lack a response for that endpoint.
    payloads[str(release._LATEST_RELEASE_URL)] = payloads[api]
    return _FakeSession(
        {url: _FakeResponse(200, body, URL(url)) for url, body in payloads.items()}
    )


async def test_rc_resolves_only_exact_requested_tag_and_signed_descriptor(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """Opt-in authenticates exact RC bytes, not latest or an APK download."""
    _install_test_key(monkeypatch, signing_key)
    session = _rc_session(signing_key)
    artifact = await release.async_resolve_rc_release(session, "v0.9.7-rc3")  # type: ignore[arg-type]
    assert artifact.tag == "v0.9.7-rc3"
    assert artifact.version == "0.9.7-rc3"
    assert artifact.descriptor is not None
    assert artifact.descriptor.release_tag == artifact.tag
    assert artifact.descriptor.version_name == artifact.version
    assert artifact.descriptor.apk_sha256 == artifact.sha256 == _SHA256
    urls = [url for url, _kwargs in session.requests]
    assert urls == [
        "https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/v0.9.7-rc3",
        artifact.apk_url + ".sha256",
        artifact.apk_url + ".sha256.sig",
        artifact.apk_url.rsplit("/", 1)[0] + "/ha-paneld-v0.9.7-rc3-install.json",
        artifact.apk_url.rsplit("/", 1)[0] + "/ha-paneld-v0.9.7-rc3-install.json.sig",
    ]
    assert artifact.apk_url not in urls
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in session.requests)


@pytest.mark.parametrize(
    "tag",
    [
        None,
        True,
        3,
        "",
        "v0.9.7",
        "0.9.7-rc3",
        "v0.9.7-rc0",
        "v0.9.7-rc03",
        "v00.9.7-rc3",
        "v0.9.7-rc",
        "v0.9.7-rc.3",
        "v0.9.7-RC3",
        "v0.9.7-beta3",
        "v0.9.7-rc3+build",
        "v0.9.7-rc3 ",
        " v0.9.7-rc3",
        "v0.9.7-rc3\n",
        "v0.9.7-rc\N{ARABIC-INDIC DIGIT THREE}",
        "v0.9.7-rc3/other",
        "v0.9.7-rc" + "3" * 60,
    ],
)
async def test_rc_invalid_selection_makes_no_request(tag: Any) -> None:
    session = _FakeSession({})
    with pytest.raises(ReleaseResolutionError):
        await release.async_resolve_rc_release(session, tag)  # type: ignore[arg-type]
    assert session.requests == []


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.0.0-rc1", True),
        ("v0.9.7-rc3", True),
        ("v0.9.7-rc" + "3" * 55, True),
        ("v0.9.7-rc" + "3" * 56, False),
        (None, False),
        (3, False),
        ("v0.9.7", False),
        ("v0.9.7-rc0", False),
        ("v0.9.7-rc03", False),
        ("v00.9.7-rc3", False),
        ("v0.9.7-rc3\n", False),
        ("v0.9.7-rc3+meta", False),
        ("v0.9.7-rc\N{ARABIC-INDIC DIGIT THREE}", False),
    ],
)
def test_rc_tag_grammar_has_positive_and_negative_controls(
    tag: object, expected: bool
) -> None:
    assert release.is_rc_release_tag(tag) is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag_name", "v0.9.7-rc4"),
        ("tag_name", "v0.9.7"),
        ("prerelease", False),
        ("prerelease", 1),
        ("prerelease", None),
        ("draft", True),
        ("draft", 0),
    ],
)
async def test_rc_refuses_tag_or_release_flag_substitution(
    signing_key: rsa.RSAPrivateKey, field: str, value: Any
) -> None:
    session = _rc_session(signing_key)
    api = "https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/v0.9.7-rc3"
    metadata = json.loads(session._responses[api].body)
    metadata[field] = value
    if field == "tag_name":
        metadata["assets"] = _required_assets_for_tag(value)
    body = json.dumps(metadata).encode()
    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(body, expected_rc_tag="v0.9.7-rc3")
    session._responses[api] = _FakeResponse(200, body, URL(api))
    with pytest.raises(ReleaseResolutionError):
        await release.async_resolve_rc_release(session, "v0.9.7-rc3")  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [api]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "redirect",
        "oversized",
        "asset_url",
        "checksum_sig",
        "descriptor_sig",
        "descriptor_tag",
        "descriptor_hash",
    ],
)
async def test_rc_failures_do_not_fall_back_or_bypass_proof(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, fault: str
) -> None:
    _install_test_key(monkeypatch, signing_key)
    session = _rc_session(signing_key)
    api = "https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/v0.9.7-rc3"
    root = "https://github.com/maxlyth/ha-paneld/releases/download/v0.9.7-rc3"
    checksum_sig = root + "/ha-paneld-v0.9.7-rc3-manual-setup-required.apk.sha256.sig"
    descriptor_url = root + "/ha-paneld-v0.9.7-rc3-install.json"
    if fault == "missing":
        session._responses[api].status = 404
    elif fault == "redirect":
        session._responses[api].status = 302
        session._responses[api].headers["Location"] = str(release._LATEST_RELEASE_URL)
    elif fault == "oversized":
        session._responses[api].declared_length = (
            release._MAX_RELEASE_RESPONSE_BYTES + 1
        )
    elif fault == "asset_url":
        metadata = json.loads(session._responses[api].body)
        metadata["assets"][0]["browser_download_url"] = _APK_URL
        session._responses[api] = _FakeResponse(
            200, json.dumps(metadata).encode(), URL(api)
        )
    elif fault in {"checksum_sig", "descriptor_sig"}:
        url = checksum_sig if fault == "checksum_sig" else descriptor_url + ".sig"
        session._responses[url] = _FakeResponse(200, b"x" * 256, URL(url))
    else:
        descriptor = json.loads(session._responses[descriptor_url].body)
        if fault == "descriptor_tag":
            descriptor["releaseTag"] = "v0.9.7-rc4"
        else:
            descriptor["apkSha256"] = "a" * 64
        body = _canonical_descriptor(descriptor)
        session._responses[descriptor_url] = _FakeResponse(
            200, body, URL(descriptor_url)
        )
        session._responses[descriptor_url + ".sig"] = _FakeResponse(
            200, _signature(signing_key, body), URL(descriptor_url + ".sig")
        )
    with pytest.raises(ReleaseResolutionError):
        await release.async_resolve_rc_release(session, "v0.9.7-rc3")  # type: ignore[arg-type]
    urls = [url for url, _kwargs in session.requests]
    assert urls[0] == api
    assert str(release._LATEST_RELEASE_URL) not in urls
    assert not any(url.endswith(".apk") for url in urls)


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
    descriptor: bytes | None = None,
    descriptor_signature: bytes | None = None,
) -> _FakeSession:
    if signature is None:
        signature = _signature(signing_key, checksum)
    responses = {
        str(release._LATEST_RELEASE_URL): _metadata_response(
            _release_document(include_descriptor=descriptor is not None)
        ),
        _CHECKSUM_URL: _FakeResponse(200, checksum, URL(_CHECKSUM_URL)),
        _SIGNATURE_URL: _FakeResponse(200, signature, URL(_SIGNATURE_URL)),
    }
    if descriptor is not None:
        if descriptor_signature is None:
            descriptor_signature = _signature(signing_key, descriptor)
        responses[_DESCRIPTOR_URL] = _FakeResponse(
            200, descriptor, URL(_DESCRIPTOR_URL)
        )
        responses[_DESCRIPTOR_SIGNATURE_URL] = _FakeResponse(
            200, descriptor_signature, URL(_DESCRIPTOR_SIGNATURE_URL)
        )
    return _FakeSession(responses)


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
    assert artifact.descriptor is None
    requested_urls = [url for url, _kwargs in session.requests]
    assert requested_urls == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
        _SIGNATURE_URL,
    ]
    assert _APK_URL not in requested_urls

    metadata_kwargs = session.requests[0][1]
    assert metadata_kwargs["allow_redirects"] is False
    assert metadata_kwargs["headers"]["Accept"] == "application/vnd.github+json"
    for _url, kwargs in session.requests:
        assert kwargs["allow_redirects"] is False
        assert "max_redirects" not in kwargs
        timeout = kwargs["timeout"]
        assert timeout.total == 10.0
        assert timeout.connect == 5.0
        assert timeout.sock_read == 5.0
    for _url, kwargs in session.requests[1:]:
        assert kwargs["headers"]["Accept"] == "application/octet-stream"


async def test_resolves_cross_bound_signed_install_descriptor_without_apk_download(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A future release adds authenticated install facts without reading the APK."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, descriptor=_canonical_descriptor())

    artifact = await async_resolve_stable_release(session)  # type: ignore[arg-type]

    assert artifact.descriptor == release.InstallDescriptor(
        schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag=_TAG,
        version_name=_VERSION,
        version_code=701,
        apk_name=_APK_NAME,
        apk_size=12_345,
        apk_sha256=_SHA256,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=_RELEASE_SIGNER,
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:11:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )
    requested_urls = [url for url, _kwargs in session.requests]
    assert requested_urls == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
        _SIGNATURE_URL,
        _DESCRIPTOR_URL,
        _DESCRIPTOR_SIGNATURE_URL,
    ]
    assert _APK_URL not in requested_urls


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
    "tag",
    ["1.2.3", "v01.2.3", "v1.2.3-rc1", "v\N{ARABIC-INDIC DIGIT ONE}.2.3"],
)
def test_tag_shape_is_rejected_with_self_consistent_assets(tag: str) -> None:
    """Tag syntax is enforced independently of asset-triplet consistency."""
    document = _release_document(assets=_required_assets_for_tag(tag), tag=tag)

    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(json.dumps(document).encode())


def test_tag_length_is_rejected_with_self_consistent_assets() -> None:
    """A syntactically numeric tag remains independently length bounded."""
    tag = "v" + "1" * 65 + ".2.3"
    document = _release_document(assets=_required_assets_for_tag(tag), tag=tag)

    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(json.dumps(document).encode())


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


@pytest.mark.parametrize("present_name", [_DESCRIPTOR_NAME, f"{_DESCRIPTOR_NAME}.sig"])
async def test_rejects_incomplete_install_descriptor_pair(present_name: str) -> None:
    """A descriptor and its signature are optional only as one complete pair."""
    assets = _release_document()["assets"]
    assets.append(
        {
            "name": present_name,
            "browser_download_url": (
                f"https://github.com/maxlyth/ha-paneld/releases/download/"
                f"{_TAG}/{present_name}"
            ),
        }
    )
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert len(session.requests) == 1


@pytest.mark.parametrize("asset_name", [_DESCRIPTOR_NAME, f"{_DESCRIPTOR_NAME}.sig"])
async def test_rejects_noncanonical_install_descriptor_asset_url(
    asset_name: str,
) -> None:
    """The optional proof pair remains bound to the selected tag and repository."""
    assets = _release_document(include_descriptor=True)["assets"]
    for asset in assets:
        if asset["name"] == asset_name:
            asset["browser_download_url"] = "https://example.invalid/proof"
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_duplicate_install_descriptor_asset() -> None:
    """Duplicate optional proof names are ambiguous just like the base triplet."""
    assets = _release_document(include_descriptor=True)["assets"]
    assets.append(dict(assets[-2]))
    session = _FakeSession(
        {
            str(release._LATEST_RELEASE_URL): _metadata_response(
                _release_document(assets=assets)
            )
        }
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_required_triplet_is_rejected_before_any_asset_fetch(
    missing_index: int,
) -> None:
    """Triplet completeness is enforced independently of later URL requests."""
    assets = _required_assets_for_tag(_TAG)
    del assets[missing_index]
    document = _release_document(assets=assets)

    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(json.dumps(document).encode())


def test_release_asset_count_bound_uses_otherwise_valid_assets() -> None:
    """The asset-count limit is independent of per-asset validation."""
    assets = _required_assets_for_tag(_TAG)
    assets.extend(
        {
            "name": f"irrelevant-{index}.txt",
            "browser_download_url": f"https://example.invalid/{index}",
        }
        for index in range(release._MAX_RELEASE_ASSETS - len(assets) + 1)
    )
    document = _release_document(assets=assets)

    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(json.dumps(document).encode())


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


async def test_rejects_invalid_install_descriptor_signature_before_parsing(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """Untrusted descriptor bytes never reach the semantic parser."""
    _install_test_key(monkeypatch, signing_key)
    monkeypatch.setattr(
        release,
        "_parse_install_descriptor",
        lambda *_args, **_kwargs: pytest.fail("untrusted descriptor was parsed"),
    )
    session = _successful_session(
        signing_key,
        descriptor=b"not-json",
        descriptor_signature=b"x" * 256,
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "descriptor",
    [
        b"not-json",
        b"\xff",
        b"[]\n",
        json.dumps(_descriptor_document(), sort_keys=True).encode() + b"\n",
        (json.dumps(_descriptor_document(), separators=(",", ":")) + "\n").encode(),
        _canonical_descriptor().removesuffix(b"\n"),
        _canonical_descriptor() + b"\n",
        _canonical_descriptor().replace(
            b'{"apkName":', b'{"apkName":"duplicate","apkName":', 1
        ),
        _canonical_descriptor().replace(b'"versionCode":701', b'"versionCode":NaN'),
    ],
    ids=[
        "invalid-json",
        "invalid-utf8",
        "not-object",
        "noncompact",
        "unsorted",
        "missing-newline",
        "extra-newline",
        "duplicate-key",
        "nonfinite-number",
    ],
)
async def test_rejects_signed_noncanonical_or_malformed_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    descriptor: bytes,
) -> None:
    """A valid signature does not relax strict UTF-8, JSON or canonical bytes."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, descriptor=descriptor)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "io.github.maxlyth.hapaneld.install.v2"),
        ("releaseTag", "v1.2.4"),
        ("versionName", "1.2.4"),
        ("versionCode", True),
        ("versionCode", 0),
        ("versionCode", 2**31),
        ("apkName", "other.apk"),
        ("apkSize", True),
        ("apkSize", 0),
        ("apkSize", 64 * 1024 * 1024 + 1),
        ("apkSha256", "A" * 64),
        ("apkSha256", "f" * 64),
        ("packageId", "example.foreign"),
        ("signerCertificateSha256", "f" * 64),
        ("minSdk", True),
        ("minSdk", 0),
        ("minSdk", 101),
        ("supportedAbis", ["armeabi-v7a", "arm64-v8a"]),
        ("supportedAbis", ["arm64-v8a"]),
        ("databaseCompatibility", "hapaneld-db:v1:ha-paneld.db:14:11"),
        ("databaseCompatibility", "hapaneld-db:v1:ha-paneld.db:01:14"),
        (
            "databaseCompatibility",
            f"hapaneld-db:v1:ha-paneld.db:1:{2**31}",
        ),
        ("launchComponent", "io.github.maxlyth.hapaneld/.DashboardActivity"),
    ],
)
async def test_rejects_signed_descriptor_outside_closed_contract(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    field: str,
    value: Any,
) -> None:
    """Every installation field is typed, bounded and bound to trusted metadata."""
    _install_test_key(monkeypatch, signing_key)
    descriptor = _canonical_descriptor(_descriptor_document(**{field: value}))
    session = _successful_session(signing_key, descriptor=descriptor)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


@pytest.mark.parametrize("change", ["missing", "unknown"])
async def test_rejects_signed_descriptor_with_nonexact_field_set(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    change: str,
) -> None:
    """Schema v1 neither defaults missing fields nor tolerates additive fields."""
    _install_test_key(monkeypatch, signing_key)
    document = _descriptor_document()
    if change == "missing":
        del document["minSdk"]
    else:
        document["futureField"] = True
    session = _successful_session(
        signing_key, descriptor=_canonical_descriptor(document)
    )

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


@pytest.mark.parametrize(
    ("target", "body"),
    [
        (
            "descriptor",
            b"x" * (release._MAX_INSTALL_DESCRIPTOR_BYTES + 1),
        ),
        (
            "descriptor-signature",
            b"x" * (release._MAX_SIGNATURE_RESPONSE_BYTES + 1),
        ),
    ],
)
async def test_rejects_oversized_install_descriptor_responses(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    target: str,
    body: bytes,
) -> None:
    """Both optional descriptor responses retain independent byte limits."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, descriptor=_canonical_descriptor())
    request_url = {
        "descriptor": _DESCRIPTOR_URL,
        "descriptor-signature": _DESCRIPTOR_SIGNATURE_URL,
    }[target]
    session._responses[request_url].body = body
    session._responses[request_url].content = _FakeContent(body)

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]


async def test_rejects_wrong_install_descriptor_signature_size(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """The optional descriptor signature has the pinned RSA-2048 width."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(
        signing_key,
        descriptor=_canonical_descriptor(),
        descriptor_signature=b"short",
    )

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
    assert response.content.yielded_chunks == 0


async def test_stream_limit_stops_before_a_second_excessive_chunk() -> None:
    """The body limit stops consumption before later parsing can reject it."""
    response = _metadata_response(_release_document())
    response.content = _FakeContent(
        [b"x" * (release._MAX_RELEASE_RESPONSE_BYTES + 1), b"not consumed"]
    )
    session = _FakeSession({str(release._LATEST_RELEASE_URL): response})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert response.content.yielded_chunks == 1


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
    "location",
    [
        "http://release-assets.githubusercontent.com/proof",
        "https://example.com/proof",
        "https://github.com:444/proof",
        "https://user@github.com/proof",
        "https://release-assets.githubusercontent.com/proof#fragment",
    ],
)
async def test_rejects_untrusted_release_asset_redirect(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    location: str,
) -> None:
    """An untrusted Location is refused before a request can reach it."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    checksum_response = session._responses[_CHECKSUM_URL]
    checksum_response.status = 302
    checksum_response.headers = CIMultiDict({"Location": location})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
    ]
    assert location not in {url for url, _kwargs in session.requests}


async def test_rejects_untrusted_initial_release_url_without_request() -> None:
    """The bounded asset reader validates even its first URL before a GET."""
    session = _FakeSession({})

    with pytest.raises(ReleaseResolutionError):
        await release._async_fetch_bounded(
            session,  # type: ignore[arg-type]
            URL("https://example.com/proof"),
            512,
            allow_release_redirects=True,
            headers={},
        )
    assert session.requests == []


async def test_accepts_bounded_github_release_asset_redirect(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """The observed GitHub-to-release-assets redirect remains usable."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    redirected_url = (
        "https://release-assets.githubusercontent.com/"
        "github-production-release-asset/proof?token=bounded"
    )
    checksum_response = session._responses[_CHECKSUM_URL]
    checksum_response.status = 302
    checksum_response.headers = CIMultiDict({"Location": redirected_url})
    session._responses[redirected_url] = _FakeResponse(
        200,
        _CHECKSUM,
        URL(redirected_url),
    )

    artifact = await async_resolve_stable_release(session)  # type: ignore[arg-type]

    assert artifact.sha256 == _SHA256
    assert [url for url, _kwargs in session.requests] == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
        redirected_url,
        _SIGNATURE_URL,
    ]
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in session.requests)


async def test_accepts_trusted_relative_release_redirect(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A relative Location is resolved against and retained on a trusted host."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    relative_location = f"/{_TAG}/{_APK_NAME}.sha256?download=1"
    redirected_url = f"https://github.com{relative_location}"
    checksum_response = session._responses[_CHECKSUM_URL]
    checksum_response.status = 307
    checksum_response.headers = CIMultiDict({"Location": relative_location})
    session._responses[redirected_url] = _FakeResponse(
        200,
        _CHECKSUM,
        URL(redirected_url),
    )

    artifact = await async_resolve_stable_release(session)  # type: ignore[arg-type]

    assert artifact.sha256 == _SHA256
    assert redirected_url in {url for url, _kwargs in session.requests}


async def test_rejects_more_than_three_redirects(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """The fourth Location is validated but never requested."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    redirect_urls = [
        f"https://github.com/maxlyth/ha-paneld/releases/redirect-{index}"
        for index in range(1, 5)
    ]
    current_url = _CHECKSUM_URL
    for redirect_url in redirect_urls:
        response = session._responses.get(current_url)
        if response is None:
            response = _FakeResponse(302, b"", URL(current_url))
            session._responses[current_url] = response
        response.status = 302
        response.headers = CIMultiDict({"Location": redirect_url})
        current_url = redirect_url

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    requested_urls = [url for url, _kwargs in session.requests]
    assert requested_urls == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
        *redirect_urls[:3],
    ]
    assert redirect_urls[3] not in requested_urls


@pytest.mark.parametrize("location", [None, "", " ", "http://[::1"])
async def test_rejects_missing_or_malformed_redirect_location(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    location: str | None,
) -> None:
    """A redirect requires one parseable nonempty Location before another GET."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    response = session._responses[_CHECKSUM_URL]
    response.status = 302
    response.headers = (
        CIMultiDict() if location is None else CIMultiDict({"Location": location})
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
    ]


async def test_rejects_multiple_redirect_locations(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """Ambiguous duplicate Location headers cannot select the next request."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    response = session._responses[_CHECKSUM_URL]
    response.status = 302
    response.headers = CIMultiDict(
        [
            ("Location", "https://release-assets.githubusercontent.com/first"),
            ("Location", "https://release-assets.githubusercontent.com/second"),
        ]
    )

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [
        str(release._LATEST_RELEASE_URL),
        _CHECKSUM_URL,
    ]


async def test_rejects_metadata_redirect() -> None:
    """The GitHub API lookup itself never follows or accepts a redirect."""
    response = _metadata_response(_release_document())
    response.status = 302
    response.headers = CIMultiDict({"Location": _CHECKSUM_URL})
    session = _FakeSession({str(release._LATEST_RELEASE_URL): response})

    with pytest.raises(ReleaseResolutionError):
        await async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [
        str(release._LATEST_RELEASE_URL)
    ]


async def test_rejects_unexpected_response_history() -> None:
    """A session must not claim it auto-followed when explicitly disabled."""
    response = _metadata_response(_release_document())
    response.history = (object(),)
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


def test_duplicate_key_is_rejected_in_otherwise_valid_document() -> None:
    """Duplicate-key rejection is independent of later release validation."""
    body = (
        json.dumps(_release_document())
        .encode()
        .replace(
            b'"tag_name": "v1.2.3"',
            b'"tag_name": "v1.2.3", "tag_name": "v1.2.3"',
            1,
        )
    )

    with pytest.raises(ReleaseResolutionError):
        release._parse_release_metadata(body)


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
