import { startReleaseHandoff } from './ha-release-handoff.mjs';
import { fetchReleaseCatalog } from './release-catalog.mjs';
import { BRAND_ICON, WIZARD_CSS, journeyHtml } from './wizard-look.mjs';

// Stable keys keep presentation separate from the release-transfer protocol.
// This is the first stop of the same wizard the installer window and the
// panel's own setup continue, so it speaks the same plain one-line language.
export const HA_INSTALL_MESSAGES = Object.freeze({
  title: 'Install ha-paneld on a panel',
  introduction: 'Plug the panel into this computer with a USB cable. A new window will find it and install the app.',
  release: 'Version',
  loading: 'Loading versions…',
  catalogError: 'The list of versions couldn’t be loaded.',
  empty: 'No versions are available yet. Try again later.',
  choose: 'Choose a version',
  recommended: 'recommended',
  testing: 'test version',
  retry: 'Try again',
  start: 'Continue',
  cancel: 'Cancel',
  ready: '',
  unavailable: 'The installer isn’t available. Update Panel Assistant, then try again.',
  admin: 'Ask a Home Assistant administrator to install panels.',
  waiting: 'Continue in the new window.',
  preparing: 'Getting the app ready…',
  downloading: 'Getting the app ready…',
  verifying: 'Getting the app ready…',
  verified: 'Continue in the new window.',
  cancelled: 'Cancelled.',
  popup_blocked: 'Your browser blocked the new window. Allow pop-ups for this page, then press Continue.',
  invalid_request: 'Choose a version first.',
  failed: 'That didn’t work. Press Continue to try again.',
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
    this.shadowRoot.innerHTML = `<style>${WIZARD_CSS}
      :host{display:block;min-height:100%;background:var(--bg);padding:24px 16px}
      .card p.status{color:var(--text);margin:14px 0 0}
    </style><main class="wiz">
      <div class="wiz-brand"><img src="${BRAND_ICON}" alt=""><span>ha-paneld</span></div>
      <ol class="wiz-dots" aria-label="Progress">${journeyHtml(0)}</ol>
      <section class="card">
        <h2 data-message="title"></h2>
        <p class="lead" data-message="introduction"></p>
        <label for="release" data-message="release"></label>
        <select id="release" aria-describedby="catalog-status"></select>
        <p id="catalog-status" role="status" aria-live="polite"></p>
        <button id="retry" class="secondary" data-message="retry"></button>
        <button id="start" class="primary" data-message="start"></button>
        <p id="status" class="status" role="status" aria-live="polite"></p>
        <button id="cancel" class="secondary" data-message="cancel"></button>
      </section>
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
    // Follow Home Assistant's own light or dark choice, not only the device's.
    const dark = value?.themes?.darkMode;
    if (typeof dark === 'boolean') this.setAttribute?.('theme', dark ? 'dark' : 'light');
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
      const recommended = releases.find(release => !release.prerelease)?.tag ?? '';
      for (const release of releases) {
        const option = document.createElement('option');
        option.value = release.tag;
        const note = release.tag === recommended ? HA_INSTALL_MESSAGES.recommended
          : release.prerelease ? HA_INSTALL_MESSAGES.testing : '';
        option.textContent = `${release.tag.replace(/^v/, '')}${note ? ` (${note})` : ''}`;
        select.append(option);
      }
      select.value = recommended;
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
    const catalog = this.shadowRoot.querySelector('#catalog-status');
    catalog.textContent = allowed && configured && this.#catalogState !== 'ready' ? HA_INSTALL_MESSAGES[this.#catalogState] : '';
    catalog.hidden = !catalog.textContent;
    this.shadowRoot.querySelector('#retry').hidden = !allowed || !configured || !['catalogError', 'empty'].includes(this.#catalogState);
    this.shadowRoot.querySelector('#cancel').hidden = !this.#transfer;
    const key = !allowed ? 'admin' : !configured ? 'unavailable' : this.#status;
    const status = this.shadowRoot.querySelector('#status');
    status.textContent = Object.hasOwn(HA_INSTALL_MESSAGES, key) ? HA_INSTALL_MESSAGES[key] : HA_INSTALL_MESSAGES.failed;
    status.hidden = !status.textContent;
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
