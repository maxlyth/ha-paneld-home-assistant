# ha-paneld for Home Assistant

This repository contains the HACS custom integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the Home Assistant dashboard application for Android wall panels.

The initial `0.1.0` integration can install ha-paneld on a clean Android panel over network Android Debug Bridge (ADB), or connect a panel that is already running it. A configured panel creates one Home Assistant device with a diagnostic status sensor and a bounded, privacy-safe projection of the panel's status endpoint in downloadable diagnostics. The installer admits only a verified clean first installation of a stable release carrying ha-paneld's signed installation descriptor. It does not upgrade or overwrite an existing installation. Existing MQTT entities remain authoritative.

## Requirements

- Home Assistant `2026.8.3` minimum; currently verified versions are listed under Development
- An Android panel on the same trusted network
- A running ha-paneld installation for the connection path, or network ADB on port `5555` and physical access to the Android panel for a clean first installation
- Internet access from Home Assistant to GitHub for clean-install release metadata and the APK
- HACS, for managed installation

## Manual installation

Copy `custom_components/ha_paneld` into the `custom_components` directory in your Home Assistant configuration, restart Home Assistant, then add **ha-paneld** from **Settings → Devices & services**. Choose **Install ha-paneld on a panel** for a clean first installation, or **Connect an existing ha-paneld installation** to add a running panel.

The installation path accepts a hostname or IP address and keeps the fixed ha-paneld port `8888` separate from ADB port `5555`. It first checks for a healthy installation. If none responds, it uses bounded ADB observations to distinguish an installed package, an unproven or retained package state, an incompatible device and a package-absent install candidate. An installed, retained, ambiguous or incompatible target is refused rather than overwritten.

A protected panel requires a separate confirmation before Home Assistant generates and offers one persistent ADB key. Android displays a prompt if the key is not already trusted, and someone must physically approve it on the panel. That approval grants general ADB shell access while network debugging remains enabled and can be revoked from the panel's developer settings. The private key is stored in Home Assistant's private storage, outside the HACS-managed integration directory, and is never replaced automatically if its stored data is corrupt.

The candidate screen shows the observed device identity and the authenticated stable release before any installation begins. Automatic installation requires that release to carry a valid signed descriptor binding the exact APK, package, version, signer, minimum Android version, supported processor architectures, launch component and database compatibility contract. Older stable releases without this descriptor remain preview-only: Home Assistant creates no ADB credential for them, downloads no APK and makes no panel change.

After confirmation, Home Assistant records a durable installation transaction, rechecks the target and credential, downloads and verifies the exact release APK, stages it through ADB, installs and launches it, then requires the expected version from the local health endpoint. The normal config entry is created only after fresh ADB identity, package, root-mode and health checks pass. The installer does not configure ha-paneld after launch.

The transaction belongs to Home Assistant rather than the setup dialog, so closing the dialog does not cancel the installer. Safe phases can resume after a Core restart when another ha-paneld config entry loads the integration. If no ha-paneld entries exist yet, reopen **Add integration**, choose the installation path and enter the same address to reattach and continue. Home Assistant does not replay a step whose outcome may be ambiguous; it stops and requests manual recovery instead.

The existing-installation path accepts a hostname or IP address. Port `8888` is used by default; append a different port as `host:port` only if the panel has been configured to use one.

The configured network endpoint identifies the config entry. The panel name returned by the current health contract is editable and is therefore used only for display; Home Assistant registry identifiers remain tied to the config entry across panel renames.

## Scope

This release reads `GET /api/v1/health` and `GET /api/v1/status` over the trusted LAN. Status warnings, free-form summaries, action text, opaque acknowledgement fingerprints and unknown fields are not retained in diagnostics. The clean first-install path uses ADB for bounded target checks, APK staging, installation, launch and final verification. It does not upgrade, repair or configure an existing installation. It does not discover panels, add commands or a sidebar UI, proxy panel traffic, or reproduce or replace MQTT entities. Each config entry retains the existing endpoint-based identity and adds only its diagnostic status sensor. MQTT remains the authority for panel entities and control.

## Development

The full test suite targets Home Assistant `2026.8.3` and Python `3.14.2`. A disposable real-Core load/reload smoke has also passed on Home Assistant `2026.9.0`; later releases have not yet been verified.

```bash
python -m pip install -e ".[test]"
python -m mypy custom_components/ha_paneld
ruff check . && ruff format --check .
python -m pytest
```

Hassfest and HACS validation workflows are defined under `.github/workflows/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
