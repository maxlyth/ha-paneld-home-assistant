"""Private, authenticated custody for one exact installer APK."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, cast

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from homeassistant.core import HomeAssistant
from yarl import URL

from .const import DOMAIN
from .release import ReleaseArtifact

_MAX_APK_BYTES = 64 * 1024 * 1024
_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRUSTED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
    }
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_OVERALL_TIMEOUT_SECONDS = 180.0
_REQUEST_TIMEOUT_SECONDS = 120.0
_CONNECT_TIMEOUT_SECONDS = 10.0
_READ_TIMEOUT_SECONDS = 30.0
_STREAM_CHUNK_BYTES = 64 * 1024
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_STORAGE_DIRECTORY = f"{DOMAIN}.install_artifacts"
_DOWNLOAD_HEADERS = {
    "Accept": "application/octet-stream",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
}

_MISSING = object()


class _WorkerState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    ABORTED = "aborted"


class ArtifactErrorCode(StrEnum):
    """Stable, privacy-safe artifact custody failures."""

    DESCRIPTOR_REQUIRED = "descriptor_required"
    CONTRACT_INVALID = "artifact_contract_invalid"
    JOB_ID_INVALID = "artifact_job_id_invalid"
    PATH_INVALID = "artifact_path_invalid"
    READY_INVALID = "artifact_ready_invalid"
    BUSY = "artifact_busy"
    REDIRECT_INVALID = "artifact_redirect_invalid"
    HTTP_STATUS = "artifact_http_status"
    RESPONSE_INVALID = "artifact_response_invalid"
    TOO_LARGE = "artifact_too_large"
    SIZE_MISMATCH = "artifact_size_mismatch"
    DIGEST_MISMATCH = "artifact_digest_mismatch"
    TIMEOUT = "artifact_timeout"
    DOWNLOAD_FAILED = "artifact_download_failed"
    IO_FAILED = "artifact_io_failed"


class ArtifactCustodyError(Exception):
    """Raise one stable code without embedding URLs, paths, or response data."""

    def __init__(self, code: ArtifactErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, repr=False, slots=True)
class InstallArtifact:
    """A verified ready file held for one durable installation job."""

    job_id: str
    path: str
    size: int
    sha256: str


@dataclass(slots=True)
class _WorkerOutcome[T]:
    value: T | object = _MISSING
    error: BaseException | None = None
    state: _WorkerState = _WorkerState.QUEUED
    lock: Lock = field(default_factory=Lock, repr=False)


@dataclass(slots=True)
class _WorkerOperation[T]:
    completion: asyncio.Event
    outcome: _WorkerOutcome[T]
    executor_future: asyncio.Future[None]


def _artifact_directory(hass: HomeAssistant) -> str:
    """Return the private path outside the HACS-replaced component directory."""
    return hass.config.path(".storage", _STORAGE_DIRECTORY)


def _request_timeout() -> ClientTimeout:
    """Return fixed connection, read, and per-request bounds."""
    return ClientTimeout(
        total=_REQUEST_TIMEOUT_SECONDS,
        connect=_CONNECT_TIMEOUT_SECONDS,
        sock_read=_READ_TIMEOUT_SECONDS,
    )


def _ready_name(job_id: str) -> str:
    return f"{job_id}.apk"


def _partial_name(job_id: str) -> str:
    return f"{job_id}.apk.part"


def _validate_job_id(job_id: object) -> str:
    if not isinstance(job_id, str) or _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ArtifactCustodyError(ArtifactErrorCode.JOB_ID_INVALID)
    return job_id


def _trusted_download_url(raw_url: object) -> URL:
    if not isinstance(raw_url, str) or not raw_url:
        raise ArtifactCustodyError(ArtifactErrorCode.CONTRACT_INVALID)
    try:
        url = URL(raw_url)
    except TypeError, ValueError:
        raise ArtifactCustodyError(ArtifactErrorCode.CONTRACT_INVALID) from None
    if (
        url.scheme != "https"
        or url.user is not None
        or url.password is not None
        or url.host not in _TRUSTED_DOWNLOAD_HOSTS
        or url.port != 443
        or bool(url.fragment)
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.CONTRACT_INVALID)
    return url


def _validate_contract(artifact: ReleaseArtifact) -> tuple[int, str, URL]:
    descriptor = artifact.descriptor
    if descriptor is None:
        raise ArtifactCustodyError(ArtifactErrorCode.DESCRIPTOR_REQUIRED)
    if (
        isinstance(descriptor.apk_size, bool)
        or not isinstance(descriptor.apk_size, int)
        or not 1 <= descriptor.apk_size <= _MAX_APK_BYTES
        or not isinstance(descriptor.apk_sha256, str)
        or _SHA256_PATTERN.fullmatch(descriptor.apk_sha256) is None
        or descriptor.apk_name != artifact.apk_name
        or descriptor.apk_sha256 != artifact.sha256
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.CONTRACT_INVALID)
    return (
        descriptor.apk_size,
        descriptor.apk_sha256,
        _trusted_download_url(artifact.apk_url),
    )


def _raise_io() -> None:
    raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED) from None


def _close_file(file_fd: int) -> None:
    try:
        os.close(file_fd)
    except OSError:
        _raise_io()


def _verify_private_directory(directory: str, *, create: bool) -> int | None:
    """Open a non-symlink, owner-only integration directory."""
    try:
        if create:
            with suppress(FileExistsError):
                os.mkdir(directory, _DIRECTORY_MODE)
        elif not os.path.lexists(directory):
            return None

        path_status = os.lstat(directory)
        if (
            not stat.S_ISDIR(path_status.st_mode)
            or stat.S_IMODE(path_status.st_mode) != _DIRECTORY_MODE
            or path_status.st_uid != os.geteuid()
        ):
            raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_status = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(opened_status.st_mode)
            or stat.S_IMODE(opened_status.st_mode) != _DIRECTORY_MODE
            or opened_status.st_uid != os.geteuid()
            or (opened_status.st_dev, opened_status.st_ino)
            != (path_status.st_dev, path_status.st_ino)
        ):
            _close_file(directory_fd)
            raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
        return directory_fd
    except ArtifactCustodyError:
        raise
    except OSError:
        _raise_io()


def _entry_status(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _raise_io()


def _require_private_file(status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != _FILE_MODE
        or status.st_uid != os.geteuid()
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)


def _hash_open_file(
    directory_fd: int,
    name: str,
    *,
    expected_size: int,
    expected_sha256: str,
    invalid_code: ArtifactErrorCode,
) -> None:
    """Reopen and hash a private file without following a link."""
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError:
        _raise_io()
    try:
        opened_status = os.fstat(file_fd)
        _require_private_file(opened_status)
        if opened_status.st_size != expected_size:
            raise ArtifactCustodyError(invalid_code)
        digest = hashlib.sha256()
        actual_size = 0
        while chunk := os.read(file_fd, _STREAM_CHUNK_BYTES):
            actual_size += len(chunk)
            if actual_size > _MAX_APK_BYTES or actual_size > expected_size:
                raise ArtifactCustodyError(invalid_code)
            digest.update(chunk)
        if actual_size != expected_size or digest.hexdigest() != expected_sha256:
            raise ArtifactCustodyError(invalid_code)
    except ArtifactCustodyError:
        raise
    except OSError:
        _raise_io()
    finally:
        _close_file(file_fd)


def _discard_open_partial(
    directory_fd: int,
    part_name: str,
    partial_fd: int,
) -> None:
    """Close and unlink a newly opened partial, even if close itself reports failure."""
    try:
        opened_status = os.fstat(partial_fd)
    except OSError:
        _raise_io()
    close_error: ArtifactCustodyError | None = None
    try:
        _close_file(partial_fd)
    except ArtifactCustodyError as err:
        close_error = err
    part_status = _entry_status(directory_fd, part_name)
    if part_status is not None:
        if (part_status.st_dev, part_status.st_ino) != (
            opened_status.st_dev,
            opened_status.st_ino,
        ):
            raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
        try:
            os.unlink(part_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            _raise_io()
    if close_error is not None:
        raise close_error


def _reopen_same_directory(
    directory: str,
    expected_identity: tuple[int, int],
) -> int:
    """Open a fresh directory descriptor bound to the pre-close inode."""
    directory_fd = _verify_private_directory(directory, create=False)
    if directory_fd is None:
        raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
    try:
        reopened_status = os.fstat(directory_fd)
    except OSError:
        with suppress(ArtifactCustodyError):
            _close_file(directory_fd)
        _raise_io()
    if (reopened_status.st_dev, reopened_status.st_ino) != expected_identity:
        _close_file(directory_fd)
        raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
    return directory_fd


def _prepare_destination(
    directory: str,
    job_id: str,
    expected_size: int,
    expected_sha256: str,
) -> int | None:
    """Return a new partial-file descriptor, or None for valid ready reuse."""
    directory_fd = _verify_private_directory(directory, create=True)
    if directory_fd is None:
        raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
    try:
        directory_status = os.fstat(directory_fd)
    except OSError:
        _close_file(directory_fd)
        _raise_io()
    directory_identity = (directory_status.st_dev, directory_status.st_ino)
    ready_name = _ready_name(job_id)
    part_name = _partial_name(job_id)
    prepared_fd: int | None = None
    try:
        ready_status = _entry_status(directory_fd, ready_name)
        part_status = _entry_status(directory_fd, part_name)
        if ready_status is not None:
            _require_private_file(ready_status)
            if part_status is not None:
                raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
            _hash_open_file(
                directory_fd,
                ready_name,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                invalid_code=ArtifactErrorCode.READY_INVALID,
            )
            return None
        if part_status is not None:
            _require_private_file(part_status)
            raise ArtifactCustodyError(ArtifactErrorCode.BUSY)
        try:
            prepared_fd = os.open(
                part_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                _FILE_MODE,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            raise ArtifactCustodyError(ArtifactErrorCode.BUSY) from None
        except OSError:
            _raise_io()
        try:
            _require_private_file(os.fstat(prepared_fd))
        except BaseException:
            _discard_open_partial(directory_fd, part_name, prepared_fd)
            prepared_fd = None
            raise
        return prepared_fd
    finally:
        try:
            _close_file(directory_fd)
        except ArtifactCustodyError:
            if prepared_fd is not None:
                try:
                    cleanup_directory_fd = _reopen_same_directory(
                        directory, directory_identity
                    )
                except ArtifactCustodyError:
                    with suppress(ArtifactCustodyError):
                        _close_file(prepared_fd)
                else:
                    try:
                        _discard_open_partial(
                            cleanup_directory_fd, part_name, prepared_fd
                        )
                    finally:
                        _close_file(cleanup_directory_fd)
            raise


def _write_all(file_fd: int, body: bytes) -> None:
    view = memoryview(body)
    try:
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                _raise_io()
            view = view[written:]
    except ArtifactCustodyError:
        raise
    except OSError:
        _raise_io()


def _finish_file(file_fd: int) -> None:
    try:
        os.fsync(file_fd)
    except OSError:
        _raise_io()
    finally:
        _close_file(file_fd)


def _promote_partial(
    directory: str,
    job_id: str,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Re-hash the closed partial file, then atomically publish it as ready."""
    directory_fd = _verify_private_directory(directory, create=False)
    if directory_fd is None:
        raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
    ready_name = _ready_name(job_id)
    part_name = _partial_name(job_id)
    promoted = False
    try:
        part_status = _entry_status(directory_fd, part_name)
        if part_status is None:
            raise ArtifactCustodyError(ArtifactErrorCode.IO_FAILED)
        _require_private_file(part_status)
        if _entry_status(directory_fd, ready_name) is not None:
            raise ArtifactCustodyError(ArtifactErrorCode.PATH_INVALID)
        _hash_open_file(
            directory_fd,
            part_name,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            invalid_code=ArtifactErrorCode.DIGEST_MISMATCH,
        )
        try:
            os.replace(
                part_name,
                ready_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            promoted = True
            os.fsync(directory_fd)
        except OSError:
            _raise_io()
        _hash_open_file(
            directory_fd,
            ready_name,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            invalid_code=ArtifactErrorCode.READY_INVALID,
        )
    except BaseException:
        if promoted:
            ready_status = _entry_status(directory_fd, ready_name)
            if ready_status is not None:
                _require_private_file(ready_status)
                try:
                    os.unlink(ready_name, dir_fd=directory_fd)
                except OSError:
                    _raise_io()
                # The original durability failure can make a second directory
                # fsync fail too. The name must still be removed before retry.
                with suppress(OSError):
                    os.fsync(directory_fd)
        raise
    finally:
        _close_file(directory_fd)


def _remove_owned_files(
    directory: str,
    job_id: str,
    ready: bool,
) -> None:
    directory_fd = _verify_private_directory(directory, create=False)
    if directory_fd is None:
        return
    names = [_partial_name(job_id)]
    if ready:
        names.append(_ready_name(job_id))
    changed = False
    try:
        for name in names:
            file_status = _entry_status(directory_fd, name)
            if file_status is None:
                continue
            _require_private_file(file_status)
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                _raise_io()
            changed = True
        if changed:
            try:
                os.fsync(directory_fd)
            except OSError:
                _raise_io()
    finally:
        _close_file(directory_fd)


def _abandon_partial(directory: str, job_id: str, file_fd: int | None) -> None:
    """Close and unlink one partial in the same worker-owned operation."""
    close_error: ArtifactCustodyError | None = None
    if file_fd is not None:
        try:
            _close_file(file_fd)
        except ArtifactCustodyError as err:
            close_error = err
    _remove_owned_files(directory, job_id, False)
    if close_error is not None:
        raise close_error


def _run_worker[T](
    target: Callable[..., T],
    args: tuple[Any, ...],
    outcome: _WorkerOutcome[T],
    loop: asyncio.AbstractEventLoop,
    completion: asyncio.Event,
) -> None:
    """Record actual worker completion independently of its asyncio Future."""
    with outcome.lock:
        if outcome.state is _WorkerState.ABORTED:
            return
        if outcome.state is not _WorkerState.QUEUED:
            outcome.error = RuntimeError()
            outcome.state = _WorkerState.FINISHED
            loop.call_soon_threadsafe(completion.set)
            return
        outcome.state = _WorkerState.RUNNING
    try:
        value = target(*args)
    except BaseException as err:
        with outcome.lock:
            outcome.error = err
            outcome.state = _WorkerState.FINISHED
    else:
        with outcome.lock:
            outcome.value = value
            outcome.state = _WorkerState.FINISHED
    loop.call_soon_threadsafe(completion.set)


def _abort_queued_worker(
    future: asyncio.Future[None],
    outcome: _WorkerOutcome[Any],
    completion: asyncio.Event,
) -> None:
    """Arbitrate Future cancellation against the worker's start claim."""
    if not future.cancelled():
        return
    with outcome.lock:
        if outcome.state is not _WorkerState.QUEUED:
            return
        outcome.state = _WorkerState.ABORTED
        outcome.error = asyncio.CancelledError()
    completion.set()


def _start_worker[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: Any,
) -> _WorkerOperation[T]:
    completion = asyncio.Event()
    outcome: _WorkerOutcome[T] = _WorkerOutcome()
    executor_future = hass.async_add_executor_job(
        _run_worker,
        target,
        args,
        outcome,
        asyncio.get_running_loop(),
        completion,
    )
    executor_future.add_done_callback(
        lambda future: _abort_queued_worker(future, outcome, completion)
    )
    return _WorkerOperation(
        completion=completion,
        outcome=outcome,
        executor_future=executor_future,
    )


def _worker_result[T](operation: _WorkerOperation[T]) -> T:
    with operation.outcome.lock:
        error = operation.outcome.error
        value = operation.outcome.value
        state = operation.outcome.state
    if error is not None:
        raise error
    if state is not _WorkerState.FINISHED or value is _MISSING:
        raise RuntimeError
    return cast(T, value)


def _worker_state(operation: _WorkerOperation[Any]) -> _WorkerState:
    with operation.outcome.lock:
        return operation.outcome.state


async def _async_wait_for_worker(operation: _WorkerOperation[Any]) -> bool:
    """Wait for the worker's signal and record every caller cancellation."""
    completion = asyncio.create_task(operation.completion.wait())
    cancelled = False
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            cancelled = True
    completion.result()
    return cancelled


async def _async_executor[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: Any,
) -> T:
    """Keep a started blocking operation alive until it is safe to clean up."""
    operation = _start_worker(hass, target, *args)
    cancelled = await _async_wait_for_worker(operation)
    if cancelled:
        raise asyncio.CancelledError
    return _worker_result(operation)


async def _async_cleanup_executor(
    hass: HomeAssistant,
    directory: str,
    job_id: str,
    file_fd: int | None,
) -> None:
    """Run abandonment once, resubmitting only a queued cancellation loser."""
    cancelled = False
    while True:
        operation = _start_worker(hass, _abandon_partial, directory, job_id, file_fd)
        cancelled = await _async_wait_for_worker(operation) or cancelled
        if _worker_state(operation) is _WorkerState.ABORTED:
            continue
        if cancelled:
            raise asyncio.CancelledError
        _worker_result(operation)
        return


async def _async_prepare_destination(
    hass: HomeAssistant,
    directory: str,
    job_id: str,
    expected_size: int,
    expected_sha256: str,
) -> int | None:
    """Prepare custody without leaking an open partial file on cancellation."""
    operation = _start_worker(
        hass,
        _prepare_destination,
        directory,
        job_id,
        expected_size,
        expected_sha256,
    )
    cancelled = await _async_wait_for_worker(operation)
    if not cancelled:
        return _worker_result(operation)

    file_fd: int | None = None
    with suppress(Exception):
        file_fd = _worker_result(operation)
    with suppress(Exception):
        await _async_cleanup_executor(hass, directory, job_id, file_fd)
    raise asyncio.CancelledError


def _declared_length(response: ClientResponse) -> int | None:
    try:
        values = response.headers.getall("Content-Length", ())
        if len(values) > 1:
            raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
        if values:
            value = values[0]
            if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
                raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
            parsed = int(value)
            if response.content_length != parsed:
                raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
            return parsed
        content_length = response.content_length
        if content_length is None:
            return None
        if isinstance(content_length, bool) or not isinstance(content_length, int):
            raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
        return content_length
    except ArtifactCustodyError:
        raise
    except TypeError, ValueError:
        raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID) from None


def _validate_response_headers(response: ClientResponse, expected_size: int) -> None:
    if response.headers.getall("Content-Range", ()):
        raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
    encodings = response.headers.getall("Content-Encoding", ())
    if len(encodings) > 1 or (
        encodings
        and (
            not isinstance(encodings[0], str)
            or encodings[0].strip().casefold() != "identity"
        )
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.RESPONSE_INVALID)
    declared_length = _declared_length(response)
    if declared_length is not None and declared_length != expected_size:
        raise ArtifactCustodyError(ArtifactErrorCode.SIZE_MISMATCH)


def _validated_redirect(current_url: URL, response: ClientResponse) -> URL:
    locations = response.headers.getall("Location", ())
    if len(locations) != 1:
        raise ArtifactCustodyError(ArtifactErrorCode.REDIRECT_INVALID)
    location = locations[0]
    if (
        not isinstance(location, str)
        or not location
        or location != location.strip()
        or any(ord(character) < 32 for character in location)
        or "\x7f" in location
    ):
        raise ArtifactCustodyError(ArtifactErrorCode.REDIRECT_INVALID)
    try:
        next_url = current_url.join(URL(location))
    except TypeError, ValueError:
        raise ArtifactCustodyError(ArtifactErrorCode.REDIRECT_INVALID) from None
    try:
        return _trusted_download_url(str(next_url))
    except ArtifactCustodyError:
        raise ArtifactCustodyError(ArtifactErrorCode.REDIRECT_INVALID) from None


async def _async_abandon_partial(
    hass: HomeAssistant,
    directory: str,
    job_id: str,
    file_fd: int | None,
) -> None:
    await _async_cleanup_executor(hass, directory, job_id, file_fd)


async def async_download_install_artifact(
    hass: HomeAssistant,
    session: ClientSession,
    artifact: ReleaseArtifact,
    job_id: str,
) -> InstallArtifact:
    """Download, verify, and privately publish one exact signed APK."""
    validated_job_id = _validate_job_id(job_id)
    expected_size, expected_sha256, initial_url = _validate_contract(artifact)
    directory = _artifact_directory(hass)
    ready_path = str(Path(directory, _ready_name(validated_job_id)))
    file_fd = await _async_prepare_destination(
        hass,
        directory,
        validated_job_id,
        expected_size,
        expected_sha256,
    )
    if file_fd is None:
        return InstallArtifact(
            job_id=validated_job_id,
            path=ready_path,
            size=expected_size,
            sha256=expected_sha256,
        )

    current_url = initial_url
    redirects = 0
    actual_size = 0
    streamed_digest = hashlib.sha256()
    try:
        async with asyncio.timeout(_OVERALL_TIMEOUT_SECONDS):
            while True:
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    auto_decompress=False,
                    headers=_DOWNLOAD_HEADERS,
                    timeout=_request_timeout(),
                ) as response:
                    if response.history or response.url != current_url:
                        raise ArtifactCustodyError(ArtifactErrorCode.REDIRECT_INVALID)
                    if response.status in _REDIRECT_STATUSES:
                        if redirects >= _MAX_REDIRECTS:
                            raise ArtifactCustodyError(
                                ArtifactErrorCode.REDIRECT_INVALID
                            )
                        current_url = _validated_redirect(current_url, response)
                        redirects += 1
                        continue
                    if response.status != 200:
                        raise ArtifactCustodyError(ArtifactErrorCode.HTTP_STATUS)
                    _validate_response_headers(response, expected_size)
                    async for chunk in response.content.iter_chunked(
                        _STREAM_CHUNK_BYTES
                    ):
                        if not isinstance(chunk, bytes):
                            raise ArtifactCustodyError(
                                ArtifactErrorCode.RESPONSE_INVALID
                            )
                        next_size = actual_size + len(chunk)
                        if next_size > _MAX_APK_BYTES:
                            raise ArtifactCustodyError(ArtifactErrorCode.TOO_LARGE)
                        if next_size > expected_size:
                            raise ArtifactCustodyError(ArtifactErrorCode.SIZE_MISMATCH)
                        await _async_executor(hass, _write_all, file_fd, chunk)
                        streamed_digest.update(chunk)
                        actual_size = next_size
                    break
        if actual_size != expected_size:
            raise ArtifactCustodyError(ArtifactErrorCode.SIZE_MISMATCH)
        if streamed_digest.hexdigest() != expected_sha256:
            raise ArtifactCustodyError(ArtifactErrorCode.DIGEST_MISMATCH)
        try:
            await _async_executor(hass, _finish_file, file_fd)
        finally:
            file_fd = None
        await _async_executor(
            hass,
            _promote_partial,
            directory,
            validated_job_id,
            expected_size,
            expected_sha256,
        )
    except asyncio.CancelledError:
        await _async_abandon_partial(hass, directory, validated_job_id, file_fd)
        raise
    except ArtifactCustodyError:
        await _async_abandon_partial(hass, directory, validated_job_id, file_fd)
        raise
    except TimeoutError:
        await _async_abandon_partial(hass, directory, validated_job_id, file_fd)
        raise ArtifactCustodyError(ArtifactErrorCode.TIMEOUT) from None
    except ClientError, ValueError:
        await _async_abandon_partial(hass, directory, validated_job_id, file_fd)
        raise ArtifactCustodyError(ArtifactErrorCode.DOWNLOAD_FAILED) from None
    except BaseException:
        await _async_abandon_partial(hass, directory, validated_job_id, file_fd)
        raise

    return InstallArtifact(
        job_id=validated_job_id,
        path=ready_path,
        size=expected_size,
        sha256=expected_sha256,
    )


async def async_cleanup_install_artifact(
    hass: HomeAssistant,
    job_id: str,
) -> None:
    """Delete only the partial and ready files belonging to one exact job."""
    validated_job_id = _validate_job_id(job_id)
    await _async_executor(
        hass,
        _remove_owned_files,
        _artifact_directory(hass),
        validated_job_id,
        True,
    )
