#!/usr/bin/env python3
"""Run restart-negative checks in two disposable Home Assistant processes."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from contextlib import AbstractContextManager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_STATUS_RESPONSE_BYTES = 64 * 1024
_ALLOWED_NETWORKS = (
    ipaddress.IPv4Network((0x0A000000, 8)),
    ipaddress.IPv4Network((0xAC100000, 12)),
    ipaddress.IPv4Network((0xC0A80000, 16)),
)


class HarnessError(RuntimeError):
    """A reproducible runtime assertion failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validate_candidate(repository: Path, expected_sha: str) -> None:
    _require(
        len(expected_sha) == 40
        and all(character in "0123456789abcdef" for character in expected_sha),
        "--expected-sha must be one full lowercase commit SHA",
    )
    actual = _run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
    _require(actual == expected_sha, f"HEAD {actual} is not expected {expected_sha}")
    production_diff = _run(
        ["git", "status", "--porcelain", "--", "custom_components/ha_paneld"],
        cwd=repository,
    ).stdout
    _require(not production_diff, "production integration has uncommitted changes")


def _component_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        _require(not path.is_symlink(), f"component contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        body = path.read_bytes()
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _private_local_address() -> str:
    candidates: set[str] = set()
    try:
        for result in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            candidates.add(result[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((str(ipaddress.IPv4Address(0xC0000201)), 9))
        candidates.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    for candidate in sorted(candidates):
        address = ipaddress.ip_address(candidate)
        if any(address in network for network in _ALLOWED_NETWORKS):
            check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                check.bind((candidate, 0))
            except OSError:
                continue
            finally:
                check.close()
            return candidate
    raise HarnessError("no bindable local RFC1918 address is available")


class _PanelRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[str, str, int]] = []

    def record(self, path: str, mode: str, size: int) -> None:
        with self._lock:
            self._events.append((path, mode, size))

    def snapshot(self) -> tuple[tuple[str, str, int], ...]:
        with self._lock:
            return tuple(self._events)


class _PanelServer(AbstractContextManager["_PanelServer"]):
    def __init__(self, host: str, mode_path: Path) -> None:
        self.recorder = _PanelRecorder()
        recorder = self.recorder

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/v1/health":
                    mode = "health"
                    body = (
                        b"ha-paneld 0.1.0 panel=runtime_negative build=100 "
                        b"cfg=1a2b3c4d ha=normal ha_src=mqtt\n"
                    )
                elif self.path == "/api/v1/status":
                    mode = mode_path.read_text(encoding="ascii").strip()
                    if mode == "valid":
                        body = b'{"warnings":[],"capabilities":[]}\n'
                    elif mode == "malformed":
                        body = b'{"warnings":{},"capabilities":[]}\n'
                    elif mode == "oversized":
                        body = json.dumps(
                            {
                                "warnings": [],
                                "capabilities": [],
                                "future": "x" * MAX_STATUS_RESPONSE_BYTES,
                            },
                            separators=(",", ":"),
                        ).encode("ascii")
                    else:
                        body = b"invalid test mode"
                        self.send_response(500)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        recorder.record(self.path, mode, len(body))
                        return
                else:
                    body = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    recorder.record(self.path, "unexpected", len(body))
                    return
                recorder.record(self.path, mode, len(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with suppress(BrokenPipeError):
                    self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ha-paneld-runtime-http",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> _PanelServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        _require(not self._thread.is_alive(), "fake panel thread did not stop")


class _AdbSentinel(AbstractContextManager["_AdbSentinel"]):
    def __init__(self, host: str) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind((host, 5555))
        except OSError as err:
            self._socket.close()
            raise HarnessError(f"cannot reserve local ADB sentinel: {err}") from err
        self._socket.listen()
        self._socket.settimeout(0.1)
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._accepts = 0
        self._thread = threading.Thread(
            target=self._listen,
            name="ha-paneld-runtime-adb",
            daemon=True,
        )

    def _listen(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _address = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._accepts += 1
            connection.close()

    @property
    def accepts(self) -> int:
        with self._lock:
            return self._accepts

    def __enter__(self) -> _AdbSentinel:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stopping.set()
        self._thread.join(timeout=2)
        self._socket.close()
        _require(not self._thread.is_alive(), "ADB sentinel thread did not stop")


def _write_configuration(config: Path) -> None:
    (config / "configuration.yaml").write_text(
        """homeassistant:
  name: ha-paneld negative runtime

logger:
  default: warning
  logs:
    custom_components.ha_paneld: debug
    custom_components.ha_paneld_runtime_probe: debug

ha_paneld_runtime_probe:
""",
        encoding="ascii",
    )


def _copy_candidate_component(
    repository: Path,
    destination: Path,
    expected_sha: str,
) -> None:
    prefix = Path("custom_components/ha_paneld")
    listing = _run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            expected_sha,
            "--",
            prefix.as_posix(),
        ],
        cwd=repository,
    ).stdout.splitlines()
    _require(bool(listing), "candidate integration has no tracked files")
    for name in listing:
        source = repository / name
        _require(source.is_file() and not source.is_symlink(), f"unsafe source: {name}")
        target = destination / Path(name).relative_to(prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _prepare_config(
    repository: Path,
    root: Path,
    expected_sha: str,
) -> tuple[Path, str]:
    config = root / "config"
    components = config / "custom_components"
    components.mkdir(parents=True)
    source = repository / "custom_components" / "ha_paneld"
    destination = components / "ha_paneld"
    destination.mkdir()
    _copy_candidate_component(repository, destination, expected_sha)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
    probe_source = repository / "tests" / "runtime" / "probe_component"
    shutil.copytree(
        probe_source,
        components / "ha_paneld_runtime_probe",
        ignore=ignored,
    )
    _write_configuration(config)
    source_digest = _component_digest(source)
    _require(
        source_digest == _component_digest(destination),
        "copied integration differs from exact candidate",
    )
    return config, source_digest


def _run_core(
    hass: Path,
    config: Path,
    control: Path,
    stage: str,
    panel_address: str,
    ambiguous_address: str,
    log: Path,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "HA_PANELD_RUNTIME_STAGE": stage,
            "HA_PANELD_RUNTIME_CONTROL": str(control),
            "HA_PANELD_RUNTIME_PANEL_ADDRESS": panel_address,
            "HA_PANELD_RUNTIME_AMBIGUOUS_ADDRESS": ambiguous_address,
        }
    )
    with log.open("wb") as output:
        process = subprocess.Popen(
            [
                str(hass),
                "-c",
                str(config),
                "--skip-pip",
                "--log-file",
                str(log.with_suffix(".home-assistant.log")),
                "--log-no-color",
            ],
            cwd=config.parent,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=60)
        except subprocess.TimeoutExpired as err:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            raise HarnessError(f"Home Assistant {stage} process timed out") from err
    _require(return_code == 0, f"Home Assistant {stage} exited {return_code}")
    failure = control / f"{stage}-failure.json"
    if failure.exists():
        detail = failure.read_text(encoding="ascii")
        raise HarnessError(f"runtime probe failed: {detail}")
    _require((control / f"{stage}.json").is_file(), f"missing {stage} marker")


def _read_combined_log(root: Path, stage: str) -> str:
    parts = []
    for suffix in (".log", ".home-assistant.log"):
        path = root / f"{stage}{suffix}"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _validate_logs(root: Path) -> None:
    for stage in ("seed", "verify"):
        log = _read_combined_log(root, stage)
        _require(
            f"RUNTIME_PROBE_PASS stage={stage}" in log,
            f"missing probe pass in {stage} log",
        )
        bad_lines = [
            line
            for line in log.splitlines()
            if ("ERROR" in line or "CRITICAL" in line)
            and "custom_components.ha_paneld" in line
        ]
        _require(not bad_lines, f"ha-paneld logged an error in {stage}: {bad_lines}")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hass", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-home-assistant-version", required=True)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="retain the disposable runtime directory after a passing run",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    repository = _repository_root()
    runtime_root = Path(tempfile.mkdtemp(prefix="ha-paneld-core-negative-"))
    passed = False
    try:
        _validate_candidate(repository, arguments.expected_sha)
        hass = arguments.hass.resolve()
        _require(hass.is_file(), "hass executable does not exist")
        version = _run([str(hass), "--version"], cwd=repository).stdout.strip()
        _require(
            version == arguments.expected_home_assistant_version,
            "Home Assistant "
            f"{version} is not {arguments.expected_home_assistant_version}",
        )
        control = runtime_root / "control"
        control.mkdir(mode=0o700)
        mode_path = control / "status-mode"
        mode_path.write_text("valid\n", encoding="ascii")
        config, component_digest = _prepare_config(
            repository,
            runtime_root,
            arguments.expected_sha,
        )
        local_address = _private_local_address()

        with _PanelServer(local_address, mode_path) as panel:
            panel_address = f"{local_address}:{panel.port}"
            ambiguous_port = panel.port + 1 if panel.port < 65535 else panel.port - 1
            ambiguous_address = f"{local_address}:{ambiguous_port}"
            with _AdbSentinel(local_address) as adb:
                _run_core(
                    hass,
                    config,
                    control,
                    "seed",
                    panel_address,
                    ambiguous_address,
                    runtime_root / "seed.log",
                )
                mode_path.write_text("malformed\n", encoding="ascii")
                _run_core(
                    hass,
                    config,
                    control,
                    "verify",
                    panel_address,
                    ambiguous_address,
                    runtime_root / "verify.log",
                )
                _require(adb.accepts == 0, f"unexpected ADB connections: {adb.accepts}")
            events = panel.recorder.snapshot()

        modes = Counter(mode for path, mode, _size in events if path.endswith("status"))
        _require(modes["valid"] >= 1, "valid status fixture was not requested")
        _require(modes["malformed"] >= 1, "malformed status was not requested")
        _require(modes["oversized"] >= 1, "oversized status was not requested")
        oversized_sizes = [
            size
            for path, mode, size in events
            if path.endswith("status") and mode == "oversized"
        ]
        _require(
            all(size > MAX_STATUS_RESPONSE_BYTES for size in oversized_sizes),
            "oversized fixture did not cross the 64 KiB boundary",
        )
        _require(
            not any(mode == "unexpected" for _path, mode, _size in events),
            "Home Assistant requested an unexpected fake-panel route",
        )
        _validate_logs(runtime_root)
        verify = json.loads((control / "verify.json").read_text(encoding="ascii"))
        summary = {
            "candidate": arguments.expected_sha,
            "home_assistant": version,
            "component_digest": component_digest,
            "health_requests": sum(path.endswith("health") for path, _, _ in events),
            "status_modes": dict(sorted(modes.items())),
            "adb_connections": 0,
            "safe_phase": verify["safe_phase"],
            "ambiguous_phase": verify["ambiguous_phase"],
            "registry_identity_preserved": True,
        }
        print(json.dumps(summary, sort_keys=True))
        passed = True
        return 0
    except Exception as err:
        print(f"FAIL: {err}", file=sys.stderr)
        print(f"Retained runtime evidence: {runtime_root}", file=sys.stderr)
        return 1
    finally:
        if passed and not arguments.keep:
            shutil.rmtree(runtime_root)
        elif passed:
            print(f"Retained runtime evidence: {runtime_root}")


if __name__ == "__main__":
    raise SystemExit(main())
