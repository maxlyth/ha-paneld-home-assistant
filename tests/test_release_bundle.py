"""Original signed release metadata retained for authenticated installation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from custom_components.panel_assistant import release

from .test_release import (
    _CHECKSUM_URL,
    _DESCRIPTOR_SIGNATURE_URL,
    _DESCRIPTOR_URL,
    _SIGNATURE_URL,
    _canonical_descriptor,
    _FakeSession,
    _install_test_key,
    _rc_session,
    _signature,
    _successful_session,
)
from .test_release import (
    signing_key as signing_key,
)

_RC_TAG = "v0.9.7-rc3"
_RC_API = f"https://api.github.com/repos/maxlyth/ha-paneld/releases/tags/{_RC_TAG}"
_RC_ROOT = f"https://github.com/maxlyth/ha-paneld/releases/download/{_RC_TAG}"
_RC_APK = f"{_RC_ROOT}/ha-paneld-{_RC_TAG}-manual-setup-required.apk"
_RC_DESCRIPTOR = f"{_RC_ROOT}/ha-paneld-{_RC_TAG}-install.json"
_STABLE_REQUESTS = [
    str(release._LATEST_RELEASE_URL),
    _CHECKSUM_URL,
    _SIGNATURE_URL,
    _DESCRIPTOR_URL,
    _DESCRIPTOR_SIGNATURE_URL,
]
_RC_REQUESTS = [
    _RC_API,
    _RC_APK + ".sha256",
    _RC_APK + ".sha256.sig",
    _RC_DESCRIPTOR,
    _RC_DESCRIPTOR + ".sig",
]


@pytest.mark.parametrize("rc_tag", [None, _RC_TAG])
async def test_bundle_preserves_original_signed_bytes_and_legacy_artifact(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    rc_tag: str | None,
) -> None:
    """Both channels fetch each proof once and preserve all four wire bodies."""
    _install_test_key(monkeypatch, signing_key)
    if rc_tag is None:
        session = _successful_session(signing_key, descriptor=_canonical_descriptor())
        expected_urls = _STABLE_REQUESTS
    else:
        session = _rc_session(signing_key)
        expected_urls = _RC_REQUESTS

    expected_bodies = [session._responses[url].body for url in expected_urls[1:]]
    bundle = await release.async_resolve_install_bundle(session, rc_tag=rc_tag)  # type: ignore[arg-type]

    assert isinstance(bundle, release.InstallReleaseBundle)
    assert isinstance(bundle.artifact, release.ReleaseArtifact)
    assert isinstance(bundle.metadata, release.SignedReleaseMetadata)
    assert bundle.artifact.descriptor is not None
    assert [
        bundle.metadata.checksum,
        bundle.metadata.checksum_signature,
        bundle.metadata.descriptor_bytes,
        bundle.metadata.descriptor_signature,
    ] == expected_bodies
    assert all(isinstance(body, bytes) for body in expected_bodies)
    assert [url for url, _kwargs in session.requests] == expected_urls
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in session.requests)

    session.requests.clear()
    if rc_tag is None:
        legacy = await release.async_resolve_stable_release(session)  # type: ignore[arg-type]
    else:
        legacy = await release.async_resolve_rc_release(session, rc_tag)  # type: ignore[arg-type]
    assert legacy == bundle.artifact
    assert [url for url, _kwargs in session.requests] == expected_urls


async def test_bundle_and_each_metadata_field_are_immutable(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """Callers cannot replace authenticated bytes or the bound artifact."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key, descriptor=_canonical_descriptor())
    bundle = await release.async_resolve_install_bundle(session)  # type: ignore[arg-type]
    for instance, fields in (
        (bundle, ("artifact", "metadata")),
        (
            bundle.metadata,
            (
                "checksum",
                "checksum_signature",
                "descriptor_bytes",
                "descriptor_signature",
            ),
        ),
    ):
        assert not hasattr(instance, "__dict__")
        for field in fields:
            with pytest.raises(FrozenInstanceError):
                setattr(instance, field, b"replacement")
    assert "checksum=" not in repr(bundle.metadata)
    assert "artifact=" not in repr(bundle)
    assert [url for url, _kwargs in session.requests] == _STABLE_REQUESTS


async def test_bundle_requires_descriptor_while_legacy_stable_still_resolves(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    """A checksum-only historical release is valid only for the legacy caller."""
    _install_test_key(monkeypatch, signing_key)
    session = _successful_session(signing_key)
    artifact = await release.async_resolve_stable_release(session)  # type: ignore[arg-type]
    assert artifact.descriptor is None
    assert [url for url, _kwargs in session.requests] == _STABLE_REQUESTS[:3]
    session.requests.clear()
    with pytest.raises(release.ReleaseResolutionError):
        await release.async_resolve_install_bundle(session)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == _STABLE_REQUESTS[:3]


@pytest.mark.parametrize("rc_tag", ["", "v1.2.3", "v0.9.7-rc0", "../latest", 7])
async def test_bundle_invalid_rc_selection_refuses_before_http(rc_tag: object) -> None:
    session = _FakeSession({})
    with pytest.raises(release.ReleaseResolutionError):
        await release.async_resolve_install_bundle(session, rc_tag=rc_tag)  # type: ignore[arg-type]
    assert session.requests == []


@pytest.mark.parametrize("status", [302, 404, 500])
async def test_bundle_rc_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, status: int
) -> None:
    _install_test_key(monkeypatch, signing_key)
    session = _rc_session(signing_key)
    session._responses[_RC_API].status = status
    session._responses[_RC_API].headers["Location"] = str(release._LATEST_RELEASE_URL)
    with pytest.raises(release.ReleaseResolutionError):
        await release.async_resolve_install_bundle(session, rc_tag=_RC_TAG)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == [_RC_API]


@pytest.mark.parametrize("rc_tag", [None, _RC_TAG])
@pytest.mark.parametrize("signature_index", [2, 4])
async def test_bundle_refuses_invalid_signature_before_returning_metadata(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
    rc_tag: str | None,
    signature_index: int,
) -> None:
    _install_test_key(monkeypatch, signing_key)
    session = (
        _successful_session(signing_key, descriptor=_canonical_descriptor())
        if rc_tag is None
        else _rc_session(signing_key)
    )
    expected_urls = _STABLE_REQUESTS if rc_tag is None else _RC_REQUESTS
    response = session._responses[expected_urls[signature_index]]
    response.body = _signature(signing_key, b"different signed bytes")
    response.__post_init__()
    with pytest.raises(release.ReleaseResolutionError):
        await release.async_resolve_install_bundle(session, rc_tag=rc_tag)  # type: ignore[arg-type]
    assert [url for url, _kwargs in session.requests] == expected_urls[
        : signature_index + 1
    ]
