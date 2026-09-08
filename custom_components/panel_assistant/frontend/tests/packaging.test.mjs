import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { readBounded } from '../src/bounded-stream.mjs';
import { installationView } from '../src/install-view.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));

test('production build contains a root installer and a standalone HA panel', async () => {
  execFileSync('npm', ['run', 'build'], { cwd: root, stdio: 'pipe' });
  const html = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');
  const script = html.match(/src="(\.\/assets\/[^"]+\.js)"/);
  assert.ok(script, 'root page must load its own built asset');
  const installer = readFileSync(new URL(`../dist/${script[1]}`, import.meta.url), 'utf8');
  assert.ok(installer.includes('ha-paneld/usb-ready'));
  assert.ok(!installer.includes('sourceMappingURL'));
  assert.deepEqual(readdirSync(new URL('../dist/', import.meta.url)).sort(),
    ['THIRD_PARTY_NOTICES.txt', 'assets', 'index.html']);
  assert.ok(readdirSync(new URL('../dist/assets/', import.meta.url)).every(name => name.endsWith('.js')));
  const notices = readFileSync(new URL('../dist/THIRD_PARTY_NOTICES.txt', import.meta.url), 'utf8');
  for (const name of ['adb', 'adb-credential-web', 'adb-daemon-webusb', 'async', 'event', 'no-data-view', 'stream-extra', 'struct']) {
    assert.ok(notices.includes(`@yume-chan/${name}`));
  }
  assert.ok(notices.includes('Permission is hereby granted'));
  const panel = readFileSync(new URL('../../static/ha-panel.js', import.meta.url), 'utf8');
  assert.ok(!panel.includes('sourceMappingURL'));
  let registered;
  globalThis.HTMLElement = class {};
  globalThis.customElements = { get() {}, define(name) { registered = name; } };
  try {
    // A data URL cannot resolve relative imports: success proves this entry is standalone.
    await import(`data:text/javascript;base64,${Buffer.from(panel).toString('base64')}`);
    assert.equal(registered, 'ha-paneld-usb-install');
  } finally {
    delete globalThis.HTMLElement;
    delete globalThis.customElements;
  }
});

test('extracted stream reader decodes split UTF-8 and rejects oversized input', async () => {
  const stream = (...chunks) => new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(Uint8Array.from(chunk));
    controller.close();
  } });
  assert.equal(await readBounded(stream([0xe2], [0x82, 0xac]), 3), '€');
  await assert.rejects(readBounded(stream([1, 2], [3, 4]), 3), /malformed/);
  await assert.rejects(readBounded(stream([0xff]), 3));
});

test('installation preview requires confirmation and interrupted steps offer observation', () => {
  assert.equal(installationView(null, { connected: true }).actionEnabled, false);
  assert.equal(installationView(null, { connected: true, confirmed: true }).actionEnabled, true);
  for (const phase of ['staging', 'installing', 'launching', 'cleanup_pending']) {
    assert.equal(installationView({ phase }, { connected: true }).actionKey, 'installReconcile');
  }
  assert.equal(installationView({ phase: 'healthy' }, { connected: true }).actionKey, null);
});
