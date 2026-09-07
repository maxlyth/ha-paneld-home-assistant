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

test('fetches authenticated fixed route and parses catalogue', async () => {
  let args;
  const result = await fetchReleaseCatalog({ fetchWithAuth: async (...input) => { args = input; return json({ releases: [stable, rc] }); } });
  assert.deepEqual(result, [stable, rc]);
  assert.equal(args[0], '/api/ha_paneld/usb/releases');
  assert.equal(args[1].method, 'GET');
  assert.equal(args[1].redirect, 'error');
  assert.ok(args[1].signal instanceof AbortSignal);
  assert.equal(args[1].body, undefined);
});
for (const [name, response] of [
  ['status', () => new Response('{}', { status: 403 })],
  ['content type', () => new Response('{}')],
  ['invalid JSON', () => new Response('{', { headers: { 'Content-Type': 'application/json' } })],
  ['oversized body', () => new Response(' '.repeat(8193), { headers: { 'Content-Type': 'application/json' } })],
  ['oversized length', () => new Response('{}', { headers: { 'Content-Type': 'application/json', 'Content-Length': '8193' } })],
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
