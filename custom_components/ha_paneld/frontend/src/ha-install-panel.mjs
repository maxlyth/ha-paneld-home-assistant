import { startReleaseHandoff } from './ha-release-handoff.mjs';

// Stable keys keep presentation separate from the release-transfer protocol.
export const HA_INSTALL_MESSAGES = Object.freeze({
  title: 'Install a panel using USB',
  introduction: 'Connect the panel to this browser’s computer or mobile device, not to the Home Assistant server. USB debugging and Android authorization are required.',
  scope: 'This experimental installer supports clean installation only. Existing installations are not overwritten. Setup permissions and connecting the panel to Home Assistant remain separate steps. MQTT is unchanged.',
  release: 'Release candidate tag (optional)',
  releaseHelp: 'Leave blank for the latest stable release, or enter an exact tag such as v1.2.3-rc1. To resume, use the same release and browser as before.',
  start: 'Open USB installer',
  cancel: 'Cancel release transfer',
  ready: 'A separate secure window will verify the release before asking you to select a USB panel. Nothing is installed without confirmation.',
  unavailable: 'The secure installer location has not been configured for this integration.',
  admin: 'An administrator must open the installer.',
  waiting: 'Installer window opened. Waiting for it to become ready…',
  preparing: 'Home Assistant is preparing the signed release…',
  downloading: 'Downloading the verified release from Home Assistant…',
  verifying: 'The installer window is independently verifying the release…',
  verified: 'Release verified. Continue in the installer window to select the USB panel and review installation. This does not mean the panel is installed.',
  cancelled: 'Release transfer cancelled. This does not cancel an installation already started in the other window.',
  popup_blocked: 'Allow this Home Assistant page to open a popup, then try again.',
  invalid_request: 'Use an exact release candidate tag such as v1.2.3-rc1, or leave it blank for stable.',
  failed: 'Release transfer did not complete. Check the installer window and try again. No installation result is implied.',
});

export class HaPaneldUsbInstallPanel extends HTMLElement {
  #hass;
  #panel;
  #transfer;
  #status = 'ready';
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    // Only fixed markup is HTML. Configuration and translations use textContent.
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#17232d);padding:24px;box-sizing:border-box}
      main{max-width:680px;margin:auto;font:inherit;line-height:1.5}
      h1{font-size:1.6rem}label{display:block;font-weight:600}
      input{display:block;box-sizing:border-box;width:100%;max-width:26rem;padding:12px;font:inherit}
      button{padding:12px 18px;font:inherit;margin:8px 8px 8px 0;cursor:pointer}
      button:disabled{cursor:default}p{overflow-wrap:anywhere}
      #status{padding:16px;border:1px solid var(--divider-color,#aab6bd);border-radius:8px}
    </style><main>
      <h1 data-message="title"></h1><p data-message="introduction"></p>
      <p data-message="scope"></p>
      <label for="release" data-message="release"></label>
      <input id="release" maxlength="64" autocomplete="off" spellcheck="false" aria-describedby="release-help">
      <p id="release-help" data-message="releaseHelp"></p>
      <button id="start" data-message="start"></button><button id="cancel" data-message="cancel"></button>
      <p id="status" role="status" aria-live="polite"></p>
    </main>`;
    for (const element of this.shadowRoot.querySelectorAll('[data-message]')) {
      element.textContent = HA_INSTALL_MESSAGES[element.dataset.message];
    }
    this.shadowRoot.querySelector('#start').addEventListener('click', () => this.#start());
    this.shadowRoot.querySelector('#cancel').addEventListener('click', () => this.#transfer?.cancel());
    this.#render();
  }
  set hass(value) { this.#hass = value; this.#render(); }
  set panel(value) {
    if (this.#panel?.config?.installer_url !== value?.config?.installer_url) this.#transfer?.cancel();
    this.#panel = value;
    this.#render();
  }
  disconnectedCallback() { this.#transfer?.cancel(); }
  #render() {
    const allowed = this.#hass?.user?.is_admin === true;
    const configured = typeof this.#panel?.config?.installer_url === 'string' &&
      this.#panel.config.installer_url.length > 0;
    this.shadowRoot.querySelector('#start').disabled = !allowed || !configured || Boolean(this.#transfer);
    this.shadowRoot.querySelector('#cancel').disabled = !this.#transfer;
    this.shadowRoot.querySelector('#release').disabled = Boolean(this.#transfer);
    const key = !allowed ? 'admin' : !configured ? 'unavailable' : this.#status;
    this.shadowRoot.querySelector('#status').textContent = HA_INSTALL_MESSAGES[key] ?? HA_INSTALL_MESSAGES.failed;
  }
  #start() {
    if (this.#transfer || this.#hass?.user?.is_admin !== true) return;
    const tag = this.shadowRoot.querySelector('#release').value.trim();
    const transfer = startReleaseHandoff(this.#hass, this.#panel?.config?.installer_url, {
      rcTag: tag || null,
      onState: state => { this.#status = state; this.#render(); },
    });
    this.#transfer = transfer;
    this.#render();
    void transfer.completion.catch(() => {}).finally(() => {
      if (this.#transfer === transfer) this.#transfer = undefined;
      this.#render();
    });
  }
}

if (!customElements.get('ha-paneld-usb-install')) {
  customElements.define('ha-paneld-usb-install', HaPaneldUsbInstallPanel);
}
