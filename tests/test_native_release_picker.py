"""Published release choices and offline native setup behavior."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

from custom_components.panel_assistant.config_flow import HaPaneldConfigFlow
from custom_components.panel_assistant.release import ReleaseResolutionError

from .test_config_flow import (
    HEALTH,
    CannotConnectError,
    InstallPhase,
    _manager_for,
    _receipt,
    install_network_pin,  # noqa: F401
)
from .test_release_catalog import document, session


async def test_real_catalog_populates_selector_and_caches(hass):
    """The actual discovery helper feeds exact published tags into HA validation."""
    client = session(document(), [document("v1.3.0-rc2", prerelease=True)])
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.panel_assistant.config_flow.async_get_clientsession",
        return_value=client,
    ):
        form = await flow.async_step_install_or_upgrade()
        again = await flow.async_step_install_or_upgrade()
    assert len(client.requests) == 2
    selector = form["data_schema"].schema["release_candidate"]
    assert selector.config["options"] == [
        {"value": "", "label": "v1.2.3 (stable)"},
        {"value": "v1.3.0-rc2", "label": "v1.3.0-rc2 (RC)"},
    ]
    assert selector.config["translation_key"] == "release_channel"
    assert again["errors"] == {}
    assert form["data_schema"]({CONF_ADDRESS: "panel.local"})["release_candidate"] == ""
    with pytest.raises(vol.Invalid):
        form["data_schema"](
            {CONF_ADDRESS: "panel.local", "release_candidate": "v9.9.9-rc1"}
        )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("state", ["new", "healthy", "saved"])
async def test_unavailable_catalog_preserves_attach_and_resume(hass, failed, state):
    """No catalogue cannot authorize a fresh install, but existing work survives."""
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    receipt = _receipt(InstallPhase.APPROVED)
    manager = _manager_for(receipt)
    manager.async_find_active.return_value = receipt if state == "saved" else None
    catalog = (
        AsyncMock(side_effect=ReleaseResolutionError)
        if failed
        else AsyncMock(return_value=[])
    )
    health = (
        AsyncMock(return_value=HEALTH)
        if state == "healthy"
        else AsyncMock(side_effect=CannotConnectError)
    )
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_list_install_releases",
            catalog,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_get_install_job_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.panel_assistant.config_flow.HaPaneldClient.async_get_health",
            health,
        ),
        patch(
            "custom_components.panel_assistant.config_flow.async_probe_install_target",
            AsyncMock(),
        ) as adb,
        patch.object(
            flow,
            "_async_show_install_progress",
            AsyncMock(return_value={"type": FlowResultType.SHOW_PROGRESS}),
        ) as progress,
    ):
        form = await flow.async_step_install_or_upgrade()
        assert form["errors"] == {
            "base": "release_catalog_unavailable" if failed else "release_catalog_empty"
        }
        assert form["data_schema"].schema["release_candidate"].config["options"] == [
            {"value": "", "label": "resume_existing"}
        ]
        result = await flow.async_step_install_or_upgrade(
            {CONF_ADDRESS: "panel.local", "release_candidate": ""}
        )
    adb.assert_not_awaited()
    if state == "saved":
        progress.assert_awaited_once_with(receipt)
    elif state == "healthy":
        assert result["step_id"] == "confirm_existing"
    else:
        assert result["errors"] == form["errors"]
    assert catalog.await_count == (2 if state == "new" else 1)


async def test_connect_existing_never_loads_catalog(hass):
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    with patch(
        "custom_components.panel_assistant.config_flow.async_list_install_releases",
        AsyncMock(),
    ) as catalog:
        result = await flow.async_step_connect_existing()
    assert result["step_id"] == "connect_existing"
    catalog.assert_not_awaited()


@pytest.mark.parametrize("initial", [[], [{"tag": "v1.3.0-rc2", "prerelease": True}]])
async def test_retry_requires_fresh_selection_before_new_install(hass, initial):
    """Resume-only cannot silently become consent to a newly discovered release."""
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    manager.async_find_active.return_value = None
    with (
        patch(
            "custom_components.panel_assistant.config_flow.async_list_install_releases",
            AsyncMock(side_effect=[initial, [{"tag": "v1.2.3", "prerelease": False}]]),
        ),
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
            AsyncMock(),
        ) as adb,
    ):
        form = await flow.async_step_install_or_upgrade()
        assert (
            form["data_schema"]
            .schema["release_candidate"]
            .config["options"][0]["label"]
            == "resume_existing"
        )
        result = await flow.async_step_install_or_upgrade({CONF_ADDRESS: "panel.local"})
    assert result["errors"] == {"base": "release_selection_required"}
    assert result["data_schema"].schema["release_candidate"].config["options"] == [
        {"value": "", "label": "v1.2.3 (stable)"}
    ]
    adb.assert_not_awaited()
