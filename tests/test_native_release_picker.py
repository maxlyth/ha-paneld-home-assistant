"""Published release choices and offline native setup behavior."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

from custom_components.panel_assistant.config_flow import HaPaneldConfigFlow
from custom_components.panel_assistant.release import ReleaseResolutionError

from .test_config_flow import (
    CANDIDATE,
    HEALTH,
    CannotConnectError,
    InstallPhase,
    _connect_found,
    _manager_for,
    _receipt,
    _start_step,
    install_network_pin,  # noqa: F401
)
from .test_release_catalog import document, session

_FLOW = "custom_components.panel_assistant.config_flow"


def _patch_clean_panel(stack: ExitStack, manager) -> dict[str, AsyncMock]:
    """Patch a panel with no ha-paneld and a clean ADB target; return the mocks."""
    mocks = {
        "manager": AsyncMock(return_value=manager),
        "health": AsyncMock(side_effect=CannotConnectError),
        "probe": AsyncMock(return_value=CANDIDATE),
        "stable": AsyncMock(),
        "rc": AsyncMock(),
        "credential": AsyncMock(),
    }
    for target, name in [
        ("async_get_install_job_manager", "manager"),
        ("HaPaneldClient.async_get_health", "health"),
        ("async_probe_install_target", "probe"),
        ("async_resolve_stable_release", "stable"),
        ("async_resolve_rc_release", "rc"),
        ("async_get_adb_credential", "credential"),
    ]:
        stack.enter_context(patch(f"{_FLOW}.{target}", mocks[name]))
    return mocks


async def test_real_catalog_populates_selector_and_caches(hass):
    """The actual discovery helper feeds exact published tags into HA validation."""
    client = session(document(), [document("v1.3.0-rc2", prerelease=True)])
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    with ExitStack() as stack:
        _patch_clean_panel(stack, _manager_for(_receipt(InstallPhase.APPROVED)))
        stack.enter_context(
            patch(f"{_FLOW}.async_get_clientsession", return_value=client)
        )
        address_form = await flow.async_step_add_panel()
        assert client.requests == []
        form = await flow.async_step_add_panel({CONF_ADDRESS: "panel.local"})
        again = await flow.async_step_choose_version()
    assert "release_candidate" not in address_form["data_schema"].schema
    assert form["step_id"] == "choose_version"
    assert len(client.requests) == 2
    selector = form["data_schema"].schema["release_candidate"]
    assert selector.config["options"] == [
        {"value": "", "label": "v1.2.3 (recommended)"},
        {"value": "v1.3.0-rc2", "label": "v1.3.0-rc2 (test version)"},
    ]
    assert form["errors"] is None
    assert again["errors"] is None
    assert form["data_schema"]({})["release_candidate"] == ""
    with pytest.raises(vol.Invalid):
        form["data_schema"]({"release_candidate": "v9.9.9-rc1"})


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
    with ExitStack() as stack:
        mocks = _patch_clean_panel(stack, manager)
        if state == "healthy":
            mocks["health"].side_effect = None
            mocks["health"].return_value = HEALTH
        stack.enter_context(patch(f"{_FLOW}.async_list_install_releases", catalog))
        progress = stack.enter_context(
            patch.object(
                flow,
                "_async_show_install_progress",
                AsyncMock(return_value={"type": FlowResultType.SHOW_PROGRESS}),
            )
        )
        form = await flow.async_step_add_panel()
        assert form["step_id"] == "add_panel"
        result = await flow.async_step_add_panel({CONF_ADDRESS: "panel.local"})
        if state == "new":
            error = {
                "base": "release_catalog_unavailable"
                if failed
                else "release_catalog_empty"
            }
            assert result["step_id"] == "choose_version"
            assert result["errors"] == error
            # Nothing to choose: the form has no field, so a plain submit works.
            assert "release_candidate" not in result["data_schema"].schema
            # Submitting again reloads the catalogue rather than installing.
            result = await flow.async_step_choose_version({})
            assert result["step_id"] == "choose_version"
            assert result["errors"] == error
    if state == "saved":
        progress.assert_awaited_once_with(receipt)
        mocks["health"].assert_not_awaited()
    else:
        progress.assert_not_awaited()
    if state == "healthy":
        assert result["step_id"] == "found_panel"
    if state == "new":
        mocks["probe"].assert_awaited_once()
        assert len(mocks["probe"].await_args.args) == 1
    else:
        mocks["probe"].assert_not_awaited()
    mocks["stable"].assert_not_awaited()
    mocks["rc"].assert_not_awaited()
    mocks["credential"].assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()
    assert catalog.await_count == (2 if state == "new" else 0)


async def test_connect_found_never_loads_catalog(hass):
    """Connecting a running panel needs no release, so the catalogue is untouched."""
    with (
        patch(f"{_FLOW}.async_list_install_releases", AsyncMock()) as catalog,
        patch(
            f"{_FLOW}.HaPaneldClient.async_get_health", AsyncMock(return_value=HEALTH)
        ),
    ):
        form = await _start_step(hass, "add_panel")
        found = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        result = await _connect_found(hass, found)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    catalog.assert_not_awaited()


async def test_retry_requires_fresh_selection_before_new_install(hass):
    """An empty stable choice cannot silently become consent to another release."""
    flow = HaPaneldConfigFlow()
    flow.hass = hass
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    catalog = AsyncMock(
        side_effect=[
            [{"tag": "v1.3.0-rc2", "prerelease": True}],
            [{"tag": "v1.2.3", "prerelease": False}],
        ]
    )
    with ExitStack() as stack:
        mocks = _patch_clean_panel(stack, manager)
        stack.enter_context(patch(f"{_FLOW}.async_list_install_releases", catalog))
        form = await flow.async_step_add_panel({CONF_ADDRESS: "panel.local"})
        assert form["data_schema"].schema["release_candidate"].config["options"] == [
            {"value": "v1.3.0-rc2", "label": "v1.3.0-rc2 (test version)"}
        ]
        result = await flow.async_step_choose_version({"release_candidate": ""})
    assert result["errors"] == {"base": "release_selection_required"}
    assert result["data_schema"].schema["release_candidate"].config["options"] == [
        {"value": "v1.3.0-rc2", "label": "v1.3.0-rc2 (test version)"}
    ]
    assert catalog.await_count == 1
    mocks["probe"].assert_awaited_once()
    mocks["stable"].assert_not_awaited()
    mocks["rc"].assert_not_awaited()
    mocks["credential"].assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()


async def test_empty_catalogue_retry_submits_in_the_ui_and_offers_a_fresh_choice(hass):
    """A reload that finds versions shows them; it never installs the old submit."""
    manager = _manager_for(_receipt(InstallPhase.APPROVED))
    manager.async_find_active.return_value = None
    stable = {"tag": "v0.9.6", "prerelease": False}
    catalog = AsyncMock(side_effect=[ReleaseResolutionError(), [stable]])
    with ExitStack() as stack:
        mocks = _patch_clean_panel(stack, manager)
        stack.enter_context(patch(f"{_FLOW}.async_list_install_releases", catalog))
        form = await _start_step(hass, "add_panel")
        empty = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ADDRESS: "panel.local"}
        )
        assert empty["step_id"] == "choose_version"
        assert empty["errors"] == {"base": "release_catalog_unavailable"}
        # Through the real flow manager, the empty form is submittable.
        fresh = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert fresh["step_id"] == "choose_version"
    assert not fresh["errors"]
    options = fresh["data_schema"].schema["release_candidate"].config["options"]
    assert [option["value"] for option in options] == [""]
    assert catalog.await_count == 2
    mocks["stable"].assert_not_awaited()
    mocks["credential"].assert_not_awaited()
    manager.async_create_or_join.assert_not_awaited()
