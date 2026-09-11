import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { readBounded } from '../src/bounded-stream.mjs';
import { INSTALL_MESSAGES, installProgress } from '../src/install-view.mjs';
import { INSTALL_SCREEN_MESSAGES } from '../src/install-screen-messages.mjs';

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
    assert.equal(registered, 'panel-assistant-usb-install');
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

test('progress only ever moves forward and never names an internal phase', () => {
  const order = ['prepared', 'staging', 'staged', 'installing', 'installed', 'launching', 'healthy'];
  const percents = order.map(phase => installProgress({ phase }).percent);
  assert.deepEqual(percents, [...percents].sort((a, b) => a - b));
  assert.ok(percents.at(-1) < 100, 'healthy is not done: permissions and setup still follow');
  // Resuming an interrupted step reads as that step, not as a reconciliation.
  assert.equal(installProgress({ phase: 'staging' }).stepKey, 'stepCopying');
  assert.equal(installProgress({ phase: 'installing' }).stepKey, 'stepInstalling');
  assert.equal(installProgress(null).stepKey, 'stepCopying');
  assert.equal(installProgress({ phase: 'nonsense' }).stepKey, 'stepCopying');
});

test('nothing the person reads mentions internals, JSON or terminal vocabulary', () => {
  const shown = [...Object.values(INSTALL_MESSAGES), ...Object.values(INSTALL_SCREEN_MESSAGES)].join('\n');
  for (const word of ['JSON', 'receipt', 'descriptor', 'sha256', 'SHA-256', 'phase', 'adb', 'ADB',
    'shell', 'reconcile', 'quarantine', 'MQTT', 'signature', 'checksum']) {
    assert.ok(!shown.includes(word), `user-facing text mentions ${word}`);
  }
  // One plain sentence each: nothing reads like a paragraph of caveats.
  for (const [key, text] of Object.entries({ ...INSTALL_MESSAGES, ...INSTALL_SCREEN_MESSAGES })) {
    assert.ok(text.length <= 140, `${key} is ${text.length} characters`);
  }
});

test('the page offers one primary action per step and no consent checkboxes', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.ok(!/type="checkbox"/.test(html), 'consent is the Install press, not a checkbox');
  assert.ok(!/<pre id="(bundle|target|receipt)-result"/.test(html), 'no raw dumps on the page');
  assert.match(html, /<details id="support">/, 'technical detail lives behind Details for support');
  for (const step of html.match(/<section id="step-[^"]+"[^]*?<\/section>/g)) {
    assert.ok((step.match(/class="primary"/g) ?? []).length <= 1, 'a step never offers two primary actions');
  }
});
