"""Update-entity, setup and backup tests for the signed build feed."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import stat
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.panel_assistant import CONFIG_SCHEMA, async_setup
from custom_components.panel_assistant import update as panel_update
from custom_components.panel_assistant.build_feed import (
    BuildFeed,
    BuildFeedError,
    FeedBuild,
)
from custom_components.panel_assistant.client import (
    CannotConnectError,
    PanelHealth,
    StagedApk,
    UpdateApprovalRequiredError,
    UploadDisabledError,
)
from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.coordinator import (
    HaPaneldDataUpdateCoordinator,
    PanelSnapshot,
)
from custom_components.panel_assistant.feed_coordinator import (
    BuildFeedCoordinator,
    async_get_feed_coordinator,
)
from custom_components.panel_assistant.panel_backup import async_store_panel_backup
from custom_components.panel_assistant.release import (
    _PACKAGE_ID,
    _RELEASE_SIGNER_CERTIFICATE_SHA256,
)
from custom_components.panel_assistant.status import PanelCachedUpdate, PanelStatus
from custom_components.panel_assistant.update import HaPaneldUpdateEntity
from custom_components.panel_assistant.update_coordinator import (
    PanelUpdateCoordinator,
    PanelUpdateSnapshot,
)

FEED_URL = URL("https://feed.example/x/maintainer.json")
NAME = "0.9.7-rc4"
APK = b"apk-bytes"
OFFER = PanelCachedUpdate("0.9.9", "0.9.10", "v0.9.10")
FETCH = "custom_components.panel_assistant.feed_coordinator.async_fetch_build_feed"


def _build(code: int) -> FeedBuild:
    sha = hashlib.sha256(str(code).encode()).hexdigest()
    return FeedBuild(
        version_code=code,
        version_name=NAME,
        apk_url=FEED_URL.join(URL(f"apks/{sha}.apk")),
        apk_sha256=sha,
        apk_size=len(APK),
        commit="0" * 40,
        database_compatibility="hapaneld-db:v1:ha-paneld.db:11:14",
        min_sdk=26,
        published="2026-09-11T10:00:00Z",
    )


def _feed_data(*codes: int) -> BuildFeed:
    return BuildFeed(
        channel="maintainer",
        builds=tuple(_build(code) for code in sorted(codes, reverse=True)),
    )


def _preview(**replacements: str) -> StagedApk:
    values = {
        "token": "tok-1",
        "package": _PACKAGE_ID,
        "version": NAME,
        "signer": _RELEASE_SIGNER_CERTIFICATE_SHA256,
    }
    values.update(replacements)
    return StagedApk(**values)


def _health(version: str, build: str = "1000") -> PanelHealth:
    return PanelHealth(
        version=version, panel_id="alpha", build=build, config_hash="1a2b3c4d"
    )


def _entity(
    hass: HomeAssistant,
    *,
    version: str = NAME,
    offer: PanelCachedUpdate | None = None,
    feed: BuildFeed | None = None,
    with_feed: bool = True,
    installed_code: int | None = 771,
) -> tuple[HaPaneldUpdateEntity, SimpleNamespace]:
    client = SimpleNamespace(
        configuration_url="http://panel.local:8888",
        async_start_panel_update=AsyncMock(),
        async_get_panel_install_status=AsyncMock(),
        async_get_version_code=AsyncMock(return_value=(version, installed_code)),
        async_backup_panel=AsyncMock(return_value=b"backup-zip"),
        async_stage_apk=AsyncMock(return_value=_preview()),
        async_commit_apk=AsyncMock(),
        async_discard_apk=AsyncMock(),
    )
    health = HaPaneldDataUpdateCoordinator(hass, client)  # type: ignore[arg-type]
    health.data = PanelSnapshot(
        health=_health(version),
        status=PanelStatus(
            warning_count=0, capability_count=0, panel_assistant_update=offer
        ),
        status_error=None,
    )
    # Health never changes unless a test says the app restarted.
    health.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    updates = PanelUpdateCoordinator(hass, client)  # type: ignore[arg-type]
    updates.data = PanelUpdateSnapshot(operation=None, error=None)
    coordinator: BuildFeedCoordinator | None = None
    if with_feed:
        coordinator = BuildFeedCoordinator(hass, FEED_URL)
        coordinator.data = feed if feed is not None else _feed_data(770, 771, 772)
        coordinator.last_update_success = True
    entity = HaPaneldUpdateEntity("entry-id", health, updates, coordinator)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    if installed_code is not None:
        entity._installed_code = installed_code
        entity._code_key = (version, "1000")
    return entity, client


def _assert_translated(error: HomeAssistantError, key: str) -> None:
    assert error.translation_domain == DOMAIN
    assert error.translation_key == key


@pytest.fixture
def delivery(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Keep backups in a temporary config dir and stub the APK download."""
    hass.config.config_dir = str(tmp_path)
    session = object()
    download = AsyncMock(return_value=APK)
    monkeypatch.setattr(panel_update, "async_get_clientsession", lambda _hass: session)
    monkeypatch.setattr(panel_update, "async_download_build", download)
    monkeypatch.setattr(panel_update.asyncio, "sleep", AsyncMock())
    # With sleep stubbed, an unexpected wait would spin to its real deadline;
    # keep that to about a second so a regression fails fast instead of hanging.
    monkeypatch.setattr(panel_update, "_ANDROID_PACKAGE_INSTALL_MAX_SECONDS", 0)
    monkeypatch.setattr(panel_update, "_RESTART_HEALTH_GRACE_SECONDS", 1)
    return SimpleNamespace(
        session=session,
        download=download,
        backups=tmp_path / DOMAIN / "backups",
    )


def _restart_into(
    entity: HaPaneldUpdateEntity,
    client: SimpleNamespace,
    code: int,
    calls: list[Any] | None = None,
) -> None:
    """Make the next health refresh show a restarted app reporting `code`."""

    async def refresh() -> None:
        if calls is not None:
            calls.append("refresh")
        entity.coordinator.data = PanelSnapshot(
            health=_health(NAME, build="2000"), status=None, status_error=None
        )

    async def diag() -> tuple[str, int]:
        if calls is not None:
            calls.append("diag")
        return NAME, code

    entity.coordinator.async_request_refresh = AsyncMock(side_effect=refresh)
    client.async_get_version_code = AsyncMock(side_effect=diag)


# --- presentation ------------------------------------------------------------


def test_without_a_feed_behaviour_is_unchanged(hass: HomeAssistant) -> None:
    """No feed: stable versions, no build numbers, no specific-version selector."""
    feed_entity, _ = _entity(hass)
    stable, client = _entity(
        hass, version="0.9.9", offer=OFFER, with_feed=False, installed_code=None
    )

    assert feed_entity.supported_features & UpdateEntityFeature.SPECIFIC_VERSION
    assert not stable.supported_features & UpdateEntityFeature.SPECIFIC_VERSION
    assert stable._feed_mode() is None
    assert stable.installed_version == "0.9.9"
    assert stable.latest_version == "0.9.10"
    assert stable.version_is_newer("0.9.10", "0.9.9") is True
    stable._refresh_installed_code()
    assert stable._code_task is None
    client.async_get_version_code.assert_not_awaited()


async def test_feed_with_unknown_installed_code_falls_back_to_stable(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """Until the panel's build number is read, the stable offer stays in charge."""
    entity, client = _entity(hass, version="0.9.9", offer=OFFER, installed_code=None)

    assert entity._feed_mode() is None
    assert entity.installed_version == "0.9.9"
    assert entity.latest_version == "0.9.10"
    assert entity.version_is_newer("0.9.10", "0.9.9") is True

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install("0.9.7-rc4 build 772", backup=False)

    _assert_translated(error.value, "update_unavailable")
    client.async_backup_panel.assert_not_awaited()
    delivery.download.assert_not_awaited()


def test_failed_feed_read_falls_back_to_stable(hass: HomeAssistant) -> None:
    """A feed that could not be authenticated offers nothing."""
    entity, _ = _entity(hass, version="0.9.9", offer=OFFER)
    assert entity._feed is not None
    entity._feed.last_update_success = False

    assert entity._feed_mode() is None
    assert entity.installed_version == "0.9.9"
    assert entity.latest_version == "0.9.10"


def test_feed_mode_names_builds_and_offers_the_newest(hass: HomeAssistant) -> None:
    """Installed 771 with a 770-772 feed offers 772 by build number."""
    entity, _ = _entity(hass)

    assert entity.supported_features & UpdateEntityFeature.SPECIFIC_VERSION
    assert entity.installed_version == "0.9.7-rc4 build 771"
    assert entity.latest_version == "0.9.7-rc4 build 772"
    assert entity.version_is_newer(entity.latest_version, entity.installed_version)
    assert not entity.version_is_newer("0.9.7-rc4 build 770", entity.installed_version)


def test_feed_mode_offers_nothing_when_installed_is_newest(
    hass: HomeAssistant,
) -> None:
    """No build newer than the running one means no update."""
    entity, _ = _entity(hass, installed_code=772)

    assert entity.latest_version == entity.installed_version == "0.9.7-rc4 build 772"


# --- install -----------------------------------------------------------------


async def test_install_delivers_the_newest_build_in_order(
    hass: HomeAssistant, delivery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup, store, download, stage, commit, then wait for exactly 772."""
    entity, client = _entity(hass)
    calls: list[Any] = []
    stored: list[Path] = []

    async def backup() -> bytes:
        calls.append("backup")
        return b"backup-zip"

    async def store(*args: Any) -> Path:
        calls.append("store")
        path = await async_store_panel_backup(*args)
        stored.append(path)
        return path

    async def download(session: object, build: FeedBuild) -> bytes:
        assert session is delivery.session
        assert entity.in_progress is True
        calls.append(("download", build.version_code))
        return APK

    async def stage(apk: bytes) -> StagedApk:
        calls.append(("stage", apk))
        return _preview()

    async def commit(token: str) -> None:
        calls.append(("commit", token))

    client.async_backup_panel = AsyncMock(side_effect=backup)
    client.async_stage_apk = AsyncMock(side_effect=stage)
    client.async_commit_apk = AsyncMock(side_effect=commit)
    delivery.download.side_effect = download
    monkeypatch.setattr(panel_update, "async_store_panel_backup", store)
    _restart_into(entity, client, 772, calls)

    await entity.async_install(None, False)

    assert calls == [
        "backup",
        "store",
        ("download", 772),
        ("stage", APK),
        ("commit", "tok-1"),
        "refresh",
        "diag",
    ]
    assert len(stored) == 1
    assert stored[0].parent == Path(hass.config.path(DOMAIN, "backups"))
    assert stored[0].name.startswith("entry-id-")
    assert stored[0].name.endswith("-vc771.zip")
    assert stored[0].read_bytes() == b"backup-zip"
    assert entity._installed_code == 772
    assert entity.installed_version == "0.9.7-rc4 build 772"
    assert entity.in_progress is False
    client.async_discard_apk.assert_not_awaited()


async def test_install_a_specific_older_build(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A named build installs even when it is older than the running one."""
    entity, client = _entity(hass)
    _restart_into(entity, client, 770)

    await entity.async_install("770", False)

    assert delivery.download.await_args.args[1].version_code == 770
    client.async_commit_apk.assert_awaited_once_with("tok-1")
    assert entity._installed_code == 770
    assert entity.in_progress is False


async def test_install_refreshes_the_feed_to_find_a_newly_published_build(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A build missing from the cached feed is looked up once more after a refresh."""
    entity, client = _entity(hass)
    feed = entity._feed
    assert feed is not None

    async def publish() -> None:
        feed.data = _feed_data(770, 771, 772, 773)

    feed.async_refresh = AsyncMock(side_effect=publish)  # type: ignore[method-assign]
    _restart_into(entity, client, 773)

    await entity.async_install("0.9.7-rc4 build 773", False)

    feed.async_refresh.assert_awaited_once()
    assert delivery.download.await_args.args[1].version_code == 773
    assert entity._installed_code == 773


async def test_install_refuses_a_build_still_missing_after_one_refresh(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """An unknown build refreshes the feed exactly once, then is unavailable."""
    entity, client = _entity(hass)
    feed = entity._feed
    assert feed is not None
    feed.async_refresh = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install("999", False)

    _assert_translated(error.value, "update_unavailable")
    feed.async_refresh.assert_awaited_once()
    client.async_backup_panel.assert_not_awaited()
    client.async_get_version_code.assert_not_awaited()
    delivery.download.assert_not_awaited()
    assert entity.in_progress is False


async def test_install_refuses_a_missing_build_when_the_refresh_fails(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A failed refresh never turns stale or absent data into an install."""
    entity, client = _entity(hass)
    feed = entity._feed
    assert feed is not None

    async def fail() -> None:
        feed.data = _feed_data(770, 771, 772, 773)
        feed.last_update_success = False

    feed.async_refresh = AsyncMock(side_effect=fail)  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install("773", False)

    _assert_translated(error.value, "update_unavailable")
    client.async_backup_panel.assert_not_awaited()


@pytest.mark.parametrize(
    ("version", "backup"),
    [
        pytest.param("771", False, id="already-installed"),
        pytest.param("0.9.7-rc4 build 771", False, id="already-installed-label"),
        pytest.param(None, True, id="backup-requested"),
        pytest.param("772", True, id="backup-requested-specific"),
    ],
)
async def test_install_refuses_without_contacting_anything(
    hass: HomeAssistant,
    delivery: SimpleNamespace,
    version: str | None,
    backup: bool,
) -> None:
    """The installed build and HA-side backups are never offered."""
    entity, client = _entity(hass)
    feed = entity._feed
    assert feed is not None
    feed.async_refresh = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(version, backup)

    _assert_translated(error.value, "update_unavailable")
    feed.async_refresh.assert_not_awaited()
    client.async_backup_panel.assert_not_awaited()
    client.async_stage_apk.assert_not_awaited()
    client.async_commit_apk.assert_not_awaited()
    client.async_get_version_code.assert_not_awaited()
    delivery.download.assert_not_awaited()
    assert not delivery.backups.exists()


@pytest.mark.parametrize(
    "preview",
    [
        pytest.param(_preview(signer="0" * 64), id="wrong-signer"),
        pytest.param(_preview(package="io.github.other"), id="wrong-package"),
        pytest.param(_preview(version="0.9.7-rc3"), id="wrong-version"),
    ],
)
async def test_preview_mismatch_discards_and_never_commits(
    hass: HomeAssistant, delivery: SimpleNamespace, preview: StagedApk
) -> None:
    """The panel's own reading of the upload must match the signed build."""
    entity, client = _entity(hass)
    client.async_stage_apk = AsyncMock(return_value=preview)

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "build_verification_failed")
    client.async_discard_apk.assert_awaited_once_with("tok-1")
    client.async_commit_apk.assert_not_awaited()
    assert entity.in_progress is False


async def test_download_failure_never_stages(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """Bytes that do not match the signed feed never reach the panel."""
    entity, client = _entity(hass)
    delivery.download.side_effect = BuildFeedError

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "build_verification_failed")
    client.async_backup_panel.assert_awaited_once()
    client.async_stage_apk.assert_not_awaited()
    client.async_commit_apk.assert_not_awaited()
    assert entity.in_progress is False


async def test_backup_failure_never_downloads(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """No backup, no change: the download is never started."""
    entity, client = _entity(hass)
    client.async_backup_panel = AsyncMock(side_effect=CannotConnectError)

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "panel_backup_failed")
    delivery.download.assert_not_awaited()
    client.async_stage_apk.assert_not_awaited()
    assert entity.in_progress is False


async def test_backup_store_failure_never_downloads(
    hass: HomeAssistant, delivery: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup Home Assistant could not keep counts as no backup."""
    entity, _ = _entity(hass)
    monkeypatch.setattr(
        panel_update, "async_store_panel_backup", AsyncMock(side_effect=OSError)
    )

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "panel_backup_failed")
    delivery.download.assert_not_awaited()


async def test_backup_needing_approval_asks_for_it(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A Hardened panel's approval request is surfaced, not treated as a failure."""
    entity, client = _entity(hass)
    client.async_backup_panel = AsyncMock(side_effect=UpdateApprovalRequiredError)

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "update_approval_required")
    delivery.download.assert_not_awaited()


async def test_upload_disabled_is_reported(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A panel that refuses uploads says so."""
    entity, client = _entity(hass)
    client.async_stage_apk = AsyncMock(side_effect=UploadDisabledError)

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "upload_disabled")
    client.async_commit_apk.assert_not_awaited()
    assert entity.in_progress is False


async def test_wait_refuses_a_restart_into_a_different_build(
    hass: HomeAssistant, delivery: SimpleNamespace
) -> None:
    """A restarted app with the wrong build number is not a completed update."""
    entity, client = _entity(hass)
    _restart_into(entity, client, 771)

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_install(None, False)

    _assert_translated(error.value, "update_not_complete")
    client.async_commit_apk.assert_awaited_once_with("tok-1")
    assert entity._installed_code == 771
    assert entity.in_progress is False


async def test_manual_update_also_rereads_the_feed(hass: HomeAssistant) -> None:
    """Asking the entity to update refreshes panel health, then the feed."""
    entity, _ = _entity(hass)
    feed = entity._feed
    assert feed is not None
    order: list[str] = []
    entity.coordinator.async_request_refresh = AsyncMock(
        side_effect=lambda: order.append("health")
    )
    feed.async_refresh = AsyncMock(side_effect=lambda: order.append("feed"))  # type: ignore[method-assign]

    await entity.async_update()

    assert order == ["health", "feed"]


# --- reading the running build number ---------------------------------------


async def test_refresh_reads_diag_once_per_install(hass: HomeAssistant) -> None:
    """The build number is read once per (version, build) and not on every poll."""
    entity, client = _entity(hass, installed_code=None)
    client.async_get_version_code = AsyncMock(return_value=(NAME, 771))

    entity._refresh_installed_code()
    entity._refresh_installed_code()
    assert entity._code_task is not None
    await entity._code_task
    entity._handle_coordinator_update()
    entity._handle_coordinator_update()

    assert client.async_get_version_code.await_count == 1
    assert entity._installed_code == 771
    assert entity._code_key == (NAME, "1000")

    entity.coordinator.data = PanelSnapshot(
        health=_health(NAME, build="2000"), status=None, status_error=None
    )
    client.async_get_version_code.return_value = (NAME, 772)
    entity._handle_coordinator_update()
    await entity._code_task

    assert client.async_get_version_code.await_count == 2
    assert entity._installed_code == 772


async def test_refresh_ignores_a_diag_for_another_version(
    hass: HomeAssistant,
) -> None:
    """A build number read across a version change is not latched, and the
    same app is not asked again on every poll."""
    entity, client = _entity(hass, installed_code=None)
    client.async_get_version_code = AsyncMock(return_value=("0.9.7-rc3", 707))

    entity._refresh_installed_code()
    assert entity._code_task is not None
    await entity._code_task

    health = entity.coordinator.data.health
    assert entity._installed_code is None
    assert entity._code_key == (health.version, health.build)
    entity._refresh_installed_code()
    await asyncio.sleep(0)
    assert client.async_get_version_code.await_count == 1


# --- coordinator and YAML ----------------------------------------------------


async def test_feed_coordinator_turns_a_bad_feed_into_a_failed_read(
    hass: HomeAssistant,
) -> None:
    """An unauthenticated feed marks the read failed rather than raising."""
    coordinator = BuildFeedCoordinator(hass, FEED_URL)

    with patch(FETCH, AsyncMock(side_effect=BuildFeedError)) as fetch:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert fetch.await_args.args[1] == FEED_URL


def _patched_setup(fetch: AsyncMock) -> Any:
    return patch.multiple(
        "custom_components.panel_assistant",
        async_register_browser_delivery=MagicMock(),
        async_register_browser_panel=AsyncMock(),
    ), patch(FETCH, fetch)


async def test_yaml_build_feed_creates_one_coordinator(hass: HomeAssistant) -> None:
    """The one YAML key stores a feed coordinator and reads the feed once."""
    config = {DOMAIN: {"build_feed": "https://x/maintainer.json"}}
    assert CONFIG_SCHEMA(config) == config
    fetch = AsyncMock(return_value=_feed_data(772))
    browser, feed = _patched_setup(fetch)

    with browser, feed:
        assert await async_setup(hass, config)
        await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = hass.data[DOMAIN]["build_feed"]
    assert isinstance(coordinator, BuildFeedCoordinator)
    assert async_get_feed_coordinator(hass) is coordinator
    assert coordinator.feed_url == URL("https://x/maintainer.json")
    fetch.assert_awaited_once()
    assert fetch.await_args.args[1] == URL("https://x/maintainer.json")
    assert coordinator.data == _feed_data(772)


async def test_yaml_invalid_build_feed_is_logged_and_ignored(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A plaintext feed URL is refused loudly and nothing is fetched."""
    fetch = AsyncMock()
    browser, feed = _patched_setup(fetch)

    with browser, feed, caplog.at_level(logging.ERROR):
        assert await async_setup(
            hass, {DOMAIN: {"build_feed": "http://x/maintainer.json"}}
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert "build_feed" not in hass.data.get(DOMAIN, {})
    assert async_get_feed_coordinator(hass) is None
    assert any(
        record.levelno == logging.ERROR and "build_feed" in record.getMessage()
        for record in caplog.records
    )
    fetch.assert_not_awaited()


@pytest.mark.parametrize("config", [{}, {DOMAIN: {}}])
async def test_no_yaml_means_no_feed(
    hass: HomeAssistant, config: dict[str, Any]
) -> None:
    """Without the key there is no coordinator and no fetch."""
    fetch = AsyncMock()
    browser, feed = _patched_setup(fetch)

    with browser, feed:
        assert await async_setup(hass, config)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert async_get_feed_coordinator(hass) is None
    fetch.assert_not_awaited()


def _setup_patches(version_code: AsyncMock) -> Any:
    executor = SimpleNamespace(
        async_acquire_finalizer=AsyncMock(return_value=True),
        async_release_finalizer=AsyncMock(),
    )
    manager = SimpleNamespace(
        async_list=AsyncMock(return_value=()), async_transition=AsyncMock()
    )
    client = "custom_components.panel_assistant.client.HaPaneldClient"
    return (
        patch(f"{client}.async_get_health", AsyncMock(return_value=_health(NAME))),
        patch(
            f"{client}.async_get_status",
            AsyncMock(return_value=PanelStatus(warning_count=0, capability_count=0)),
        ),
        patch(f"{client}.async_get_version_code", version_code),
        patch(
            "custom_components.panel_assistant.async_resume_loaded_install_jobs",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.panel_assistant.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.panel_assistant.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    )


def _update_state(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    update = next(
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id and item.domain == "update"
    )
    return hass.states.get(update.entity_id)


async def test_stable_setup_never_reads_a_feed(hass: HomeAssistant) -> None:
    """A panel set up without YAML never fetches a feed or reads diagnostics."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: "panel.local"})
    entry.add_to_hass(hass)
    version_code = AsyncMock(return_value=(NAME, 771))
    patches = _setup_patches(version_code)
    fetch = AsyncMock()

    with ExitStack() as stack:
        for context in (patch(FETCH, fetch), *patches):
            stack.enter_context(context)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        state = _update_state(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)

    fetch.assert_not_awaited()
    version_code.assert_not_awaited()
    assert state.attributes["installed_version"] == NAME
    assert not (
        state.attributes["supported_features"] & UpdateEntityFeature.SPECIFIC_VERSION
    )


async def test_yaml_feed_reaches_the_panel_update_entity(hass: HomeAssistant) -> None:
    """With the YAML feed, a set-up panel shows and offers builds by number."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: "panel.local"})
    entry.add_to_hass(hass)
    version_code = AsyncMock(return_value=(NAME, 771))
    patches = _setup_patches(version_code)
    fetch = AsyncMock(return_value=_feed_data(770, 771, 772))

    with ExitStack() as stack:
        for context in (patch(FETCH, fetch), *patches):
            stack.enter_context(context)
        assert await async_setup_component(
            hass, DOMAIN, {DOMAIN: {"build_feed": str(FEED_URL)}}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        state = _update_state(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)

    fetch.assert_awaited()
    version_code.assert_awaited_once()
    assert state.attributes["installed_version"] == "0.9.7-rc4 build 771"
    assert state.attributes["latest_version"] == "0.9.7-rc4 build 772"
    assert state.state == "on"
    assert state.attributes["supported_features"] & (
        UpdateEntityFeature.SPECIFIC_VERSION
    )


# --- backup store ------------------------------------------------------------


async def test_backup_store_keeps_the_newest_five_private_and_atomic(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Five newest per panel survive, files are 0600, no partial file remains."""
    hass.config.config_dir = str(tmp_path)
    directory = tmp_path / DOMAIN / "backups"
    directory.mkdir(parents=True)
    older = [
        directory / f"entry-id-2000010{day}T000000Z-vc700.zip" for day in range(1, 6)
    ]
    for path in older:
        path.write_bytes(b"old")
    other = directory / "other-entry-20000101T000000Z-vc1.zip"
    other.write_bytes(b"other")

    path = await async_store_panel_backup(hass, "entry-id", 771, b"new-backup")

    assert path.parent == directory
    assert path.name.endswith("-vc771.zip")
    assert path.read_bytes() == b"new-backup"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = sorted(p.name for p in directory.glob("entry-id-*.zip"))
    assert kept == sorted([p.name for p in older[1:]] + [path.name])
    assert not older[0].exists()
    assert other.exists()
    assert not [p for p in directory.iterdir() if p.name.endswith(".partial")]


async def test_backup_store_creates_a_private_directory(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The first backup creates an owner-only directory; unknown codes are named."""
    hass.config.config_dir = str(tmp_path)

    path = await async_store_panel_backup(hass, "entry-id", None, b"zip")

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.name.endswith("-vcunknown.zip")
    assert [p.name for p in path.parent.iterdir()] == [path.name]
