#!/usr/bin/env python3
"""Require a release tag to match the integration manifest version exactly."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_MANIFEST = Path("custom_components/ha_paneld/manifest.json")


def verify_release_version(tag: str, manifest_path: Path) -> None:
    """Raise ValueError unless tag and manifest contain the same raw version."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("version")

    if not isinstance(version, str) or not version:
        msg = "manifest version must be a non-empty string"
        raise ValueError(msg)
    if not tag:
        msg = "release tag must be non-empty"
        raise ValueError(msg)
    if tag[:1].lower() == "v":
        msg = "release tags must not use a v prefix"
        raise ValueError(msg)
    if tag != version:
        msg = f"release tag {tag!r} does not match manifest version {version!r}"
        raise ValueError(msg)


def main() -> None:
    """Check the tag supplied by the release workflow."""
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    try:
        verify_release_version(args.tag, args.manifest)
    except (OSError, json.JSONDecodeError, ValueError) as err:
        raise SystemExit(str(err)) from err


if __name__ == "__main__":
    main()
