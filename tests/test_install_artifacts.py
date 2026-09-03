"""Tests for private installer artifact custody."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientConnectionError, ClientTimeout
from multidict import CIMultiDict
from yarl import URL

from custom_components.ha_paneld import install_artifacts
from custom_components.ha_paneld.install_artifacts import (
    ArtifactCustodyError,
    ArtifactErrorCode,
    async_cleanup_install_artifact,
    async_download_install_artifact,
)
from custom_components.ha_paneld.release import InstallDescriptor, ReleaseArtifact

_JOB_ID = "0123456789abcdef0123456789abcdef"
_OTHER_JOB_ID = "fedcba9876543210fedcba9876543210"
_APK_URL = (
    "https://github.com/maxlyth/ha-paneld/releases/download/"
    "v1.2.3/ha-paneld-v1.2.3-manual-setup-required.apk"
)
_APK_NAME = "ha-paneld-v1.2.3-manual-setup-required.apk"
_BODY = b"authenticated apk bytes"


class _FakeConfig:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))


class _FakeHass:
    def __init__(
        self,
        root: Path,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.config = _FakeConfig(root)
        self.executor = executor
        self.executor_calls: list[str] = []
        self.executor_futures: list[asyncio.Future[Any]] = []
        self.executor_added = asyncio.Event()

    def async_add_executor_job(
        self, target: Callable[..., Any], *args: Any
    ) -> asyncio.Future[Any]:
        logical_target = args[0] if target.__name__ == "_run_worker" else target
        self.executor_calls.append(logical_target.__name__)
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, target, *args
        )
        self.executor_futures.append(future)
        self.executor_added.set()
        return future


class _FakeContent:
    def __init__(
        self,
        chunks: list[bytes | str] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._error = error

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes | str]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class _BlockingContent:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"


@dataclass
class _FakeResponse:
    status: int = 200
    url: URL = field(default_factory=lambda: URL(_APK_URL))
    chunks: list[bytes | str] = field(default_factory=lambda: [_BODY])
    headers: CIMultiDict[str] = field(default_factory=CIMultiDict)
    history: tuple[Any, ...] = ()
    declared_length: int | None = None
    content: Any = None

    def __post_init__(self) -> None:
        if self.content is None:
            self.content = _FakeContent(self.chunks)

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
        responses: list[_FakeResponse] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests: list[tuple[URL, dict[str, Any]]] = []

    def get(self, url: URL, **kwargs: Any) -> _FakeResponse:
        self.requests.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


@pytest.fixture
def fake_hass(tmp_path: Path) -> _FakeHass:
    """Return a minimal HA executor/config surface with an existing .storage."""
    tmp_path.joinpath(".storage").mkdir()
    return _FakeHass(tmp_path)


def _release(body: bytes = _BODY, **replacements: Any) -> ReleaseArtifact:
    sha256 = hashlib.sha256(body).hexdigest()
    descriptor = InstallDescriptor(
        schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v1.2.3",
        version_name="1.2.3",
        version_code=701,
        apk_name=_APK_NAME,
        apk_size=len(body),
        apk_sha256=sha256,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=(
            "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
        ),
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:11:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )
    values: dict[str, Any] = {
        "tag": "v1.2.3",
        "version": "1.2.3",
        "apk_name": _APK_NAME,
        "apk_url": _APK_URL,
        "sha256": sha256,
        "descriptor": descriptor,
    }
    values.update(replacements)
    return ReleaseArtifact(**values)


def _custody_directory(fake_hass: _FakeHass) -> Path:
    return Path(
        fake_hass.config.path(
            ".storage",
            install_artifacts._STORAGE_DIRECTORY,
        )
    )


async def _wait_for_executor_count(fake_hass: _FakeHass, count: int) -> None:
    async with asyncio.timeout(1):
        while len(fake_hass.executor_futures) < count:
            fake_hass.executor_added.clear()
            if len(fake_hass.executor_futures) < count:
                await fake_hass.executor_added.wait()


async def _assert_error(
    code: ArtifactErrorCode,
    fake_hass: _FakeHass,
    session: _FakeSession,
    artifact: ReleaseArtifact | None = None,
    job_id: str = _JOB_ID,
    partial_removed: bool = True,
) -> None:
    with pytest.raises(ArtifactCustodyError) as captured:
        await async_download_install_artifact(
            fake_hass, session, artifact or _release(), job_id
        )
    assert captured.value.code is code
    assert str(captured.value) == code.value
    directory = _custody_directory(fake_hass)
    assert directory.joinpath(f"{job_id}.apk.part").exists() is not partial_removed


async def test_download_verifies_and_atomically_publishes_private_file(
    fake_hass: _FakeHass,
) -> None:
    """A matching body becomes one private ready artifact."""
    response = _FakeResponse(
        chunks=[_BODY[:5], _BODY[5:]],
        headers=CIMultiDict({"Content-Length": str(len(_BODY))}),
        declared_length=len(_BODY),
    )
    session = _FakeSession([response])

    result = await async_download_install_artifact(
        fake_hass, session, _release(), _JOB_ID
    )

    ready = Path(result.path)
    assert result.job_id == _JOB_ID
    assert result.size == len(_BODY)
    assert result.sha256 == hashlib.sha256(_BODY).hexdigest()
    assert await asyncio.to_thread(ready.read_bytes) == _BODY
    assert stat.S_IMODE((await asyncio.to_thread(ready.stat)).st_mode) == 0o600
    assert stat.S_IMODE((await asyncio.to_thread(ready.parent.stat)).st_mode) == 0o700
    assert not ready.with_suffix(".apk.part").exists()
    assert len(session.requests) == 1
    request_url, request_options = session.requests[0]
    assert request_url == URL(_APK_URL)
    assert request_options["allow_redirects"] is False
    assert request_options["auto_decompress"] is False
    assert request_options["headers"] == {
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    assert request_options["timeout"] == ClientTimeout(
        total=120.0, connect=10.0, sock_read=30.0
    )
    assert fake_hass.executor_calls == [
        "_prepare_destination",
        "_write_all",
        "_write_all",
        "_finish_file",
        "_promote_partial",
    ]


async def test_valid_ready_artifact_is_rehashed_and_reused_without_network(
    fake_hass: _FakeHass,
) -> None:
    first_session = _FakeSession([_FakeResponse()])
    first = await async_download_install_artifact(
        fake_hass, first_session, _release(), _JOB_ID
    )
    second_session = _FakeSession()

    second = await async_download_install_artifact(
        fake_hass, second_session, _release(), _JOB_ID
    )

    assert second == first
    assert second_session.requests == []
    assert fake_hass.executor_calls[-1] == "_prepare_destination"


@pytest.mark.parametrize("tamper", [b"wrong but same length!!!", b"short"])
async def test_invalid_ready_artifact_fails_closed_without_network(
    fake_hass: _FakeHass,
    tamper: bytes,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    ready = directory.joinpath(f"{_JOB_ID}.apk")
    ready.write_bytes(tamper[: len(_BODY)])
    ready.chmod(0o600)
    session = _FakeSession()

    await _assert_error(ArtifactErrorCode.READY_INVALID, fake_hass, session)

    assert session.requests == []
    assert ready.exists()


async def test_descriptor_is_required_before_filesystem_or_network(
    fake_hass: _FakeHass,
) -> None:
    session = _FakeSession()

    await _assert_error(
        ArtifactErrorCode.DESCRIPTOR_REQUIRED,
        fake_hass,
        session,
        _release(descriptor=None),
    )

    assert session.requests == []
    assert not _custody_directory(fake_hass).exists()


@pytest.mark.parametrize(
    ("job_id", "code"),
    [
        ("", ArtifactErrorCode.JOB_ID_INVALID),
        ("A" * 32, ArtifactErrorCode.JOB_ID_INVALID),
        ("0" * 31, ArtifactErrorCode.JOB_ID_INVALID),
        ("../" + "0" * 29, ArtifactErrorCode.JOB_ID_INVALID),
    ],
)
async def test_job_id_is_closed_before_filesystem_or_network(
    fake_hass: _FakeHass,
    job_id: str,
    code: ArtifactErrorCode,
) -> None:
    session = _FakeSession()

    await _assert_error(code, fake_hass, session, job_id=job_id)

    assert session.requests == []
    assert not _custody_directory(fake_hass).exists()


@pytest.mark.parametrize(
    "replacements",
    [
        {"sha256": "f" * 64},
        {"apk_name": "other.apk"},
        {"apk_url": "http://github.com/release.apk"},
        {"apk_url": "https://example.com/release.apk"},
        {"apk_url": "https://user@github.com/release.apk"},
        {"apk_url": "https://github.com:444/release.apk"},
        {"apk_url": "https://github.com/release.apk#fragment"},
        {"apk_url": object()},
        {"apk_url": "https://["},
    ],
)
async def test_release_contract_is_cross_bound_before_download(
    fake_hass: _FakeHass,
    replacements: dict[str, Any],
) -> None:
    session = _FakeSession()

    await _assert_error(
        ArtifactErrorCode.CONTRACT_INVALID,
        fake_hass,
        session,
        _release(**replacements),
    )

    assert session.requests == []


@pytest.mark.parametrize(
    "descriptor_change",
    [
        {"apk_size": True},
        {"apk_size": 0},
        {"apk_size": 64 * 1024 * 1024 + 1},
        {"apk_sha256": "F" * 64},
        {"apk_name": "other.apk"},
    ],
)
async def test_descriptor_custody_fields_are_bounded(
    fake_hass: _FakeHass,
    descriptor_change: dict[str, Any],
) -> None:
    release = _release()
    descriptor = replace(release.descriptor, **descriptor_change)

    await _assert_error(
        ArtifactErrorCode.CONTRACT_INVALID,
        fake_hass,
        _FakeSession(),
        replace(release, descriptor=descriptor),
    )


async def test_three_trusted_manual_redirects_are_allowed(
    fake_hass: _FakeHass,
) -> None:
    urls = [
        URL(_APK_URL),
        URL("https://release-assets.githubusercontent.com/a"),
        URL("https://github.com/b"),
        URL("https://release-assets.githubusercontent.com/c"),
    ]
    responses = [
        _FakeResponse(
            status=302,
            url=urls[index],
            headers=CIMultiDict({"Location": str(urls[index + 1])}),
        )
        for index in range(3)
    ]
    responses.append(_FakeResponse(url=urls[-1]))
    session = _FakeSession(responses)

    await async_download_install_artifact(fake_hass, session, _release(), _JOB_ID)

    assert [request[0] for request in session.requests] == urls
    assert all(request[1]["allow_redirects"] is False for request in session.requests)


async def test_fourth_redirect_is_rejected_and_partial_is_removed(
    fake_hass: _FakeHass,
) -> None:
    urls = [URL(_APK_URL)] + [
        URL(f"https://release-assets.githubusercontent.com/{index}")
        for index in range(4)
    ]
    responses = [
        _FakeResponse(
            status=302,
            url=urls[index],
            headers=CIMultiDict({"Location": str(urls[index + 1])}),
        )
        for index in range(4)
    ]
    session = _FakeSession(responses)

    await _assert_error(ArtifactErrorCode.REDIRECT_INVALID, fake_hass, session)

    assert len(session.requests) == 4


@pytest.mark.parametrize(
    "location",
    [
        "http://github.com/file.apk",
        "https://example.com/file.apk",
        " https://github.com/file.apk",
        "https://github.com/file.apk\x00",
    ],
)
async def test_untrusted_or_malformed_redirect_is_rejected(
    fake_hass: _FakeHass,
    location: str,
) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=302,
                headers=CIMultiDict({"Location": location}),
            )
        ]
    )

    await _assert_error(ArtifactErrorCode.REDIRECT_INVALID, fake_hass, session)


async def test_redirect_requires_one_location_header(
    fake_hass: _FakeHass,
) -> None:
    await _assert_error(
        ArtifactErrorCode.REDIRECT_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(status=302)]),
    )


async def test_duplicate_location_headers_are_rejected(
    fake_hass: _FakeHass,
) -> None:
    headers: CIMultiDict[str] = CIMultiDict()
    headers.add("Location", "https://release-assets.githubusercontent.com/one")
    headers.add("Location", "https://release-assets.githubusercontent.com/two")

    await _assert_error(
        ArtifactErrorCode.REDIRECT_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(status=302, headers=headers)]),
    )


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(status=206),
        _FakeResponse(status=404),
        _FakeResponse(history=(object(),)),
        _FakeResponse(url=URL("https://github.com/unexpected")),
    ],
)
async def test_status_or_implicit_redirect_is_rejected(
    fake_hass: _FakeHass,
    response: _FakeResponse,
) -> None:
    expected = (
        ArtifactErrorCode.REDIRECT_INVALID
        if response.history or response.url != URL(_APK_URL)
        else ArtifactErrorCode.HTTP_STATUS
    )
    await _assert_error(expected, fake_hass, _FakeSession([response]))


@pytest.mark.parametrize(
    "headers",
    [
        CIMultiDict({"Content-Range": f"bytes 0-{len(_BODY) - 1}/{len(_BODY)}"}),
        CIMultiDict({"Content-Encoding": "gzip"}),
        CIMultiDict({"Content-Encoding": "identity, gzip"}),
        CIMultiDict({"Content-Length": "not-a-number"}),
    ],
)
async def test_ambiguous_or_encoded_response_is_rejected(
    fake_hass: _FakeHass,
    headers: CIMultiDict[str],
) -> None:
    await _assert_error(
        ArtifactErrorCode.RESPONSE_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(headers=headers)]),
    )


async def test_duplicate_content_headers_are_rejected(
    fake_hass: _FakeHass,
) -> None:
    headers: CIMultiDict[str] = CIMultiDict()
    headers.add("Content-Length", str(len(_BODY)))
    headers.add("Content-Length", str(len(_BODY)))

    await _assert_error(
        ArtifactErrorCode.RESPONSE_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(headers=headers, declared_length=len(_BODY))]),
    )


async def test_duplicate_identity_content_encoding_is_rejected(
    fake_hass: _FakeHass,
) -> None:
    headers: CIMultiDict[str] = CIMultiDict()
    headers.add("Content-Encoding", "identity")
    headers.add("Content-Encoding", "identity")

    await _assert_error(
        ArtifactErrorCode.RESPONSE_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(headers=headers)]),
    )


@pytest.mark.parametrize("declared_length", [True, "23"])
async def test_non_integer_response_length_is_rejected(
    fake_hass: _FakeHass,
    declared_length: Any,
) -> None:
    await _assert_error(
        ArtifactErrorCode.RESPONSE_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse(declared_length=declared_length)]),
    )


async def test_declared_length_must_equal_signed_size(
    fake_hass: _FakeHass,
) -> None:
    await _assert_error(
        ArtifactErrorCode.SIZE_MISMATCH,
        fake_hass,
        _FakeSession(
            [
                _FakeResponse(
                    headers=CIMultiDict({"Content-Length": "1"}),
                    declared_length=1,
                )
            ]
        ),
    )


async def test_header_and_parsed_content_length_must_agree(
    fake_hass: _FakeHass,
) -> None:
    await _assert_error(
        ArtifactErrorCode.RESPONSE_INVALID,
        fake_hass,
        _FakeSession(
            [
                _FakeResponse(
                    headers=CIMultiDict({"Content-Length": str(len(_BODY))}),
                    declared_length=len(_BODY) - 1,
                )
            ]
        ),
    )


async def test_parsed_content_length_without_raw_header_is_accepted(
    fake_hass: _FakeHass,
) -> None:
    await async_download_install_artifact(
        fake_hass,
        _FakeSession([_FakeResponse(declared_length=len(_BODY))]),
        _release(),
        _JOB_ID,
    )


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        ([_BODY[:-1]], ArtifactErrorCode.SIZE_MISMATCH),
        ([_BODY + b"x"], ArtifactErrorCode.SIZE_MISMATCH),
        (["not bytes"], ArtifactErrorCode.RESPONSE_INVALID),
    ],
)
async def test_actual_stream_shape_is_exact(
    fake_hass: _FakeHass,
    chunks: list[bytes | str],
    code: ArtifactErrorCode,
) -> None:
    await _assert_error(code, fake_hass, _FakeSession([_FakeResponse(chunks=chunks)]))


async def test_hard_maximum_is_enforced_independently(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_artifacts, "_MAX_APK_BYTES", len(_BODY))

    await _assert_error(
        ArtifactErrorCode.TOO_LARGE,
        fake_hass,
        _FakeSession([_FakeResponse(chunks=[_BODY + b"x"])]),
    )


async def test_digest_mismatch_never_creates_ready_file(
    fake_hass: _FakeHass,
) -> None:
    different = b"x" * len(_BODY)

    await _assert_error(
        ArtifactErrorCode.DIGEST_MISMATCH,
        fake_hass,
        _FakeSession([_FakeResponse(chunks=[different])]),
    )

    assert not _custody_directory(fake_hass).joinpath(f"{_JOB_ID}.apk").exists()


async def test_network_and_timeout_errors_are_stable_and_clean_partial(
    fake_hass: _FakeHass,
) -> None:
    await _assert_error(
        ArtifactErrorCode.DOWNLOAD_FAILED,
        fake_hass,
        _FakeSession(error=ClientConnectionError("private detail")),
    )


async def test_overall_timeout_cleans_partial(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_artifacts, "_OVERALL_TIMEOUT_SECONDS", 0.01)
    content = _BlockingContent()

    await asyncio.wait_for(
        _assert_error(
            ArtifactErrorCode.TIMEOUT,
            fake_hass,
            _FakeSession([_FakeResponse(content=content)]),
        ),
        timeout=1,
    )

    assert content.started.is_set()


async def test_unexpected_stream_failure_propagates_after_cleanup(
    fake_hass: _FakeHass,
) -> None:
    with pytest.raises(RuntimeError, match="programming failure"):
        await async_download_install_artifact(
            fake_hass,
            _FakeSession(
                [
                    _FakeResponse(
                        content=_FakeContent(error=RuntimeError("programming failure"))
                    )
                ]
            ),
            _release(),
            _JOB_ID,
        )
    assert not _custody_directory(fake_hass).joinpath(f"{_JOB_ID}.apk.part").exists()
    await _assert_error(
        ArtifactErrorCode.TIMEOUT,
        fake_hass,
        _FakeSession(
            [_FakeResponse(content=_FakeContent(error=TimeoutError("private detail")))]
        ),
        job_id=_OTHER_JOB_ID,
    )


async def test_existing_partial_is_busy_and_not_removed(
    fake_hass: _FakeHass,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    partial = directory.joinpath(f"{_JOB_ID}.apk.part")
    partial.write_bytes(b"owned in-flight state")
    partial.chmod(0o600)

    await _assert_error(
        ArtifactErrorCode.BUSY,
        fake_hass,
        _FakeSession(),
        partial_removed=False,
    )

    assert partial.read_bytes() == b"owned in-flight state"


async def test_ready_and_partial_together_fail_closed(
    fake_hass: _FakeHass,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    for suffix, body in ((".apk", _BODY), (".apk.part", b"partial")):
        path = directory.joinpath(f"{_JOB_ID}{suffix}")
        path.write_bytes(body)
        path.chmod(0o600)

    await _assert_error(
        ArtifactErrorCode.PATH_INVALID,
        fake_hass,
        _FakeSession(),
        partial_removed=False,
    )


@pytest.mark.parametrize("kind", ["directory_mode", "ready_mode", "ready_link"])
async def test_unexpected_path_types_fail_closed(
    fake_hass: _FakeHass,
    kind: str,
    tmp_path: Path,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    ready = directory.joinpath(f"{_JOB_ID}.apk")
    if kind == "directory_mode":
        directory.chmod(0o755)
    elif kind == "ready_mode":
        ready.write_bytes(_BODY)
        ready.chmod(0o644)
    else:
        target = tmp_path.joinpath("outside")
        target.write_bytes(_BODY)
        ready.symlink_to(target)

    await _assert_error(ArtifactErrorCode.PATH_INVALID, fake_hass, _FakeSession())


async def test_symlink_directory_is_rejected_without_touching_target(
    fake_hass: _FakeHass,
    tmp_path: Path,
) -> None:
    outside = tmp_path.joinpath("outside-directory")
    outside.mkdir(mode=0o700)
    directory = _custody_directory(fake_hass)
    directory.symlink_to(outside, target_is_directory=True)

    await _assert_error(ArtifactErrorCode.PATH_INVALID, fake_hass, _FakeSession())

    assert list(outside.iterdir()) == []


async def test_cancellation_removes_partial_and_propagates(
    fake_hass: _FakeHass,
) -> None:
    content = _BlockingContent()
    task = asyncio.create_task(
        async_download_install_artifact(
            fake_hass,
            _FakeSession([_FakeResponse(content=content)]),
            _release(),
            _JOB_ID,
        )
    )
    await content.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()
    assert not directory.joinpath(f"{_JOB_ID}.apk").exists()


async def test_cancellation_after_promotion_keeps_verified_ready_file(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_promote = install_artifacts._promote_partial
    promoted = threading.Event()
    release_promoter = threading.Event()

    def _slow_promote(*args: Any) -> None:
        original_promote(*args)
        promoted.set()
        release_promoter.wait(timeout=5)

    monkeypatch.setattr(install_artifacts, "_promote_partial", _slow_promote)
    task = asyncio.create_task(
        async_download_install_artifact(
            fake_hass, _FakeSession([_FakeResponse()]), _release(), _JOB_ID
        )
    )
    assert await asyncio.to_thread(promoted.wait, 5)

    task.cancel()
    release_promoter.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    ready = _custody_directory(fake_hass).joinpath(f"{_JOB_ID}.apk")
    assert ready.read_bytes() == _BODY
    assert not ready.with_suffix(".apk.part").exists()


async def test_cancelled_executor_future_and_repeated_task_cancel_clean_partial(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_prepare = install_artifacts._prepare_destination
    prepared = threading.Event()
    release_prepare = threading.Event()

    def _slow_prepare(*args: Any) -> int | None:
        result = original_prepare(*args)
        prepared.set()
        release_prepare.wait(timeout=5)
        return result

    monkeypatch.setattr(install_artifacts, "_prepare_destination", _slow_prepare)
    session = _FakeSession()
    task = asyncio.create_task(
        async_download_install_artifact(fake_hass, session, _release(), _JOB_ID)
    )
    assert await asyncio.to_thread(prepared.wait, 5)

    executor_future = fake_hass.executor_futures[-1]
    assert executor_future.cancel()
    await asyncio.sleep(0)
    assert executor_future.cancelled()
    assert not task.done()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_prepare.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.requests == []
    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()
    assert not directory.joinpath(f"{_JOB_ID}.apk").exists()


async def test_queued_executor_future_cancel_before_worker_start_does_not_hang(
    tmp_path: Path,
) -> None:
    tmp_path.joinpath(".storage").mkdir()
    executor = ThreadPoolExecutor(max_workers=1)
    fake_hass = _FakeHass(tmp_path, executor)
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def _block_executor() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=5)

    blocker_future = fake_hass.async_add_executor_job(_block_executor)
    assert await asyncio.to_thread(blocker_started.wait, 5)
    task = asyncio.create_task(
        async_download_install_artifact(fake_hass, _FakeSession(), _release(), _JOB_ID)
    )
    try:
        await _wait_for_executor_count(fake_hass, 2)
        queued_future = fake_hass.executor_futures[-1]
        assert queued_future.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert queued_future.cancelled()
        assert not _custody_directory(fake_hass).exists()
    finally:
        release_blocker.set()
        await blocker_future
        await asyncio.to_thread(executor.shutdown, True)


async def test_cancelled_queued_abandonment_is_resubmitted_and_cleans_partial(
    tmp_path: Path,
) -> None:
    tmp_path.joinpath(".storage").mkdir()
    executor = ThreadPoolExecutor(max_workers=1)
    fake_hass = _FakeHass(tmp_path, executor)
    content = _BlockingContent()
    task = asyncio.create_task(
        async_download_install_artifact(
            fake_hass,
            _FakeSession([_FakeResponse(content=content)]),
            _release(),
            _JOB_ID,
        )
    )
    await content.started.wait()
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def _block_executor() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=5)

    blocker_future = fake_hass.async_add_executor_job(_block_executor)
    assert await asyncio.to_thread(blocker_started.wait, 5)
    executor_count = len(fake_hass.executor_futures)
    try:
        task.cancel()
        await _wait_for_executor_count(fake_hass, executor_count + 1)
        first_cleanup_future = fake_hass.executor_futures[-1]
        assert first_cleanup_future.cancel()
        await _wait_for_executor_count(fake_hass, executor_count + 2)

        release_blocker.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert first_cleanup_future.cancelled()
        directory = _custody_directory(fake_hass)
        assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()
        assert not directory.joinpath(f"{_JOB_ID}.apk").exists()
    finally:
        release_blocker.set()
        await blocker_future
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(executor.shutdown, True)


async def test_second_cancellation_during_close_cannot_skip_partial_unlink(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _BlockingContent()
    task = asyncio.create_task(
        async_download_install_artifact(
            fake_hass,
            _FakeSession([_FakeResponse(content=content)]),
            _release(),
            _JOB_ID,
        )
    )
    await content.started.wait()

    original_close = install_artifacts._close_file
    close_started = threading.Event()
    release_close = threading.Event()

    def _slow_close(file_fd: int) -> None:
        close_started.set()
        release_close.wait(timeout=5)
        original_close(file_fd)

    monkeypatch.setattr(install_artifacts, "_close_file", _slow_close)
    task.cancel()
    assert await asyncio.to_thread(close_started.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()
    assert not directory.joinpath(f"{_JOB_ID}.apk").exists()


async def test_prepare_directory_close_failure_cleans_fd_and_path_before_error(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_close = install_artifacts._close_file
    failed_directory_close = False
    closed_partial_fds: list[int] = []
    closed_directory_fds: list[int] = []

    def _fail_first_directory_close(file_fd: int) -> None:
        nonlocal failed_directory_close
        file_status = os.fstat(file_fd)
        if stat.S_ISDIR(file_status.st_mode) and not failed_directory_close:
            failed_directory_close = True
            closed_directory_fds.append(file_fd)
            original_close(file_fd)
            raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
        if stat.S_ISREG(file_status.st_mode):
            closed_partial_fds.append(file_fd)
        elif stat.S_ISDIR(file_status.st_mode):
            closed_directory_fds.append(file_fd)
        original_close(file_fd)

    monkeypatch.setattr(install_artifacts, "_close_file", _fail_first_directory_close)
    session = _FakeSession()

    await _assert_error(
        ArtifactErrorCode.IO_FAILED,
        fake_hass,
        session,
    )

    assert failed_directory_close
    assert session.requests == []
    assert len(closed_partial_fds) == 1
    assert len(closed_directory_fds) == 2
    for closed_fd in {*closed_partial_fds, *closed_directory_fds}:
        with pytest.raises(OSError):
            os.fstat(closed_fd)
    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()

    monkeypatch.undo()
    result = await async_download_install_artifact(
        fake_hass, _FakeSession([_FakeResponse()]), _release(), _JOB_ID
    )
    assert await asyncio.to_thread(Path(result.path).read_bytes) == _BODY


async def test_cleanup_removes_only_exact_job_owned_regular_files(
    fake_hass: _FakeHass,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    owned_ready = directory.joinpath(f"{_JOB_ID}.apk")
    owned_partial = directory.joinpath(f"{_JOB_ID}.apk.part")
    other_ready = directory.joinpath(f"{_OTHER_JOB_ID}.apk")
    unrelated = directory.joinpath("unrelated")
    for path in (owned_ready, owned_partial, other_ready, unrelated):
        path.write_bytes(b"data")
        path.chmod(0o600)

    await async_cleanup_install_artifact(fake_hass, _JOB_ID)

    assert not owned_ready.exists()
    assert not owned_partial.exists()
    assert other_ready.exists()
    assert unrelated.exists()


async def test_cleanup_refuses_symlink_and_does_not_follow_it(
    fake_hass: _FakeHass,
    tmp_path: Path,
) -> None:
    directory = _custody_directory(fake_hass)
    directory.mkdir(mode=0o700)
    outside = tmp_path.joinpath("outside-file")
    outside.write_bytes(b"preserve")
    directory.joinpath(f"{_JOB_ID}.apk.part").symlink_to(outside)

    with pytest.raises(ArtifactCustodyError) as captured:
        await async_cleanup_install_artifact(fake_hass, _JOB_ID)

    assert captured.value.code is ArtifactErrorCode.PATH_INVALID
    assert outside.read_bytes() == b"preserve"


async def test_cleanup_missing_private_directory_is_idempotent(
    fake_hass: _FakeHass,
) -> None:
    await async_cleanup_install_artifact(fake_hass, _JOB_ID)

    assert not _custody_directory(fake_hass).exists()


async def test_io_failure_code_does_not_disclose_private_path(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "/secret/private/location"

    def _fail_prepare(*_args: Any) -> None:
        raise OSError(private_detail)

    monkeypatch.setattr(os, "mkdir", _fail_prepare)

    await _assert_error(ArtifactErrorCode.IO_FAILED, fake_hass, _FakeSession())

    try:
        await async_download_install_artifact(
            fake_hass, _FakeSession(), _release(), _OTHER_JOB_ID
        )
    except ArtifactCustodyError as err:
        assert private_detail not in str(err)


def test_write_failure_paths_return_only_stable_io_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "write", lambda *_args: 0)
    with pytest.raises(ArtifactCustodyError) as zero_write:
        install_artifacts._write_all(99, b"body")
    assert zero_write.value.code is ArtifactErrorCode.IO_FAILED

    def _raise_oserror(*_args: Any) -> None:
        raise OSError("private write failure")

    monkeypatch.setattr(os, "write", _raise_oserror)
    with pytest.raises(ArtifactCustodyError) as failed_write:
        install_artifacts._write_all(99, b"body")
    assert failed_write.value.code is ArtifactErrorCode.IO_FAILED


def test_entry_stat_failure_returns_stable_io_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_oserror(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("private stat failure")

    monkeypatch.setattr(os, "stat", _raise_oserror)
    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._entry_status(99, "safe-name")
    assert captured.value.code is ArtifactErrorCode.IO_FAILED


def test_finish_and_close_failures_return_stable_io_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path.joinpath("target")
    file_fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)

    def _raise_fsync(_fd: int) -> None:
        raise OSError("private fsync failure")

    monkeypatch.setattr(os, "fsync", _raise_fsync)
    with pytest.raises(ArtifactCustodyError) as fsync_failure:
        install_artifacts._finish_file(file_fd)
    assert fsync_failure.value.code is ArtifactErrorCode.IO_FAILED

    monkeypatch.undo()
    file_fd = os.open(target, os.O_WRONLY)

    def _raise_close(_fd: int) -> None:
        raise OSError("private close failure")

    monkeypatch.setattr(os, "close", _raise_close)
    with pytest.raises(ArtifactCustodyError) as close_failure:
        install_artifacts._close_file(file_fd)
    assert close_failure.value.code is ArtifactErrorCode.IO_FAILED
    monkeypatch.undo()
    os.close(file_fd)


def test_hash_open_and_read_failures_return_stable_io_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:

        def _raise_open(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("private open failure")

        monkeypatch.setattr(os, "open", _raise_open)
        with pytest.raises(ArtifactCustodyError) as open_failure:
            install_artifacts._hash_open_file(
                directory_fd,
                "missing",
                expected_size=1,
                expected_sha256="0" * 64,
                invalid_code=ArtifactErrorCode.READY_INVALID,
            )
        assert open_failure.value.code is ArtifactErrorCode.IO_FAILED

        monkeypatch.undo()
        path = directory.joinpath("ready")
        path.write_bytes(b"x")
        path.chmod(0o600)

        def _raise_read(*_args: Any) -> None:
            raise OSError("private read failure")

        monkeypatch.setattr(os, "read", _raise_read)
        with pytest.raises(ArtifactCustodyError) as read_failure:
            install_artifacts._hash_open_file(
                directory_fd,
                "ready",
                expected_size=1,
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
                invalid_code=ArtifactErrorCode.READY_INVALID,
            )
        assert read_failure.value.code is ArtifactErrorCode.IO_FAILED
    finally:
        monkeypatch.undo()
        os.close(directory_fd)


def test_directory_identity_change_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    status = directory.lstat()
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=status.st_mode,
            st_uid=status.st_uid,
            st_dev=status.st_dev,
            st_ino=status.st_ino + 1,
        ),
    )

    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._verify_private_directory(str(directory), create=False)

    assert captured.value.code is ArtifactErrorCode.PATH_INVALID


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (FileExistsError(), ArtifactErrorCode.BUSY),
        (OSError(), ArtifactErrorCode.IO_FAILED),
    ],
)
def test_partial_open_race_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: OSError,
    code: ArtifactErrorCode,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    original_open = os.open

    def _race_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == f"{_JOB_ID}.apk.part":
            raise failure
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _race_open)
    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._prepare_destination(
            str(directory),
            _JOB_ID,
            len(_BODY),
            hashlib.sha256(_BODY).hexdigest(),
        )
    assert captured.value.code is code


def test_invalid_new_partial_is_closed_and_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)

    def _reject_file(_status: os.stat_result) -> None:
        raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)

    monkeypatch.setattr(install_artifacts, "_require_private_file", _reject_file)
    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._prepare_destination(
            str(directory),
            _JOB_ID,
            len(_BODY),
            hashlib.sha256(_BODY).hexdigest(),
        )

    assert captured.value.code is ArtifactErrorCode.PATH_INVALID
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()


def test_promotion_requires_exact_partial_and_absent_ready(tmp_path: Path) -> None:
    missing_directory = tmp_path.joinpath("missing")
    with pytest.raises(ArtifactCustodyError) as missing_directory_error:
        install_artifacts._promote_partial(
            str(missing_directory), _JOB_ID, len(_BODY), "0" * 64
        )
    assert missing_directory_error.value.code is ArtifactErrorCode.IO_FAILED

    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    with pytest.raises(ArtifactCustodyError) as missing_partial_error:
        install_artifacts._promote_partial(
            str(directory), _JOB_ID, len(_BODY), "0" * 64
        )
    assert missing_partial_error.value.code is ArtifactErrorCode.IO_FAILED

    partial = directory.joinpath(f"{_JOB_ID}.apk.part")
    ready = directory.joinpath(f"{_JOB_ID}.apk")
    for path in (partial, ready):
        path.write_bytes(_BODY)
        path.chmod(0o600)
    with pytest.raises(ArtifactCustodyError) as existing_ready_error:
        install_artifacts._promote_partial(
            str(directory),
            _JOB_ID,
            len(_BODY),
            hashlib.sha256(_BODY).hexdigest(),
        )
    assert existing_ready_error.value.code is ArtifactErrorCode.PATH_INVALID


def test_replace_failure_keeps_partial_and_returns_io_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    partial = directory.joinpath(f"{_JOB_ID}.apk.part")
    partial.write_bytes(_BODY)
    partial.chmod(0o600)

    def _raise_replace(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("private replacement failure")

    monkeypatch.setattr(os, "replace", _raise_replace)
    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._promote_partial(
            str(directory),
            _JOB_ID,
            len(_BODY),
            hashlib.sha256(_BODY).hexdigest(),
        )
    assert captured.value.code is ArtifactErrorCode.IO_FAILED
    assert partial.exists()


async def test_directory_fsync_failure_removes_promoted_ready_and_allows_retry(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync

    def _fail_directory_fsync(file_fd: int) -> None:
        if stat.S_ISDIR(os.fstat(file_fd).st_mode):
            raise OSError("private directory durability failure")
        original_fsync(file_fd)

    monkeypatch.setattr(os, "fsync", _fail_directory_fsync)
    await _assert_error(
        ArtifactErrorCode.IO_FAILED,
        fake_hass,
        _FakeSession([_FakeResponse()]),
    )
    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk").exists()
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()

    monkeypatch.undo()
    result = await async_download_install_artifact(
        fake_hass, _FakeSession([_FakeResponse()]), _release(), _JOB_ID
    )
    assert await asyncio.to_thread(Path(result.path).read_bytes) == _BODY


async def test_final_rehash_failure_removes_corrupt_ready_and_allows_retry(
    fake_hass: _FakeHass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = os.replace

    def _replace_then_corrupt(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        file_fd = os.open(destination, os.O_WRONLY, dir_fd=dst_dir_fd)
        try:
            os.write(file_fd, b"X" * len(_BODY))
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

    monkeypatch.setattr(os, "replace", _replace_then_corrupt)
    await _assert_error(
        ArtifactErrorCode.READY_INVALID,
        fake_hass,
        _FakeSession([_FakeResponse()]),
    )
    directory = _custody_directory(fake_hass)
    assert not directory.joinpath(f"{_JOB_ID}.apk").exists()
    assert not directory.joinpath(f"{_JOB_ID}.apk.part").exists()

    monkeypatch.undo()
    result = await async_download_install_artifact(
        fake_hass, _FakeSession([_FakeResponse()]), _release(), _JOB_ID
    )
    assert await asyncio.to_thread(Path(result.path).read_bytes) == _BODY


@pytest.mark.parametrize("operation", ["unlink", "fsync"])
def test_cleanup_io_failures_are_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    directory = tmp_path.joinpath("private")
    directory.mkdir(mode=0o700)
    partial = directory.joinpath(f"{_JOB_ID}.apk.part")
    partial.write_bytes(_BODY)
    partial.chmod(0o600)

    def _raise_oserror(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("private cleanup failure")

    monkeypatch.setattr(os, operation, _raise_oserror)
    with pytest.raises(ArtifactCustodyError) as captured:
        install_artifacts._remove_owned_files(str(directory), _JOB_ID, False)
    assert captured.value.code is ArtifactErrorCode.IO_FAILED
