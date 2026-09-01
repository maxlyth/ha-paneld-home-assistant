"""Tests for the ha-paneld config flow."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_paneld.client import (
    CannotConnectError,
    InvalidAddressError,
    PanelHealth,
)
from custom_components.ha_paneld.const import DOMAIN

HEALTH = PanelHealth(
    version="0.9.0",
    panel_id="alpha",
    build="1000",
    config_hash="1a2b3c4d",
)
BETA_HEALTH = PanelHealth(
    version="0.9.0",
    panel_id="beta",
    build="1001",
    config_hash="1a2b3c4d",
)


async def test_user_form(hass: HomeAssistant) -> None:
    """Manual setup starts with the address form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_successful_flow(hass: HomeAssistant) -> None:
    """A health response creates an endpoint-keyed entry with a display title."""
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(return_value=HEALTH),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: " PANEL.local "},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alpha"
    assert result["data"] == {CONF_ADDRESS: "panel.local"}
    assert result["result"].unique_id is None


async def test_duplicate_address_is_rejected(hass: HomeAssistant) -> None:
    """The address remains the identity when the mutable panel name changes."""
    health_mock = AsyncMock(side_effect=[HEALTH, HEALTH, BETA_HEALTH])
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        first = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: "PANEL.local"},
        )
        second = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: "panel.local"},
        )

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert first["result"].data[CONF_ADDRESS] == "panel.local"
    # Creating the first entry schedules its initial coordinator refresh between
    # the two config-flow validations.
    assert health_mock.await_count == 3


async def test_expected_errors(hass: HomeAssistant) -> None:
    """Address and connection failures remain actionable form errors."""
    with patch(
        "custom_components.ha_paneld.config_flow.normalize_address",
        side_effect=InvalidAddressError,
    ):
        invalid = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: "bad"},
        )

    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(side_effect=CannotConnectError),
    ):
        unavailable = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: "panel.local"},
        )

    assert invalid["errors"] == {"base": "invalid_address"}
    assert unavailable["errors"] == {"base": "cannot_connect"}


async def test_unexpected_error(hass: HomeAssistant) -> None:
    """Unexpected validation failures do not escape the flow."""
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_ADDRESS: "panel.local"},
        )

    assert result["errors"] == {"base": "unknown"}
