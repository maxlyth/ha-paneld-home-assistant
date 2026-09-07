"""Repository and distribution contract tests."""

import ast
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from custom_components.ha_paneld.client import parse_health_response

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "ha_paneld"
RELEASE_VERSION_SCRIPT = ROOT / ".github" / "scripts" / "verify_release_version.py"
PLACEHOLDER = re.compile(r"\{[a-z][a-z0-9_]*\}")
MARKDOWN_LINK = re.compile(
    r"(?<!\\)(?P<image>!?)\[[^\]\r\n]*\]\((?P<target>[^()\r\n]+)\)"
)
FROZEN_TRANSLATION_TOKENS = (
    "ha-paneld",
    "Home Assistant",
    "Android Debug Bridge (ADB)",
    "Android",
    "ADB",
    "HTTP",
    "IPv4",
    "IPv6",
    "APK",
    "DNS",
    "IP",
    "RC",
    "SDK",
    "ABI",
    "SHA-256",
    "URL",
    "v0.9.7-rc3",
    "8888",
    "5555",
)


def _translation_leaves(
    value: Any, prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], str]:
    """Return every string leaf under its exact translation-key path."""
    if isinstance(value, str):
        return {prefix: value}
    assert isinstance(value, dict)
    leaves: dict[tuple[str, ...], str] = {}
    for key, child in value.items():
        assert isinstance(key, str)
        leaves.update(_translation_leaves(child, (*prefix, key)))
    return leaves


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting silently shadowed duplicate keys."""
    value: dict[str, Any] = {}
    for key, child in pairs:
        assert key not in value
        value[key] = child
    return value


def _load_translation_catalogue(path: Path) -> dict[str, Any]:
    """Load a catalogue through the strict JSON object boundary."""
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
    )
    assert isinstance(value, dict)
    return value


def _translation_shape(value: Any) -> Any:
    """Return the complete object shape while discarding translated text."""
    if isinstance(value, str):
        return None
    assert isinstance(value, dict)
    return {key: _translation_shape(child) for key, child in value.items()}


def _placeholders(value: str) -> Counter[str]:
    """Return valid placeholders and reject every unmatched or malformed brace."""
    placeholders = PLACEHOLDER.findall(value)
    remainder = PLACEHOLDER.sub("", value)
    assert "{" not in remainder and "}" not in remainder
    return Counter(placeholders)


def _markdown_links(value: str) -> list[tuple[bool, str]]:
    """Return link kinds and targets, rejecting malformed Markdown delimiters."""
    links = [
        (bool(match.group("image")), match.group("target"))
        for match in MARKDOWN_LINK.finditer(value)
    ]
    assert value.count("](") == len(links)
    for opening, closing in (("[", "]"), ("(", ")")):
        depth = 0
        for character in value:
            if character == opening:
                depth += 1
            elif character == closing:
                depth -= 1
                assert depth >= 0
        assert depth == 0
    return links


def _literal_count(value: str, literal: str) -> int:
    """Count an exact technical literal, excluding word or numeric supersets."""
    return len(
        re.findall(rf"(?<!\w){re.escape(literal)}(?!\w)", value, flags=re.UNICODE)
    )


def _load_release_version_module():
    spec = importlib.util.spec_from_file_location(
        "verify_release_version", RELEASE_VERSION_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_and_hacs_versions_match_repository_policy() -> None:
    """Distribution metadata has the deliberate independent version floor."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert manifest == {
        "codeowners": ["@maxlyth"],
        "config_flow": True,
        "dependencies": ["http", "panel_custom"],
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


def test_release_version_guard_accepts_current_and_prerelease_versions(
    tmp_path: Path,
) -> None:
    """Tag admission uses exact raw equality for stable and prerelease versions."""
    verifier = _load_release_version_module()

    verifier.verify_release_version("0.1.0", INTEGRATION / "manifest.json")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"version":"0.2.0-rc.1"}', encoding="utf-8")
    verifier.verify_release_version("0.2.0-rc.1", manifest)


@pytest.mark.parametrize(
    ("tag", "version", "message"),
    [
        ("v0.1.0", "0.1.0", "must not use a v prefix"),
        ("V0.1.0", "0.1.0", "must not use a v prefix"),
        ("0.1.1", "0.1.0", "does not match manifest version"),
        ("", "0.1.0", "release tag must be non-empty"),
        ("0.1.0", "", "manifest version must be a non-empty string"),
        ("0.1.0", 1, "manifest version must be a non-empty string"),
    ],
)
def test_release_version_guard_rejects_invalid_pairs(
    tmp_path: Path,
    tag: str,
    version: str | int,
    message: str,
) -> None:
    """Tag admission rejects prefixes, drift and malformed manifest versions."""
    verifier = _load_release_version_module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        verifier.verify_release_version(tag, manifest)


@pytest.mark.parametrize("tag", ["-h", "--help"])
def test_release_version_guard_cli_does_not_parse_tag_as_option(tag: str) -> None:
    """A valid option-shaped Git tag must reach the equality check and fail."""
    result = subprocess.run(
        [sys.executable, str(RELEASE_VERSION_SCRIPT), "--", tag],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match manifest version '0.1.0'" in result.stderr


def test_hacs_workflow_runs_release_version_guard_for_tags() -> None:
    """The HACS workflow checks a tag before invoking external validation."""
    workflow = (ROOT / ".github" / "workflows" / "hacs.yml").read_text(encoding="utf-8")
    guard = 'run: python .github/scripts/verify_release_version.py -- "$RELEASE_TAG"'

    assert "if: startsWith(github.ref, 'refs/tags/')" in workflow
    assert "RELEASE_TAG: ${{ github.ref_name }}" in workflow
    assert workflow.index(guard) < workflow.index("name: Run HACS validation")


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
    strings = _load_translation_catalogue(INTEGRATION / "strings.json")
    english = _load_translation_catalogue(INTEGRATION / "translations" / "en.json")

    assert english == strings
    assert "[%key:" not in json.dumps(english)


def test_shipped_translation_catalogues_preserve_machine_contracts() -> None:
    """Every locale has exact keys, placeholders, links, and technical literals."""
    english_catalogue = _load_translation_catalogue(
        INTEGRATION / "translations" / "en.json"
    )
    english = _translation_leaves(english_catalogue)
    translations = INTEGRATION / "translations"
    locale_paths = sorted(translations.glob("*.json"))
    assert [path.name for path in locale_paths] == ["de.json", "en.json"]
    assert len(english) == 78

    for locale_path in locale_paths:
        target_catalogue = _load_translation_catalogue(locale_path)
        target = _translation_leaves(target_catalogue)
        assert _translation_shape(target_catalogue) == _translation_shape(
            english_catalogue
        )
        assert target.keys() == english.keys()
        assert all(value.strip() for value in target.values())
        assert all("[%key:" not in value for value in target.values())
        for key, source_text in english.items():
            target_text = target[key]
            assert _placeholders(target_text) == _placeholders(source_text)
            assert _markdown_links(target_text) == _markdown_links(source_text)
            assert target_text.count("`") == source_text.count("`")
            for token in FROZEN_TRANSLATION_TOKENS:
                required_count = _literal_count(source_text, token)
                if required_count:
                    assert _literal_count(target_text, token) >= required_count


@pytest.mark.parametrize(
    "catalogue", ["strings.json", "translations/en.json", "translations/de.json"]
)
@pytest.mark.parametrize("step", ["confirm_install_candidate", "confirm_install_rc"])
def test_install_confirmation_keeps_each_fact_on_its_own_line(
    catalogue: str, step: str
) -> None:
    """A readable native confirmation must not collapse into one paragraph."""
    description = _load_translation_catalogue(INTEGRATION / catalogue)["config"][
        "step"
    ][step]["description"]
    fact_lines = [line for line in description.splitlines() if line.startswith("- ")]
    assert [_placeholders(line) for line in fact_lines] == [
        Counter({"{" + field + "}": 1})
        for field in (
            "address",
            "model",
            "serial",
            "abi",
            "sdk",
            "version",
            "tag",
            "sha256",
        )
    ]


def test_rc_selection_and_confirmation_are_keyed_and_explicit() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    steps = strings["config"]["step"]
    help_text = steps["install_or_upgrade"]["data_description"]["release_candidate"]
    warning = steps["confirm_install_rc"]["description"]
    assert "Leave blank for the latest stable release" in help_text
    assert "exact published tag" in help_text
    assert "not a stable release" in warning
    assert "may contain bugs" in warning
    assert "{version}" in warning and "{tag}" in warning and "{sha256}" in warning
    assert (
        "even if a release candidate tag was entered"
        in steps["confirm_existing"]["description"]
    )


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
    assert "[model-specific panel access guide]({panel_access_url})" in install
    assert "https://" not in install
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
    assert "stored ADB credential" in abort["install_authorization_failed"]
    assert "approval on the panel" not in abort["install_authorization_failed"]
    assert "connection, download or staging step" in abort["install_transport_failed"]
    assert (
        "could not complete or verify the launch step" in abort["install_launch_failed"]
    )
    assert "Do not retry automatic installation" in abort["install_launch_failed"]
    assert "Do not retry automatic installation" in abort["install_ambiguous_mutation"]
