"""Feed builds travel both install paths through one list and one resolver."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from yarl import URL

from custom_components.panel_assistant import (
    install_artifacts,
    install_executor,
    install_jobs,
    release_catalog,
)
from custom_components.panel_assistant.build_feed import (
    BuildFeed,
    FeedBuild,
    FeedInstallBundle,
    feed_release_artifact,
)
from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.feed_coordinator import (
    DATA_BUILD_FEED,
    BuildFeedCoordinator,
)
from custom_components.panel_assistant.install_artifacts import ArtifactCustodyError
from custom_components.panel_assistant.install_plan import (
    InstallPlanError,
    _build_artifact,
)
from custom_components.panel_assistant.release import (
    ReleaseResolutionError,
    artifact_identity_matches,
)

FEED_URL = URL("https://builds.example/maintainer.json")
SHA = hashlib.sha256(b"772").hexdigest()


def _build(code: int = 772, sha: str = SHA) -> FeedBuild:
    return FeedBuild(
        version_code=code,
        version_name="0.9.7-rc4",
        apk_url=FEED_URL.join(URL(f"apks/{sha}.apk")),
        apk_sha256=sha,
        apk_size=14,
        commit="0" * 40,
        database_compatibility="hapaneld-db:v1:ha-paneld.db:11:14",
        min_sdk=26,
        published="2026-09-11T10:00:00Z",
    )


def _install_feed(hass: HomeAssistant, *builds: FeedBuild) -> BuildFeedCoordinator:
    coordinator = BuildFeedCoordinator(hass, FEED_URL)
    feed = BuildFeed("maintainer", tuple(builds), raw=b"feed\n", signature=b"s" * 256)
    coordinator.async_refresh = AsyncMock()  # type: ignore[method-assign]
    coordinator.data = feed
    coordinator.last_update_success = True
    hass.data.setdefault(DOMAIN, {})[DATA_BUILD_FEED] = coordinator
    return coordinator


# --- one identity rule ---------------------------------------------------------


def test_identity_rule_binds_each_kind_of_artifact() -> None:
    """A GitHub tag binds name and file; a feed tag binds code and content hash."""
    assert artifact_identity_matches(
        "v0.9.7-rc3",
        "0.9.7-rc3",
        707,
        "ha-paneld-v0.9.7-rc3-manual-setup-required.apk",
        SHA,
    )
    assert artifact_identity_matches("build-772", "0.9.7-rc4", 772, f"{SHA}.apk", SHA)
    for tag, name, code, apk in (
        ("build-772", "0.9.7-rc4", 771, f"{SHA}.apk"),  # code is not the tag's
        ("build-772", "0.9.7-rc4", 772, "other.apk"),  # not content-addressed
        ("build-772", "bad name!", 772, f"{SHA}.apk"),
        ("build-0772", "0.9.7-rc4", 772, f"{SHA}.apk"),
        (
            "v0.9.7-rc3",
            "0.9.7-rc4",
            707,
            "ha-paneld-v0.9.7-rc3-manual-setup-required.apk",
        ),
        ("v0.9.7-rc3", "0.9.7-rc3", 707, f"{SHA}.apk"),
    ):
        assert not artifact_identity_matches(tag, name, code, apk, SHA), (tag, apk)


# --- one list, one resolver ------------------------------------------------------


async def test_both_paths_list_github_then_feed_builds(hass: HomeAssistant) -> None:
    _install_feed(hass, _build(772), _build(771, hashlib.sha256(b"771").hexdigest()))
    github = [{"tag": "v0.9.7-rc3", "prerelease": True}]
    with patch.object(
        release_catalog, "async_list_install_releases", AsyncMock(return_value=github)
    ):
        choices = await release_catalog.async_list_install_choices(hass)
    assert choices == [
        {"tag": "v0.9.7-rc3", "prerelease": True},
        {"tag": "build-772", "prerelease": True, "name": "0.9.7-rc4 build 772"},
        {"tag": "build-771", "prerelease": True, "name": "0.9.7-rc4 build 771"},
    ]


async def test_github_outage_still_offers_feed_builds_but_not_nothing(
    hass: HomeAssistant,
) -> None:
    outage = AsyncMock(side_effect=ReleaseResolutionError)
    with patch.object(release_catalog, "async_list_install_releases", outage):
        with pytest.raises(ReleaseResolutionError):
            await release_catalog.async_list_install_choices(hass)
        _install_feed(hass, _build())
        choices = await release_catalog.async_list_install_choices(hass)
    assert [choice["tag"] for choice in choices] == ["build-772"]


async def test_without_a_feed_the_list_is_github_only(hass: HomeAssistant) -> None:
    github = [{"tag": "v0.9.6", "prerelease": False}]
    with patch.object(
        release_catalog, "async_list_install_releases", AsyncMock(return_value=github)
    ):
        assert await release_catalog.async_list_install_choices(hass) == github


async def test_one_resolver_names_the_exact_feed_build(hass: HomeAssistant) -> None:
    coordinator = _install_feed(hass, _build())
    artifact = await release_catalog.async_resolve_install_choice(hass, "build-772")
    assert artifact.tag == "build-772"
    assert artifact.apk_name == f"{SHA}.apk"
    assert artifact.apk_url == str(FEED_URL.join(URL(f"apks/{SHA}.apk")))
    assert artifact.descriptor is not None
    assert artifact.descriptor.version_code == 772
    coordinator.async_refresh.assert_awaited()  # a fresh read, not a stale list
    with pytest.raises(ReleaseResolutionError):
        await release_catalog.async_resolve_install_choice(hass, "build-999")
    with pytest.raises(ReleaseResolutionError):
        await release_catalog.async_resolve_install_choice(hass, "latest")


async def test_browser_bundle_carries_the_exact_signed_feed(
    hass: HomeAssistant,
) -> None:
    _install_feed(hass, _build())
    bundle = await release_catalog.async_resolve_install_bundle_choice(
        hass, "build-772"
    )
    assert isinstance(bundle, FeedInstallBundle)
    assert bundle.feed == b"feed\n"
    assert bundle.feed_signature == b"s" * 256
    assert bundle.artifact.tag == "build-772"


# --- the network path accepts it end to end ----------------------------------------


def test_install_plan_accepts_a_feed_build_only_when_it_was_chosen() -> None:
    release = feed_release_artifact(_build())
    artifact = _build_artifact(release, "build-772")
    assert artifact.release_tag == "build-772"
    assert artifact.version_code == 772
    for expected in (None, "build-771", "v0.9.7-rc3"):
        with pytest.raises(InstallPlanError):
            _build_artifact(release, expected)


def test_a_feed_build_survives_the_durable_receipt() -> None:
    artifact = _build_artifact(feed_release_artifact(_build()), "build-772")
    parsed = install_jobs._parse_artifact(asdict(artifact))
    assert parsed == artifact
    tampered = asdict(artifact) | {"version_code": 771}
    with pytest.raises(install_jobs.InstallJobStoreError):
        install_jobs._parse_artifact(tampered)


def test_custody_fetches_only_content_addressed_apks_from_the_feed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_artifacts, "_FEED_DOWNLOAD_HOSTS", set())
    good = f"https://builds.example/apks/{SHA}.apk"
    with pytest.raises(ArtifactCustodyError):
        install_artifacts._trusted_download_url(good)  # host not registered yet
    install_artifacts.register_feed_download_host("builds.example")
    assert str(install_artifacts._trusted_download_url(good)) == good
    for refused in (
        "https://builds.example/maintainer.json",
        f"https://builds.example/apks/{SHA}.apk.exe",
        "https://builds.example/apks/notahash.apk",
        f"http://builds.example/apks/{SHA}.apk",
        f"https://other.example/apks/{SHA}.apk",
    ):
        with pytest.raises(ArtifactCustodyError):
            install_artifacts._trusted_download_url(refused)


def test_executor_rebuilds_a_feed_url_and_never_guesses_one() -> None:
    artifact = _build_artifact(feed_release_artifact(_build()), "build-772")
    receipt = SimpleNamespace(artifact=artifact)
    assert install_executor._apk_url(receipt, FEED_URL) == (  # type: ignore[arg-type]
        f"https://builds.example/apks/{SHA}.apk"
    )
    assert install_executor._apk_url(receipt, None) == ""  # type: ignore[arg-type]
