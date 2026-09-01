"""Repository and distribution contract tests."""

import json
from pathlib import Path

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
        "requirements": [],
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
