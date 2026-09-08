"""Tests for the ha-paneld config flow."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.panel_assistant.adb_credentials import (
    AdbCredential,
    AdbCredentialError,
)
from custom_components.panel_assistant.browser_delivery import (
    DATA_BROWSER_DELIVERY,
    async_register_browser_delivery,
)
from custom_components.panel_assistant.client import (
    CannotConnectError,
    InvalidAddressError,
    InvalidResponseError,
    PanelAddress,
    PanelHealth,
)
from custom_components.panel_assistant.config_flow import (
    HaPaneldConfigFlow,
    _install_candidate_placeholders,
)
from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.install_adb import (
    InstallAdbError,
    InstallAdbErrorCode,
)
from custom_components.panel_assistant.install_jobs import (
    InstallArtifact,
    InstallJobReceipt,
    InstallJobStoreError,
    InstallPhase,
    InstallResultCode,
    InstallTarget,
)
from custom_components.panel_assistant.install_network import (
    InstallNetworkError,
    InstallNetworkErrorCode,
    PinnedPanelTarget,
)
from custom_components.panel_assistant.provisioning import (
    InstallTargetProbe,
    InstallTargetState,
)
from custom_components.panel_assistant.release import (
    InstallDescriptor,
    ReleaseArtifact,
    ReleaseResolutionError,
)

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
DESCRIPTOR = InstallDescriptor(
    schema="io.github.maxlyth.hapaneld.install.v1",
    release_tag="v0.9.7",
    version_name="0.9.7",
    version_code=907,
    apk_name="ha-paneld-v0.9.7-manual-setup-required.apk",
    apk_size=1234,
    apk_sha256="a" * 64,
    package_id="io.github.maxlyth.hapaneld",
    signer_certificate_sha256=(
        "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"
    ),
    min_sdk=26,
    supported_abis=("arm64-v8a", "armeabi-v7a"),
    database_compatibility="hapaneld-db:v1:ha-paneld.db:1:1",
    launch_component="io.github.maxlyth.hapaneld/.MainActivity",
)
RELEASE = ReleaseArtifact(
    tag="v0.9.7",
    version="0.9.7",
    apk_name=DESCRIPTOR.apk_name,
    apk_url=f"https://example.invalid/{DESCRIPTOR.apk_name}",
    sha256="a" * 64,
    descriptor=DESCRIPTOR,
)
LEGACY_RELEASE = replace(RELEASE, descriptor=None)


@pytest.fixture(autouse=True)
def install_release_catalog():
    """Keep native setup discovery offline with published stable and RC choices."""
    with patch(
        "custom_components.panel_assistant.config_flow.async_list_install_releases",
        AsyncMock(
            return_value=[
                {"tag": "v0.9.7", "prerelease": False},
                {"tag": "v0.9.7-rc3", "prerelease": True},
                {"tag": "v0.9.7-rc4", "prerelease": True},
            ]
        ),
    ) as catalog:
        yield catalog


@pytest.fixture(autouse=True)
def install_network_pin() -> SimpleNamespace:
    """Keep config-flow tests off DNS while exposing the pinned target calls."""

    async def _pin(_hass: HomeAssistant, address: PanelAddress) -> PinnedPanelTarget:
        return PinnedPanelTarget(
            original=address,
            pinned=PanelAddress(host="192.168.1.23", port=address.port),
        )

    async def _revalidate(
        _hass: HomeAssistant, target: PinnedPanelTarget
    ) -> PinnedPanelTarget:
        return target

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_pin_install_target",
            AsyncMock(side_effect=_pin),
        ) as pin_mock,
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            AsyncMock(side_effect=_revalidate),
        ) as revalidate_mock,
    ):
        yield SimpleNamespace(pin=pin_mock, revalidate=revalidate_mock)


def _probe(state: str, **facts: str | int | None) -> InstallTargetProbe:
    """Return the provisioning probe shape consumed by the config flow."""
    return InstallTargetProbe(state=InstallTargetState(state), **facts)


async def _start_step(hass: HomeAssistant, step_id: str) -> dict:
    """Start the user flow and select one of its menu options."""
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": step_id}
    )


async def test_user_starts_with_install_first_menu(hass: HomeAssistant) -> None:
    """Offer browser USB, network installation and existing attachment."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == [
        "install_usb",
        "install_or_upgrade",
        "connect_existing",
    ]


async def test_first_user_flow_registers_browser_delivery_before_any_panel(
    hass: HomeAssistant, hass_client
) -> None:
    """First-panel delivery cannot depend on successful entry creation."""
    assert not hass.config_entries.async_entries(DOMAIN)
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert DATA_BROWSER_DELIVERY in hass.data.get(DOMAIN, {})
    service = hass.data[DOMAIN][DATA_BROWSER_DELIVERY]
    async_register_browser_delivery(hass)
    assert hass.data[DOMAIN][DATA_BROWSER_DELIVERY] is service
    client = await hass_client()
    response = await client.get(f"/api/panel_assistant/usb/release/{'a' * 32}/apk")
    assert response.status == 404
    assert await response.json() == {"error": "browser_release_not_found"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_usb_step_links_local_admin_panel_without_creating_entry(
    hass: HomeAssistant,
) -> None:
    """The installer opens through HA so credentials stay at the HA origin."""
    from homeassistant.components import frontend

    result = await _start_step(hass, "install_usb")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_usb"
    assert result["description_placeholders"] == {"usb_install_url": "/ha-paneld-usb"}
    panel = hass.data[frontend.DATA_PANELS]["ha-paneld-usb"]
    assert panel.require_admin is True
    assert panel.config["installer_url"] == "https://install.panel-assistant.io/"
    assert not hass.config_entries.async_entries(DOMAIN)
    returned = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert returned["type"] is FlowResultType.MENU
    assert returned["step_id"] == "user"
    assert not hass.config_entries.async_entries(DOMAIN)


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
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
        "custom_components.panel_assistant.config_flow.normalize_address",
        side_effect=InvalidAddressError,
    ):
        invalid = await hass.config_entries.flow.async_configure(
            invalid_form["flow_id"], {CONF_ADDRESS: "bad"}
        )

    unavailable_form = await _start_step(hass, "connect_existing")
    with patch(
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
            "custom_components.panel_assistant.config_flow.normalize_address",
            side_effect=InvalidAddressError,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "bad"}
        )

    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": "invalid_install_address"}
    assert result["description_placeholders"] == {
        "panel_access_url": (
            "https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware"
            "#gaining-adb--root-access"
        )
    }
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "expected_error"),
    [
        (InstallNetworkErrorCode.INVALID_HOST, "invalid_install_address"),
        (InstallNetworkErrorCode.RESOLUTION_FAILED, "install_resolution_failed"),
        (InstallNetworkErrorCode.RESOLUTION_TIMEOUT, "install_resolution_timeout"),
        (InstallNetworkErrorCode.TOO_MANY_RESULTS, "install_too_many_addresses"),
        (InstallNetworkErrorCode.UNSAFE_TARGET, "unsafe_install_target"),
        (InstallNetworkErrorCode.PINNED_TARGET_REMOVED, "install_target_changed"),
    ],
)
async def test_install_network_refusal_precedes_all_panel_contact(
    hass: HomeAssistant,
    code: InstallNetworkErrorCode,
    expected_error: str,
) -> None:
    """Unsafe or unresolved targets cannot reach HTTP or ADB from the install flow."""
    health_mock = AsyncMock()
    probe_mock = AsyncMock()
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_pin_install_target",
            AsyncMock(side_effect=InstallNetworkError(code)),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_or_upgrade"
    assert result["errors"] == {"base": expected_error}
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()


async def test_install_uses_pinned_address_for_health_and_adb(
    hass: HomeAssistant, install_network_pin: SimpleNamespace
) -> None:
    """The display identity stays original while both protocols use the LAN pin."""
    health_mock = AsyncMock(side_effect=CannotConnectError)
    probe_mock = AsyncMock(return_value=_probe("adb_unreachable"))
    with (
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient"
        ) as client_class,
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
    ):
        client_class.return_value.async_get_health = health_mock
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "Panel.local"}
        )

    assert result["errors"] == {"base": "adb_unreachable"}
    install_network_pin.pin.assert_awaited_once()
    assert install_network_pin.pin.await_args.args[1].stored_value == "panel.local"
    assert client_class.call_args.args[1].stored_value == "192.168.1.23"
    assert probe_mock.await_args.args[0].stored_value == "192.168.1.23"
    assert health_mock.await_count == 1


async def test_install_existing_panel_requires_confirmation(
    hass: HomeAssistant,
) -> None:
    """A healthy installation is connected only after an explicit confirmation."""
    probe_mock = AsyncMock()
    health_mock = AsyncMock(return_value=HEALTH)
    with (
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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


async def test_install_existing_panel_revalidates_pin_before_confirmation(
    hass: HomeAssistant,
) -> None:
    """A healthy endpoint cannot be connected through a changed DNS target."""
    health_mock = AsyncMock(return_value=HEALTH)
    with patch(
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
        health_mock,
    ):
        form = await _start_step(hass, "install_or_upgrade")
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        with patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            AsyncMock(
                side_effect=InstallNetworkError(
                    InstallNetworkErrorCode.PINNED_TARGET_REMOVED
                )
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                confirm["flow_id"], {}
            )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_existing"
    assert result["errors"] == {"base": "install_target_changed"}
    assert health_mock.await_count == 1
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_existing_panel_handles_unexpected_confirmation_failure(
    hass: HomeAssistant,
) -> None:
    """An unexpected revalidation failure creates no entry."""
    health_mock = AsyncMock(side_effect=[HEALTH, RuntimeError("unexpected")])
    with patch(
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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
        "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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
@pytest.mark.parametrize("empty_candidate", [False, True])
async def test_install_candidate_readiness_is_non_mutating_until_confirmation(
    hass: HomeAssistant, health_error: type[Exception], empty_candidate: bool
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=health_error),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            release_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        user_input = {CONF_ADDRESS: "Panel.local"}
        if empty_candidate:
            user_input["release_candidate"] = ""
        confirm = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input
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
        assert probe_mock.await_args.args[0].stored_value == "192.168.1.23"
        release_mock.assert_awaited_once()

        refreshed = await hass.config_entries.flow.async_configure(confirm["flow_id"])
        assert (
            refreshed["description_placeholders"] == confirm["description_placeholders"]
        )

        assert refreshed["type"] is FlowResultType.FORM

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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(return_value=incomplete),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
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
    assert probe_mock.await_args.args[0].stored_value == "192.168.1.23"
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
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
    assert probe_mock.await_args_list[0].args[0].stored_value == "192.168.1.23"
    assert probe_mock.await_args_list[1].args[0].stored_value == "192.168.1.23"
    assert probe_mock.await_args_list[1].args[1] is signer
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_install_authorization_revalidates_pin_before_loading_key(
    hass: HomeAssistant,
) -> None:
    """DNS drift blocks the authorization attempt before a key can be offered."""
    signer_mock = AsyncMock()
    probe_mock = AsyncMock(return_value=_probe("adb_unauthorized"))
    with (
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        authorize = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        with patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            AsyncMock(
                side_effect=InstallNetworkError(
                    InstallNetworkErrorCode.PINNED_TARGET_REMOVED
                )
            ),
        ) as revalidate_mock:
            result = await hass.config_entries.flow.async_configure(
                authorize["flow_id"], {}
            )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize_adb"
    assert result["errors"] == {"base": "install_target_changed"}
    revalidate_mock.assert_awaited_once()
    signer_mock.assert_not_awaited()
    assert probe_mock.await_count == 1
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("model", "display"),
    [
        ("WF1589T", "WF1589T"),
        (
            "![Panel](https://example.invalid) <img> &amp; `x` \\ *_{}",
            "&#33;&#91;Panel&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#41; "
            "&#60;img&#62; &#38;amp; &#96;x&#96; &#92; &#42;&#95;&#123;&#125;",
        ),
    ],
)
def test_install_confirmation_device_facts_are_literal(
    model: str, display: str
) -> None:
    """Escape only presentation, including entities and Markdown metacharacters."""
    probe = _probe(
        "install_candidate",
        model=model,
        serial="serial_123",
        primary_abi="arm64-v8a",
        android_sdk=31,
    )
    assert _install_candidate_placeholders(probe) == {
        "model": display,
        "serial": "serial&#95;123",
        "abi": "arm64-v8a",
        "sdk": "31",
    }
    assert probe.model == model and probe.serial == "serial_123"


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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
    assert probe_mock.await_args_list[0].args[0].stored_value == "192.168.1.23"
    assert probe_mock.await_args_list[1].args[0].stored_value == "192.168.1.23"
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            AsyncMock(side_effect=AdbCredentialError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(return_value=clean),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
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
        (
            CannotConnectError(),
            SimpleNamespace(state=SimpleNamespace(value="future_state")),
        ),
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
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=health_error),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
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


TARGET = InstallTarget(
    address="panel.local",
    pinned_address="192.168.1.23",
    adb_serial="serial-123",
    model="WF1589T",
    primary_abi="arm64-v8a",
    android_sdk=31,
)
ARTIFACT = InstallArtifact(
    descriptor_schema=DESCRIPTOR.schema,
    release_tag=DESCRIPTOR.release_tag,
    version_name=DESCRIPTOR.version_name,
    version_code=DESCRIPTOR.version_code,
    apk_name=DESCRIPTOR.apk_name,
    apk_sha256=DESCRIPTOR.apk_sha256,
    apk_size=DESCRIPTOR.apk_size,
    package_id=DESCRIPTOR.package_id,
    signer_certificate_sha256=DESCRIPTOR.signer_certificate_sha256,
    min_sdk=DESCRIPTOR.min_sdk,
    supported_abis=DESCRIPTOR.supported_abis,
    database_compatibility=DESCRIPTOR.database_compatibility,
    launch_component=DESCRIPTOR.launch_component,
)
CANDIDATE = InstallTargetProbe(
    state=InstallTargetState.INSTALL_CANDIDATE,
    model=TARGET.model,
    serial=TARGET.adb_serial,
    primary_abi=TARGET.primary_abi,
    android_sdk=TARGET.android_sdk,
)
CREDENTIAL = AdbCredential(signer=object(), generation_id="b" * 64)


def _rc_release() -> ReleaseArtifact:
    tag = "v0.9.7-rc3"
    apk = f"ha-paneld-{tag}-manual-setup-required.apk"
    return replace(
        RELEASE,
        tag=tag,
        version=tag[1:],
        apk_name=apk,
        descriptor=replace(
            DESCRIPTOR, release_tag=tag, version_name=tag[1:], apk_name=apk
        ),
    )


@pytest.mark.parametrize(
    "tag",
    [
        None,
        False,
        3,
        " ",
        "v0.9.7",
        "v0.9.7-rc03",
        "v0.9.7-rc3\n",
        "v0.9.7-rc" + "3" * 60,
    ],
)
async def test_invalid_rc_selection_precedes_all_contact(
    hass: HomeAssistant, tag: object
) -> None:
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.panel_assistant.config_flow.async_pin_install_target",
        AsyncMock(),
    ) as pin:
        result = await flow.async_step_install_or_upgrade(
            {CONF_ADDRESS: "panel.local", "release_candidate": tag}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"release_candidate": "invalid_release_candidate"}
    pin.assert_not_awaited()


async def test_rc_selection_requires_exact_translated_consent_and_frozen_plan(
    hass: HomeAssistant,
) -> None:
    selected = _rc_release()
    receipt = _receipt(InstallPhase.APPROVED)
    manager = _manager_for(receipt)
    stable = AsyncMock()
    rc = AsyncMock(return_value=selected)
    credential = AsyncMock(return_value=CREDENTIAL)
    progress = AsyncMock(
        return_value={"type": FlowResultType.ABORT, "reason": "install_worker_stopped"}
    )
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(return_value=CANDIDATE),
        ) as probe,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            stable,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_rc_release", rc
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            credential,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch.object(HaPaneldConfigFlow, "_async_show_install_progress", progress),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        assert "release_candidate" in form["data_schema"].schema
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {CONF_ADDRESS: "panel.local", "release_candidate": selected.tag},
        )
        assert preview["step_id"] == "confirm_install_rc"
        assert preview["description_placeholders"]["tag"] == selected.tag
        assert preview["description_placeholders"]["sha256"] == selected.sha256
        credential.assert_not_awaited()
        manager.async_create_or_join.assert_not_awaited()
        await hass.config_entries.flow.async_configure(preview["flow_id"], {})
    stable.assert_not_awaited()
    rc.assert_awaited_once()
    assert rc.await_args.args[1] == selected.tag
    assert probe.await_count == 2
    manager.async_create_or_join.assert_awaited_once()
    planned = manager.async_create_or_join.await_args.args[1]
    assert planned.release_tag == selected.tag
    assert planned.apk_sha256 == selected.sha256
    progress.assert_awaited_once_with(receipt)


async def test_rc_resolution_failure_never_falls_back_or_creates_credential(
    hass: HomeAssistant,
) -> None:
    with (
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(return_value=CANDIDATE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_rc_release",
            AsyncMock(side_effect=ReleaseResolutionError),
        ) as rc,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(),
        ) as stable,
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            AsyncMock(),
        ) as credential,
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {CONF_ADDRESS: "panel.local", "release_candidate": "v0.9.7-rc3"},
        )
    assert result["errors"] == {"base": "cannot_resolve_release"}
    rc.assert_awaited_once()
    stable.assert_not_awaited()
    credential.assert_not_awaited()


async def test_rc_requested_on_installed_panel_only_connects(
    hass: HomeAssistant,
) -> None:
    with (
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(return_value=HEALTH),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(),
        ) as adb,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_rc_release",
            AsyncMock(),
        ) as rc,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(),
        ) as stable,
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {CONF_ADDRESS: "panel.local", "release_candidate": "v0.9.7-rc3"},
        )
        assert result["step_id"] == "confirm_existing"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: "panel.local"}
    adb.assert_not_awaited()
    rc.assert_not_awaited()
    stable.assert_not_awaited()


@pytest.mark.parametrize("requested", ["", "v0.9.7-rc3", "v0.9.7-rc4"])
@pytest.mark.parametrize("active_tag", ["v0.9.7", "v0.9.7-rc3"])
async def test_active_job_does_not_switch_release(
    hass: HomeAssistant, requested: str, active_tag: str
) -> None:
    receipt = replace(
        _receipt(InstallPhase.APPROVED),
        artifact=replace(
            ARTIFACT,
            release_tag=active_tag,
            version_name=active_tag[1:],
            apk_name=f"ha-paneld-{active_tag}-manual-setup-required.apk",
        ),
    )
    manager = _manager_for(receipt)
    manager.async_find_active.return_value = receipt
    progress = AsyncMock(
        return_value={"type": FlowResultType.ABORT, "reason": "install_worker_stopped"}
    )
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(),
        ) as health,
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(),
        ) as adb,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_rc_release",
            AsyncMock(),
        ) as rc,
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(),
        ) as stable,
        patch.object(HaPaneldConfigFlow, "_async_show_install_progress", progress),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {CONF_ADDRESS: "panel.local", "release_candidate": requested},
        )
    if requested and requested != active_tag:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "install_release_conflict"}
        progress.assert_not_awaited()
    else:
        progress.assert_awaited_once_with(receipt)
    health.assert_not_awaited()
    adb.assert_not_awaited()
    rc.assert_not_awaited()
    stable.assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()


def _receipt(
    phase: InstallPhase,
    *,
    result_code: InstallResultCode | None = None,
) -> InstallJobReceipt:
    """Build the receipt shape consumed by flow-only tests."""
    return InstallJobReceipt(
        job_id="1" * 32,
        revision=12,
        executor_generation=1,
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T00:01:00+00:00",
        phase=phase,
        cancel_requested=phase is InstallPhase.CANCELLED,
        attempt=1,
        target=TARGET,
        artifact=ARTIFACT,
        plan_sha256="c" * 64,
        adb_credential_id=CREDENTIAL.generation_id,
        preflight_root_mode=(
            None
            if phase
            in {
                InstallPhase.APPROVED,
                InstallPhase.AUTHORIZING,
                InstallPhase.PREFLIGHT,
            }
            else "rootless"
        ),
        actual_apk_bytes=(
            ARTIFACT.apk_size
            if phase
            in {
                InstallPhase.ARTIFACT_READY,
                InstallPhase.REVALIDATING,
                InstallPhase.STAGING,
                InstallPhase.INSTALLING,
                InstallPhase.INSTALLED,
                InstallPhase.LAUNCHING,
                InstallPhase.HEALTH_CHECK,
                InstallPhase.HEALTHY_UNCLAIMED,
                InstallPhase.CONSUMED,
            }
            else None
        ),
        health_checked_at=(
            "2026-09-03T00:00:30+00:00"
            if phase in {InstallPhase.HEALTHY_UNCLAIMED, InstallPhase.CONSUMED}
            else None
        ),
        result_code=result_code,
        consumed_entry_id=(
            "01M1JA9YX70TNCXNDNPJRC7QFC" if phase is InstallPhase.CONSUMED else None
        ),
    )


def _manager_for(receipt: InstallJobReceipt) -> SimpleNamespace:
    """Return the narrow receipt authority used by the flow."""
    return SimpleNamespace(
        async_find_active=AsyncMock(return_value=None),
        async_create_or_join=AsyncMock(return_value=(receipt, True)),
        async_get=AsyncMock(return_value=receipt),
        async_transition=AsyncMock(return_value=receipt),
    )


def _executor_for(*, acquired: bool = True) -> SimpleNamespace:
    """Return the narrow detached-executor API used by the flow."""
    return SimpleNamespace(
        async_ensure_job=AsyncMock(return_value=None),
        async_wait=AsyncMock(),
        async_acquire_finalizer=AsyncMock(return_value=acquired),
        async_release_finalizer=AsyncMock(),
    )


def _direct_result_flow(
    hass: HomeAssistant,
    receipt: InstallJobReceipt,
    executor: SimpleNamespace,
    *,
    flow_id: str = "flow-finalizer",
) -> HaPaneldConfigFlow:
    """Construct a finalization flow without involving unrelated UI setup."""
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    flow.flow_id = flow_id
    flow._pending_job_id = receipt.job_id
    flow._install_executor = executor
    return flow


async def test_descriptorless_unauthorized_release_never_loads_a_credential(
    hass: HomeAssistant,
) -> None:
    """Legacy releases remain exact preview-only before any ADB key is offered."""
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    credential_mock = AsyncMock()
    durable_mock = AsyncMock()
    signer_mock = AsyncMock()
    verify_mock = AsyncMock()
    probe_mock = AsyncMock(return_value=_probe("adb_unauthorized"))
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=LEGACY_RELEASE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            credential_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            durable_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_signer",
            signer_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            verify_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(preview["flow_id"], {})

    assert preview["step_id"] == "release_preview_only"
    assert preview["description_placeholders"] == {
        "address": "panel.local",
        "version": RELEASE.version,
        "tag": RELEASE.tag,
        "sha256": RELEASE.sha256,
    }
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "preview_only_release"
    credential_mock.assert_not_awaited()
    durable_mock.assert_not_awaited()
    signer_mock.assert_not_awaited()
    verify_mock.assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()
    assert len(probe_mock.await_args.args) == 1


@pytest.mark.parametrize(
    "changed_probe",
    [
        replace(CANDIDATE, state=InstallTargetState.INSTALLED),
        replace(CANDIDATE, serial="serial-456"),
        replace(CANDIDATE, model="other-model"),
        replace(CANDIDATE, primary_abi="armeabi-v7a"),
        replace(CANDIDATE, android_sdk=30),
    ],
    ids=["state", "serial", "model", "abi", "sdk"],
)
async def test_signed_confirmation_rejects_every_target_identity_drift(
    hass: HomeAssistant, changed_probe: InstallTargetProbe
) -> None:
    """Confirmation binds state, serial, model, ABI, and SDK to the preview."""
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    probe_mock = AsyncMock(side_effect=[CANDIDATE, changed_probe])
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await hass.config_entries.flow.async_configure(preview["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_install_candidate"
    assert result["errors"] == {"base": "install_candidate_changed"}
    manager.async_create_or_join.assert_not_awaited()


async def test_confirmation_late_duplicate_guard_precedes_all_contact(
    hass: HomeAssistant,
) -> None:
    """An entry created after preview prevents every confirmation side effect."""
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    revalidate_mock = AsyncMock()
    credential_mock = AsyncMock()
    probe_mock = AsyncMock(return_value=CANDIDATE)
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            revalidate_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            credential_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: "panel.local"}).add_to_hass(
            hass
        )
        result = await hass.config_entries.flow.async_configure(preview["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    revalidate_mock.assert_not_awaited()
    credential_mock.assert_not_awaited()
    assert probe_mock.await_count == 1


async def test_signed_confirmation_pin_drift_precedes_credentials_and_job(
    hass: HomeAssistant,
) -> None:
    """The original LAN pin remains part of the explicit install authorization."""
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    credential_mock = AsyncMock()
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(return_value=CANDIDATE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            credential_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        with patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            AsyncMock(
                side_effect=InstallNetworkError(
                    InstallNetworkErrorCode.PINNED_TARGET_REMOVED
                )
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                preview["flow_id"], {}
            )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "install_target_changed"}
    credential_mock.assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()


async def test_install_progress_removal_detaches_without_cancelling_worker(
    hass: HomeAssistant,
) -> None:
    """The flow passes only its shielded waiter to HA progress lifecycle."""
    receipt = _receipt(InstallPhase.APPROVED)
    manager = _manager_for(receipt)
    worker_gate = asyncio.Event()
    worker = hass.async_create_task(worker_gate.wait())

    async def _wait(_job_id: str) -> InstallJobReceipt:
        await asyncio.shield(worker)
        return receipt

    executor = _executor_for()
    executor.async_ensure_job.return_value = worker
    executor.async_wait.side_effect = _wait
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=CannotConnectError),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(side_effect=[CANDIDATE, CANDIDATE]),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            AsyncMock(return_value=RELEASE),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        preview = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        progress = await hass.config_entries.flow.async_configure(
            preview["flow_id"], {}
        )
        hass.config_entries.flow.async_abort(progress["flow_id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    executor.async_ensure_job.assert_awaited_once_with(receipt.job_id)
    assert not worker.cancelled()
    worker_gate.set()
    await worker


async def test_completed_flow_waiter_advances_through_progress_done() -> None:
    """HA receives the required progress-done transition before install_result."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    flow = HaPaneldConfigFlow()
    flow.flow_id = "flow-progress"
    flow._pending_job_id = receipt.job_id
    waiter: asyncio.Future[InstallJobReceipt] = asyncio.Future()
    waiter.set_result(receipt)
    flow._progress_waiter = waiter  # type: ignore[assignment]

    result = await flow.async_step_install_progress()

    assert result["type"] is FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "install_result"
    assert flow._progress_waiter is None


async def test_nonrestartable_worker_does_not_create_progress_callback_loop(
    hass: HomeAssistant,
) -> None:
    """A locally cancelled executor job requires restart without a new waiter."""
    receipt = _receipt(InstallPhase.DOWNLOADING)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    flow.flow_id = "flow-stopped-worker"
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        result = await flow._async_show_install_progress(receipt)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "install_worker_stopped"
    executor.async_wait.assert_not_awaited()
    assert flow._progress_waiter is None


async def test_nonrestartable_worker_refresh_failure_is_privacy_safe(
    hass: HomeAssistant,
) -> None:
    """Unexpected receipt refresh failure neither escapes nor creates a waiter."""
    receipt = _receipt(InstallPhase.DOWNLOADING)
    manager = _manager_for(receipt)
    manager.async_get.side_effect = RuntimeError("unexpected")
    executor = _executor_for()
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    flow.flow_id = "flow-stopped-worker-refresh"
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
    ):
        result = await flow._async_show_install_progress(receipt)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "install_failed"
    executor.async_wait.assert_not_awaited()
    assert flow._progress_waiter is None


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.APPROVED,
        InstallPhase.AUTHORIZING,
        InstallPhase.PREFLIGHT,
        InstallPhase.DOWNLOADING,
        InstallPhase.ARTIFACT_READY,
        InstallPhase.REVALIDATING,
        InstallPhase.INSTALLED,
        InstallPhase.HEALTH_CHECK,
    ],
)
async def test_existing_job_reattaches_before_any_panel_or_release_contact(
    hass: HomeAssistant, phase: InstallPhase
) -> None:
    """Opening the flow is sufficient to resume a safe zero-entry job."""
    receipt = _receipt(phase)
    manager = _manager_for(receipt)
    manager.async_find_active.return_value = receipt
    wait_gate = asyncio.Event()

    async def _wait(_job_id: str) -> InstallJobReceipt:
        await wait_gate.wait()
        return receipt

    executor = _executor_for()
    worker = hass.async_create_task(wait_gate.wait())
    executor.async_ensure_job.return_value = worker
    executor.async_wait.side_effect = _wait
    health_mock = AsyncMock()
    probe_mock = AsyncMock()
    release_mock = AsyncMock()
    credential_mock = AsyncMock()
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_executor",
            AsyncMock(return_value=executor),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            probe_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_resolve_stable_release",
            release_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_adb_credential",
            credential_mock,
        ),
    ):
        form = await _start_step(hass, "install_or_upgrade")
        progress = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        hass.config_entries.flow.async_abort(progress["flow_id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    manager.async_find_active.assert_awaited_once_with(
        TARGET.address, TARGET.pinned_address
    )
    executor.async_ensure_job.assert_awaited_once_with(receipt.job_id)
    health_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()
    release_mock.assert_not_awaited()
    credential_mock.assert_not_awaited()
    wait_gate.set()
    await worker


@pytest.mark.parametrize(
    ("phase", "code", "reason"),
    [
        (
            InstallPhase.CANCELLED,
            InstallResultCode.CANCELLED_BY_USER,
            "install_cancelled",
        ),
        (
            InstallPhase.CANCELLED,
            InstallResultCode.CANCELLED_AFTER_STAGING_CLEANUP,
            "install_cancelled_after_staging_cleanup",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.AUTHORIZATION_FAILED,
            "install_authorization_failed",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.PREFLIGHT_REJECTED,
            "install_preflight_rejected",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.ARTIFACT_REJECTED,
            "install_artifact_rejected",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.TRANSPORT_FAILED,
            "install_transport_failed",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.INSTALL_FAILED,
            "install_package_failed",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.LAUNCH_FAILED,
            "install_launch_failed",
        ),
        (
            InstallPhase.FAILED,
            InstallResultCode.HEALTH_CHECK_FAILED,
            "install_health_check_failed",
        ),
        (
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.AMBIGUOUS_MUTATION,
            "install_ambiguous_mutation",
        ),
        (
            InstallPhase.RECOVERY_REQUIRED,
            InstallResultCode.VERIFICATION_REQUIRED,
            "install_verification_required",
        ),
        (
            InstallPhase.CONSUMED,
            InstallResultCode.ENTRY_CREATED,
            "already_configured",
        ),
    ],
)
async def test_every_terminal_install_result_is_privacy_safe_and_creates_no_entry(
    hass: HomeAssistant,
    phase: InstallPhase,
    code: InstallResultCode,
    reason: str,
) -> None:
    """Terminal receipts expose only stable result categories."""
    receipt = _receipt(phase, result_code=code)
    manager = _manager_for(receipt)
    flow = _direct_result_flow(hass, receipt, _executor_for())
    with patch(
        "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
        AsyncMock(return_value=manager),
    ):
        result = await flow.async_step_install_result()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        ("dns_transport", FlowResultType.FORM),
        ("pin_drift", FlowResultType.ABORT),
        ("credential_error", FlowResultType.ABORT),
        ("credential_drift", FlowResultType.ABORT),
        ("adb_transport", FlowResultType.FORM),
        ("adb_identity", FlowResultType.ABORT),
        ("adb_root", FlowResultType.ABORT),
        ("adb_package", FlowResultType.ABORT),
        ("health_transport", FlowResultType.FORM),
        ("health_invalid", FlowResultType.ABORT),
        ("version_drift", FlowResultType.ABORT),
    ],
)
async def test_final_verification_retry_and_recovery_boundaries(
    hass: HomeAssistant, failure: str, expected_type: FlowResultType
) -> None:
    """Only temporary transport loss keeps a healthy receipt retryable."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    revalidate = AsyncMock(return_value=install_network_pin)
    durable = AsyncMock(return_value=CREDENTIAL)
    verify = AsyncMock()
    health = AsyncMock(return_value=replace(HEALTH, version=ARTIFACT.version_name))

    if failure == "dns_transport":
        revalidate.side_effect = InstallNetworkError(
            InstallNetworkErrorCode.RESOLUTION_TIMEOUT
        )
    elif failure == "pin_drift":
        revalidate.side_effect = InstallNetworkError(
            InstallNetworkErrorCode.PINNED_TARGET_REMOVED
        )
    elif failure == "credential_error":
        durable.side_effect = AdbCredentialError
    elif failure == "credential_drift":
        durable.return_value = AdbCredential(signer=object(), generation_id="d" * 64)
    elif failure == "adb_transport":
        verify.side_effect = InstallAdbError(InstallAdbErrorCode.TARGET_UNREACHABLE)
    elif failure == "adb_identity":
        verify.side_effect = InstallAdbError(InstallAdbErrorCode.TARGET_CHANGED)
    elif failure == "adb_root":
        verify.side_effect = InstallAdbError(InstallAdbErrorCode.ROOT_MODE_CHANGED)
    elif failure == "adb_package":
        verify.side_effect = InstallAdbError(
            InstallAdbErrorCode.INSTALLED_PACKAGE_MISSING
        )
    elif failure == "health_transport":
        health.side_effect = CannotConnectError
    elif failure == "health_invalid":
        health.side_effect = InvalidResponseError
    elif failure == "version_drift":
        health.return_value = HEALTH

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            revalidate,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            durable,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            verify,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health,
        ),
    ):
        result = await flow.async_step_install_result()

    assert result["type"] is expected_type
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, flow.flow_id
    )
    if expected_type is FlowResultType.FORM:
        assert result["errors"] == {"base": "install_finalization_retry"}
        manager.async_transition.assert_not_awaited()
    else:
        assert result["reason"] == "install_recovery_required"
        manager.async_transition.assert_awaited_once_with(
            receipt.job_id,
            receipt.revision,
            InstallPhase.RECOVERY_REQUIRED,
            result_code=InstallResultCode.VERIFICATION_REQUIRED,
        )
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_two_finalizers_produce_only_one_create_result(
    hass: HomeAssistant,
) -> None:
    """The process-wide lease prevents two dialogs creating duplicate entries."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    owner: str | None = None

    async def _acquire(_job_id: str, flow_id: str) -> bool:
        nonlocal owner
        if owner is None:
            owner = flow_id
            return True
        return owner == flow_id

    async def _release(_job_id: str, flow_id: str) -> None:
        nonlocal owner
        if owner == flow_id:
            owner = None

    executor = _executor_for()
    executor.async_acquire_finalizer.side_effect = _acquire
    executor.async_release_finalizer.side_effect = _release
    flow_one = _direct_result_flow(hass, receipt, executor, flow_id="flow-one")
    flow_two = _direct_result_flow(hass, receipt, executor, flow_id="flow-two")
    verify_started = asyncio.Event()
    allow_verify = asyncio.Event()

    async def _verify(*_args: object, **_kwargs: object) -> None:
        verify_started.set()
        await allow_verify.wait()

    verify_mock = AsyncMock(side_effect=_verify)

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            verify_mock,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(return_value=replace(HEALTH, version=ARTIFACT.version_name)),
        ),
    ):
        first_task = hass.async_create_task(flow_one.async_step_install_result())
        await verify_started.wait()
        second = await flow_two.async_step_install_result()
        allow_verify.set()
        first = await first_task

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert first["title"] == HEALTH.panel_id
    assert first["data"] == {CONF_ADDRESS: TARGET.address}
    assert flow_one.unique_id is None
    assert second["type"] is FlowResultType.FORM
    assert second["errors"] == {"base": "install_finalization_busy"}
    verified_target = verify_mock.await_args.args[0]
    assert verified_target.address.stored_value == TARGET.pinned_address
    assert verified_target.serial == TARGET.adb_serial
    assert verified_target.model == TARGET.model
    assert verified_target.primary_abi == TARGET.primary_abi
    assert verified_target.android_sdk == TARGET.android_sdk
    assert verify_mock.await_args.kwargs["expected_root_mode"].value == "rootless"


async def test_removal_during_verification_defers_finalizer_release(
    hass: HomeAssistant,
) -> None:
    """Closing one dialog cannot expose its lease while verification still runs."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    owner: str | None = None

    async def _acquire(_job_id: str, flow_id: str) -> bool:
        nonlocal owner
        if owner is None:
            owner = flow_id
            return True
        return owner == flow_id

    async def _release(_job_id: str, flow_id: str) -> None:
        nonlocal owner
        if owner == flow_id:
            owner = None

    executor = _executor_for()
    executor.async_acquire_finalizer.side_effect = _acquire
    executor.async_release_finalizer.side_effect = _release
    first = _direct_result_flow(hass, receipt, executor, flow_id="flow-removed")
    second = _direct_result_flow(hass, receipt, executor, flow_id="flow-other")
    verify_started = asyncio.Event()
    allow_verify = asyncio.Event()

    async def _verify(*_args: object, **_kwargs: object) -> None:
        verify_started.set()
        await allow_verify.wait()

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            AsyncMock(side_effect=_verify),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(return_value=replace(HEALTH, version=ARTIFACT.version_name)),
        ),
    ):
        first_task = hass.async_create_task(first.async_step_install_result())
        await verify_started.wait()
        first.async_remove()
        await asyncio.sleep(0)
        executor.async_release_finalizer.assert_not_awaited()
        other_result = await second.async_step_install_result()
        allow_verify.set()
        first_result = await first_task
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert other_result["type"] is FlowResultType.FORM
    assert other_result["errors"] == {"base": "install_finalization_busy"}
    assert first_result["type"] is FlowResultType.ABORT
    assert first_result["reason"] == "install_worker_stopped"
    executor.async_release_finalizer.assert_any_await(receipt.job_id, first.flow_id)
    executor.async_release_finalizer.assert_any_await(receipt.job_id, second.flow_id)


async def test_removal_during_lease_acquisition_cannot_leak_finalizer(
    hass: HomeAssistant,
) -> None:
    """Flow removal is fenced before the executor lease call can complete."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    acquire_started = asyncio.Event()
    allow_acquire = asyncio.Event()

    async def _acquire(_job_id: str, _flow_id: str) -> bool:
        acquire_started.set()
        await allow_acquire.wait()
        return True

    executor.async_acquire_finalizer.side_effect = _acquire
    durable = AsyncMock(return_value=CREDENTIAL)
    verify = AsyncMock()
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            durable,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            verify,
        ),
    ):
        task = hass.async_create_task(flow.async_step_install_result())
        await acquire_started.wait()
        flow.async_remove()
        executor.async_release_finalizer.assert_not_awaited()
        allow_acquire.set()
        result = await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "install_worker_stopped"
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, flow.flow_id
    )
    durable.assert_not_awaited()
    verify.assert_not_awaited()


async def test_cancellation_at_release_lock_keeps_lease_retryable(
    hass: HomeAssistant,
) -> None:
    """Caller cancellation cannot orphan a lease or duplicate its release."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    owner: str | None = None
    release_started = asyncio.Event()
    allow_first_release = asyncio.Event()
    release_count = 0

    async def _acquire(_job_id: str, flow_id: str) -> bool:
        nonlocal owner
        if owner is None:
            owner = flow_id
            return True
        return owner == flow_id

    async def _release(_job_id: str, flow_id: str) -> None:
        nonlocal owner, release_count
        release_count += 1
        if release_count == 1:
            release_started.set()
            await allow_first_release.wait()
        if owner == flow_id:
            owner = None

    executor.async_acquire_finalizer.side_effect = _acquire
    executor.async_release_finalizer.side_effect = _release
    revalidate = AsyncMock(
        side_effect=InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
    )
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            revalidate,
        ),
    ):
        first_task = hass.async_create_task(flow.async_step_install_result())
        await release_started.wait()
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        assert flow._finalizer_job_id == receipt.job_id
        assert flow._finalizer_release_task is not None
        busy = await flow.async_step_install_result()
        assert busy["type"] is FlowResultType.FORM
        assert busy["errors"] == {"base": "install_finalization_busy"}
        assert executor.async_acquire_finalizer.await_count == 1
        assert executor.async_release_finalizer.await_count == 1

        allow_first_release.set()
        await flow._finalizer_release_task
        await asyncio.sleep(0)
        assert flow._finalizer_job_id is None

        retry = await flow.async_step_install_result()

    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "install_finalization_retry"}
    assert executor.async_acquire_finalizer.await_count == 2
    assert executor.async_release_finalizer.await_count == 2
    assert owner is None
    assert flow._finalizer_job_id is None


async def test_completed_owner_retries_a_failed_finalizer_release(
    hass: HomeAssistant,
) -> None:
    """A later submission can finish cleanup left by a failed owner task."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    executor.async_release_finalizer.side_effect = [RuntimeError, None, None]
    revalidate = AsyncMock(
        side_effect=InstallNetworkError(InstallNetworkErrorCode.RESOLUTION_FAILED)
    )

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            revalidate,
        ),
    ):
        first_task = hass.async_create_task(flow.async_step_install_result())
        with pytest.raises(RuntimeError):
            await first_task
        await asyncio.sleep(0)

        assert flow._finalizer_job_id == receipt.job_id
        assert flow._finalization_owner_task is first_task
        assert first_task.done()

        retry = await flow.async_step_install_result()

    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "install_finalization_retry"}
    assert executor.async_acquire_finalizer.await_count == 2
    assert executor.async_release_finalizer.await_count == 3
    assert flow._finalizer_job_id is None
    assert flow._finalization_owner_task is None


async def test_removed_flow_retries_failed_background_release(
    hass: HomeAssistant,
) -> None:
    """A removed flow retries cleanup because no later UI request can do so."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    executor = _executor_for()
    executor.async_release_finalizer.side_effect = [RuntimeError, None]
    flow = _direct_result_flow(hass, receipt, executor)
    flow._finalizer_job_id = receipt.job_id

    flow.async_remove()
    for _ in range(4):
        await asyncio.sleep(0)

    assert executor.async_release_finalizer.await_count == 2
    assert flow._finalizer_job_id is None
    assert flow._finalization_owner_task is None


async def test_removed_active_owner_shares_background_release_retry_budget(
    hass: HomeAssistant,
) -> None:
    """Deferred owner cleanup cannot add a third removed-flow release attempt."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    release_started = asyncio.Event()
    allow_failure = asyncio.Event()
    release_count = 0

    async def _release(_job_id: str, _flow_id: str) -> None:
        nonlocal release_count
        release_count += 1
        if release_count == 1:
            release_started.set()
            await allow_failure.wait()
        raise RuntimeError

    executor.async_release_finalizer.side_effect = _release
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_revalidate_install_target",
            AsyncMock(
                side_effect=InstallNetworkError(
                    InstallNetworkErrorCode.RESOLUTION_FAILED
                )
            ),
        ),
    ):
        owner = hass.async_create_task(flow.async_step_install_result())
        await release_started.wait()
        flow.async_remove()
        allow_failure.set()
        with pytest.raises(RuntimeError):
            await owner
        for _ in range(4):
            await asyncio.sleep(0)

    assert executor.async_release_finalizer.await_count == 2
    assert flow._finalizer_job_id == receipt.job_id
    assert flow._finalization_owner_task is owner
    assert flow._removed_release_retry_started


async def test_same_flow_double_submit_runs_one_finalizer(
    hass: HomeAssistant,
) -> None:
    """Concurrent submissions in one dialog cannot share its executor lease."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    verify_started = asyncio.Event()
    allow_verify = asyncio.Event()
    verify = AsyncMock()

    async def _verify(*_args: object, **_kwargs: object) -> None:
        verify_started.set()
        await allow_verify.wait()

    verify.side_effect = _verify
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            verify,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(return_value=replace(HEALTH, version=ARTIFACT.version_name)),
        ),
    ):
        first_task = hass.async_create_task(flow.async_step_install_result())
        await verify_started.wait()
        second_task = hass.async_create_task(flow.async_step_install_result())
        await asyncio.sleep(0)
        allow_verify.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)
        flow.async_remove()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert first_result["type"] is FlowResultType.CREATE_ENTRY
    assert second_result["type"] is FlowResultType.FORM
    assert second_result["errors"] == {"base": "install_finalization_busy"}
    assert executor.async_acquire_finalizer.await_count == 1
    assert verify.await_count == 1


async def test_final_duplicate_guard_rechecks_after_health_contact(
    hass: HomeAssistant,
) -> None:
    """An entry added during final verification wins before create_entry is returned."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)

    async def _health_then_duplicate() -> PanelHealth:
        MockConfigEntry(
            domain=DOMAIN, data={CONF_ADDRESS: receipt.target.address}
        ).add_to_hass(hass)
        return replace(HEALTH, version=ARTIFACT.version_name)

    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_durable_adb_credential",
            AsyncMock(return_value=CREDENTIAL),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_verify_installed_target",
            AsyncMock(),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            AsyncMock(side_effect=_health_then_duplicate),
        ),
    ):
        result = await flow.async_step_install_result()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, flow.flow_id
    )
    manager.async_transition.assert_not_awaited()


@pytest.mark.parametrize("persist_fails", [False, True], ids=["success", "failure"])
async def test_on_create_uses_actual_entry_id_and_never_fails_existing_entry(
    hass: HomeAssistant, persist_fails: bool
) -> None:
    """Receipt cleanup cannot undo an entry already added by Home Assistant."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    manager = _manager_for(receipt)
    if persist_fails:
        manager.async_transition.side_effect = InstallJobStoreError
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    flow._finalizer_job_id = receipt.job_id
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: TARGET.address})
    entry.add_to_hass(hass)
    result: dict = {"result": entry}

    with patch(
        "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
        AsyncMock(return_value=manager),
    ):
        returned = await flow.async_on_create_entry(result)  # type: ignore[arg-type]

    assert returned is result
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    manager.async_transition.assert_awaited_once_with(
        receipt.job_id,
        receipt.revision,
        InstallPhase.CONSUMED,
        result_code=InstallResultCode.ENTRY_CREATED,
        consumed_entry_id=entry.entry_id,
    )
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, flow.flow_id
    )


async def test_flow_removal_releases_lease_and_only_cancels_local_waiter(
    hass: HomeAssistant,
) -> None:
    """Removal cleans up flow ownership without touching an executor worker."""
    receipt = _receipt(InstallPhase.HEALTHY_UNCLAIMED)
    executor = _executor_for()
    flow = _direct_result_flow(hass, receipt, executor)
    flow._finalizer_job_id = receipt.job_id
    waiter_gate = asyncio.Event()
    worker_gate = asyncio.Event()
    flow._progress_waiter = hass.async_create_task(waiter_gate.wait())
    worker = hass.async_create_task(worker_gate.wait())

    flow.async_remove()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert flow._progress_waiter is None
    executor.async_release_finalizer.assert_awaited_once_with(
        receipt.job_id, flow.flow_id
    )
    assert not worker.cancelled()
    worker_gate.set()
    await worker
