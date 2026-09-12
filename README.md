<p align="center">
  <img src="custom_components/panel_assistant/static/icon.svg" width="216" height="216" alt="Panel Assistant">
</p>

<h1 align="center">Panel Assistant</h1>

A Home Assistant integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the dashboard app for Android wall panels.

Panel Assistant installs ha-paneld on a panel and connects the panel to Home Assistant. You can install over USB from the computer you are browsing on, or over the network if the panel has Android Debug Bridge enabled, and you can connect a panel that already runs ha-paneld. Panel Assistant is beta software: the direction is settled, and the internals are still changing. Installing reaches the panel directly, over USB or Android Debug Bridge, and expects a panel that does not have ha-paneld yet. Updating a panel that already runs it goes through ha-paneld's own update API, from that panel's update entity in Home Assistant. MQTT still carries panel controls and entities.

## Try it

Requires Home Assistant 2026.8.3 or later and HACS.

1. Add `https://github.com/panel-assistant/ha-integration` as a custom repository in HACS, with category **Integration**.
2. Download **Panel Assistant** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, select **Panel Assistant**, and follow the prompts.

[Open in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=panel-assistant&repository=ha-integration&category=integration)

Panel Assistant is in beta, so turn on **Show beta versions** for it in HACS to be offered new versions.

### Install over USB

Open **Panel Assistant** in the Home Assistant sidebar, choose an ha-paneld version and follow the wizard. Use Chrome or Edge on a computer, and plug the panel into that computer, not into the Home Assistant server. Home Assistant itself does not need a certificate. The wizard hands over to the panel's own setup when the app is installed.

### Install over the network

[Enable ADB on your panel](https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware#gaining-adb--root-access) first, then add the integration and enter the panel's address. Panel Assistant checks the panel, offers the versions it can install, and connects the panel when its setup is done. Release candidates are marked for testing.

If installation stops, see the [recovery guide](https://github.com/maxlyth/ha-paneld/blob/main/docs/provisioning-safety.md).

## Updating from 0.1.x

Remove the old integration entry in **Settings → Devices & services**, then remove its download from HACS. Add this repository in HACS, enable beta releases and download Panel Assistant. Restart Home Assistant and add the integration again using your panel's hostname or IP. This replaces the integration's Status entity; it does not change ha-paneld's MQTT entities, topics or panel settings.

## License

[Apache License 2.0](LICENSE).
