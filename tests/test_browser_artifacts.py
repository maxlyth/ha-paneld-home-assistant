"""Browser artifact namespaces remain independent from native job custody."""

import asyncio
from pathlib import Path

import pytest

from custom_components.ha_paneld import browser_artifacts as browser
from custom_components.ha_paneld import install_artifacts as native

from .test_install_artifacts import (
    _BODY,
    _JOB_ID,
    _FakeHass,
    _FakeResponse,
    _FakeSession,
    _release,
)
from .test_install_artifacts import (
    fake_hass as fake_hass,
)


async def test_native_cleanup_cannot_delete_browser_artifact(
    fake_hass: _FakeHass,
) -> None:
    """Even identical IDs in the two namespaces have independent custody."""
    original = _release()
    installed = await native.async_download_install_artifact(
        fake_hass,
        _FakeSession([_FakeResponse()]),
        original,
        _JOB_ID,  # type: ignore[arg-type]
    )
    downloaded = await browser.async_download_browser_artifact(
        fake_hass,
        _FakeSession([_FakeResponse()]),
        original,
        _JOB_ID,  # type: ignore[arg-type]
    )
    assert Path(installed.path).parent != Path(downloaded.path).parent
    assert Path(downloaded.path).parent.name == "ha_paneld.browser_artifacts"
    assert await asyncio.to_thread(Path(downloaded.path).read_bytes) == _BODY
    await native.async_cleanup_install_artifact(fake_hass, _JOB_ID)  # type: ignore[arg-type]
    await native.async_reconcile_install_artifacts(fake_hass, [])  # type: ignore[arg-type]
    assert not await asyncio.to_thread(Path(installed.path).exists)
    assert await asyncio.to_thread(Path(downloaded.path).read_bytes) == _BODY
    await browser.async_reconcile_browser_artifacts(fake_hass, [_JOB_ID])  # type: ignore[arg-type]
    assert await asyncio.to_thread(Path(downloaded.path).read_bytes) == _BODY
    await browser.async_cleanup_browser_artifact(fake_hass, _JOB_ID)  # type: ignore[arg-type]
    assert not await asyncio.to_thread(Path(downloaded.path).exists)


async def test_browser_reconciliation_cannot_delete_native_job(
    fake_hass: _FakeHass,
) -> None:
    original = _release()
    installed = await native.async_download_install_artifact(
        fake_hass,
        _FakeSession([_FakeResponse()]),
        original,
        _JOB_ID,  # type: ignore[arg-type]
    )
    downloaded = await browser.async_download_browser_artifact(
        fake_hass,
        _FakeSession([_FakeResponse()]),
        original,
        _JOB_ID,  # type: ignore[arg-type]
    )
    await browser.async_reconcile_browser_artifacts(fake_hass, [])  # type: ignore[arg-type]
    assert not await asyncio.to_thread(Path(downloaded.path).exists)
    assert await asyncio.to_thread(Path(installed.path).read_bytes) == _BODY


async def test_browser_custody_verifies_bytes_and_cleans_failed_download(
    fake_hass: _FakeHass,
) -> None:
    session = _FakeSession([_FakeResponse(chunks=[b"x" * len(_BODY)])])
    with pytest.raises(native.ArtifactCustodyError) as caught:
        await browser.async_download_browser_artifact(
            fake_hass,
            session,
            _release(),
            _JOB_ID,  # type: ignore[arg-type]
        )
    assert caught.value.code == native.ArtifactErrorCode.DIGEST_MISMATCH
    assert len(session.requests) == 1
    directory = Path(fake_hass.config.path(".storage", "ha_paneld.browser_artifacts"))
    assert await asyncio.to_thread(lambda: list(directory.iterdir())) == []


@pytest.mark.parametrize("identifier", ["", "../outside", "a" * 31, "a" * 33])
async def test_browser_identifiers_cannot_select_paths(
    fake_hass: _FakeHass, identifier: str
) -> None:
    session = _FakeSession([])
    with pytest.raises(native.ArtifactCustodyError):
        await browser.async_download_browser_artifact(
            fake_hass,
            session,
            _release(),
            identifier,  # type: ignore[arg-type]
        )
    with pytest.raises(native.ArtifactCustodyError):
        await browser.async_cleanup_browser_artifact(fake_hass, identifier)  # type: ignore[arg-type]
    assert session.requests == []
    directory = Path(fake_hass.config.path(".storage"))
    assert await asyncio.to_thread(lambda: list(directory.iterdir())) == []
