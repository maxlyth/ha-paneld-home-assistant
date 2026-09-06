"""Browser artifact namespaces remain independent from native job custody."""

import asyncio
from dataclasses import replace
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


async def test_browser_read_returns_verified_bytes_without_reopening_download(
    fake_hass: _FakeHass,
) -> None:
    session = _FakeSession([_FakeResponse()])
    artifact = await browser.async_download_browser_artifact(
        fake_hass,
        session,
        _release(),
        _JOB_ID,  # type: ignore[arg-type]
    )
    body = await browser.async_read_browser_artifact(fake_hass, _JOB_ID, artifact)  # type: ignore[arg-type]
    assert body == _BODY
    assert isinstance(body, bytes)
    assert len(session.requests) == 1
    for changed in (
        replace(artifact, path="/unrelated/file.apk"),
        replace(artifact, job_id="a" * 32),
        replace(artifact, size=0),
        replace(artifact, size=64 * 1024 * 1024 + 1),
        replace(artifact, sha256="0" * 64),
    ):
        with pytest.raises(native.ArtifactCustodyError):
            await browser.async_read_browser_artifact(fake_hass, _JOB_ID, changed)  # type: ignore[arg-type]


@pytest.mark.parametrize("fault", ["bytes", "symlink", "hardlink", "fifo"])
async def test_browser_read_refuses_changed_or_linked_custody(
    fake_hass: _FakeHass, fault: str
) -> None:
    artifact = await browser.async_download_browser_artifact(
        fake_hass,
        _FakeSession([_FakeResponse()]),
        _release(),
        _JOB_ID,  # type: ignore[arg-type]
    )

    def alter() -> None:
        import os

        path = Path(artifact.path)
        if fault == "bytes":
            path.write_bytes(b"x" * len(_BODY))
        elif fault == "hardlink":
            os.link(path, path.parent / "extra.apk")
        else:
            path.unlink()
            if fault == "symlink":
                other = path.parent / "private.txt"
                other.write_bytes(_BODY)
                other.chmod(0o600)
                path.symlink_to(other)
            else:
                os.mkfifo(path, 0o600)

    await asyncio.to_thread(alter)
    with pytest.raises(native.ArtifactCustodyError):
        await browser.async_read_browser_artifact(fake_hass, _JOB_ID, artifact)  # type: ignore[arg-type]
