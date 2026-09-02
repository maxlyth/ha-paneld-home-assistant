# ha-paneld for Home Assistant

This repository contains the HACS custom integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the Home Assistant dashboard application for Android wall panels.

The initial `0.1.0` integration is intentionally read-only. It connects to a panel's stable local health endpoint, creates one Home Assistant device and exposes a diagnostic status sensor. It also adds a bounded, privacy-safe projection of the panel's status endpoint to downloadable diagnostics. The installation preview can identify an Android panel whose Package Manager has no ha-paneld record, apply the integration's current Android API and ABI checks, and authenticate the exact current stable release. It stops before establishing ADB trust, checking rooted residual files, downloading the APK or changing the panel. Existing MQTT entities remain authoritative.

## Requirements

- Home Assistant `2026.8.3` or newer
- An Android panel on the same trusted network
- A running ha-paneld installation for the connection path, or network ADB on port `5555` for the installation preview
- HACS, for managed installation after this repository is published

## Manual installation

Copy `custom_components/ha_paneld` into the `custom_components` directory in your Home Assistant configuration, restart Home Assistant, then add **ha-paneld** from **Settings → Devices & services**. Choose **Install ha-paneld on a panel** to run the non-mutating first-install preview, or **Connect an existing ha-paneld installation** to add a running panel.

The installation preview accepts a hostname or IP address and keeps the fixed ha-paneld port `8888` separate from ADB port `5555`. It first checks for a healthy installation. If none responds, it uses read-only ADB observations to distinguish an installed package, an unproven or retained package state, an incompatible device and a package-absent install candidate. A candidate screen includes the device model, serial, ABI, Android SDK, release tag and authenticated SHA-256, then exits without changing the panel. This preview does not inspect residual app files that may remain on a rooted panel, so it is not yet installation admission.

The existing-installation path accepts a hostname or IP address. Port `8888` is used by default; append a different port as `host:port` only if the panel has been configured to use one.

The configured network endpoint identifies the config entry. The panel name returned by the current health contract is editable and is therefore used only for display; Home Assistant registry identifiers remain tied to the config entry across panel renames.

## Scope

This release reads `GET /api/v1/health` and `GET /api/v1/status` over the trusted LAN. Status warnings, free-form summaries, action text, opaque acknowledgement fingerprints and unknown fields are not retained in diagnostics. The installation preview uses ADB only for bounded package-manager and device-property reads, and it verifies the signed checksum record for the current stable release without downloading the APK. It does not inspect rooted residual files, install or configure ha-paneld, create or submit an ADB key, proxy panel traffic, mutate a panel, replace MQTT entities or add a sidebar UI.

## Development

The test suite targets Home Assistant `2026.8.3` and Python `3.14.2`:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

Hassfest and HACS validation workflows are defined under `.github/workflows/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
