# Changelog

All notable changes to this project will be documented in this file.

## 0.1.0 - Unreleased

- Add a clean first-install workflow over network ADB while keeping connection to an existing installation as a separate path. Home Assistant verifies the panel and signed stable-release descriptor, downloads the exact APK, installs and launches it, then creates the config entry only after final identity and health checks pass.
- Require physical approval of one persistent ADB credential stored in Home Assistant's private storage. Unreachable targets and the initial authorization probe do not create or offer a key.
- Keep the installation transaction running if its setup dialog closes, resume safe phases after the integration loads again and refuse to replay any operation with an ambiguous outcome.
- Keep older stable releases without a signed installation descriptor preview-only. Installed, retained, ambiguous and incompatible targets are refused, and this release does not upgrade or overwrite an existing installation.
- Add manual setup by panel hostname or IP address.
- Validate panels through the stable read-only `/api/v1/health` endpoint.
- Add bounded, privacy-safe `/api/v1/status` data to downloadable diagnostics.
- Create one Home Assistant device with a diagnostic status sensor and downloadable redacted diagnostics.
- Support config-entry setup, unload and reload.
