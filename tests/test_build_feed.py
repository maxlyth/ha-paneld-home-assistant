"""Tests for the signed build feed and its APK downloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import ClientConnectionError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from multidict import CIMultiDict
from yarl import URL

from custom_components.panel_assistant import build_feed, release
from custom_components.panel_assistant.build_feed import (
    FEED_SCHEMA,
    BuildFeedError,
    FeedBuild,
    async_download_build,
    async_fetch_build_feed,
    build_label,
    normalize_feed_url,
    parse_build_feed,
    parse_build_request,
)

FEED_URL = URL("https://feed.example/x/maintainer.json")
SIGNATURE_URL = URL("https://feed.example/x/maintainer.json.sig")
SIGNER = release._RELEASE_SIGNER_CERTIFICATE_SHA256


def _sha(code: int) -> str:
    return hashlib.sha256(str(code).encode()).hexdigest()


def _build_entry(code: int, **replacements: Any) -> dict[str, Any]:
    sha = _sha(code)
    entry: dict[str, Any] = {
        "apkPath": f"apks/{sha}.apk",
        "apkSha256": sha,
        "apkSize": 12_345,
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "databaseCompatibility": "hapaneld-db:v1:ha-paneld.db:11:14",
        "launchComponent": "io.github.maxlyth.hapaneld/.MainActivity",
        "minSdk": 26,
        "packageId": "io.github.maxlyth.hapaneld",
        "published": "2026-09-11T10:00:00Z",
        "signerCertificateSha256": SIGNER,
        "supportedAbis": ["arm64-v8a", "armeabi-v7a"],
        "versionCode": code,
        "versionName": "0.9.7-rc4",
    }
    entry.update(replacements)
    return entry


def _feed(builds: list[Any] | None = None, **replacements: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "builds": (
            [_build_entry(770), _build_entry(772), _build_entry(771)]
            if builds is None
            else builds
        ),
        "channel": "maintainer",
        "schema": FEED_SCHEMA,
    }
    document.update(replacements)
    return document


def _canonical(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """Create a disposable release key for offline feed proofs."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    """Create a second disposable key that the integration does not trust."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sign(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> Callable[[bytes], bytes]:
    """Trust the disposable key and return a signer for exact bytes."""
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(release, "_RELEASE_PUBLIC_KEY_PEM", public_key)

    def _sign(body: bytes) -> bytes:
        return signing_key.sign(body, padding.PKCS1v15(), hashes.SHA256())

    return _sign


def test_valid_feed_parses_newest_first(sign: Callable[[bytes], bytes]) -> None:
    """A signed canonical feed yields every build, newest first, content-addressed."""
    body = _canonical(_feed())

    feed = parse_build_feed(body, sign(body), FEED_URL)

    assert feed.channel == "maintainer"
    assert [build.version_code for build in feed.builds] == [772, 771, 770]
    newest = feed.newest()
    assert newest is not None
    assert newest.version_code == 772
    assert newest.apk_url == FEED_URL.join(URL(f"apks/{_sha(772)}.apk"))
    assert str(newest.apk_url) == f"https://feed.example/x/apks/{_sha(772)}.apk"
    assert newest.apk_sha256 == _sha(772)
    assert newest.apk_size == 12_345
    assert newest.label == "0.9.7-rc4 build 772"
    assert feed.find(771) is not None
    assert feed.find(773) is None


def test_empty_feed_parses_with_no_newest(sign: Callable[[bytes], bytes]) -> None:
    """An empty feed is valid and offers nothing."""
    body = _canonical(_feed(builds=[], channel="beta"))

    feed = parse_build_feed(body, sign(body), FEED_URL)

    assert feed.channel == "beta"
    assert feed.newest() is None


def test_tampered_byte_breaks_the_signature(sign: Callable[[bytes], bytes]) -> None:
    """A still-canonical, still-valid feed changed after signing is refused."""
    body = _canonical(_feed())
    signature = sign(body)
    tampered = body.replace(b'"versionCode":772', b'"versionCode":773')
    assert tampered != body
    assert tampered == _canonical(json.loads(tampered))

    with pytest.raises(BuildFeedError):
        parse_build_feed(tampered, signature, FEED_URL)


def test_feed_signed_by_another_key_is_refused(
    sign: Callable[[bytes], bytes], other_key: rsa.RSAPrivateKey
) -> None:
    """Only the trusted release key authenticates the feed."""
    body = _canonical(_feed())
    signature = other_key.sign(body, padding.PKCS1v15(), hashes.SHA256())

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, signature, FEED_URL)


@pytest.mark.parametrize(
    "encode",
    [
        pytest.param(
            lambda doc: (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(),
            id="indented",
        ),
        pytest.param(
            lambda doc: json.dumps(doc, separators=(",", ":"), sort_keys=True).encode(),
            id="no-trailing-newline",
        ),
        pytest.param(
            lambda doc: (
                json.dumps(dict(reversed(doc.items())), separators=(",", ":")) + "\n"
            ).encode(),
            id="unsorted-keys",
        ),
    ],
)
def test_non_canonical_feed_is_refused_even_when_signed(
    sign: Callable[[bytes], bytes], encode: Callable[[Any], bytes]
) -> None:
    """The signed bytes must be the one canonical encoding of the document."""
    body = encode(_feed())
    assert body != _canonical(_feed())

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, sign(body), FEED_URL)


def _without(key: str) -> dict[str, Any]:
    entry = _build_entry(772)
    del entry[key]
    return entry


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            _feed([_build_entry(772, signerCertificateSha256="0" * 64)]),
            id="wrong-signer",
        ),
        pytest.param(
            _feed([_build_entry(772, packageId="io.github.other")]),
            id="wrong-package",
        ),
        pytest.param(
            _feed([_build_entry(772, launchComponent="io.github.maxlyth.hapaneld/.X")]),
            id="wrong-launch-component",
        ),
        pytest.param(
            _feed([_build_entry(772, supportedAbis=["arm64-v8a"])]),
            id="wrong-abis",
        ),
        pytest.param(
            _feed([_build_entry(772, supportedAbis=["armeabi-v7a", "arm64-v8a"])]),
            id="reordered-abis",
        ),
        pytest.param(
            _feed([_build_entry(772, apkPath=f"apks/{_sha(1)}.apk")]),
            id="apk-path-other-sha",
        ),
        pytest.param(
            _feed([_build_entry(772, apkPath=f"other/{_sha(772)}.apk")]),
            id="apk-path-other-directory",
        ),
        pytest.param(
            _feed([_build_entry(772, apkPath=f"https://evil.example/{_sha(772)}.apk")]),
            id="apk-path-absolute",
        ),
        pytest.param(
            _feed(
                [
                    _build_entry(772),
                    _build_entry(772, apkSha256=_sha(9), apkPath=f"apks/{_sha(9)}.apk"),
                ]
            ),
            id="duplicate-version-code",
        ),
        pytest.param(
            _feed([{**_build_entry(772), "notes": "x"}]),
            id="unknown-build-field",
        ),
        pytest.param(_feed([_without("commit")]), id="missing-build-field"),
        pytest.param(_feed(notes="x"), id="unknown-feed-field"),
        pytest.param(
            {k: v for k, v in _feed().items() if k != "channel"},
            id="missing-feed-field",
        ),
        pytest.param(_feed(channel="stable"), id="bad-channel"),
        pytest.param(_feed(schema="other.v1"), id="bad-schema"),
        pytest.param(_feed([_build_entry(772, commit="abc123")]), id="short-commit"),
        pytest.param(_feed([_build_entry(772, commit="A" * 40)]), id="upper-commit"),
        pytest.param(
            _feed([_build_entry(772, published="2026-09-11 10:00:00Z")]),
            id="bad-published",
        ),
        pytest.param(
            _feed([_build_entry(772, published="2026-09-11T10:00:00+00:00")]),
            id="offset-published",
        ),
        pytest.param(
            _feed([_build_entry(772, versionName="-0.9.7")]), id="bad-version-name"
        ),
        pytest.param(
            _feed([_build_entry(772, versionName="0.9.7 rc4")]),
            id="spaced-version-name",
        ),
        pytest.param(_feed([_build_entry(772, versionCode=0)]), id="zero-code"),
        pytest.param(_feed([_build_entry(772, versionCode="772")]), id="string-code"),
        pytest.param(_feed([_build_entry(772, apkSize=0)]), id="zero-size"),
        pytest.param(_feed([_build_entry(772, minSdk=True)]), id="bool-min-sdk"),
        pytest.param(
            _feed([_build_entry(772, databaseCompatibility="db:v1")]),
            id="bad-database-compatibility",
        ),
        pytest.param(
            _feed(
                [_build_entry(772, apkSha256="A" * 64, apkPath=f"apks/{'A' * 64}.apk")]
            ),
            id="uppercase-sha",
        ),
        pytest.param(_feed(builds={"772": _build_entry(772)}), id="builds-not-list"),
        pytest.param([_feed()], id="document-not-object"),
    ],
)
def test_signed_canonical_feed_with_a_bad_field_is_refused(
    sign: Callable[[bytes], bytes], document: Any
) -> None:
    """Each closed-shape rule refuses on its own; signature and encoding are valid."""
    body = _canonical(document)

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, sign(body), FEED_URL)


def test_duplicate_json_key_is_refused(sign: Callable[[bytes], bytes]) -> None:
    """An ambiguous object with a repeated key never parses."""
    body = _canonical(_feed()).replace(
        b'"channel":"maintainer"', b'"channel":"beta","channel":"maintainer"'
    )

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, sign(body), FEED_URL)


def test_build_count_cap_refuses_one_more_than_the_limit(
    sign: Callable[[bytes], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build-count cap admits exactly its limit and refuses one more."""
    monkeypatch.setattr(build_feed, "_MAX_FEED_BUILDS", 2)
    at_limit = _canonical(_feed([_build_entry(1), _build_entry(2)]))
    over_limit = _canonical(_feed([_build_entry(1), _build_entry(2), _build_entry(3)]))

    assert len(parse_build_feed(at_limit, sign(at_limit), FEED_URL).builds) == 2
    with pytest.raises(BuildFeedError):
        parse_build_feed(over_limit, sign(over_limit), FEED_URL)


def test_more_than_500_builds_is_refused(sign: Callable[[bytes], bytes]) -> None:
    """At the real limits 501 builds are refused; the byte cap binds first."""
    assert build_feed._MAX_FEED_BUILDS == 500
    builds = [_build_entry(code) for code in range(1, 502)]
    body = _canonical(_feed(builds))
    assert len(body) > build_feed._MAX_FEED_BYTES

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, sign(body), FEED_URL)


def test_feed_over_256_kib_is_refused_even_when_signed_and_valid(
    sign: Callable[[bytes], bytes],
) -> None:
    """A signed, canonical, in-count feed larger than 256 KiB is refused on size."""
    builds = [_build_entry(code) for code in range(1, 451)]
    body = _canonical(_feed(builds))
    assert build_feed._MAX_FEED_BYTES == 256 * 1024
    assert len(body) > build_feed._MAX_FEED_BYTES
    assert len(builds) <= build_feed._MAX_FEED_BUILDS

    with pytest.raises(BuildFeedError):
        parse_build_feed(body, sign(body), FEED_URL)


def test_normalize_feed_url_accepts_a_plain_https_json_url() -> None:
    """A plain HTTPS URL to a JSON document is the only accepted feed location."""
    assert normalize_feed_url("https://h/x/maintainer.json") == URL(
        "https://h/x/maintainer.json"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://h/x/maintainer.json",
        "https://h/x/maintainer.json?token=1",
        "https://user@h/x/maintainer.json",
        "https://user:pw@h/x/maintainer.json",
        "https://h/x/maintainer.json#frag",
        "https://h/x/maintainer.txt",
        "https://h/x/",
        " https://h/x/maintainer.json",
        "https://h/x/maintainer.json ",
        "https://h/x/maintainer.json\n",
        "https:///maintainer.json",
        "",
        None,
        7,
        "https://h/" + "a" * 2048 + ".json",
    ],
)
def test_normalize_feed_url_refuses_anything_else(value: object) -> None:
    """Plaintext, queries, credentials, fragments, other paths and padding fail."""
    with pytest.raises(BuildFeedError):
        normalize_feed_url(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("772", 772),
        ("0.9.7-rc4 build 772", 772),
        (" 772 ", 772),
        ("2147483647", 2147483647),
        ("", None),
        ("0", None),
        ("abc", None),
        ("99999999999", None),
        ("2147483648", None),
        ("0.9.7-rc4", None),
        ("0772", None),
    ],
)
def test_parse_build_request(value: str, expected: int | None) -> None:
    """A build is requested by its number, alone or inside its label."""
    assert parse_build_request(value) == expected


def test_build_label_round_trips_through_parse_build_request() -> None:
    """The entity's label is exactly what the request parser reads back."""
    assert build_label("0.9.7-rc4", 772) == "0.9.7-rc4 build 772"
    assert parse_build_request(build_label("0.9.7-rc4", 772)) == 772


# --- network doubles -------------------------------------------------------


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _limit: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


@dataclass
class _FakeResponse:
    status: int
    body: bytes | list[bytes]
    url: URL
    declared_length: int | None = None
    history: tuple[Any, ...] = ()
    headers: CIMultiDict[str] = field(default_factory=CIMultiDict)

    def __post_init__(self) -> None:
        chunks = [self.body] if isinstance(self.body, bytes) else self.body
        self.content = _FakeContent(chunks)

    @property
    def content_length(self) -> int | None:
        return self.declared_length

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSession:
    def __init__(
        self, responses: dict[str, _FakeResponse], error: Exception | None = None
    ) -> None:
        self._responses = responses
        self._error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: URL, **kwargs: Any) -> _FakeResponse:
        self.requests.append((str(url), kwargs))
        if self._error is not None:
            raise self._error
        return self._responses[str(url)]

    def post(self, url: URL, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("the build feed never posts")


async def test_fetch_reads_exactly_the_feed_and_its_signature(
    sign: Callable[[bytes], bytes],
) -> None:
    """Only the configured URL and its .sig sibling are requested, without redirects."""
    body = _canonical(_feed())
    session = _FakeSession(
        {
            str(FEED_URL): _FakeResponse(200, body, FEED_URL),
            str(SIGNATURE_URL): _FakeResponse(200, sign(body), SIGNATURE_URL),
        }
    )

    feed = await async_fetch_build_feed(session, FEED_URL)  # type: ignore[arg-type]

    assert [build.version_code for build in feed.builds] == [772, 771, 770]
    assert [url for url, _ in session.requests] == [str(FEED_URL), str(SIGNATURE_URL)]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.requests)


async def test_fetch_refuses_a_redirected_feed(sign: Callable[[bytes], bytes]) -> None:
    """A redirect is never followed for the feed, wherever it points."""
    session = _FakeSession(
        {str(FEED_URL): _FakeResponse(302, b"", FEED_URL)},
    )

    with pytest.raises(BuildFeedError):
        await async_fetch_build_feed(session, FEED_URL)  # type: ignore[arg-type]
    assert [url for url, _ in session.requests] == [str(FEED_URL)]


async def test_fetch_maps_network_failure() -> None:
    """A transport failure is a feed failure, not an unhandled client error."""
    session = _FakeSession({}, error=ClientConnectionError())

    with pytest.raises(BuildFeedError):
        await async_fetch_build_feed(session, FEED_URL)  # type: ignore[arg-type]


APK = b"panel-apk-bytes" * 100
APK_SHA = hashlib.sha256(APK).hexdigest()
APK_URL = FEED_URL.join(URL(f"apks/{APK_SHA}.apk"))


def _download_build(**replacements: Any) -> FeedBuild:
    values: dict[str, Any] = {
        "version_code": 772,
        "version_name": "0.9.7-rc4",
        "apk_url": APK_URL,
        "apk_sha256": APK_SHA,
        "apk_size": len(APK),
        "commit": "0" * 40,
        "database_compatibility": "hapaneld-db:v1:ha-paneld.db:11:14",
        "min_sdk": 26,
        "published": "2026-09-11T10:00:00Z",
    }
    values.update(replacements)
    return FeedBuild(**values)


async def test_download_returns_exact_signed_bytes() -> None:
    """Bytes that match the signed size and hash are returned intact."""
    session = _FakeSession(
        {str(APK_URL): _FakeResponse(200, [APK[:500], APK[500:]], APK_URL, len(APK))}
    )

    assert await async_download_build(session, _download_build()) == APK  # type: ignore[arg-type]
    assert [url for url, _ in session.requests] == [str(APK_URL)]
    assert session.requests[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("response", "build"),
    [
        pytest.param(
            _FakeResponse(200, APK[:-1] + b"X", APK_URL),
            _download_build(),
            id="wrong-hash-same-size",
        ),
        pytest.param(
            _FakeResponse(200, APK + b"X", APK_URL),
            _download_build(),
            id="too-many-bytes",
        ),
        pytest.param(
            _FakeResponse(200, APK[:-1], APK_URL),
            _download_build(),
            id="too-few-bytes",
        ),
        pytest.param(
            _FakeResponse(302, b"", APK_URL), _download_build(), id="redirect-status"
        ),
        pytest.param(
            _FakeResponse(200, APK, URL("https://other.example/apk")),
            _download_build(),
            id="different-final-url",
        ),
        pytest.param(
            _FakeResponse(200, APK, APK_URL, len(APK) + 1),
            _download_build(),
            id="content-length-mismatch",
        ),
    ],
)
async def test_download_refuses_anything_but_the_signed_bytes(
    response: _FakeResponse, build: FeedBuild
) -> None:
    """Hash, size, status, final URL and declared length must all match."""
    session = _FakeSession({str(build.apk_url): response})

    with pytest.raises(BuildFeedError):
        await async_download_build(session, build)  # type: ignore[arg-type]


async def test_download_maps_network_failure() -> None:
    """A transport failure during download is a verification failure."""
    session = _FakeSession({}, error=ClientConnectionError())

    with pytest.raises(BuildFeedError):
        await async_download_build(session, _download_build())  # type: ignore[arg-type]
