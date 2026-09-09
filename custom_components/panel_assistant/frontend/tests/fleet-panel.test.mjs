import test from 'node:test';
import assert from 'node:assert/strict';
class Element {
  style = {};
  listeners = {}; children = []; textContent = '';
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute() {}
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
}
globalThis.HTMLElement = class {
  isConnected = false;
  attachShadow() {
    const elements = new Map();
    this.shadowRoot = { querySelectorAll: () => [], querySelector: key => {
      if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key);
    } };
  }
};
globalThis.document = { createElement: () => new Element() };
globalThis.customElements = { get() {}, define() {} };
const { parseFleet, fetchFleet, PanelAssistantFleet, FLEET_MESSAGES } = await import('../src/fleet-panel.mjs');
const row = { entry_id: 'abc', name: '<img src=x>', available: true, version: '0.9.7', warning_count: 0, status_available: true };
test('fleet projection accepts additive fields but returns only the display contract', () => {
  assert.deepEqual(parseFleet({ panels: [{ ...row, secret: 'drop' }], truncated: false }).panels, [row]);
  assert.deepEqual(parseFleet({ panels: [], truncated: false }), { panels: [], truncated: false });
  assert.ok(FLEET_MESSAGES.empty); assert.ok(FLEET_MESSAGES.admin);
});
const tick = () => new Promise(resolve => setImmediate(resolve));
test('setup checklist fetches only on demand and never displays raw panel prose', async () => {
  const panel = new PanelAssistantFleet(); const paths = [];
  panel.isConnected = true;
  panel.hass = { user: { id: 'admin', is_admin: true }, fetchWithAuth: async path => {
    paths.push(path);
    return path.endsWith('/provisioning') ? new Response(JSON.stringify({ items: [{ id: 'software.webview', status: 'actionable', observed_state: 'private' }], needs_updated_client: false }), { headers: { 'Content-Type': 'application/json' } }) : response([row]);
  } };
  await tick();
  const card = panel.shadowRoot.querySelector('#panels').children[0];
  assert.deepEqual(paths, ['/api/panel_assistant/fleet']);
  const button = card.children.find(child => child.textContent === FLEET_MESSAGES.checklist);
  await button.listeners.click(); await tick();
  assert.equal(paths[1], '/api/panel_assistant/fleet/abc/provisioning');
  assert.match(card.children.at(-1).textContent, /WebView: Action needed/);
  assert.doesNotMatch(card.children.at(-1).textContent, /private/);
});

test('setup checklist ignores a late response after auth changes', async () => {
  const panel = new PanelAssistantFleet(); let resolve;
  panel.isConnected = true;
  const hass = { user: { id: 'admin', is_admin: true }, fetchWithAuth: async path => path.endsWith('/provisioning') ? new Promise(done => { resolve = done; }) : response([row]) };
  panel.hass = hass; await tick();
  const card = panel.shadowRoot.querySelector('#panels').children[0];
  card.children.find(child => child.textContent === FLEET_MESSAGES.checklist).listeners.click(); await tick();
  panel.hass = { ...hass, user: { id: 'guest', is_admin: false } };
  resolve(new Response(JSON.stringify({ items: [], needs_updated_client: false }), { headers: { 'Content-Type': 'application/json' } })); await tick();
  assert.equal(panel.shadowRoot.querySelector('#panels').children.length, 0);
  assert.equal(panel.shadowRoot.querySelector('#status').textContent, FLEET_MESSAGES.admin);
});
const response = panels => new Response(JSON.stringify({ panels, truncated: false }), { headers: { 'Content-Type': 'application/json' } });
test('fleet fetch is bounded and cancellation settles ignored aborts', async () => {
  const controller = new AbortController();
  const pending = fetchFleet({ fetchWithAuth: () => new Promise(() => {}) }, controller.signal);
  controller.abort(); await assert.rejects(pending);
  await assert.rejects(fetchFleet({ fetchWithAuth: async () => new Response('x'.repeat(524289), { headers: { 'Content-Type': 'application/json' } }) }, new AbortController().signal));
});
test('component renders safe text, refreshes only explicitly and invalidates identity', async () => {
  const panel = new PanelAssistantFleet(); let calls = 0;
  const hass = { user: { id: 'admin', is_admin: true }, connection: {}, auth: {}, fetchWithAuth: async () => { calls++; return response([row]); } };
  panel.hass = hass; panel.isConnected = true; panel.connectedCallback(); await tick();
  const select = id => panel.shadowRoot.querySelector(`#${id}`);
  assert.equal(select('panels').children[0].children[0].textContent, '<img src=x>');
  panel.hass = { ...hass }; await tick(); assert.equal(calls, 1);
  select('refresh').listeners.click(); await tick(); assert.equal(calls, 2);
  panel.hass = { ...hass, user: { id: 'guest', is_admin: false } };
  assert.equal(select('panels').children.length, 0); assert.equal(select('status').textContent, FLEET_MESSAGES.admin);
  assert.equal(calls, 2); panel.disconnectedCallback();
});
test('disconnected component ignores late results', async () => {
  const panel = new PanelAssistantFleet(); let resolve;
  panel.hass = { user: { id: 'admin', is_admin: true }, fetchWithAuth: () => new Promise(done => { resolve = done; }) };
  panel.isConnected = true; panel.connectedCallback(); panel.isConnected = false; panel.disconnectedCallback();
  resolve(response([row])); await tick();
  assert.equal(panel.shadowRoot.querySelector('#panels').children.length, 0);
});
test('fleet parser rejects stale, malformed and excessive inventory', () => {
  for (const value of [null, { panels: [row, row], truncated: false }, { panels: Array(201).fill(row), truncated: false },
    ...[{ available: false }, { warning_count: -1 }, { name: 'x'.repeat(257) }, { entry_id: '../secret' }, { status_available: false }].map(change => ({ panels: [{ ...row, ...change }], truncated: false }))]) {
    assert.throws(() => parseFleet(value));
  }
});
