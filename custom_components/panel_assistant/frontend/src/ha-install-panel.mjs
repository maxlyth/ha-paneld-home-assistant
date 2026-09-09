import { startReleaseHandoff } from './ha-release-handoff.mjs';
import { fetchReleaseCatalog } from './release-catalog.mjs';

// Stable keys keep presentation separate from the release-transfer protocol.
export const HA_INSTALL_MESSAGES = Object.freeze({
  title: 'Panel Assistant USB installation',
  introduction: 'Connect the panel to this browser’s computer or mobile device, not to the Home Assistant server. USB debugging and Android authorization are required.',
  scope: 'This experimental installer supports clean installation only. Existing installations are not overwritten. Setup permissions and connecting the panel to Home Assistant remain separate steps. MQTT is unchanged.',
  release: 'ha-paneld version',
  releaseHelp: 'Stable is recommended. Release candidates are for testing. To resume, use the same release and browser as before.',
  loading: 'Loading available versions…',
  catalogError: 'Available versions could not be loaded. Try again.',
  empty: 'No supported versions are available yet. Try again later.',
  choose: 'Choose a version',
  retry: 'Retry',
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
  invalid_request: 'Choose an available version before opening the installer.',
  failed: 'Release transfer did not complete. Check the installer window and try again. No installation result is implied.',
});

export class HaPaneldUsbInstallPanel extends HTMLElement {
  #hass;
  #panel;
  #transfer;
  #status = 'ready';
  #catalogRequest;
  #catalogState = 'loading';
  #releases = [];
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    // Only fixed markup is HTML. Configuration and translations use textContent.
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#17232d);padding:24px;box-sizing:border-box}
      main{max-width:680px;margin:auto;font:inherit;line-height:1.5}
      h1{font-size:1.6rem}label{display:block;font-weight:600}
      select{display:block;box-sizing:border-box;width:100%;max-width:26rem;padding:12px;font:inherit}
      button{padding:12px 18px;font:inherit;margin:8px 8px 8px 0;cursor:pointer}
      button:disabled{cursor:default}p{overflow-wrap:anywhere}
      #status{padding:16px;border:1px solid var(--divider-color,#aab6bd);border-radius:8px}
    </style><main>
      <h1 data-message="title"></h1><p data-message="introduction"></p>
      <p data-message="scope"></p>
      <label for="release" data-message="release"></label>
      <select id="release" aria-describedby="release-help catalog-status"></select>
      <p id="release-help" data-message="releaseHelp"></p>
      <p id="catalog-status" role="status" aria-live="polite"></p><button id="retry" data-message="retry"></button>
      <button id="start" data-message="start"></button><button id="cancel" data-message="cancel"></button>
      <p id="status" role="status" aria-live="polite"></p>
    </main>`;
    for (const element of this.shadowRoot.querySelectorAll('[data-message]')) {
      element.textContent = HA_INSTALL_MESSAGES[element.dataset.message];
    }
    this.shadowRoot.querySelector('#start').addEventListener('click', () => this.#start());
    this.shadowRoot.querySelector('#cancel').addEventListener('click', () => this.#transfer?.cancel());
    this.shadowRoot.querySelector('#retry').addEventListener('click', () => this.#loadCatalog());
    this.shadowRoot.querySelector('#release').addEventListener('change', () => this.#render());
    this.#render();
  }
  set hass(value) {
    const changed = this.#hass?.user?.id !== value?.user?.id ||
      this.#hass?.user?.is_admin !== value?.user?.is_admin || this.#hass?.connection !== value?.connection ||
      this.#hass?.auth !== value?.auth;
    this.#hass = value;
    if (changed) { this.#transfer?.cancel(); this.#loadCatalog(); }
    this.#render();
  }
  set panel(value) {
    const changed = this.#panel?.config?.installer_url !== value?.config?.installer_url;
    if (changed) this.#transfer?.cancel();
    this.#panel = value;
    if (changed) this.#loadCatalog();
    this.#render();
  }
  connectedCallback() { this.#loadCatalog(); }
  disconnectedCallback() { this.#transfer?.cancel(); this.#catalogRequest?.abort(); this.#catalogRequest = undefined; }
  async #loadCatalog() {
    this.#catalogRequest?.abort();
    this.#catalogRequest = undefined;
    this.#releases = [];
    this.#catalogState = 'loading';
    this.shadowRoot.querySelector('#release').replaceChildren();
    this.#render();
    if (!this.isConnected || this.#hass?.user?.is_admin !== true || !this.#panel?.config?.installer_url) return;
    const request = new AbortController();
    this.#catalogRequest = request;
    try {
      const releases = await fetchReleaseCatalog(this.#hass, { signal: request.signal });
      if (this.#catalogRequest !== request) return;
      this.#releases = releases;
      this.#catalogState = releases.length ? 'ready' : 'empty';
      const select = this.shadowRoot.querySelector('#release');
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = HA_INSTALL_MESSAGES.choose;
      placeholder.disabled = true;
      select.append(placeholder);
      for (const release of releases) {
        const option = document.createElement('option');
        option.value = release.tag;
        option.textContent = `${release.tag} — ${release.prerelease ? 'Release candidate (testing)' : 'Stable'}`;
        select.append(option);
      }
      select.value = releases.find(release => !release.prerelease)?.tag ?? '';
    } catch {
      if (this.#catalogRequest !== request) return;
      this.#catalogState = 'catalogError';
    } finally {
      if (this.#catalogRequest === request) { this.#catalogRequest = undefined; this.#render(); }
    }
  }
  #render() {
    const allowed = this.#hass?.user?.is_admin === true;
    const configured = typeof this.#panel?.config?.installer_url === 'string' &&
      this.#panel.config.installer_url.length > 0;
    const selected = this.#releases.find(release => release.tag === this.shadowRoot.querySelector('#release').value);
    this.shadowRoot.querySelector('#start').disabled = !allowed || !configured || Boolean(this.#transfer) || !selected;
    this.shadowRoot.querySelector('#cancel').disabled = !this.#transfer;
    this.shadowRoot.querySelector('#release').disabled = Boolean(this.#transfer) || this.#catalogState !== 'ready';
    this.shadowRoot.querySelector('#catalog-status').textContent = allowed && configured && this.#catalogState !== 'ready' ? HA_INSTALL_MESSAGES[this.#catalogState] : '';
    this.shadowRoot.querySelector('#retry').hidden = !allowed || !configured || !['catalogError', 'empty'].includes(this.#catalogState);
    const key = !allowed ? 'admin' : !configured ? 'unavailable' : this.#status;
    this.shadowRoot.querySelector('#status').textContent = HA_INSTALL_MESSAGES[key] ?? HA_INSTALL_MESSAGES.failed;
  }
  #start() {
    if (this.#transfer || this.#hass?.user?.is_admin !== true) return;
    const release = this.#releases.find(item => item.tag === this.shadowRoot.querySelector('#release').value);
    if (!release || !this.isConnected) return;
    const transfer = startReleaseHandoff(this.#hass, this.#panel?.config?.installer_url, {
      rcTag: release.prerelease ? release.tag : null,
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

if (!customElements.get('panel-assistant-usb-install')) {
  customElements.define('panel-assistant-usb-install', HaPaneldUsbInstallPanel);
}
