"""The one device card every platform shares, and the identity it must not adopt."""

from __future__ import annotations

from custom_components.panel_assistant.client import PanelHealth
from custom_components.panel_assistant.const import DOMAIN
from custom_components.panel_assistant.coordinator import PanelSnapshot
from custom_components.panel_assistant.device import panel_device_info
from custom_components.panel_assistant.status import PanelDevice, PanelStatus

HEALTH = PanelHealth(
    version="0.9.7-rc4",
    panel_id="alpha_panel",
    build="1788979164237",
    config_hash="7ac44093",
)
URL = "http://192.168.1.23:8888/"


def _snapshot(device: PanelDevice | None, *, with_status: bool = True) -> PanelSnapshot:
    status = (
        PanelStatus(warning_count=0, capability_count=0, panel_assistant_device=device)
        if with_status
        else None
    )
    return PanelSnapshot(health=HEALTH, status=status, status_error=None)


def test_reported_facts_fill_every_card_field() -> None:
    """A panel that states its hardware should leave no blank on the device page."""
    info = panel_device_info(
        "entry-1",
        _snapshot(
            PanelDevice(
                name="Alpha panel",
                manufacturer="Acme",
                model="AP-1",
                hw_version="Android 14 · TQ3A",
                area="Study",
            )
        ),
        URL,
    )

    assert info["name"] == "Alpha panel"
    assert info["manufacturer"] == "Acme"
    assert info["model"] == "AP-1"
    assert info["hw_version"] == "Android 14 · TQ3A"
    assert info["suggested_area"] == "Study"
    assert info["sw_version"] == "0.9.7-rc4"
    assert info["configuration_url"] == URL


def test_the_card_never_adopts_an_identity_the_mqtt_bridge_owns() -> None:
    """Sharing an identifier or a MAC merges the devices and splits authority."""
    info = panel_device_info(
        "entry-1",
        _snapshot(PanelDevice(name="Alpha panel", manufacturer="Acme")),
        URL,
    )

    assert info["identifiers"] == {(DOMAIN, "entry-1")}
    assert "connections" not in info
    assert "serial_number" not in info


def test_a_silent_panel_still_produces_a_usable_card() -> None:
    """Panels below the release that added the projection must not blank the page."""
    for snapshot in (_snapshot(None), _snapshot(None, with_status=False)):
        info = panel_device_info("entry-1", snapshot, URL)

        assert info["name"] == "alpha_panel"
        assert info["model"] == "ha-paneld"
        assert info["sw_version"] == "0.9.7-rc4"
        assert "manufacturer" not in info
        assert "hw_version" not in info
        assert "suggested_area" not in info


def test_a_partial_report_fills_only_what_the_panel_stated() -> None:
    """A field the panel dropped stays absent instead of arriving empty."""
    info = panel_device_info(
        "entry-1", _snapshot(PanelDevice(manufacturer="Acme", area="Study")), URL
    )

    assert info["manufacturer"] == "Acme"
    assert info["suggested_area"] == "Study"
    assert info["name"] == "alpha_panel"
    assert info["model"] == "ha-paneld"
    assert "hw_version" not in info
