"""Config flow for ha-paneld."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .adb_credentials import AdbCredentialError, async_get_adb_signer
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
from .provisioning import InstallTargetProbe, async_probe_install_target
from .release import (
    ReleaseArtifact,
    ReleaseResolutionError,
    async_resolve_stable_release,
)

_LOGGER = logging.getLogger(__name__)

_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_ADDRESS): TextSelector(TextSelectorConfig())}
)


class HaPaneldConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an ha-paneld config flow."""

    VERSION = 1

    _pending_address: PanelAddress | None = None
    _pending_health: PanelHealth | None = None
    _pending_probe: InstallTargetProbe | None = None
    _pending_release: ReleaseArtifact | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose whether to bootstrap or connect a panel."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["install_or_upgrade", "connect_existing"],
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

    async def async_step_install_or_upgrade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Classify a panel before any installation or repair is attempted."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                address = normalize_address(user_input[CONF_ADDRESS])
                if address.port != DEFAULT_PORT:
                    raise InvalidAddressError
                self._async_abort_entries_match({CONF_ADDRESS: address.stored_value})
            except InvalidAddressError:
                errors["base"] = "invalid_install_address"
            else:
                client = HaPaneldClient(async_get_clientsession(self.hass), address)
                try:
                    health = await client.async_get_health()
                except CannotConnectError, InvalidResponseError:
                    try:
                        # The first probe deliberately has no key. Discovering an
                        # authorization requirement must remain read-only; only the
                        # explicit next step may create and offer HA's durable key.
                        probe = await async_probe_install_target(address)
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected exception while classifying install target"
                        )
                        errors["base"] = "unknown"
                    else:
                        state = probe.state.value
                        if state == "adb_unauthorized":
                            self._pending_address = address
                            return self._show_authorize_adb()
                        if state == "install_candidate":
                            placeholders = _install_candidate_placeholders(probe)
                            if placeholders is None:
                                errors["base"] = "retained_or_ambiguous"
                            else:
                                try:
                                    release = await async_resolve_stable_release(
                                        async_get_clientsession(self.hass)
                                    )
                                except ReleaseResolutionError:
                                    errors["base"] = "cannot_resolve_release"
                                except Exception:
                                    _LOGGER.exception(
                                        "Unexpected exception while resolving "
                                        "ha-paneld release"
                                    )
                                    errors["base"] = "unknown"
                                else:
                                    self._pending_address = address
                                    self._pending_probe = probe
                                    self._pending_release = release
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
                        "Unexpected exception while checking for an existing ha-paneld"
                    )
                    errors["base"] = "unknown"
                else:
                    # Preserve endpoint identity even though entry creation is
                    # deferred until the user confirms the already-installed panel.
                    self._async_abort_entries_match(
                        {CONF_ADDRESS: address.stored_value}
                    )
                    self._pending_address = address
                    self._pending_health = health
                    return self.async_show_form(
                        step_id="confirm_existing",
                        data_schema=vol.Schema({}),
                        description_placeholders={
                            "address": address.stored_value,
                            "version": self._pending_health.version,
                        },
                    )

        return self.async_show_form(
            step_id="install_or_upgrade",
            data_schema=self.add_suggested_values_to_schema(_DATA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_authorize_adb(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for explicit approval of Home Assistant's ADB key on the panel."""
        if self._pending_address is None:
            return self.async_abort(reason="unknown")
        if user_input is None:
            return self._show_authorize_adb()

        try:
            signer = await async_get_adb_signer(self.hass)
            probe = await async_probe_install_target(self._pending_address, signer)
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
            try:
                release = await async_resolve_stable_release(
                    async_get_clientsession(self.hass)
                )
            except ReleaseResolutionError:
                return self._show_authorize_adb({"base": "cannot_resolve_release"})
            except Exception:
                _LOGGER.exception(
                    "Unexpected exception while resolving ha-paneld release"
                )
                return self._show_authorize_adb({"base": "unknown"})
            self._pending_probe = probe
            self._pending_release = release
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
            if self._pending_address is None:
                return self.async_abort(reason="unknown")
            try:
                client = HaPaneldClient(
                    async_get_clientsession(self.hass), self._pending_address
                )
                health = await client.async_get_health()
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
        """Review an install candidate without mutating it in this prototype."""
        if (
            self._pending_address is None
            or self._pending_probe is None
            or self._pending_release is None
        ):
            return self.async_abort(reason="unknown")
        if user_input is not None:
            return self.async_abort(reason="prototype_ready")
        return self._show_install_candidate_preview()

    def _show_install_candidate_preview(self) -> ConfigFlowResult:
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
            step_id="confirm_install_candidate",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
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
        "model": probe.model,
        "serial": probe.serial,
        "abi": probe.primary_abi,
        "sdk": str(probe.android_sdk),
    }
