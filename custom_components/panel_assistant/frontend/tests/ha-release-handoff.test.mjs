import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { startReleaseHandoff } from '../src/ha-release-handoff.mjs';

const metadata = () => ({
  id: 'a'.repeat(32), tag: 'v1.2.3', checksum: btoa('checksum'),
  checksum_signature: btoa('s'.repeat(256)), descriptor: btoa('{}'),
  descriptor_signature: btoa('d'.repeat(256)), apk_size: 3, apk_sha256: 'b'.repeat(64),
});
const json = (value) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
const tick = () => new Promise((resolve) => setImmediate(resolve));
function fixture(responses = [json(metadata()), new Response('apk')], options = {}) {
  const listeners = new Set();
  const calls = [];
  const posts = [];
  const states = [];
  const child = { closed: false, postMessage: (...args) => posts.push(args) };
  let opened;
  const windowObject = {
    crypto: webcrypto, location: { origin: 'http://ha.example:8123' },
    addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn),
    open: (url, target) => { opened = { url: new URL(url), target }; return child; },
  };
  const hass = { fetchWithAuth: async (...args) => { calls.push(args); return await responses.shift(); } };
  const handle = startReleaseHandoff(hass, 'https://installer.example/', {
    windowObject, onState: (state) => states.push(state), ...options,
  });
  const send = (type, overrides = {}, additions = {}) => {
    const event = { source: child, origin: 'https://installer.example',
      data: { type: `ha-paneld/usb-${type}`, nonce: opened.url.hash && new URLSearchParams(opened.url.hash.slice(1)).get('nonce'), ...additions }, ...overrides };
    for (const listener of listeners) listener(event);
  };
  return { handle, send, calls, posts, states, listeners, opened, child, windowObject };
}
test('opens synchronously, selects stable, sends only bounded bytes and waits for verified', async () => {
  const f = fixture();
  assert.equal(f.opened.target, '_blank');
  const fragment = new URLSearchParams(f.opened.url.hash.slice(1));
  assert.deepEqual([...fragment.keys()], ['ha_origin', 'nonce', 'rc']);
  assert.equal(fragment.get('ha_origin'), 'http://ha.example:8123');
  assert.match(fragment.get('nonce'), /^[a-f0-9]{32}$/);
  assert.equal(fragment.get('rc'), '');
  assert.equal(f.calls.length, 0);
  f.send('verified');
  f.send('ready'); f.send('ready');
  await tick();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0][0], '/api/panel_assistant/usb/release');
  assert.equal(f.calls[0][1].body, '{}');
  assert.equal(f.calls[1][0], `/api/panel_assistant/usb/release/${'a'.repeat(32)}/apk`);
  assert.ok(f.calls.every(([, init]) => init.redirect === 'error' && init.signal instanceof AbortSignal));
  assert.equal(f.posts.length, 1);
  const [message, target] = f.posts[0];
  assert.equal(target, 'https://installer.example');
  assert.deepEqual(Object.keys(message), ['type', 'nonce', 'bundle', 'apk']);
  assert.deepEqual(Object.keys(message.bundle), ['tag', 'checksum', 'checksumSignature', 'descriptor', 'descriptorSignature']);
  assert.ok(message.bundle.checksum instanceof Uint8Array);
  assert.equal(await message.apk.text(), 'apk');
  assert.equal(f.listeners.size, 1);
  f.send('verified'); await f.handle.completion;
  assert.equal(f.child.closed, false);
  assert.deepEqual(f.states, ['waiting', 'preparing', 'downloading', 'verifying', 'verified']);
  f.handle.cancel();
  assert.equal(f.listeners.size, 0, 'cancel stops serving the verified bytes');
});
test('a reloaded installer window gets the same verified bytes again without a new download', async () => {
  const f = fixture();
  f.send('ready'); await tick(); await tick();
  f.send('verified'); await f.handle.completion;
  f.send('ready', { source: {} }); f.send('ready', {}, { nonce: 'wrong' });
  assert.equal(f.posts.length, 1, 'only the same window with the same nonce is answered');
  f.send('ready');
  assert.equal(f.posts.length, 2);
  assert.equal(f.posts[1][0], f.posts[0][0], 'the identical message, not a new fetch');
  assert.equal(f.posts[1][1], 'https://installer.example');
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.states.at(-1), 'verified', 'a re-send does not reopen the transfer');
  f.child.closed = true;
  f.send('ready');
  assert.equal(f.posts.length, 2, 'a closed window gets nothing');
  f.handle.cancel();
  assert.equal(f.listeners.size, 0);
});
test('ignores wrong source, origin, nonce and additional message keys', async () => {
  const f = fixture();
  f.send('ready', { source: {} }); f.send('ready', { origin: 'https://evil.example' });
  f.send('ready', {}, { nonce: 'wrong' }); f.send('ready', {}, { unexpected: true });
  await tick(); assert.equal(f.calls.length, 0);
  f.handle.cancel(); await assert.rejects(f.handle.completion, { code: 'cancelled' });
});
test('RC is selected explicitly in fragment and POST, without stable fallback', async () => {
  const m = metadata(); m.tag = 'v1.2.3-rc2';
  const f = fixture([json(m), new Response('apk')], { rcTag: m.tag });
  assert.equal(new URLSearchParams(f.opened.url.hash.slice(1)).get('rc'), m.tag);
  f.send('ready'); await tick();
  assert.deepEqual(JSON.parse(f.calls[0][1].body), { release_candidate: m.tag });
  f.send('verified'); await f.handle.completion;
  f.handle.cancel();
});
test('cancel aborts fetch and prevents late response from posting', async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const f = fixture([pending]); f.send('ready');
  f.handle.cancel(); await assert.rejects(f.handle.completion, { code: 'cancelled' });
  assert.equal(f.calls[0][1].signal.aborted, true);
  resolve(json(metadata())); await tick();
  assert.equal(f.calls.length, 1); assert.equal(f.posts.length, 0); assert.equal(f.listeners.size, 0);
});
for (const [name, mutate] of [
  ['unknown URL field', (m) => { m.url = 'https://evil.example'; }],
  ['path-like identifier', (m) => { m.id = '../secret'; }],
  ['APK limit', (m) => { m.apk_size = 64 * 1024 * 1024 + 1; }],
  ['stable channel mismatch', (m) => { m.tag = 'v1.2.3-rc1'; }],
  ['signature length', (m) => { m.checksum_signature = btoa('s'.repeat(255)); }],
  ['checksum limit', (m) => { m.checksum = btoa('c'.repeat(513)); }],
  ['descriptor limit', (m) => { m.descriptor = btoa('d'.repeat(4097)); }],
  ['base64 whitespace', (m) => { m.descriptor += '\n'; }],
  ['noncanonical base64 padding', (m) => { m.descriptor = 'Zh=='; }],
]) {
  test(`rejects ${name} before APK request`, async () => {
    const m = metadata(); mutate(m); const f = fixture([json(m)]);
    f.send('ready'); await assert.rejects(f.handle.completion, { code: 'invalid_response' });
    assert.equal(f.calls.length, 1); assert.equal(f.posts.length, 0); assert.equal(f.listeners.size, 0);
  });
}
for (const [name, response] of [
  ['non-success status', () => new Response('apk', { status: 403 })],
  ['declared size mismatch', () => new Response('apk', { headers: { 'Content-Length': '4' } })],
  ['oversized stream', () => new Response('apkk')],
  ['truncated stream', () => new Response('ap')],
]) {
  test(`rejects APK ${name}`, async () => {
    const f = fixture([json(metadata()), response()]); f.send('ready');
    await assert.rejects(f.handle.completion, { code: 'invalid_response' }); assert.equal(f.posts.length, 0);
  });
}
test('metadata stream is bounded even without content-length', async () => {
  const f = fixture([new Response(' '.repeat(8193), { headers: { 'Content-Type': 'application/json' } })]);
  f.send('ready'); await assert.rejects(f.handle.completion, { code: 'invalid_response' });
});
test('deadline and child verification error clean up listeners', async () => {
  const f = fixture([], { timeoutMs: 5 });
  await assert.rejects(f.handle.completion, { code: 'timeout' }); assert.equal(f.listeners.size, 0);
  const failed = fixture(); failed.send('error');
  await assert.rejects(failed.handle.completion, { code: 'verification_failed' }); assert.equal(failed.listeners.size, 0);
});
test('rejects unsafe destinations and existing fragments before opening', async () => {
  const windowObject = { removeEventListener() {}, open() { assert.fail('must not open'); } };
  for (const url of ['http://installer.example/install', 'javascript:alert(1)',
    'https://user:password@installer.example/', 'https://installer.example/#existing']) {
    const handle = startReleaseHandoff({ fetchWithAuth() {} }, url, { windowObject });
    await assert.rejects(handle.completion, { code: 'invalid_destination' });
  }
});
const feedMetadata = () => ({
  id: 'c'.repeat(32), tag: 'build-772', feed: btoa('{"builds":[]}\n'),
  feed_signature: btoa('f'.repeat(256)), apk_size: 3, apk_sha256: 'b'.repeat(64),
});
test('a dev build is requested by its build tag and delivered as the signed feed', async () => {
  const f = fixture([json(feedMetadata()), new Response('apk')], { rcTag: 'build-772' });
  assert.equal(new URLSearchParams(f.opened.url.hash.slice(1)).get('rc'), 'build-772');
  f.send('ready'); await tick(); await tick();
  assert.deepEqual(JSON.parse(f.calls[0][1].body), { release_candidate: 'build-772' });
  assert.equal(f.calls[1][0], `/api/panel_assistant/usb/release/${'c'.repeat(32)}/apk`);
  const [message] = f.posts[0];
  assert.deepEqual(Object.keys(message.bundle), ['tag', 'feed', 'feedSignature']);
  assert.equal(message.bundle.tag, 'build-772');
  assert.equal(new TextDecoder().decode(message.bundle.feed), '{"builds":[]}\n');
  assert.equal(message.bundle.feedSignature.length, 256);
  f.send('verified'); await f.handle.completion;
  f.handle.cancel();
});
test('a signed feed at its full 256 KiB fits the feed response bound', async () => {
  const m = feedMetadata(); m.feed = btoa('x'.repeat(256 * 1024));
  // Python's default separators add a space after every comma and colon.
  const body = JSON.stringify(m).replace(/","|":/g, (s) => `${s[0]}${s[1]} ${s.slice(2)}`);
  const f = fixture([new Response(body, { headers: { 'Content-Type': 'application/json' } }), new Response('apk')], { rcTag: 'build-772' });
  f.send('ready'); await tick(); await tick();
  assert.equal(f.posts[0][0].bundle.feed.length, 256 * 1024);
  f.send('verified'); await f.handle.completion;
  f.handle.cancel();
});
for (const [name, rcTag, mutate] of [
  ['GitHub fields for a feed tag', 'build-772', () => metadata()],
  ['GitHub fields named with the feed tag', 'build-772', () => ({ ...metadata(), tag: 'build-772' })],
  ['feed fields for an RC tag', 'v1.2.3-rc2', () => ({ ...feedMetadata(), tag: 'v1.2.3-rc2' })],
  ['feed fields for stable', null, () => ({ ...feedMetadata(), tag: 'v1.2.3' })],
  ['both shapes at once', 'build-772', () => ({ ...metadata(), ...feedMetadata() })],
  ['another build', 'build-772', () => ({ ...feedMetadata(), tag: 'build-771' })],
  ['an oversized feed', 'build-772', () => ({ ...feedMetadata(), feed: btoa('x'.repeat(256 * 1024 + 1)) })],
  ['a short feed signature', 'build-772', () => ({ ...feedMetadata(), feed_signature: btoa('f'.repeat(255)) })],
  ['an empty feed', 'build-772', () => ({ ...feedMetadata(), feed: '' })],
]) {
  test(`rejects ${name} before APK request`, async () => {
    const f = fixture([json(mutate())], { rcTag });
    f.send('ready'); await assert.rejects(f.handle.completion, { code: 'invalid_response' });
    assert.equal(f.calls.length, 1); assert.equal(f.posts.length, 0);
  });
}
test('refuses a build tag outside the grammar before opening', async () => {
  for (const rcTag of ['build-0', 'build-0772', 'build-2147483648', 'build-772\n', 'build-']) {
    const windowObject = { removeEventListener() {}, open() { assert.fail('must not open'); } };
    const handle = startReleaseHandoff({ fetchWithAuth() {} }, 'https://installer.example/', { windowObject, rcTag });
    await assert.rejects(handle.completion, { code: 'invalid_request' });
  }
});
