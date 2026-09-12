# Changelog

All notable changes to this project will be documented in this file.

## 0.2.0b1 - Unreleased

- Rename the integration to Panel Assistant and move to semantic versions. Earlier installs
  tracked a moving branch and will not be offered this as an update, so follow "Updating from
  0.1.x" in the README.
- Install ha-paneld over USB with a guided wizard: plug the panel into the computer you are
  browsing on, press Install once, and the installer copies, installs, starts the app, grants
  what it needs and hands over to the panel's own setup. It shows plain progress rather than a
  log, and technical detail stays behind "Details for support".
- Home Assistant does not need a TLS certificate for USB installation. The installer runs on its
  own secure page and Home Assistant passes it the signed release.
- Choose which ha-paneld version to install, with the newest stable preselected and release
  candidates marked for testing.
- Add a panel from one path that starts with its address. Home Assistant looks at the panel and
  then either connects it, offers to install, or explains what is missing.
- A panel that already runs ha-paneld says so, and offers to open its setup wizard when that is
  unfinished. Home Assistant connects the panel by itself once setup is done, or connects it
  straight away if you skip.
- Say what to do when a panel cannot be reached: a panel whose Android Debug Bridge is off is
  told to turn it on or to install over USB instead, a silent address is told to check the
  address, and the screen links to per-model help on panel-assistant.io.
- Reinstalling the version a panel already runs finishes its setup instead of stopping with an
  error, and a finished install no longer blocks the next one.
- Fill in each panel's device details: manufacturer, model, Android version and area.
- Tell the panel when Home Assistant shows its ha-paneld update, so a panel does not offer the
  same update twice once both are updated.
- Optionally read internal builds from a signed build feed, set with `build_feed` under
  `panel_assistant` in `configuration.yaml`. Without it, only published GitHub releases are used.
- Fix USB installation on panels whose Android Debug Bridge refused the app's port, wait for the
  app to start before checking it, correct the staged file's permissions, and never advise
  unplugging a panel that is powered over that cable.
- Give the Home Assistant page and the installer the same look as the panel's own setup wizard.

## 0.1.0 - Unreleased

- Allow advanced users to test one exact published Android release candidate while keeping the latest stable release as the default. RC installs retain the signed descriptor, exact consent, durable receipt and recovery checks; existing installations are never upgraded by this option.
- Add a clean first-install workflow over network ADB while keeping connection to an existing installation as a separate path. Home Assistant verifies the panel and signed stable-release descriptor, downloads the exact APK, installs and launches it, then creates the config entry only after final identity and health checks pass.
- On panels requiring ADB authorization, request approval on the panel for one persistent credential stored in Home Assistant's private storage. Unreachable targets and the initial authorization probe do not create or offer a key.
- Keep the installation transaction running if its setup dialog closes, resume safe phases after the integration loads again and refuse to replay any operation with an ambiguous outcome.
- Keep older stable releases without a signed installation descriptor preview-only. Installed, retained, ambiguous and incompatible targets are refused, and this release does not upgrade or overwrite an existing installation.
- Add manual setup by panel hostname or IP address.
- Validate panels through the stable read-only `/api/v1/health` endpoint.
- Add bounded, privacy-safe `/api/v1/status` data to downloadable diagnostics.
- Create one Home Assistant device with a diagnostic status sensor and downloadable redacted diagnostics.
- Support config-entry setup, unload and reload.
