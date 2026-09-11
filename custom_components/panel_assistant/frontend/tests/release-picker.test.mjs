import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

class Element {
  listeners = {}; children = []; value = ''; disabled = false; hidden = false; textContent = '';
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; this.value = ''; }
}
globalThis.HTMLElement = class {
  isConnected = false;
  attachShadow() {
    const elements = new Map();
    this.shadowRoot = {
      querySelectorAll: () => [],
      querySelector: key => { if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key); },
    };
  }
};
globalThis.document = { createElement: () => new Element() };
globalThis.customElements = { get: () => true };
const { HaPaneldUsbInstallPanel, HA_INSTALL_MESSAGES } = await import('../src/ha-install-panel.mjs');
const tick = () => new Promise(resolve => setImmediate(resolve));
const stable = { tag: 'v1.2.3', prerelease: false };
const rc = { tag: 'v1.2.4-rc1', prerelease: true };
const response = releases => new Response(JSON.stringify({ releases }), { headers: { 'Content-Type': 'application/json' } });
function fixture(fetchWithAuth) {
  const panel = new HaPaneldUsbInstallPanel();
  const hass = { user: { id: 'admin', is_admin: true }, connection: {}, auth: {}, fetchWithAuth };
  panel.hass = hass;
  panel.panel = { config: { installer_url: 'https://installer.example/' } };
  panel.isConnected = true;
  panel.connectedCallback();
  return { panel, hass, element: id => panel.shadowRoot.querySelector(`#${id}`) };
}
test('stable defaults, labels use text, RC requires selection and handoff receives exact RC', async () => {
  const f = fixture(async () => response([rc, stable]));
  assert.equal(f.element('start').disabled, true);
  assert.equal(f.element('catalog-status').textContent, HA_INSTALL_MESSAGES.loading);
  await tick();
  assert.equal(f.element('release').value, stable.tag);
  assert.equal(f.element('start').disabled, false);
  assert.deepEqual(f.element('release').children.map(option => option.textContent), ['Choose a version', '1.2.4-rc1 (test version)', '1.2.3 (recommended)']);
  let opened;
  globalThis.window = { crypto: webcrypto, location: { origin: 'http://ha.example' }, addEventListener() {}, removeEventListener() {}, open(url) { opened = new URL(url); return { closed: false }; } };
  f.element('start').listeners.click();
  assert.equal(new URLSearchParams(opened.hash.slice(1)).get('rc'), '');
  f.element('cancel').listeners.click();
  await tick();
  f.element('release').value = rc.tag;
  f.element('release').listeners.change();
  f.element('start').listeners.click();
  assert.equal(new URLSearchParams(opened.hash.slice(1)).get('rc'), rc.tag);
  assert.equal(f.element('release').disabled, true);
  f.panel.disconnectedCallback();
  await tick();
});
test('RC-only catalogue never auto-selects and forged choices cannot start', async () => {
  const f = fixture(async () => response([rc]));
  await tick();
  assert.equal(f.element('release').value, '');
  assert.equal(f.element('start').disabled, true);
  f.element('release').value = 'v9.9.9-rc1';
  f.element('release').listeners.change();
  assert.equal(f.element('start').disabled, true);
  f.element('release').value = rc.tag;
  f.element('release').listeners.change();
  assert.equal(f.element('start').disabled, false);
});
test('error and empty states offer Retry and disable starting', async () => {
  const responses = [new Response('Forbidden', { status: 403 }), response([]), response([stable])];
  const f = fixture(async () => responses.shift());
  await tick();
  assert.equal(f.element('catalog-status').textContent, HA_INSTALL_MESSAGES.catalogError);
  assert.equal(f.element('retry').hidden, false);
  assert.equal(f.element('start').disabled, true);
  f.element('retry').listeners.click(); await tick();
  assert.equal(f.element('catalog-status').textContent, HA_INSTALL_MESSAGES.empty);
  assert.equal(f.element('retry').hidden, false);
  assert.equal(f.element('start').disabled, true);
  f.element('retry').listeners.click(); await tick();
  assert.equal(f.element('retry').hidden, true);
  assert.equal(f.element('start').disabled, false);
});
test('disconnect and identity changes abort pending requests and ignore late catalogues', async () => {
  const pending = [];
  const f = fixture((_, init) => new Promise(resolve => pending.push({ resolve, signal: init.signal })));
  f.panel.hass = { ...f.hass };
  assert.equal(pending.length, 1, 'ordinary state refresh does not refetch');
  f.panel.hass = { ...f.hass, user: { id: 'other', is_admin: true } };
  assert.equal(pending[0].signal.aborted, true);
  assert.equal(pending.length, 2);
  pending[0].resolve(response([stable])); await tick();
  assert.equal(f.element('start').disabled, true);
  f.panel.isConnected = false;
  f.panel.disconnectedCallback();
  assert.equal(pending[1].signal.aborted, true);
  pending[1].resolve(response([rc])); await tick();
  assert.equal(f.element('release').children.length, 0);
});
test('installer destination changes invalidate selection and non-admin users never fetch', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return response([stable]); });
  await tick();
  f.panel.panel = { config: { installer_url: 'https://other.example/' } };
  assert.equal(f.element('start').disabled, true);
  await tick();
  assert.equal(calls, 2);
  f.panel.hass = { ...f.hass, user: { id: 'regular', is_admin: false } };
  assert.equal(calls, 2);
  assert.equal(f.element('start').disabled, true);
});
test('dev builds are labelled by name, stable stays the default, and a chosen build is passed like an RC', async () => {
  const feed = { tag: 'build-772', prerelease: true, name: '0.9.7-rc4 build 772' };
  const f = fixture(async () => response([stable, rc, feed]));
  await tick();
  assert.deepEqual(f.element('release').children.map(option => option.textContent),
    ['Choose a version', '1.2.3 (recommended)', '1.2.4-rc1 (test version)', '0.9.7-rc4 build 772 (dev build)']);
  assert.deepEqual(f.element('release').children.map(option => option.value), ['', stable.tag, rc.tag, feed.tag]);
  assert.equal(f.element('release').value, stable.tag);
  let opened;
  globalThis.window = { crypto: webcrypto, location: { origin: 'http://ha.example' }, addEventListener() {}, removeEventListener() {}, open(url) { opened = new URL(url); return { closed: false }; } };
  f.element('release').value = feed.tag;
  f.element('release').listeners.change();
  f.element('start').listeners.click();
  assert.equal(new URLSearchParams(opened.hash.slice(1)).get('rc'), feed.tag);
  f.panel.disconnectedCallback();
  await tick();
});
test('a catalogue of only dev builds never auto-selects', async () => {
  const f = fixture(async () => response([{ tag: 'build-772', prerelease: true, name: '0.9.7-rc4 build 772' }]));
  await tick();
  assert.deepEqual(f.element('release').children.map(option => option.textContent),
    ['Choose a version', '0.9.7-rc4 build 772 (dev build)']);
  assert.equal(f.element('release').value, '');
  assert.equal(f.element('start').disabled, true);
});
