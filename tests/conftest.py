"""Shared pytest fixtures for ha-paneld."""

from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest

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
    yield
