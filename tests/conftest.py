"""Shared pytest fixtures for ha-paneld."""

from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest

from custom_components.panel_assistant import config_flow
from custom_components.panel_assistant.client import HaPaneldClient, PanelInstallStatus


@pytest.fixture(autouse=True)
def _enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Allow Home Assistant to load the integration under test."""
    yield


@pytest.fixture(autouse=True)
def _stub_panel_update_operation_in_lifecycle_tests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Generator[None]:
    """Keep lifecycle tests on their declared local panel fixtures."""
    if request.path.name != "test_client.py":
        monkeypatch.setattr(
            HaPaneldClient,
            "async_get_panel_install_status",
            AsyncMock(return_value=PanelInstallStatus(running=False, component="")),
        )
        # A found panel reports its setup as done unless a test says otherwise.
        monkeypatch.setattr(
            HaPaneldClient, "async_get_setup_complete", AsyncMock(return_value=True)
        )
        # Reachability is answered locally; only its own tests open that path.
        monkeypatch.setattr(
            config_flow, "_async_host_answers", AsyncMock(return_value=True)
        )
    yield
