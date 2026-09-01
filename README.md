# ha-paneld for Home Assistant

This repository contains the HACS custom integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the Home Assistant dashboard application for Android wall panels.

The initial `0.1.0` integration is intentionally read-only. It connects to a panel's stable local health endpoint, creates one Home Assistant device and exposes a diagnostic status sensor. It also adds a bounded, privacy-safe projection of the panel's status endpoint to downloadable diagnostics. Existing MQTT entities remain authoritative.

## Requirements

- Home Assistant `2026.8.3` or newer
- A panel running ha-paneld on the same trusted network
- HACS, for managed installation after this repository is published

## Manual installation

Copy `custom_components/ha_paneld` into the `custom_components` directory in your Home Assistant configuration, restart Home Assistant, then add **ha-paneld** from **Settings → Devices & services**.

Enter the panel hostname or IP address. Port `8888` is used by default; append a different port as `host:port` only if the panel has been configured to use one.

The configured network endpoint identifies the config entry. The panel name returned by the current health contract is editable and is therefore used only for display; Home Assistant registry identifiers remain tied to the config entry across panel renames.

## Scope

This release reads `GET /api/v1/health` and `GET /api/v1/status` over the trusted LAN. Status warnings, free-form summaries, action text, opaque acknowledgement fingerprints and unknown fields are not retained in diagnostics. The integration does not install or configure ha-paneld, use ADB, proxy panel traffic, mutate a panel, replace MQTT entities or add a sidebar UI.

## Development

The test suite targets Home Assistant `2026.8.3` and Python `3.14.2`:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

Hassfest and HACS validation workflows are defined under `.github/workflows/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
