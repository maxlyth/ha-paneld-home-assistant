"""Shared pytest fixtures for ha-paneld."""

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def _enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Allow Home Assistant to load the integration under test."""
    yield
