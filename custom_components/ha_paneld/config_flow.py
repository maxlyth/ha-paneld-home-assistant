"""Config flow for ha-paneld."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .client import (
    CannotConnectError,
    HaPaneldClient,
    InvalidAddressError,
    InvalidResponseError,
    PanelAddress,
    PanelHealth,
    normalize_address,
)
from .const import DOMAIN
from .provisioning import async_probe_install_target
from .release import async_resolve_stable_release

_LOGGER = logging.getLogger(__name__)

_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_ADDRESS): TextSelector(TextSelectorConfig())}
)


class HaPaneldConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an ha-paneld config flow."""

    VERSION = 1

    _pending_address: PanelAddress | None = None

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
            except InvalidAddressError:
                errors["base"] = "invalid_address"
            else:
                client = HaPaneldClient(async_get_clientsession(self.hass), address)
                try:
                    await client.async_get_health()
                except (CannotConnectError, InvalidResponseError):
                    try:
                        probe = await async_probe_install_target(address)
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected exception while classifying install target"
                        )
                        errors["base"] = "unknown"
                    else:
                        state = probe.state.value
                        if state == "clean":
                            try:
                                release = await async_resolve_stable_release(
                                    async_get_clientsession(self.hass)
                                )
                            except Exception:
                                _LOGGER.exception(
                                    "Unable to resolve the stable ha-paneld release"
                                )
                                errors["base"] = "cannot_resolve_release"
                            else:
                                self._pending_address = address
                                return self.async_show_form(
                                    step_id="confirm_clean_install",
                                    data_schema=vol.Schema({}),
                                    description_placeholders={
                                        "address": address.stored_value,
                                        "model": _probe_detail(probe, "model"),
                                        "serial": _probe_detail(probe, "serial"),
                                        "abi": _probe_detail(probe, "primary_abi"),
                                        "sdk": _probe_detail(probe, "android_sdk"),
                                        "version": release.version,
                                        "tag": release.tag,
                                        "sha256": release.sha256,
                                    },
                                )
                        else:
                            errors["base"] = {
                                "adb_unreachable": "adb_unreachable",
                                "adb_unauthorized": "adb_unauthorized",
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
                    return self.async_show_form(
                        step_id="confirm_existing",
                        data_schema=vol.Schema({}),
                        description_placeholders={"address": address.stored_value},
                    )

        return self.async_show_form(
            step_id="install_or_upgrade",
            data_schema=self.add_suggested_values_to_schema(_DATA_SCHEMA, user_input),
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
            except (CannotConnectError, InvalidResponseError):
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
                else ""
            },
            errors=errors,
        )

    async def async_step_confirm_clean_install(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review a clean target without mutating it in this prototype."""
        if user_input is not None:
            return self.async_abort(reason="prototype_ready")

        return self.async_show_form(
            step_id="confirm_clean_install",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": self._pending_address.stored_value
                if self._pending_address is not None
                else ""
            },
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


def _probe_detail(probe: Any, name: str) -> str:
    """Render optional target facts without requiring them from the first backend."""
    value = getattr(probe, name, None)
    if value is None or value == "":
        return "Not reported"
    return str(value)
