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
    normalize_address,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_ADDRESS): TextSelector(TextSelectorConfig())}
)


class HaPaneldConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an ha-paneld config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by panel address."""
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
                # The configured network endpoint is the entry identity. The health
                # contract exposes only a user-editable panel name, not a stable
                # hardware identifier.
                self._async_abort_entries_match({CONF_ADDRESS: address.stored_value})
                return self.async_create_entry(
                    title=health.panel_id,
                    data={CONF_ADDRESS: address.stored_value},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_DATA_SCHEMA, user_input),
            errors=errors,
        )
