"""Test-only probe loaded inside disposable Home Assistant Core processes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.panel_assistant.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.panel_assistant.install_executor import _execution_id
from custom_components.panel_assistant.install_jobs import (
    InstallArtifact,
    InstallJobManager,
    InstallJobReceipt,
    InstallPhase,
    InstallResultCode,
    InstallTarget,
    async_get_install_job_manager,
    install_plan_sha256,
)

DOMAIN = "panel_assistant_runtime_probe"
ENTRY_ID = "01M1K000000000000000000001"
VERSION = "0.1.0"
APK_BYTES = b"crash-held-apk"
APK_SHA256 = sha256(APK_BYTES).hexdigest()
ADB_CREDENTIAL_ID = "b" * 64
_LOGGER = logging.getLogger(__name__)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _control_directory() -> Path:
    value = os.environ.get("HA_PANELD_RUNTIME_CONTROL")
    if not value:
        raise RuntimeError("missing runtime control directory")
    return Path(value)


def _panel_address() -> str:
    value = os.environ.get("HA_PANELD_RUNTIME_PANEL_ADDRESS")
    if not value:
        raise RuntimeError("missing runtime panel address")
    return value


def _ambiguous_address() -> str:
    value = os.environ.get("HA_PANELD_RUNTIME_AMBIGUOUS_ADDRESS")
    if not value:
        raise RuntimeError("missing ambiguous runtime address")
    return value


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="ascii",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise RuntimeError("runtime control document is not an object")
    return value


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value, encoding="ascii")
    os.replace(temporary, path)


def _artifact() -> InstallArtifact:
    return InstallArtifact(
        descriptor_schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v0.1.0",
        version_name=VERSION,
        version_code=100,
        apk_name="ha-paneld-v0.1.0-manual-setup-required.apk",
        apk_sha256=APK_SHA256,
        apk_size=len(APK_BYTES),
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=(
            "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
        ),
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:1:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )


def _target(address: str, serial: str) -> InstallTarget:
    return InstallTarget(
        address=address,
        pinned_address=address,
        adb_serial=serial,
        model="Runtime Fixture",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )


async def _create_job(
    manager: InstallJobManager,
    target: InstallTarget,
) -> InstallJobReceipt:
    artifact = _artifact()
    receipt, created = await manager.async_create_or_join(
        target,
        artifact,
        install_plan_sha256(target, artifact, ADB_CREDENTIAL_ID),
        ADB_CREDENTIAL_ID,
    )
    _require(created, "runtime job unexpectedly joined existing state")
    return receipt


async def _advance_to(
    manager: InstallJobManager,
    receipt: InstallJobReceipt,
    selected_phase: InstallPhase,
) -> InstallJobReceipt:
    receipt = await manager.async_claim(receipt.job_id, receipt.revision)
    phases = (
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.STAGING,
        InstallPhase.INSTALLING,
        InstallPhase.INSTALLED,
        InstallPhase.LAUNCHING,
        InstallPhase.HEALTH_CHECK,
    )
    for phase in phases:
        fields: dict[str, object] = {}
        if phase is InstallPhase.DOWNLOADING:
            fields["preflight_root_mode"] = "rootless"
        elif phase is InstallPhase.ARTIFACT_READY:
            fields["actual_apk_bytes"] = len(APK_BYTES)
        receipt = await manager.async_transition(
            receipt.job_id,
            receipt.revision,
            phase,
            **fields,
        )
        if phase is selected_phase:
            return receipt
    raise RuntimeError("requested runtime phase was not reached")


def _registry_snapshot(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    entities = [
        entity
        for entity in er.async_get(hass).entities.values()
        if entity.config_entry_id == entry_id and entity.platform == "panel_assistant"
    ]
    _require(len(devices) == 1, "expected exactly one runtime device")
    _require(len(entities) == 2, "expected status and update runtime entities")
    sensor = next(entity for entity in entities if entity.domain == "sensor")
    update = next(entity for entity in entities if entity.domain == "update")
    state = hass.states.get(sensor.entity_id)
    _require(state is not None and state.state == "online", "sensor is not online")
    return {
        "entry_id": entry_id,
        "device_id": devices[0].id,
        "sensor_registry_id": sensor.id,
        "sensor_entity_id": sensor.entity_id,
        "update_registry_id": update.id,
        "update_entity_id": update.entity_id,
    }


def _write_custody(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("xb") as stream:
        stream.write(APK_BYTES)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


async def _seed(hass: HomeAssistant) -> dict[str, Any]:
    entry = ConfigEntry(
        data={CONF_ADDRESS: _panel_address()},
        discovery_keys=MappingProxyType({}),
        domain="panel_assistant",
        entry_id=ENTRY_ID,
        minor_version=1,
        options={},
        source=SOURCE_USER,
        subentries_data=(),
        title="runtime-negative",
        unique_id=None,
        version=1,
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    _require(entry.state is ConfigEntryState.LOADED, "seed entry did not load")
    registry = _registry_snapshot(hass, entry.entry_id)

    manager = await async_get_install_job_manager(hass)
    safe = await _create_job(
        manager,
        _target(_panel_address(), "RUNTIME-SAFE"),
    )
    safe = await _advance_to(manager, safe, InstallPhase.HEALTH_CHECK)
    ambiguous = await _create_job(
        manager,
        _target(_ambiguous_address(), "RUNTIME-AMBIGUOUS"),
    )
    ambiguous = await _advance_to(manager, ambiguous, InstallPhase.INSTALLING)
    custody_path = Path(
        hass.config.path(
            ".storage",
            "panel_assistant.install_artifacts",
            f"{_execution_id(ambiguous)}.apk",
        )
    )
    await hass.async_add_executor_job(_write_custody, custody_path)

    _require(safe.attempt == safe.executor_generation == 1, "bad safe seed claim")
    _require(
        ambiguous.attempt == ambiguous.executor_generation == 1,
        "bad ambiguous seed claim",
    )
    return {
        **registry,
        "safe_job_id": safe.job_id,
        "ambiguous_job_id": ambiguous.job_id,
        "ambiguous_execution_id": _execution_id(ambiguous),
        "custody_path": str(custody_path),
    }


async def _wait_for(
    reader: Callable[[], Any],
    predicate: Callable[[Any], bool],
    message: str,
) -> Any:
    try:
        async with asyncio.timeout(15):
            while True:
                value = await reader()
                if predicate(value):
                    return value
                await asyncio.sleep(0.05)
    except TimeoutError as err:
        raise RuntimeError(message) from err


async def _assert_invalid_status(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    expected: dict[str, Any],
) -> dict[str, str]:
    _require(entry.state is ConfigEntryState.LOADED, "runtime entry is not loaded")
    registry = _registry_snapshot(hass, entry.entry_id)
    for field in (
        "entry_id",
        "device_id",
        "sensor_registry_id",
        "sensor_entity_id",
        "update_registry_id",
        "update_entity_id",
    ):
        _require(
            registry[field] == expected[field],
            f"registry identity changed: {field}",
        )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _require(diagnostics["last_update_success"] is True, "health update failed")
    _require(diagnostics["status"] is None, "invalid status was retained")
    _require(
        diagnostics["status_error"] == "invalid_response",
        "invalid status was not classified safely",
    )
    return registry


async def _verify(hass: HomeAssistant) -> dict[str, Any]:
    control = _control_directory()
    expected = await hass.async_add_executor_job(
        _read_json,
        control / "seed.json",
    )
    entry = hass.config_entries.async_get_entry(ENTRY_ID)
    _require(entry is not None, "runtime config entry was not restored")
    await _assert_invalid_status(hass, entry, expected)

    manager = await async_get_install_job_manager(hass)
    safe = await _wait_for(
        lambda: manager.async_get(expected["safe_job_id"]),
        lambda receipt: (
            receipt.phase in {InstallPhase.HEALTHY_UNCLAIMED, InstallPhase.CONSUMED}
        ),
        "safe receipt was not replayed",
    )
    ambiguous = await _wait_for(
        lambda: manager.async_get(expected["ambiguous_job_id"]),
        lambda receipt: receipt.phase is InstallPhase.RECOVERY_REQUIRED,
        "ambiguous receipt was not quarantined",
    )
    _require(
        ambiguous.result_code is InstallResultCode.VERIFICATION_REQUIRED,
        "ambiguous receipt used the wrong result",
    )
    _require(
        ambiguous.attempt == ambiguous.executor_generation == 2,
        "ambiguous receipt was not claimed by the new process",
    )
    custody_path = Path(expected["custody_path"])
    custody_exists = await hass.async_add_executor_job(custody_path.exists)
    _require(not custody_exists, "ambiguous local custody survived quarantine")

    await hass.async_add_executor_job(
        _write_text,
        control / "status-mode",
        "oversized\n",
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(ENTRY_ID)
    _require(entry is not None, "runtime config entry vanished after reload")
    registry = await _assert_invalid_status(hass, entry, expected)

    safe = await _wait_for(
        lambda: manager.async_get(expected["safe_job_id"]),
        lambda receipt: receipt.phase is InstallPhase.CONSUMED,
        "safe receipt was not consumed by the existing entry",
    )
    _require(
        safe.result_code is InstallResultCode.ENTRY_CREATED,
        "safe receipt used the wrong result",
    )
    _require(safe.consumed_entry_id == ENTRY_ID, "receipt consumed by wrong entry")
    _require(safe.health_checked_at is not None, "safe replay omitted health proof")
    _require(
        safe.attempt == safe.executor_generation == 2,
        "safe receipt was not claimed by the new process",
    )
    return {
        **registry,
        "safe_phase": safe.phase.value,
        "safe_result": safe.result_code.value,
        "ambiguous_phase": ambiguous.phase.value,
        "ambiguous_result": ambiguous.result_code.value,
        "safe_attempt": safe.attempt,
        "ambiguous_attempt": ambiguous.attempt,
    }


async def _run_probe(hass: HomeAssistant, stage: str) -> None:
    control = _control_directory()
    output = control / f"{stage}.json"
    try:
        operation = _seed(hass) if stage == "seed" else _verify(hass)
        result = await asyncio.wait_for(operation, timeout=30)
        await hass.async_add_executor_job(_write_json, output, result)
        _LOGGER.warning("RUNTIME_PROBE_PASS stage=%s", stage)
    except Exception as err:
        await hass.async_add_executor_job(
            _write_json,
            control / f"{stage}-failure.json",
            {"error": f"{type(err).__name__}: {err}"},
        )
        _LOGGER.exception("RUNTIME_PROBE_FAIL stage=%s", stage)
    finally:
        hass.stop()


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register one bounded test operation after Home Assistant starts."""
    stage = os.environ.get("HA_PANELD_RUNTIME_STAGE")
    if stage not in {"seed", "verify"}:
        raise RuntimeError("invalid runtime probe stage")

    @callback
    def _started(_event: Event[Any]) -> None:
        hass.async_create_task(
            _run_probe(hass, stage),
            f"{DOMAIN} {stage}",
            eager_start=False,
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
    return True
