"""Repository and distribution contract tests."""

import ast
import json
from pathlib import Path

from custom_components.ha_paneld.client import parse_health_response

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "ha_paneld"


def test_manifest_and_hacs_versions_match_repository_policy() -> None:
    """Distribution metadata has the deliberate independent version floor."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert manifest == {
        "codeowners": ["@maxlyth"],
        "config_flow": True,
        "dependencies": [],
        "documentation": "https://github.com/maxlyth/ha-paneld-home-assistant",
        "domain": "ha_paneld",
        "integration_type": "device",
        "iot_class": "local_polling",
        "issue_tracker": "https://github.com/maxlyth/ha-paneld-home-assistant/issues",
        "name": "ha-paneld",
        "requirements": ["adb-shell[async]==0.4.4"],
        "version": "0.1.0",
    }
    assert hacs == {"homeassistant": "2026.8.3", "name": "ha-paneld"}


def test_hacs_repository_foundation() -> None:
    """Local files cover the HACS checks that do not require GitHub metadata."""
    integration_directories = sorted(
        path.name for path in (ROOT / "custom_components").iterdir() if path.is_dir()
    )

    assert integration_directories == ["ha_paneld"]
    assert (ROOT / "README.md").is_file()
    assert (
        (ROOT / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("Apache License\nVersion 2.0")
    )
    icon = INTEGRATION / "brand" / "icon.png"
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_runtime_translations_are_complete() -> None:
    """The custom-component translation does not depend on Core placeholders."""
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert english == strings
    assert "[%key:" not in json.dumps(english)


def test_runtime_harness_health_fixture_uses_production_grammar() -> None:
    """Keep the real-Core fixture admissible by the shipping health parser."""
    source = (ROOT / "tests" / "runtime" / "core_negative_harness.py").read_text(
        encoding="utf-8"
    )
    health_bodies = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, bytes)
        and node.value.startswith(b"ha-paneld ")
    ]

    assert len(health_bodies) == 1
    health = parse_health_response(health_bodies[0].decode("ascii"))
    assert health.panel_id == "runtime_negative"


def test_readme_leads_with_complete_hacs_installation() -> None:
    """The primary installation path stays workstation-tool-free and discoverable."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hacs_heading = "## Install with HACS"
    manual_heading = "## Manual integration installation"

    assert readme.index(hacs_heading) < readme.index(manual_heading)
    hacs_section = readme.split(hacs_heading, 1)[1].split(manual_heading, 1)[0]
    assert (
        "https://my.home-assistant.io/redirect/hacs_repository/"
        "?owner=maxlyth&repository=ha-paneld-home-assistant&category=integration"
    ) in hacs_section
    assert "https://github.com/maxlyth/ha-paneld-home-assistant" in hacs_section
    assert "Git Bash, PowerShell, a workstation `adb` executable" in hacs_section
    assert (
        "https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware"
        "#gaining-adb--root-access"
    ) in hacs_section
    assert "root access is not a requirement" in hacs_section


def test_install_flow_copy_covers_first_time_handoffs() -> None:
    """Visible setup copy explains panel preparation, reattachment, and recovery."""
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    config = strings["config"]
    steps = config["step"]
    install = steps["install_or_upgrade"]["description"]
    progress = config["progress"]["installing"]
    all_errors = " ".join(config["error"].values())

    assert "network ADB on port 5555" in install
    assert "Root access is not required" in install
    assert "Leave this dialog open to finish automatically" in progress
    assert "Settings → Devices & services → Add integration" in progress
    assert "enter the same address" in progress
    assert "complete ha-paneld's guided setup on the panel" in progress
    assert "dedicated recovery workflow" not in all_errors

    abort = config["abort"]
    mapped_reasons = {
        "install_cancelled_after_staging_cleanup",
        "install_authorization_failed",
        "install_preflight_rejected",
        "install_artifact_rejected",
        "install_transport_failed",
        "install_package_failed",
        "install_launch_failed",
        "install_health_check_failed",
        "install_ambiguous_mutation",
        "install_verification_required",
    }
    assert mapped_reasons <= abort.keys()
    assert "Do not retry automatic installation" in abort["install_launch_failed"]
    assert "Do not retry automatic installation" in abort["install_ambiguous_mutation"]
