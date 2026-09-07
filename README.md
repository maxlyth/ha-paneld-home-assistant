# Panel Assistant for Home Assistant

Panel Assistant is the HACS custom integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the Home Assistant dashboard application for Android wall panels. The integration's technical domain remains `ha_paneld`; existing devices and configuration do not need to be recreated.

The initial `0.1.0` integration can install ha-paneld on a clean Android panel over network Android Debug Bridge (ADB), or connect a panel that is already running it. A configured panel creates one Home Assistant device with a diagnostic status sensor and a bounded, privacy-safe projection of the panel's status endpoint in downloadable diagnostics. The installer defaults to the latest stable release and requires its signed installation descriptor before installation, with an explicit option to test one exact release candidate. It does not upgrade or overwrite an existing installation. Existing MQTT entities remain authoritative.

## Requirements

- Home Assistant `2026.8.3` minimum; currently verified versions are listed under Development
- An Android panel on the same trusted network
- A running ha-paneld installation for the connection path, or the panel's IP address, network ADB on port `5555` and physical access to the panel for a clean first installation; root access is not required
- Internet access from Home Assistant to GitHub for clean-install release metadata and the APK
- HACS, for managed installation

## Install with HACS

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=maxlyth&repository=ha-paneld-home-assistant&category=integration)

Until Panel Assistant appears in the default HACS catalogue, open **HACS → Integrations**, choose **Custom repositories** from the menu, add `https://github.com/maxlyth/ha-paneld-home-assistant` with the **Integration** category, then search for and download **Panel Assistant**. Restart Home Assistant after HACS finishes the download, then open **Settings → Devices & services → Add integration** and select **Panel Assistant**.

The HACS workflow runs the installer inside Home Assistant. Once network ADB is available on the panel, it does not require Git Bash, PowerShell, a workstation `adb` executable or any other workstation installer.

Before starting a clean installation, find the panel's IP address, keep physical access to its screen and use the public [model-specific panel access guide](https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware#gaining-adb--root-access) to make network ADB available on port `5555`. Some models need a one-time USB or vendor-specific preparation step before Home Assistant can reach network ADB. The Home Assistant installer supports both normal rootless ADB and rooted ADB; root access is not a requirement.

Choose **Install ha-paneld on a panel** for a clean first installation, or **Connect an existing ha-paneld installation** to add a running panel.

Choose the ha-paneld version from the release list; there is no need to look up or type a tag. It lists the latest stable release when it has the required installation assets, plus eligible recent release candidates. Release candidates are marked for testing and must be selected explicitly. The selected files and signatures are verified before installation; appearing in the list is not verification. This choice does not change the Panel Assistant integration version or upgrade an already-installed panel.

## Manual integration installation

Copy `custom_components/ha_paneld` into the `custom_components` directory in your Home Assistant configuration, restart Home Assistant, then add **Panel Assistant** from **Settings → Devices & services**. Choose **Install ha-paneld on a panel** for a clean first installation, or **Connect an existing ha-paneld installation** to add a running panel.

The installation path accepts a hostname or IP address and keeps the fixed ha-paneld port `8888` separate from ADB port `5555`. It first checks for a healthy installation. If none responds, it uses bounded ADB observations to distinguish an installed package, an unproven or retained package state, an incompatible device and a package-absent install candidate. An installed, retained, ambiguous or incompatible target is refused rather than overwritten.

A protected panel requires a separate confirmation before Home Assistant generates and offers one persistent ADB key. Android displays a prompt if the key is not already trusted, and someone must physically approve it on the panel. That approval grants general ADB shell access while network debugging remains enabled and can be revoked from the panel's developer settings. The private key is stored in Home Assistant's private storage, outside the HACS-managed integration directory, and is never replaced automatically if its stored data is corrupt.

The candidate screen shows the observed device identity and the authenticated release before any installation begins. Automatic installation requires that release to carry a valid signed descriptor binding the exact APK, package, version, signer, minimum Android version, supported processor architectures, launch component and database compatibility contract. Releases without this descriptor remain preview-only: Home Assistant creates no ADB credential for them, downloads no APK and makes no panel change.

After confirmation, Home Assistant records a durable installation transaction, rechecks the target and credential, downloads and verifies the exact release APK, stages it through ADB, installs and launches it, then requires the expected version from the local health endpoint. The normal config entry is created only after fresh ADB identity, package, root-mode and health checks pass. The installer does not configure ha-paneld after launch. When Home Assistant adds the device, select **Set up** on the panel or open `http://<panel>:8888/setup` from a phone or computer to complete ha-paneld's guided setup for the Home Assistant connection, dashboard and entity filter.

The transaction belongs to Home Assistant rather than the setup dialog, so closing the dialog does not cancel the installer. Leave the dialog open to finish setup automatically. If you close it, reopen **Settings → Devices & services → Add integration**, select **Panel Assistant**, choose the installation path and enter the same address to reattach; Home Assistant cannot add the device until the flow reattaches. Safe phases can resume after a Core restart when another Panel Assistant config entry loads the integration. Home Assistant does not replay a step whose outcome may be ambiguous; it stops and directs you to the [provisioning safety and recovery guide](https://github.com/maxlyth/ha-paneld/blob/main/docs/provisioning-safety.md) instead.

The existing-installation path accepts a hostname or IP address. Port `8888` is used by default; append a different port as `host:port` only if the panel has been configured to use one.

An installation job keeps its originally confirmed release across dialog closure and restart. To reattach, use the default stable/resume choice and the same panel address, or select the job's original release candidate if it is still listed. If the catalogue is unavailable, the resume-only choice still allows existing jobs to be recovered. A different tag is refused rather than changing or replaying the job. The same ambiguity and recovery rules apply to RC and stable installations.

The configured network endpoint identifies the config entry. The panel name returned by the current health contract is editable and is therefore used only for display; Home Assistant registry identifiers remain tied to the config entry across panel renames.

## Scope

This release reads `GET /api/v1/health` and `GET /api/v1/status` over the trusted LAN. Status warnings, free-form summaries, action text, opaque acknowledgement fingerprints and unknown fields are not retained in diagnostics. The clean first-install path uses ADB for bounded target checks, APK staging, installation, launch and final verification. It does not upgrade, repair or configure an existing installation. It does not discover panels, add commands or a sidebar UI, proxy panel traffic, or reproduce or replace MQTT entities. Each config entry retains the existing endpoint-based identity and adds only its diagnostic status sensor. MQTT remains the authority for panel entities and control.

## Development

The full test suite targets Home Assistant `2026.8.3` and `2026.9.0` on Python `3.14.2`. The default local dependency set selects the minimum supported version; CI runs the same suite against both versions. Later Home Assistant releases have not yet been verified.

```bash
python -m pip install -e ".[test]"
python -m mypy custom_components/ha_paneld
ruff check . && ruff format --check .
python -m pytest
```

Hassfest and HACS validation workflows are defined under `.github/workflows/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
