"""Config flow for ha-paneld."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .adb_credentials import (
    AdbCredentialError,
    async_get_adb_credential,
    async_get_adb_signer,
    async_get_durable_adb_credential,
)
from .browser_delivery import async_register_browser_delivery
from .browser_panel import PANEL_PATH, async_register_browser_panel
from .client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidAddressError,
    InvalidResponseError,
    PanelAddress,
    PanelHealth,
    normalize_address,
)
from .const import DEFAULT_PORT, DOMAIN
from .install_adb import (
    AdbInstallTarget,
    AdbRootMode,
    InstallAdbError,
    InstallAdbErrorCode,
    async_verify_installed_target,
)
from .install_executor import InstallExecutor, async_get_install_executor
from .install_jobs import (
    InstallJobConflictError,
    InstallJobError,
    InstallJobManager,
    InstallJobReceipt,
    InstallPhase,
    InstallResultCode,
    async_get_install_job_manager,
)
from .install_network import (
    InstallNetworkError,
    InstallNetworkErrorCode,
    PinnedPanelTarget,
    async_pin_install_target,
    async_revalidate_install_target,
)
from .install_plan import InstallPlanError, build_install_plan
from .provisioning import InstallTargetProbe, async_probe_install_target
from .release import (
    ReleaseArtifact,
    ReleaseResolutionError,
    async_resolve_rc_release,
    async_resolve_stable_release,
    is_rc_release_tag,
)

_LOGGER = logging.getLogger(__name__)

_PANEL_ACCESS_GUIDE_URL = (
    "https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware"
    "#gaining-adb--root-access"
)
_CANCELLED_ABORT_REASONS = {
    InstallResultCode.CANCELLED_BY_USER: "install_cancelled",
    InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP: (
        "install_cancelled_after_staging_cleanup"
    ),
}
_FAILED_ABORT_REASONS = {
    InstallResultCode.AUTHORIZATION_FAILED: "install_authorization_failed",
    InstallResultCode.PREFLIGHT_REJECTED: "install_preflight_rejected",
    InstallResultCode.ARTIFACT_REJECTED: "install_artifact_rejected",
    InstallResultCode.TRANSPORT_FAILED: "install_transport_failed",
    InstallResultCode.INSTALL_FAILED: "install_package_failed",
    InstallResultCode.LAUNCH_FAILED: "install_launch_failed",
    InstallResultCode.HEALTH_CHECK_FAILED: "install_health_check_failed",
}
_RECOVERY_ABORT_REASONS = {
    InstallResultCode.AMBIGUOUS_MUTATION: "install_ambiguous_mutation",
    InstallResultCode.VERIFICATION_REQUIRED: "install_verification_required",
}

_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_ADDRESS): TextSelector(TextSelectorConfig())}
)
_CONF_RELEASE_CANDIDATE = "release_candidate"


class HaPaneldConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an ha-paneld config flow."""

    VERSION = 1

    _pending_address: PanelAddress | None = None
    _pending_health: PanelHealth | None = None
    _pending_probe: InstallTargetProbe | None = None
    _pending_release: ReleaseArtifact | None = None
    _pending_rc_tag: str | None = None
    _pending_install_target: PinnedPanelTarget | None = None
    _pending_job_id: str | None = None
    _progress_waiter: asyncio.Task[InstallJobReceipt] | None = None
    _install_executor: InstallExecutor | None = None
    _finalizer_job_id: str | None = None
    _finalization_owner_task: asyncio.Task[Any] | None = None
    _release_after_finalization = False
    _finalizer_release_task: asyncio.Task[None] | None = None
    _removed_release_retry_started = False
    _flow_removed = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose whether to bootstrap or connect a panel."""
        # HA loads dependencies before the first flow, but domain async_setup
        # need not run until an entry exists. USB delivery must be ready now.
        async_register_browser_delivery(self.hass)
        await async_register_browser_panel(self.hass)
        return self.async_show_menu(
            step_id="user",
            menu_options=["install_usb", "install_or_upgrade", "connect_existing"],
        )

    async def async_step_install_usb(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link to browser-owned USB installation without creating an entry."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="install_usb",
            data_schema=vol.Schema({}),
            description_placeholders={"usb_install_url": f"/{PANEL_PATH}"},
        )

    async def async_step_connect_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect to a panel that is already running ha-paneld."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                address = normalize_address(user_input[CONF_ADDRESS])
                client = HaPaneldClient(async_get_clientsession(self.hass), address)
                health = await client.async_get_health()
            except InvalidAddressError:
                errors["base"] = "invalid_address"
            except CannotConnectError, InvalidResponseError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while validating ha-paneld")
                errors["base"] = "unknown"
            else:
                return self._async_create_panel_entry(address, health)

        return self.async_show_form(
            step_id="connect_existing",
            data_schema=self.add_suggested_values_to_schema(_DATA_SCHEMA, user_input),
            errors=errors,
        )

    def _show_install_address_form(
        self,
        user_input: dict[str, Any] | None,
        errors: dict[str, str],
    ) -> ConfigFlowResult:
        """Show the install form with its fixed trusted documentation link."""
        schema = _DATA_SCHEMA.extend(
            {vol.Optional(_CONF_RELEASE_CANDIDATE): TextSelector(TextSelectorConfig())}
        )
        return self.async_show_form(
            step_id="install_or_upgrade",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"panel_access_url": _PANEL_ACCESS_GUIDE_URL},
        )

    async def async_step_install_or_upgrade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Classify a panel before any installation or repair is attempted."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._pending_address = None
            self._pending_health = None
            self._pending_probe = None
            self._pending_release = None
            self._pending_rc_tag = None
            self._pending_install_target = None
            self._pending_job_id = None
            rc_tag = user_input.get(_CONF_RELEASE_CANDIDATE, "")
            if rc_tag != "":
                if not is_rc_release_tag(rc_tag):
                    return self._show_install_address_form(
                        user_input,
                        {_CONF_RELEASE_CANDIDATE: "invalid_release_candidate"},
                    )
                self._pending_rc_tag = rc_tag
            try:
                address = normalize_address(user_input[CONF_ADDRESS])
                if address.port != DEFAULT_PORT:
                    raise InvalidAddressError
                self._async_abort_entries_match({CONF_ADDRESS: address.stored_value})
            except InvalidAddressError:
                errors["base"] = "invalid_install_address"
            else:
                try:
                    target = await async_pin_install_target(self.hass, address)
                except InstallNetworkError as err:
                    errors["base"] = _install_network_error(err)
                except Exception:
                    _LOGGER.exception(
                        "Unexpected exception while pinning install target"
                    )
                    errors["base"] = "unknown"
                else:
                    self._pending_address = address
                    self._pending_install_target = target
                    try:
                        manager = await async_get_install_job_manager(self.hass)
                        active = await manager.async_find_active(
                            address.stored_value, target.pinned.stored_value
                        )
                    except InstallJobError:
                        errors["base"] = "install_receipt_error"
                        active = None
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected exception while loading an install receipt"
                        )
                        errors["base"] = "unknown"
                        active = None
                    if errors:
                        return self._show_install_address_form(user_input, errors)
                    if active is not None:
                        if (
                            self._pending_rc_tag is not None
                            and self._pending_rc_tag != active.artifact.release_tag
                        ):
                            return self._show_install_address_form(
                                user_input, {"base": "install_release_conflict"}
                            )
                        self._pending_job_id = active.job_id
                        if active.phase is InstallPhase.HEALTHY_UNCLAIMED:
                            return await self.async_step_install_result()
                        return await self._async_show_install_progress(active)

                    client = HaPaneldClient(
                        async_get_clientsession(self.hass), target.pinned
                    )
                    try:
                        health = await client.async_get_health()
                    except CannotConnectError, InvalidResponseError:
                        try:
                            # The first probe deliberately has no key. Discovering an
                            # authorization requirement must remain read-only; only the
                            # explicit next step may create and offer HA's durable key.
                            probe = await async_probe_install_target(target.pinned)
                        except Exception:
                            _LOGGER.exception(
                                "Unexpected exception while classifying install target"
                            )
                            errors["base"] = "unknown"
                        else:
                            state = probe.state.value
                            if state == "adb_unauthorized":
                                release_result = (
                                    await self._async_resolve_install_release(
                                        user_input=user_input
                                    )
                                )
                                if release_result is not None:
                                    return release_result
                                if self._pending_release is None:
                                    errors["base"] = "unknown"
                                elif self._pending_release.descriptor is None:  # type: ignore[unreachable]
                                    return self._show_release_preview_only()
                                else:
                                    return self._show_authorize_adb()
                            if state == "install_candidate":
                                placeholders = _install_candidate_placeholders(probe)
                                if placeholders is None:
                                    errors["base"] = "retained_or_ambiguous"
                                else:
                                    release_result = (
                                        await self._async_resolve_install_release(
                                            user_input=user_input
                                        )
                                    )
                                    if release_result is not None:
                                        return release_result
                                    self._pending_probe = probe
                                    if self._pending_release is None:
                                        errors["base"] = "unknown"
                                    elif self._pending_release.descriptor is None:  # type: ignore[unreachable]
                                        return self._show_release_preview_only()
                                    else:
                                        return self._show_install_candidate_preview()
                            else:
                                errors["base"] = {
                                    "adb_unreachable": "adb_unreachable",
                                    "installed": "installed_without_health",
                                    "retained_or_ambiguous": "retained_or_ambiguous",
                                    "incompatible": "incompatible",
                                }.get(state, "unknown")
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected exception while checking for an existing "
                            "ha-paneld"
                        )
                        errors["base"] = "unknown"
                    else:
                        # Preserve endpoint identity even though entry creation is
                        # deferred until the user confirms the already-installed panel.
                        self._async_abort_entries_match(
                            {CONF_ADDRESS: address.stored_value}
                        )
                        self._pending_health = health
                        return self.async_show_form(
                            step_id="confirm_existing",
                            data_schema=vol.Schema({}),
                            description_placeholders={
                                "address": address.stored_value,
                                "version": self._pending_health.version,
                            },
                        )

        return self._show_install_address_form(user_input, errors)

    async def async_step_authorize_adb(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for explicit approval of Home Assistant's ADB key on the panel."""
        if (
            self._pending_address is None
            or self._pending_install_target is None
            or self._pending_release is None
            or self._pending_release.descriptor is None
        ):
            return self.async_abort(reason="unknown")
        if user_input is None:
            return self._show_authorize_adb()

        try:
            target = await async_revalidate_install_target(
                self.hass, self._pending_install_target
            )
            signer = await async_get_adb_signer(self.hass)
            probe = await async_probe_install_target(target.pinned, signer)
        except InstallNetworkError as err:
            return self._show_authorize_adb({"base": _install_network_error(err)})
        except AdbCredentialError:
            return self._show_authorize_adb({"base": "adb_credential_error"})
        except Exception:
            _LOGGER.exception("Unexpected exception while retrying ADB authorization")
            return self._show_authorize_adb({"base": "unknown"})

        state = probe.state.value
        if state == "adb_unauthorized":
            return self._show_authorize_adb({"base": "adb_still_unauthorized"})
        if state == "install_candidate":
            placeholders = _install_candidate_placeholders(probe)
            if placeholders is None:
                return self.async_abort(reason="unknown")
            self._pending_probe = probe
            return self._show_install_candidate_preview()

        return self._show_authorize_adb(
            {
                "base": {
                    "adb_unreachable": "adb_unreachable",
                    "installed": "installed_without_health",
                    "retained_or_ambiguous": "retained_or_ambiguous",
                    "incompatible": "incompatible",
                }.get(state, "unknown")
            }
        )

    def _show_authorize_adb(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the physical ADB trust checkpoint."""
        return self.async_show_form(
            step_id="authorize_adb",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": self._pending_address.stored_value
                if self._pending_address is not None
                else ""
            },
            errors=errors,
        )

    async def async_step_confirm_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm connecting a panel that already runs ha-paneld."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._pending_address is None or self._pending_install_target is None:
                return self.async_abort(reason="unknown")
            try:
                target = await async_revalidate_install_target(
                    self.hass, self._pending_install_target
                )
                client = HaPaneldClient(
                    async_get_clientsession(self.hass), target.pinned
                )
                health = await client.async_get_health()
            except InstallNetworkError as err:
                errors["base"] = _install_network_error(err)
            except CannotConnectError, InvalidResponseError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected exception while confirming existing ha-paneld"
                )
                errors["base"] = "unknown"
            else:
                return self._async_create_panel_entry(self._pending_address, health)

        return self.async_show_form(
            step_id="confirm_existing",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": self._pending_address.stored_value
                if self._pending_address is not None
                else "",
                "version": self._pending_health.version
                if self._pending_health is not None
                else "",
            },
            errors=errors,
        )

    async def async_step_confirm_install_candidate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-prove and durably authorize one exact clean installation."""
        if (
            self._pending_address is None
            or self._pending_probe is None
            or self._pending_release is None
            or self._pending_install_target is None
        ):
            return self.async_abort(reason="unknown")
        if self._pending_release.descriptor is None:
            return self._show_release_preview_only()
        if user_input is None:
            return self._show_install_candidate_preview()

        # Entry identity is the normalized endpoint. This guard deliberately runs
        # before DNS, credentials, ADB, release, or durable job work.
        self._async_abort_entries_match(
            {CONF_ADDRESS: self._pending_address.stored_value}
        )
        try:
            target = await async_revalidate_install_target(
                self.hass, self._pending_install_target
            )
            created_credential = await async_get_adb_credential(self.hass)
            credential = await async_get_durable_adb_credential(self.hass)
            if credential.generation_id != created_credential.generation_id:
                raise AdbCredentialError
            probe = await async_probe_install_target(target.pinned, credential.signer)
        except InstallNetworkError as err:
            return self._show_install_candidate_preview(
                {"base": _install_network_error(err)}
            )
        except AdbCredentialError:
            return self._show_install_candidate_preview(
                {"base": "adb_credential_error"}
            )
        except Exception:
            _LOGGER.exception("Unexpected exception while confirming installation")
            return self._show_install_candidate_preview({"base": "unknown"})

        if not _same_install_candidate(self._pending_probe, probe):
            return self._show_install_candidate_preview(
                {"base": "install_candidate_changed"}
            )

        try:
            plan = build_install_plan(
                target,
                probe,
                self._pending_release,
                credential.generation_id,
                expected_rc_tag=self._pending_rc_tag,
            )
            manager = await async_get_install_job_manager(self.hass)
            receipt, _created = await manager.async_create_or_join(
                plan.target,
                plan.artifact,
                plan.plan_sha256,
                plan.adb_credential_id,
            )
        except InstallJobConflictError:
            return self._show_install_candidate_preview(
                {"base": "install_job_conflict"}
            )
        except InstallPlanError:
            return self._show_install_candidate_preview(
                {"base": "install_plan_rejected"}
            )
        except InstallJobError:
            return self._show_install_candidate_preview(
                {"base": "install_receipt_error"}
            )
        except Exception:
            _LOGGER.exception("Unexpected exception while creating an install job")
            return self._show_install_candidate_preview({"base": "unknown"})

        self._pending_job_id = receipt.job_id
        return await self._async_show_install_progress(receipt)

    def _show_install_candidate_preview(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Render the complete retained target and authenticated release plan."""
        assert self._pending_address is not None
        assert self._pending_probe is not None
        assert self._pending_release is not None
        placeholders = _install_candidate_placeholders(self._pending_probe)
        assert placeholders is not None
        placeholders.update(
            {
                "address": self._pending_address.stored_value,
                "version": self._pending_release.version,
                "tag": self._pending_release.tag,
                "sha256": self._pending_release.sha256,
            }
        )
        return self.async_show_form(
            step_id=(
                "confirm_install_rc"
                if self._pending_rc_tag is not None
                else "confirm_install_candidate"
            ),
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
            errors=errors,
        )

    async def async_step_confirm_install_rc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use the same frozen consent path with an explicitly translated RC warning."""
        return await self.async_step_confirm_install_candidate(user_input)

    async def async_step_release_preview_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain why an authenticated legacy release cannot be installed."""
        if self._pending_address is None or self._pending_release is None:
            return self.async_abort(reason="unknown")
        if user_input is not None:
            return self.async_abort(reason="preview_only_release")
        return self._show_release_preview_only()

    def _show_release_preview_only(self) -> ConfigFlowResult:
        """Present an authenticated release that lacks the signed install contract."""
        assert self._pending_address is not None
        assert self._pending_release is not None
        return self.async_show_form(
            step_id="release_preview_only",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": self._pending_address.stored_value,
                "version": self._pending_release.version,
                "tag": self._pending_release.tag,
                "sha256": self._pending_release.sha256,
            },
        )

    async def _async_resolve_install_release(
        self,
        *,
        user_input: dict[str, Any],
    ) -> ConfigFlowResult | None:
        """Resolve the release before any durable credential may be requested."""
        try:
            session = async_get_clientsession(self.hass)
            self._pending_release = (
                await async_resolve_stable_release(session)
                if self._pending_rc_tag is None
                else await async_resolve_rc_release(session, self._pending_rc_tag)
            )
        except ReleaseResolutionError:
            return self._show_install_address_form(
                user_input, {"base": "cannot_resolve_release"}
            )
        except Exception:
            _LOGGER.exception("Unexpected exception while resolving ha-paneld release")
            return self._show_install_address_form(user_input, {"base": "unknown"})
        return None

    async def _async_show_install_progress(
        self, receipt: InstallJobReceipt
    ) -> ConfigFlowResult:
        """Attach a flow-owned waiter without transferring worker ownership."""
        self._pending_job_id = receipt.job_id
        try:
            executor = await async_get_install_executor(self.hass)
            self._install_executor = executor
            worker = await executor.async_ensure_job(receipt.job_id)
        except InstallJobError:
            return self.async_abort(reason="install_receipt_error")
        except Exception:
            _LOGGER.exception("Unexpected exception while starting installation")
            return self.async_abort(reason="install_failed")

        if worker is None:
            try:
                manager = await async_get_install_job_manager(self.hass)
                refreshed = await manager.async_get(receipt.job_id)
            except InstallJobError:
                return self.async_abort(reason="install_receipt_error")
            except Exception:
                _LOGGER.exception("Unexpected exception while refreshing install job")
                return self.async_abort(reason="install_failed")
            if (
                refreshed.phase is InstallPhase.HEALTHY_UNCLAIMED
                or refreshed.is_terminal
            ):
                return await self.async_step_install_result()
            # The executor deliberately refuses to replay a worker cancelled in
            # this process. Registering an immediately completed waiter here would
            # create an unbounded progress callback loop.
            return self.async_abort(reason="install_worker_stopped")

        self._progress_waiter = self.hass.async_create_task(
            executor.async_wait(receipt.job_id),
            f"wait for ha-paneld install {receipt.job_id}",
        )
        return self.async_show_progress(
            step_id="install_progress",
            progress_action="installing",
            description_placeholders={"address": receipt.target.address},
            progress_task=self._progress_waiter,
        )

    async def async_step_install_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Complete only the flow waiter; the detached worker remains process-owned."""
        if self._pending_job_id is None:
            return self.async_show_progress_done(next_step_id="install_result")
        waiter = self._progress_waiter
        if waiter is not None and not waiter.done():
            return self.async_show_progress(
                step_id="install_progress",
                progress_action="installing",
                description_placeholders={
                    "address": (
                        self._pending_address.stored_value
                        if self._pending_address is not None
                        else ""
                    )
                },
                progress_task=waiter,
            )
        if waiter is not None:
            try:
                waiter.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.exception("Install progress waiter failed")
            self._progress_waiter = None
        return self.async_show_progress_done(next_step_id="install_result")

    async def async_step_install_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display a terminal result or finalize one healthy install exactly once."""
        if self._pending_job_id is None:
            return self.async_abort(reason="unknown")
        try:
            manager = await async_get_install_job_manager(self.hass)
            receipt = await manager.async_get(self._pending_job_id)
        except InstallJobError:
            return self.async_abort(reason="install_receipt_error")
        except Exception:
            _LOGGER.exception("Unexpected exception while loading install result")
            return self.async_abort(reason="install_failed")

        if receipt.phase in {
            InstallPhase.CANCELLED,
            InstallPhase.FAILED,
            InstallPhase.RECOVERY_REQUIRED,
        }:
            return self.async_abort(reason=_install_terminal_abort_reason(receipt))
        if receipt.phase is InstallPhase.CONSUMED:
            return self.async_abort(reason="already_configured")
        if receipt.phase is not InstallPhase.HEALTHY_UNCLAIMED:
            return await self._async_show_install_progress(receipt)
        return await self._async_finalize_healthy_install(manager, receipt)

    async def _async_finalize_healthy_install(
        self, manager: InstallJobManager, receipt: InstallJobReceipt
    ) -> ConfigFlowResult:
        """Serialize finalization attempts made through this individual flow."""
        if self._flow_removed:
            return self.async_abort(reason="install_worker_stopped")
        previous_owner = self._finalization_owner_task
        if previous_owner is not None:
            release_task = self._finalizer_release_task
            if not previous_owner.done() or (
                release_task is not None and not release_task.done()
            ):
                return self._show_install_result_retry(
                    receipt, "install_finalization_busy"
                )
            try:
                await self._async_release_finalizer()
            except asyncio.CancelledError:
                raise
            except Exception:
                return self._show_install_result_retry(
                    receipt, "install_finalization_retry"
                )
            if self._finalizer_job_id is not None:
                return self._show_install_result_retry(
                    receipt, "install_finalization_retry"
                )
            if self._flow_removed:
                # async_remove may run while the release await yields control.
                return self.async_abort(reason="install_worker_stopped")  # type: ignore[unreachable]

        # Reserve this flow before the first await. Removal can then defer a safe
        # release even while executor lookup or lease acquisition is in flight.
        owner = asyncio.current_task()
        if owner is None:
            return self.async_abort(reason="install_failed")
        self._finalizer_job_id = receipt.job_id
        self._finalization_owner_task = owner
        self._removed_release_retry_started = False
        try:
            return await self._async_finalize_healthy_install_locked(manager, receipt)
        except asyncio.CancelledError:
            self._schedule_finalizer_release()
            raise

    async def _async_finalize_healthy_install_locked(
        self, manager: InstallJobManager, receipt: InstallJobReceipt
    ) -> ConfigFlowResult:
        """Re-prove one healthy receipt under the process-wide finalizer lease."""
        executor = self._install_executor
        if executor is None:
            try:
                executor = await async_get_install_executor(self.hass)
            except Exception:
                _LOGGER.exception("Unable to load install finalizer")
                await self._async_release_finalizer()
                return self._show_install_result_retry(
                    receipt, "install_finalization_retry"
                )
            self._install_executor = executor
        try:
            acquired = await executor.async_acquire_finalizer(
                receipt.job_id, self.flow_id
            )
        except asyncio.CancelledError:
            self._schedule_finalizer_release()
            raise
        except InstallJobError:
            await self._async_release_finalizer()
            return self.async_abort(reason="install_receipt_error")
        except Exception:
            _LOGGER.exception("Unexpected exception while acquiring install finalizer")
            await self._async_release_finalizer()
            return self._show_install_result_retry(
                receipt, "install_finalization_retry"
            )
        if not acquired:
            await self._async_release_finalizer()
            return self._show_install_result_retry(receipt, "install_finalization_busy")
        if self._flow_removed:
            await self._async_release_finalizer()
            return self.async_abort(reason="install_worker_stopped")

        if self._address_is_configured(receipt.target.address):
            await self._async_release_finalizer()
            return self.async_abort(reason="already_configured")

        try:
            pinned = _pinned_from_receipt(receipt)
            await async_revalidate_install_target(self.hass, pinned)
        except InstallNetworkError as err:
            if err.code in {
                InstallNetworkErrorCode.RESOLUTION_FAILED,
                InstallNetworkErrorCode.RESOLUTION_TIMEOUT,
            }:
                await self._async_release_finalizer()
                return self._show_install_result_retry(
                    receipt, "install_finalization_retry"
                )
            return await self._async_reject_healthy_receipt(manager, receipt)
        except Exception:
            _LOGGER.exception("Unexpected exception while revalidating final target")
            await self._async_release_finalizer()
            return self._show_install_result_retry(
                receipt, "install_finalization_retry"
            )

        try:
            credential = await async_get_durable_adb_credential(self.hass)
        except AdbCredentialError:
            return await self._async_reject_healthy_receipt(manager, receipt)
        except Exception:
            _LOGGER.exception("Unexpected exception while loading final ADB identity")
            return await self._async_reject_healthy_receipt(manager, receipt)
        if credential.generation_id != receipt.adb_credential_id:
            return await self._async_reject_healthy_receipt(manager, receipt)

        stored_root_mode = receipt.preflight_root_mode
        if stored_root_mode is None:
            return await self._async_reject_healthy_receipt(manager, receipt)
        adb_target = _adb_target_from_receipt(receipt)
        try:
            await async_verify_installed_target(
                adb_target,
                credential.signer,
                expected_root_mode=AdbRootMode(stored_root_mode),
            )
        except InstallAdbError as err:
            if err.code is InstallAdbErrorCode.TARGET_UNREACHABLE:
                await self._async_release_finalizer()
                return self._show_install_result_retry(
                    receipt, "install_finalization_retry"
                )
            return await self._async_reject_healthy_receipt(manager, receipt)
        except Exception:
            _LOGGER.exception("Unexpected exception while verifying final ADB target")
            await self._async_release_finalizer()
            return self._show_install_result_retry(
                receipt, "install_finalization_retry"
            )

        try:
            health = await HaPaneldClient(
                async_get_clientsession(self.hass), adb_target.address
            ).async_get_health()
        except CannotConnectError:
            await self._async_release_finalizer()
            return self._show_install_result_retry(
                receipt, "install_finalization_retry"
            )
        except InvalidResponseError:
            return await self._async_reject_healthy_receipt(manager, receipt)
        except Exception:
            _LOGGER.exception("Unexpected exception while validating final health")
            await self._async_release_finalizer()
            return self._show_install_result_retry(
                receipt, "install_finalization_retry"
            )

        if health.version != receipt.artifact.version_name:
            return await self._async_reject_healthy_receipt(manager, receipt)
        if self._flow_removed:
            # async_remove may run during the preceding network awaits.
            await self._async_release_finalizer()  # type: ignore[unreachable]
            return self.async_abort(reason="install_worker_stopped")
        if self._address_is_configured(receipt.target.address):
            await self._async_release_finalizer()
            return self.async_abort(reason="already_configured")

        # Keep the lease through ConfigEntries' actual add. async_on_create_entry
        # consumes the receipt using HA's generated entry ID and always releases it.
        return self.async_create_entry(
            title=health.panel_id,
            data={CONF_ADDRESS: receipt.target.address},
        )

    async def _async_reject_healthy_receipt(
        self, manager: InstallJobManager, receipt: InstallJobReceipt
    ) -> ConfigFlowResult:
        """Make final verification drift durable before refusing entry creation."""
        try:
            await manager.async_transition(
                receipt.job_id,
                receipt.revision,
                InstallPhase.RECOVERY_REQUIRED,
                result_code=InstallResultCode.VERIFICATION_REQUIRED,
            )
        except InstallJobError:
            await self._async_release_finalizer()
            return self.async_abort(reason="install_receipt_error")
        except Exception:
            _LOGGER.exception("Unexpected exception while rejecting install receipt")
            await self._async_release_finalizer()
            return self.async_abort(reason="install_receipt_error")
        await self._async_release_finalizer()
        return self.async_abort(reason="install_recovery_required")

    def _show_install_result_retry(
        self, receipt: InstallJobReceipt, error: str
    ) -> ConfigFlowResult:
        """Keep a healthy receipt retryable without exposing target internals."""
        return self.async_show_form(
            step_id="install_result",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": receipt.target.address,
                "version": receipt.artifact.version_name,
            },
            errors={"base": error},
        )

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Consume the healthy receipt with HA's actual config-entry identity."""
        job_id = self._finalizer_job_id
        if job_id is not None:
            try:
                entry = result["result"]
                if not isinstance(entry, ConfigEntry):
                    raise TypeError
                manager = await async_get_install_job_manager(self.hass)
                receipt = await manager.async_get(job_id)
                await manager.async_transition(
                    job_id,
                    receipt.revision,
                    InstallPhase.CONSUMED,
                    result_code=InstallResultCode.ENTRY_CREATED,
                    consumed_entry_id=entry.entry_id,
                )
            except Exception:
                # The entry already exists. Receipt persistence must never make HA
                # remove it or report a failed setup after that point.
                _LOGGER.exception("Unable to consume completed install receipt")
            finally:
                try:
                    await self._async_release_finalizer()
                except Exception:
                    _LOGGER.exception("Unable to release completed install finalizer")
        return await super().async_on_create_entry(result)

    def async_remove(self) -> None:
        """Detach this UI flow without cancelling the process-owned worker."""
        self._flow_removed = True
        if self._progress_waiter is not None and not self._progress_waiter.done():
            self._progress_waiter.cancel()
        self._progress_waiter = None
        if self._finalizer_job_id is not None and self._install_executor is not None:
            owner = self._finalization_owner_task
            if owner is not None and not owner.done():
                # Releasing while read-only final verification is still in flight
                # would allow a second flow to run concurrently. Defer release until
                # the owning configure task has exited.
                if not self._release_after_finalization:
                    self._release_after_finalization = True
                    owner.add_done_callback(self._finalization_done)
            else:
                self._schedule_finalizer_release()
        super().async_remove()

    def _finalization_done(self, _task: asyncio.Task[Any]) -> None:
        """Release a removed flow's lease only after its finalizer has exited."""
        if not self._release_after_finalization:
            return
        release_task = self._finalizer_release_task
        if release_task is not None or self._removed_release_retry_started:
            return
        self._schedule_finalizer_release()

    def _schedule_finalizer_release(self) -> None:
        """Schedule non-blocking finalizer release from a synchronous callback."""
        self._ensure_finalizer_release_task()

    async def _async_release_finalizer(self) -> None:
        """Release this flow's lease without transferring caller cancellation."""
        task = self._ensure_finalizer_release_task()
        if task is not None:
            try:
                await asyncio.shield(task)
            finally:
                if task.done() and self._finalizer_release_task is task:
                    self._finalizer_release_task = None

    def _ensure_finalizer_release_task(self) -> asyncio.Task[None] | None:
        """Return one process-tracked release task for the retained lease identity."""
        task = self._finalizer_release_task
        if task is not None:
            return task

        job_id = self._finalizer_job_id
        executor = self._install_executor
        if job_id is None or executor is None:
            # No executor means lease acquisition never started. There is no
            # process-wide ownership to release, only the optimistic local guard.
            self._clear_finalizer_state(job_id)
            return None

        task = self.hass.async_create_task(
            self._async_run_finalizer_release(job_id, executor),
            f"release ha-paneld install finalizer {job_id}",
        )
        self._finalizer_release_task = task
        task.add_done_callback(self._finalizer_release_done)
        return task

    async def _async_run_finalizer_release(
        self, job_id: str, executor: InstallExecutor
    ) -> None:
        """Keep the lease coordinates durable in memory until release succeeds."""
        await executor.async_release_finalizer(job_id, self.flow_id)
        if self._finalizer_job_id == job_id and self._install_executor is executor:
            self._clear_finalizer_state(job_id)

    def _finalizer_release_done(self, task: asyncio.Task[None]) -> None:
        """Permit a failed release to be retried without hiding its coordinates."""
        if self._finalizer_release_task is task:
            self._finalizer_release_task = None
        failed = task.cancelled()
        if not failed:
            try:
                task.result()
            except Exception:
                failed = True
                _LOGGER.exception("Unable to release install finalizer")
        if (
            failed
            and self._flow_removed
            and self._finalizer_job_id is not None
            and not self._removed_release_retry_started
        ):
            # A removed flow has no future UI submission to trigger cleanup.
            # Retry once; the executor operation is exact-owner and idempotent.
            self._removed_release_retry_started = True
            self._schedule_finalizer_release()

    def _clear_finalizer_state(self, job_id: str | None) -> None:
        """Clear local ownership only if it still describes this release."""
        if self._finalizer_job_id != job_id:
            return
        self._finalizer_job_id = None
        self._finalization_owner_task = None
        self._release_after_finalization = False
        self._removed_release_retry_started = False

    def _address_is_configured(self, address: str) -> bool:
        """Check the existing endpoint identity without contacting the panel."""
        return any(
            entry.data.get(CONF_ADDRESS) == address
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    def _async_create_panel_entry(
        self, address: PanelAddress, health: PanelHealth
    ) -> ConfigFlowResult:
        """Create an entry while preserving the existing endpoint identity contract."""
        # The configured network endpoint is the entry identity. The health contract
        # exposes only a user-editable panel name, not a stable hardware identifier.
        self._async_abort_entries_match({CONF_ADDRESS: address.stored_value})
        return self.async_create_entry(
            title=health.panel_id,
            data={CONF_ADDRESS: address.stored_value},
        )


def _markdown_literal(value: str) -> str:
    """Keep interpolated device text literal in HA's Markdown descriptions."""
    return "".join(
        f"&#{ord(character)};" if character in "\\`*_{}[]()<>!&:./@" else character
        for character in value
    )


def _install_candidate_placeholders(
    probe: InstallTargetProbe,
) -> dict[str, str] | None:
    """Return facts for a package-absent candidate, or refuse an incomplete one."""
    if (
        probe.model is None
        or probe.serial is None
        or probe.primary_abi is None
        or probe.android_sdk is None
    ):
        return None
    return {
        "model": _markdown_literal(probe.model),
        "serial": _markdown_literal(probe.serial),
        "abi": _markdown_literal(probe.primary_abi),
        "sdk": str(probe.android_sdk),
    }


def _same_install_candidate(
    expected: InstallTargetProbe, observed: InstallTargetProbe
) -> bool:
    """Require the complete read-only target identity to remain exact."""
    return (
        observed.state.value == "install_candidate"
        and _install_candidate_placeholders(observed) is not None
        and observed.serial == expected.serial
        and observed.model == expected.model
        and observed.primary_abi == expected.primary_abi
        and observed.android_sdk == expected.android_sdk
    )


def _pinned_from_receipt(receipt: InstallJobReceipt) -> PinnedPanelTarget:
    """Reconstruct the already-validated LAN pin from a durable receipt."""
    return PinnedPanelTarget(
        original=normalize_address(receipt.target.address),
        pinned=normalize_address(receipt.target.pinned_address),
    )


def _adb_target_from_receipt(receipt: InstallJobReceipt) -> AdbInstallTarget:
    """Reconstruct exact final ADB identity without retaining flow preview state."""
    return AdbInstallTarget(
        address=normalize_address(receipt.target.pinned_address),
        serial=receipt.target.adb_serial,
        model=receipt.target.model,
        primary_abi=receipt.target.primary_abi,
        android_sdk=receipt.target.android_sdk,
    )


def _install_network_error(error: InstallNetworkError) -> str:
    """Map a privacy-safe network refusal to a translated flow error."""
    return {
        InstallNetworkErrorCode.INVALID_HOST: "invalid_install_address",
        InstallNetworkErrorCode.RESOLUTION_FAILED: "install_resolution_failed",
        InstallNetworkErrorCode.RESOLUTION_TIMEOUT: "install_resolution_timeout",
        InstallNetworkErrorCode.TOO_MANY_RESULTS: "install_too_many_addresses",
        InstallNetworkErrorCode.UNSAFE_TARGET: "unsafe_install_target",
        InstallNetworkErrorCode.PINNED_TARGET_REMOVED: "install_target_changed",
    }[error.code]


def _install_terminal_abort_reason(receipt: InstallJobReceipt) -> str:
    """Expose one privacy-safe and actionable durable installer outcome."""
    result_code = receipt.result_code
    if receipt.phase is InstallPhase.CANCELLED:
        if result_code is None:
            return "install_cancelled"
        return _CANCELLED_ABORT_REASONS.get(result_code, "install_cancelled")
    if receipt.phase is InstallPhase.FAILED:
        if result_code is None:
            return "install_failed"
        return _FAILED_ABORT_REASONS.get(result_code, "install_failed")
    if receipt.phase is InstallPhase.RECOVERY_REQUIRED:
        if result_code is None:
            return "install_recovery_required"
        return _RECOVERY_ABORT_REASONS.get(result_code, "install_recovery_required")
    return "install_failed"
