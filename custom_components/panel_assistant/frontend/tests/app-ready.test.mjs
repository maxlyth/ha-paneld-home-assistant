import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildAppReady, parseAppReady } from '../src/install-contract.mjs';

const nonce = 'f'.repeat(32);
const frame = status => `HAPANELD_READY_BEGIN:${nonce}\nHAPANELD_READY_END:${nonce}:${status}\n`;

test('the wait is a bounded, read-only loop on the app port', () => {
  const program = buildAppReady(nonce);
  assert.match(program, /while \[ \$i -lt 30 \]/, 'at most 30 checks');
  assert.match(program, /sleep 2/, 'two seconds apart, about a minute in all');
  assert.match(program, /grep -q ':8888 '/);
  assert.ok(!/am |pm |kill|rm |settings /.test(program), 'waiting never starts, stops or changes anything');
  assert.throws(() => buildAppReady('bad'));
});

test('listening and not-yet-listening are both answers, anything else is malformed', () => {
  assert.equal(parseAppReady(frame(0), nonce), true);
  assert.equal(parseAppReady(frame(1), nonce), false);
  assert.throws(() => parseAppReady(frame(2), nonce));
  assert.throws(() => parseAppReady(`HAPANELD_READY_BEGIN:${nonce}\nstray\nHAPANELD_READY_END:${nonce}:0\n`, nonce));
});

test('the strict health read runs only after waiting for the app', () => {
  const source = readFileSync(new URL('../src/usb-transaction-ports.mjs', import.meta.url), 'utf8');
  const wait = source.indexOf('buildAppReady(n)'), read = source.indexOf('readUsbHealth(adb');
  assert.ok(wait >= 0 && read >= 0 && wait < read, 'wait for the app, then read its health once');
  assert.match(source, /buildAppReady\(n\), \{ timeoutMs: 75000/, 'the shell may run longer than the loop');
});
