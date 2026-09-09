export const FLEET_MESSAGES = Object.freeze({
  title: 'Panel Assistant', menu: 'Open navigation', refresh: 'Refresh',
  install: 'Install using USB', connect: 'Add or connect a panel',
  introduction: 'Refresh to see the latest status received by Home Assistant.',
  empty: 'No panels are connected yet.', loading: 'Loading panels…',
  failed: 'Panels could not be loaded. Try refreshing.', admin: 'An administrator must open this page.',
  available: 'Available', unavailable: 'Unavailable or not loaded',
  version: 'Installed version', warnings: 'Warnings', diagnostics: 'Diagnostics unavailable',
  settings: 'Panel settings', truncated: 'Only the first 200 panels are shown.',
  checklist: 'Check setup', checklistLoading: 'Checking setup…',
  checklistFailed: 'Setup checks are unavailable on this panel.',
  checklistHelp: 'Helper, Shizuku and WebView guidance only. Complete Android permissions and guided setup separately.',
  checklistNew: 'Some checks require a newer integration.',
});

const SETUP_LABELS = Object.freeze({ 'access.helper': 'Panel helper', 'access.shizuku': 'Shizuku', 'software.webview': 'WebView' });
const SETUP_STATUS = Object.freeze({ satisfied: 'Ready', actionable: 'Action needed', manual: 'Manual setup needed', blocked: 'Cannot proceed', degraded: 'Needs attention', not_applicable: 'Not needed' });

export function parseFleet(value) {
  if (!value || !Array.isArray(value.panels) || value.panels.length > 200 || typeof value.truncated !== 'boolean') throw Error('invalid fleet');
  const ids = new Set();
  const panels = value.panels.map(row => {
    if (!row || typeof row.entry_id !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(row.entry_id) || ids.has(row.entry_id) ||
      typeof row.name !== 'string' || row.name.length > 256 || typeof row.available !== 'boolean' || typeof row.status_available !== 'boolean' ||
      !(row.version === null || typeof row.version === 'string' && row.version.length <= 128) ||
      !(row.warning_count === null || Number.isSafeInteger(row.warning_count) && row.warning_count >= 0)) throw Error('invalid fleet');
    if (!row.available && (row.version !== null || row.warning_count !== null || row.status_available)) throw Error('stale fleet');
    if (row.status_available !== (row.warning_count !== null)) throw Error('invalid status');
    ids.add(row.entry_id);
    return { entry_id: row.entry_id, name: row.name, available: row.available, version: row.version, warning_count: row.warning_count, status_available: row.status_available };
  });
  return { panels, truncated: value.truncated };
}

export async function fetchFleet(hass, signal) {
  return parseFleet(await fetchDocument(hass, signal, '/api/panel_assistant/fleet'));
}

async function fetchDocument(hass, signal, path) {
  let reader;
  let onAbort;
  const aborted = new Promise((_, reject) => { onAbort = () => reject(Error('cancelled')); });
  signal.addEventListener('abort', onAbort, { once: true });
  try {
    if (signal.aborted) throw Error('cancelled');
    return await Promise.race([aborted, (async () => {
      const response = await hass.fetchWithAuth(path, { signal, cache: 'no-store', redirect: 'error' });
      if (signal.aborted || response.status !== 200 || response.redirected || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw Error('invalid response');
      reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: true }); let text = ''; let size = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (signal.aborted) throw Error('cancelled');
        if (done) break;
        size += value.byteLength; if (size > 524288) throw Error('excessive response');
        text += decoder.decode(value, { stream: true });
      }
      return JSON.parse(text + decoder.decode());
    })()]);
  } finally {
    signal.removeEventListener('abort', onAbort);
    if (reader) void reader.cancel().catch(() => {});
  }
}

export class PanelAssistantFleet extends HTMLElement {
  #hass; #request; #setupRequest; #setupOutput; #state = 'loading'; #inventory;
  constructor() {
    super(); this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#212121);min-height:100%;font:inherit}
      header{display:flex;align-items:center;gap:12px;background:var(--app-header-background-color,var(--primary-color,#03a9f4));color:var(--app-header-text-color,#fff);padding:8px 16px}
      h1{font-size:1.25rem}main{max-width:1000px;margin:auto;padding:20px;box-sizing:border-box}
      .brand{display:flex;align-items:center;gap:8px}.brand img{width:108px;height:108px;flex:none}.brand p{margin:0}
      nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center}button,a{font:inherit;padding:12px;min-height:44px;box-sizing:border-box}
      button{cursor:pointer;color:inherit;background:transparent;border:1px solid var(--divider-color,#888);border-radius:6px}a{color:var(--primary-color,#0288d1)}
      #panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px;margin-top:20px}
      article{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:var(--ha-card-border-radius,12px);padding:16px;overflow-wrap:anywhere}h2{font-size:1.1rem}article a{display:inline-block}
    </style><header><button id="menu" aria-label=""></button><h1 data-message="title"></h1></header><main>
      <div class="brand"><img src="/panel_assistant/usb/icon.svg" width="108" height="108" alt=""><p data-message="introduction"></p></div><nav><button id="refresh" data-message="refresh"></button><a href="/ha-paneld-usb" data-message="install"></a><a href="/config/integrations/dashboard/add?domain=panel_assistant" data-message="connect"></a></nav>
      <p id="status" role="status" aria-live="polite"></p><section id="panels"></section></main>`;
    for (const element of this.shadowRoot.querySelectorAll('[data-message]')) element.textContent = FLEET_MESSAGES[element.dataset.message];
    const menu = this.shadowRoot.querySelector('#menu'); menu.textContent = '☰'; menu.setAttribute('aria-label', FLEET_MESSAGES.menu);
    menu.addEventListener('click', () => this.dispatchEvent(new CustomEvent('hass-toggle-menu', { bubbles: true, composed: true })));
    this.shadowRoot.querySelector('#refresh').addEventListener('click', () => this.#load());
    this.#render();
  }
  set hass(value) {
    const changed = this.#hass?.user?.id !== value?.user?.id || this.#hass?.user?.is_admin !== value?.user?.is_admin || this.#hass?.connection !== value?.connection || this.#hass?.auth !== value?.auth;
    this.#hass = value; if (changed) this.#load();
  }
  connectedCallback() { this.#load(); }
  disconnectedCallback() { this.#request?.abort(); this.#request = undefined; this.#setupRequest?.abort(); this.#setupRequest = undefined; }
  async #load() {
    this.#setupRequest?.abort(); this.#setupRequest = undefined;
    this.#request?.abort(); this.#request = undefined; this.#inventory = undefined;
    this.#state = this.#hass?.user?.is_admin === true ? 'loading' : 'admin'; this.#render();
    if (!this.isConnected || this.#state === 'admin') return;
    const request = new AbortController(); this.#request = request;
    const timer = setTimeout(() => request.abort(), 15000);
    try {
      const inventory = await fetchFleet(this.#hass, request.signal);
      if (request !== this.#request) return;
      this.#inventory = inventory; this.#state = inventory.panels.length ? null : 'empty';
    } catch { if (request === this.#request) this.#state = 'failed'; }
    finally { clearTimeout(timer); if (request === this.#request) { this.#request = undefined; this.#render(); } }
  }
  async #checkSetup(entryId, output) {
    this.#setupRequest?.abort();
    if (this.#setupOutput) this.#setupOutput.textContent = '';
    this.#setupOutput = output;
    const request = new AbortController(); this.#setupRequest = request;
    const timer = setTimeout(() => request.abort(), 15000);
    output.textContent = FLEET_MESSAGES.checklistLoading;
    try {
      const plan = await fetchDocument(this.#hass, request.signal, `/api/panel_assistant/fleet/${encodeURIComponent(entryId)}/provisioning`);
      if (this.#setupRequest !== request) return;
      if (!plan || !Array.isArray(plan.items) || plan.items.length > 32 || typeof plan.needs_updated_client !== 'boolean') throw Error('invalid plan');
      const lines = plan.items.map(item => {
        if (!Object.hasOwn(SETUP_LABELS, item.id) || !Object.hasOwn(SETUP_STATUS, item.status)) throw Error('invalid item');
        return `${SETUP_LABELS[item.id]}: ${SETUP_STATUS[item.status]}`;
      });
      output.textContent = [...lines, plan.needs_updated_client ? FLEET_MESSAGES.checklistNew : '', FLEET_MESSAGES.checklistHelp].filter(Boolean).join('\n');
    } catch { if (this.#setupRequest === request) output.textContent = FLEET_MESSAGES.checklistFailed; }
    finally { clearTimeout(timer); if (this.#setupRequest === request) this.#setupRequest = undefined; }
  }
  #render() {
    this.shadowRoot.querySelector('#status').textContent = this.#state ? FLEET_MESSAGES[this.#state] : this.#inventory?.truncated ? FLEET_MESSAGES.truncated : '';
    this.shadowRoot.querySelector('#refresh').disabled = this.#state === 'admin' || this.#state === 'loading';
    const container = this.shadowRoot.querySelector('#panels'); container.replaceChildren();
    for (const row of this.#inventory?.panels ?? []) {
      const card = document.createElement('article');
      const add = (tag, text) => { const element = document.createElement(tag); element.textContent = text; card.append(element); return element; };
      add('h2', row.name); add('p', FLEET_MESSAGES[row.available ? 'available' : 'unavailable']);
      if (row.version !== null) add('p', `${FLEET_MESSAGES.version}: ${row.version}`);
      if (row.status_available) add('p', `${FLEET_MESSAGES.warnings}: ${row.warning_count}`);
      else if (row.available) add('p', FLEET_MESSAGES.diagnostics);
      add('a', FLEET_MESSAGES.settings).href = `/config/integrations/integration/panel_assistant#config_entry=${encodeURIComponent(row.entry_id)}`;
      const check = add('button', FLEET_MESSAGES.checklist);
      check.disabled = !row.available;
      const output = add('p', ''); output.setAttribute('role', 'status'); output.style.whiteSpace = 'pre-line';
      check.addEventListener('click', () => this.#checkSetup(row.entry_id, output));
      container.append(card);
    }
  }
}
if (!customElements.get('panel-assistant-fleet')) customElements.define('panel-assistant-fleet', PanelAssistantFleet);
