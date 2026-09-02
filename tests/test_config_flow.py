"""Tests for the ha-paneld config flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_paneld.adb_credentials import AdbCredentialError
from custom_components.ha_paneld.client import (
    CannotConnectError,
    InvalidAddressError,
    InvalidResponseError,
    PanelHealth,
)
from custom_components.ha_paneld.config_flow import HaPaneldConfigFlow
from custom_components.ha_paneld.const import DOMAIN
from custom_components.ha_paneld.release import ReleaseResolutionError

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
RELEASE = SimpleNamespace(
    tag="v0.9.7",
    version="0.9.7",
    apk_name="ha-paneld-v0.9.7.apk",
    apk_url="https://example.invalid/ha-paneld-v0.9.7.apk",
    sha256="a" * 64,
)


def _probe(state: str, **facts: str | int) -> SimpleNamespace:
    """Return the provisioning probe shape consumed by the config flow."""
    return SimpleNamespace(state=SimpleNamespace(value=state), **facts)


async def _start_step(hass: HomeAssistant, step_id: str) -> dict:
    """Start the user flow and select one of its menu options."""
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": step_id}
    )


async def test_user_starts_with_install_first_menu(hass: HomeAssistant) -> None:
    """The primary user path is installation, with existing attach second."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["install_or_upgrade", "connect_existing"]


async def test_connect_existing_starts_with_address_form(
    hass: HomeAssistant,
) -> None:
    """The secondary path retains the existing manual address form."""
    result = await _start_step(hass, "connect_existing")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect_existing"


async def test_connect_existing_success(hass: HomeAssistant) -> None:
    """Existing attach keeps its normalized endpoint identity and display title."""
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(return_value=HEALTH),
    ):
        form = await _start_step(hass, "connect_existing")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: " PANEL.local "}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alpha"
    assert result["data"] == {CONF_ADDRESS: "panel.local"}
    assert result["result"].unique_id is None


async def test_connect_existing_duplicate_address_is_rejected(
    hass: HomeAssistant,
) -> None:
    """The address remains identity when the mutable panel name changes."""
    health_mock = AsyncMock(side_effect=[HEALTH, HEALTH, BETA_HEALTH])
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        first_form = await _start_step(hass, "connect_existing")
        first = await hass.config_entries.flow.async_configure(
            first_form["flow_id"], {CONF_ADDRESS: "PANEL.local"}
        )
        second_form = await _start_step(hass, "connect_existing")
        second = await hass.config_entries.flow.async_configure(
            second_form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert first["result"].data[CONF_ADDRESS] == "panel.local"
    # Creating the first entry schedules its initial coordinator refresh between
    # the two config-flow validations.
    assert health_mock.await_count == 3


async def test_connect_existing_expected_errors(hass: HomeAssistant) -> None:
    """Address and health failures remain actionable connect-form errors."""
    invalid_form = await _start_step(hass, "connect_existing")
    with patch(
        "custom_components.ha_paneld.config_flow.normalize_address",
        side_effect=InvalidAddressError,
    ):
        invalid = await hass.config_entries.flow.async_configure(
            invalid_form["flow_id"], {CONF_ADDRESS: "bad"}
        )

    unavailable_form = await _start_step(hass, "connect_existing")
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(side_effect=CannotConnectError),
    ):
        unavailable = await hass.config_entries.flow.async_configure(
            unavailable_form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert invalid["step_id"] == "connect_existing"
    assert invalid["errors"] == {"base": "invalid_address"}
    assert unavailable["step_id"] == "connect_existing"
    assert unavailable["errors"] == {"base": "cannot_connect"}


async def test_connect_existing_unexpected_error(hass: HomeAssistant) -> None:
    """Unexpected attach failures do not escape the flow."""
    form = await _start_step(hass, "connect_existing")
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["errors"] == {"base": "unknown"}


async def test_install_rejects_invalid_address_before_network_calls(
    hass: HomeAssistant,
) -> None:
    """An invalid target returns to the install form without probing it."""
    health_mock = AsyncMock()
    probe_mock = AsyncMock()
    form = await _start_step(hass, "install_or_upgrade")
    with (
        patch(
            "custom_components.ha_paneld.config_flow.normalize_address",
            side_effect=InvalidAddressError,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "bad"}
        )

    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": "invalid_install_address"}
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()


async def test_install_existing_panel_requires_confirmation(
    hass: HomeAssistant,
) -> None:
    """A healthy installation is connected only after an explicit confirmation."""
    probe_mock = AsyncMock()
    health_mock = AsyncMock(return_value=HEALTH)
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: " PANEL.local "}
        )

        assert confirm["type"] is FlowResultType.FORM
        assert confirm["step_id"] == "confirm_existing"
        assert confirm["description_placeholders"] == {
            "address": "panel.local",
            "version": "0.9.0",
        }
        assert not hass.config_entries.async_entries(DOMAIN)
        probe_mock.assert_not_awaited()

        result = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alpha"
    assert result["data"] == {CONF_ADDRESS: "panel.local"}
    assert result["result"].unique_id is None
    # Entry setup performs the third health refresh after the two flow checks.
    assert health_mock.await_count == 3


async def test_install_existing_panel_rechecks_health_before_create(
    hass: HomeAssistant,
) -> None:
    """Confirmation refuses an installation that stopped answering meanwhile."""
    health_mock = AsyncMock(side_effect=[HEALTH, CannotConnectError])
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        form = await _start_step(hass, "install_or_upgrade")
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_existing"
    assert result["errors"] == {"base": "cannot_connect"}
    assert health_mock.await_count == 2
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_existing_panel_handles_unexpected_confirmation_failure(
    hass: HomeAssistant,
) -> None:
    """An unexpected revalidation failure creates no entry."""
    health_mock = AsyncMock(side_effect=[HEALTH, RuntimeError("unexpected")])
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        form = await _start_step(hass, "install_or_upgrade")
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})

    assert result["step_id"] == "confirm_existing"
    assert result["errors"] == {"base": "unknown"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_rejects_an_http_port_as_an_adb_port(
    hass: HomeAssistant,
) -> None:
    """The install target keeps fixed HTTP and ADB ports as separate protocols."""
    health_mock = AsyncMock()
    probe_mock = AsyncMock()
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local:5555"}
        )

    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": "invalid_install_address"}
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()


async def test_install_existing_duplicate_address_is_rejected(
    hass: HomeAssistant,
) -> None:
    """The install path shares the exact existing endpoint identity."""
    health_mock = AsyncMock(side_effect=[HEALTH, HEALTH, BETA_HEALTH])
    with patch(
        "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        connect = await _start_step(hass, "connect_existing")
        first = await hass.config_entries.flow.async_configure(
            connect["flow_id"], {CONF_ADDRESS: "PANEL.local"}
        )
        install = await _start_step(hass, "install_or_upgrade")
        duplicate = await hass.config_entries.flow.async_configure(
            install["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"


async def test_install_unavailable_duplicate_address_is_rejected_before_probe(
    hass: HomeAssistant,
) -> None:
    """An unavailable configured endpoint retains its config-entry identity."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Configured panel",
        data={CONF_ADDRESS: "panel.local"},
    ).add_to_hass(hass)
    health_mock = AsyncMock(side_effect=CannotConnectError)
    probe_mock = AsyncMock()
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        install = await _start_step(hass, "install_or_upgrade")
        duplicate = await hass.config_entries.flow.async_configure(
            install["flow_id"], {CONF_ADDRESS: "PANEL.local"}
        )

    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()


@pytest.mark.parametrize("health_error", [CannotConnectError, InvalidResponseError])
async def test_install_candidate_readiness_is_non_mutating_prototype(
    hass: HomeAssistant, health_error: type[Exception]
) -> None:
    """Both absent and invalid health fall through to a clean ADB classification."""
    probe_mock = AsyncMock(
        return_value=_probe(
            "install_candidate",
            model="WF1589T",
            serial="serial-123",
            primary_abi="arm64-v8a",
            android_sdk=31,
        )
    )
    release_mock = AsyncMock(return_value=RELEASE)
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=health_error),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            release_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "Panel.local"}
        )

        assert confirm["type"] is FlowResultType.FORM
        assert confirm["step_id"] == "confirm_install_candidate"
        assert confirm["description_placeholders"] == {
            "address": "panel.local",
            "model": "WF1589T",
            "serial": "serial-123",
            "abi": "arm64-v8a",
            "sdk": "31",
            "version": "0.9.7",
            "tag": "v0.9.7",
            "sha256": "a" * 64,
        }
        assert not hass.config_entries.async_entries(DOMAIN)
        probe_mock.assert_awaited_once()
        assert len(probe_mock.await_args.args) == 1
        assert probe_mock.await_args.args[0].stored_value == "panel.local"
        release_mock.assert_awaited_once()

        refreshed = await hass.config_entries.flow.async_configure(confirm["flow_id"])
        assert (
            refreshed["description_placeholders"] == confirm["description_placeholders"]
        )

        result = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "prototype_ready"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_classification_error_can_retry_to_candidate(
    hass: HomeAssistant,
) -> None:
    """The same address form can recover from a transient classification failure."""
    probe_mock = AsyncMock(
        side_effect=[
            _probe("adb_unreachable"),
            _probe(
                "install_candidate",
                model="WF1589T",
                serial="serial-123",
                primary_abi="arm64-v8a",
                android_sdk=31,
            ),
        ]
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        refused = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        recovered = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert refused["errors"] == {"base": "adb_unreachable"}
    assert recovered["type"] is FlowResultType.FORM
    assert recovered["step_id"] == "confirm_install_candidate"
    assert probe_mock.await_count == 2


async def test_install_candidate_without_identity_fails_closed(
    hass: HomeAssistant,
) -> None:
    """A backend cannot promote package absence without complete target facts."""
    release_mock = AsyncMock()
    incomplete = _probe(
        "install_candidate",
        model=None,
        serial=None,
        primary_abi=None,
        android_sdk=None,
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            AsyncMock(return_value=incomplete),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            release_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["errors"] == {"base": "retained_or_ambiguous"}
    release_mock.assert_not_awaited()
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        ("adb_unreachable", "adb_unreachable"),
        ("installed", "installed_without_health"),
        ("retained_or_ambiguous", "retained_or_ambiguous"),
        ("incompatible", "incompatible"),
    ],
)
async def test_install_classification_refusals_return_to_address_form(
    hass: HomeAssistant, state: str, expected_error: str
) -> None:
    """Every unsafe target state is specific, actionable, and retryable."""
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            AsyncMock(return_value=_probe(state)),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": expected_error}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_unauthorized_requires_physical_approval_without_creating_key(
    hass: HomeAssistant,
) -> None:
    """The read-only first probe cannot create or offer Home Assistant's ADB key."""
    signer_mock = AsyncMock()
    probe_mock = AsyncMock(return_value=_probe("adb_unauthorized"))
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "Panel.local"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize_adb"
    assert result["description_placeholders"] == {"address": "panel.local"}
    assert result["errors"] is None
    signer_mock.assert_not_awaited()
    probe_mock.assert_awaited_once()
    assert len(probe_mock.await_args.args) == 1
    assert probe_mock.await_args.args[0].stored_value == "panel.local"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_authorization_retry_uses_persistent_signer(
    hass: HomeAssistant,
) -> None:
    """A retry offers the shared signer and remains actionable until approved."""
    signer = object()
    signer_mock = AsyncMock(return_value=signer)
    probe_mock = AsyncMock(
        side_effect=[_probe("adb_unauthorized"), _probe("adb_unauthorized")]
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        authorize = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(
            authorize["flow_id"], {}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize_adb"
    assert result["errors"] == {"base": "adb_still_unauthorized"}
    signer_mock.assert_awaited_once_with(hass)
    assert len(probe_mock.await_args_list[0].args) == 1
    assert probe_mock.await_args_list[1].args[1] is signer
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_authorization_approval_reaches_release_preview(
    hass: HomeAssistant,
) -> None:
    """Physical approval reclassifies with the signer before resolving a release."""
    signer = object()
    signer_mock = AsyncMock(return_value=signer)
    candidate = _probe(
        "install_candidate",
        model="WF1589T",
        serial="serial-123",
        primary_abi="arm64-v8a",
        android_sdk=31,
    )
    probe_mock = AsyncMock(side_effect=[_probe("adb_unauthorized"), candidate])
    release_mock = AsyncMock(return_value=RELEASE)
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            release_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        authorize = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(
            authorize["flow_id"], {}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_install_candidate"
    assert result["description_placeholders"] == {
        "address": "panel.local",
        "model": "WF1589T",
        "serial": "serial-123",
        "abi": "arm64-v8a",
        "sdk": "31",
        "version": "0.9.7",
        "tag": "v0.9.7",
        "sha256": "a" * 64,
    }
    signer_mock.assert_awaited_once_with(hass)
    assert probe_mock.await_args_list[1].args[1] is signer
    release_mock.assert_awaited_once()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_authorization_credential_storage_error_is_actionable(
    hass: HomeAssistant,
) -> None:
    """Failure to durably load the ADB identity does not probe or create an entry."""
    probe_mock = AsyncMock(return_value=_probe("adb_unauthorized"))
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_get_adb_signer",
            AsyncMock(side_effect=AdbCredentialError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        authorize = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(
            authorize["flow_id"], {}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize_adb"
    assert result["errors"] == {"base": "adb_credential_error"}
    assert probe_mock.await_count == 1
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("failure_at", "expected_error"),
    [
        ("signer", "unknown"),
        ("probe", "unknown"),
        ("release", "cannot_resolve_release"),
    ],
)
async def test_install_authorization_failures_create_no_config_entry(
    hass: HomeAssistant, failure_at: str, expected_error: str
) -> None:
    """Authorization retry failures stay in the trust checkpoint without an entry."""
    signer = object()
    signer_mock = AsyncMock(
        side_effect=RuntimeError("signer") if failure_at == "signer" else None,
        return_value=signer,
    )
    candidate = _probe(
        "install_candidate",
        model="WF1589T",
        serial="serial-123",
        primary_abi="arm64-v8a",
        android_sdk=31,
    )
    second_probe: object = RuntimeError("probe") if failure_at == "probe" else candidate
    probe_mock = AsyncMock(side_effect=[_probe("adb_unauthorized"), second_probe])
    release_mock = AsyncMock(
        side_effect=ReleaseResolutionError if failure_at == "release" else None,
        return_value=RELEASE,
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            release_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        authorize = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(
            authorize["flow_id"], {}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize_adb"
    assert result["errors"] == {"base": expected_error}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_candidate_release_resolution_failure_is_non_mutating(
    hass: HomeAssistant,
) -> None:
    """An install candidate is not presented without an exact verified release."""
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            AsyncMock(
                return_value=_probe(
                    "install_candidate",
                    model="WF1589T",
                    serial="serial-123",
                    primary_abi="arm64-v8a",
                    android_sdk=31,
                )
            ),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            AsyncMock(side_effect=ReleaseResolutionError),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": "cannot_resolve_release"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_unexpected_release_failure_is_not_misclassified(
    hass: HomeAssistant,
) -> None:
    """Only the resolver's expected refusal becomes a release availability error."""
    clean = _probe(
        "install_candidate",
        model="WF1589T",
        serial="serial-123",
        primary_abi="arm64-v8a",
        android_sdk=31,
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            AsyncMock(return_value=clean),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_resolve_stable_release",
            AsyncMock(side_effect=RuntimeError("unexpected")),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["errors"] == {"base": "unknown"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("health_error", "probe_result"),
    [
        (RuntimeError("health"), _probe("install_candidate")),
        (CannotConnectError(), RuntimeError("probe")),
        (CannotConnectError(), _probe("future_state")),
    ],
)
async def test_install_unexpected_failures_are_safe(
    hass: HomeAssistant, health_error: Exception, probe_result: object
) -> None:
    """Unknown failures and future states neither escape nor create an entry."""
    probe_mock = (
        AsyncMock(side_effect=probe_result)
        if isinstance(probe_result, Exception)
        else AsyncMock(return_value=probe_result)
    )
    with (
        patch(
            "custom_components.ha_paneld.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=health_error),
        ),
        patch(
            "custom_components.ha_paneld.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": "unknown"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    "step_method",
    [
        "async_step_authorize_adb",
        "async_step_confirm_existing",
        "async_step_confirm_install_candidate",
    ],
)
async def test_stale_confirmation_submission_fails_closed(
    hass: HomeAssistant, step_method: str
) -> None:
    """A confirmation cannot succeed after its retained flow state is lost."""
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    result = await getattr(flow, step_method)({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"
