import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchReleaseCatalog, parseReleaseCatalog } from '../src/release-catalog.mjs';

const stable = { tag: 'v1.2.3', prerelease: false };
const rc = { tag: 'v1.2.4-rc1', prerelease: true };
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
test('accepts stable, RC-only, empty and bounded catalogues', () => {
  for (const releases of [[], [stable], [rc], [stable, rc], Array.from({ length: 30 }, (_, index) => ({ tag: `v1.2.4-rc${index + 1}`, prerelease: true }))]) {
    const result = parseReleaseCatalog({ releases });
    assert.deepEqual(result, releases);
    assert.ok(Object.isFrozen(result));
    assert.ok(result.every(Object.isFrozen));
  }
});
for (const [name, value] of [
  ['missing field', {}], ['extra field', { releases: [], url: 'https://example.com' }],
  ['non-array', { releases: {} }], ['null entry', { releases: [null] }],
  ['extra entry field', { releases: [{ ...stable, url: 'https://example.com' }] }],
  ['duplicate', { releases: [rc, rc] }],
  ['multiple stable', { releases: [stable, { tag: 'v1.2.2', prerelease: false }] }],
  ['nonboolean', { releases: [{ ...rc, prerelease: 'true' }] }],
  ['channel mismatch', { releases: [{ ...rc, prerelease: false }] }],
  ['stable marked RC', { releases: [{ ...stable, prerelease: true }] }],
  ['HTML', { releases: [{ tag: '<img src=x>', prerelease: false }] }],
  ['trailing newline', { releases: [{ tag: 'v1.2.3\n', prerelease: false }] }],
  ['leading zero', { releases: [{ tag: 'v01.2.3', prerelease: false }] }],
  ['long tag', { releases: [{ tag: `v1.2.${'3'.repeat(65)}`, prerelease: false }] }],
  ['too many', { releases: Array.from({ length: 31 }, (_, index) => ({ tag: `v1.2.4-rc${index + 1}`, prerelease: true })) }],
]) test(`refuses ${name}`, () => assert.throws(() => parseReleaseCatalog(value)));

const feed = { tag: 'build-772', prerelease: true, name: '0.9.7-rc4 build 772' };
const older = { tag: 'build-771', prerelease: true, name: '0.9.7-rc3+dev.1 build 771' };
test('accepts dev builds from the signed feed after GitHub releases', () => {
  const releases = [stable, rc, feed, older];
  const result = parseReleaseCatalog({ releases });
  assert.deepEqual(result, releases);
  assert.ok(result.every(Object.isFrozen));
  // Thirty GitHub releases and a full feed of 500 builds, within the byte bound.
  const full = [...Array.from({ length: 30 }, (_, index) => ({ tag: `v1.2.4-rc${index + 1}`, prerelease: true })),
    ...Array.from({ length: 500 }, (_, index) => ({ tag: `build-${2147483647 - index}`, prerelease: true,
      name: `${'9'.repeat(64)} build ${2147483647 - index}` }))];
  assert.equal(parseReleaseCatalog({ releases: full }).length, 530);
  assert.ok(JSON.stringify({ releases: full }, null, 2).length <= 128 * 1024);
});
for (const [name, value] of [
  ['feed build marked stable', { ...feed, prerelease: false }],
  ['feed build extra key', { ...feed, url: 'https://example.com' }],
  ['feed build without a name', { tag: feed.tag, prerelease: true }],
  ['name for another build', { ...feed, name: '0.9.7-rc4 build 771' }],
  ['name without a build number', { ...feed, name: '0.9.7-rc4' }],
  ['name with a space in the version', { ...feed, name: '0.9.7 rc4 build 772' }],
  ['name with an invalid version', { ...feed, name: '-0.9.7 build 772' }],
  ['non-string name', { ...feed, name: 772 }],
  ['GitHub tag with a name', { ...rc, name: '1.2.4-rc1 build 1' }],
  ['leading-zero build tag', { ...feed, tag: 'build-0772', name: '0.9.7-rc4 build 0772' }],
  ['build number above the Android bound', { ...feed, tag: 'build-2147483648', name: '0.9.7-rc4 build 2147483648' }],
]) test(`refuses ${name}`, () => assert.throws(() => parseReleaseCatalog({ releases: [stable, value] })));
test('refuses a duplicate or 501st feed build', () => {
  assert.throws(() => parseReleaseCatalog({ releases: [feed, feed] }));
  assert.throws(() => parseReleaseCatalog({ releases: Array.from({ length: 501 }, (_, index) => ({
    tag: `build-${index + 1}`, prerelease: true, name: `0.9.7 build ${index + 1}` })) }));
});

test('fetches authenticated fixed route and parses catalogue', async () => {
  let args;
  const result = await fetchReleaseCatalog({ fetchWithAuth: async (...input) => { args = input; return json({ releases: [stable, rc] }); } });
  assert.deepEqual(result, [stable, rc]);
  assert.equal(args[0], '/api/panel_assistant/usb/releases');
  assert.equal(args[1].method, 'GET');
  assert.equal(args[1].redirect, 'error');
  assert.ok(args[1].signal instanceof AbortSignal);
  assert.equal(args[1].body, undefined);
});
for (const [name, response] of [
  ['status', () => new Response('{}', { status: 403 })],
  ['content type', () => new Response('{}')],
  ['invalid JSON', () => new Response('{', { headers: { 'Content-Type': 'application/json' } })],
  ['oversized body', () => new Response(' '.repeat(128 * 1024 + 1), { headers: { 'Content-Type': 'application/json' } })],
  ['oversized length', () => new Response('{}', { headers: { 'Content-Type': 'application/json', 'Content-Length': String(128 * 1024 + 1) } })],
  ['incorrect length', () => new Response('{}', { headers: { 'Content-Type': 'application/json', 'Content-Length': '3' } })],
]) test(`fetch refuses ${name}`, async () => {
  await assert.rejects(fetchReleaseCatalog({ fetchWithAuth: async () => response() }));
});
test('abort and deadline settle even when fetch ignores cancellation', async () => {
  const hass = { fetchWithAuth: () => new Promise(() => {}) };
  await assert.rejects(fetchReleaseCatalog(hass, { timeoutMs: 5 }));
  const controller = new AbortController();
  const pending = fetchReleaseCatalog(hass, { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending);
});
test('deadline cancels a stalled response stream', async () => {
  let cancelled = false;
  const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }), { headers: { 'Content-Type': 'application/json' } });
  await assert.rejects(fetchReleaseCatalog({ fetchWithAuth: async () => response }, { timeoutMs: 5 }));
  assert.equal(cancelled, true);
});
