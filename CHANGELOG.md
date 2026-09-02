# Changelog

All notable changes to this project will be documented in this file.

## 0.1.0 - Unreleased

- Add manual setup by panel hostname or IP address.
- Add an install-first setup menu while keeping existing-panel connection as a separate path.
- Add a non-mutating ADB preview that fails closed on unauthorized, unreachable, installed, retained, ambiguous or incompatible targets.
- Authenticate and display the exact current stable release metadata for a package-absent install candidate without downloading its APK.
- Validate panels through the stable read-only `/api/v1/health` endpoint.
- Add bounded, privacy-safe `/api/v1/status` data to downloadable diagnostics.
- Create one Home Assistant device with a diagnostic status sensor and downloadable redacted diagnostics.
- Support config-entry setup, unload and reload.
