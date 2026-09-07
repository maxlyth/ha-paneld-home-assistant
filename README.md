# Panel Assistant

A Home Assistant integration for [ha-paneld](https://github.com/maxlyth/ha-paneld), the dashboard app for Android wall panels.

This is a proof of concept, not a finished installer. It can connect to an existing panel or install ha-paneld on a clean panel over network ADB. Browser USB installation is experimental. Upgrades are not supported yet, and MQTT still handles panel controls and entities.

## Try it

Requires Home Assistant 2026.8.3 or later and HACS.

1. Add `https://github.com/maxlyth/ha-paneld-home-assistant` as a custom repository in HACS, with category **Integration**.
2. Download **Panel Assistant** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, select **Panel Assistant**, and follow the prompts.

[Open in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=maxlyth&repository=ha-paneld-home-assistant&category=integration)

For a network installation, [enable ADB on your panel](https://github.com/maxlyth/ha-paneld/tree/main/docs/hardware#gaining-adb--root-access) first. Choose an available ha-paneld release in the installer; release candidates are marked for testing. After installation, finish setup on the panel.

If installation stops, see the [recovery guide](https://github.com/maxlyth/ha-paneld/blob/main/docs/provisioning-safety.md).

## License

[Apache License 2.0](LICENSE).
