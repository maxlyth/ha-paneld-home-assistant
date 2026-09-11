import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createInstallController} from '../src/install-controller.mjs';

const target = {model: 'Test panel', serial: 'serial', primaryAbi: 'arm64-v8a', androidSdk: 33,
  rootMode: 'rootless', usbVendorId: 1, usbProductId: 2, usbSerial: 'usb'};

async function fixture() {
  const artifact = {apkSha256: 'a'.repeat(64)};
  let receipt = {id: 'b'.repeat(32), phase: 'healthy', target, artifact};
  let release = {kind: 'authenticated-apk-bytes', descriptor: artifact};
  let current = true, grants = 0, lockAvailable = true;
  let grant = async () => {grants++; return {permissionsVerified: true};};
  const controller = createInstallController({store: {load: async () => receipt},
    ports: {authenticate: async () => release, inspect: async () => ({installed: true}),
      commissionPermissions: async (...args) => grant(...args), setup: async () => ({reportedComplete: false})},
    ensureCurrent: () => {if (!current) throw new Error('session_closed');},
    locks: {request: async (name, options, callback) => {
      assert.match(name, /^ha-paneld-usb:[a-f0-9]{64}$/);
      assert.deepEqual(options, {mode: 'exclusive', ifAvailable: true});
      return callback(lockAvailable ? {} : null);
    }}});
  await controller.preview(target);
  return {controller, get grants() {return grants;}, setReceipt: value => {receipt = {...receipt, ...value};},
    setRelease: value => {release = value;}, stop: () => {current = false;},
    denyLock: () => {lockAvailable = false;}, setGrant: value => {grant = value;}};
}

test('permission action requires separate explicit confirmation and healthy receipt', async () => {
  const f = await fixture();
  for (const confirmation of [undefined, false, 1, 'yes']) {
    await assert.rejects(f.controller.commissionPermissions(confirmation), /confirmation_required/);
  }
  assert.equal(f.grants, 0);
  assert.deepEqual(await f.controller.commissionPermissions(true), {permissionsVerified: true});
  f.setReceipt({phase: 'installed'});
  await assert.rejects(f.controller.commissionPermissions(true), /transaction_invalid/);
  assert.equal(f.grants, 1);
});

test('fresh authentication, job identity and session guard fail before grants', async () => {
  for (const change of [f => f.setReceipt({id: 'c'.repeat(32)}), f => f.stop(), f => f.denyLock(),
    f => f.setRelease({kind: 'authenticated-apk-bytes', descriptor: {apkSha256: 'f'.repeat(64)}})]) {
    const f = await fixture(); change(f);
    await assert.rejects(f.controller.commissionPermissions(true));
    assert.equal(f.grants, 0);
  }
});

test('permission mutation holds controller lock against read, preview or concurrent grant', async () => {
  const f = await fixture();
  let finish, entered;
  const running = new Promise(resolve => {entered = resolve;});
  f.setGrant(() => new Promise(resolve => {finish = resolve; entered();}));
  const first = f.controller.commissionPermissions(true);
  await running;
  await assert.rejects(f.controller.commissionPermissions(true), /transaction_busy/);
  await assert.rejects(f.controller.observeSetup(), /transaction_busy/);
  await assert.rejects(f.controller.preview(target), /transaction_busy/);
  finish({permissionsVerified: true});
  await first;
  assert.deepEqual(await f.controller.observeSetup(), {reportedComplete: false});
});

test('permissions are granted only by the Install press, after the app is healthy', () => {
  const source = readFileSync(new URL('../src/install-main.mjs', import.meta.url), 'utf8');
  // The single consent flows through: there is exactly one grant call, inside installAll.
  const calls = source.match(/controller\.commissionPermissions\(/g) ?? [];
  assert.equal(calls.length, 1);
  const body = source.slice(source.indexOf('async function installAll()'), source.indexOf('function finish('));
  assert.match(body, /controller\.commissionPermissions\(true\)/);
  const guard = body.indexOf("receipt?.phase !== 'healthy') throw");
  assert.ok(guard >= 0, 'installAll refuses to continue unless the app reports healthy');
  assert.ok(guard < body.indexOf('commissionPermissions'),
    'permissions are never granted before the installed app reports healthy');
  assert.match(body, /permissionsVerified !== true/, 'an unverified grant is a failure, not a success');
  // Never on load: installAll only runs from the Install press or a job already under way.
  const onLoad = source.slice(source.lastIndexOf('if (!supported) {'));
  assert.ok(!onLoad.includes('installAll('));
});

function finishedJobFixture({installed = true, descriptor} = {}) {
  const artifact = {apkSha256: 'a'.repeat(64)};
  let stored = {id: 'b'.repeat(32), revision: 6, phase: 'healthy', target, artifact};
  const retired = [], inspected = [];
  const release = {kind: 'authenticated-apk-bytes', descriptor: descriptor ?? artifact};
  const controller = createInstallController({
    store: {load: async () => stored,
      retire: async (key, revision) => {retired.push(revision); stored = null;}},
    ports: {authenticate: async () => release,
      inspect: async receipt => {inspected.push(receipt.phase); return {installed};}},
    locks: {request: async (name, options, callback) => callback({})}});
  return {controller, retired, inspected};
}

test('a finished job resumes only while the panel still runs its app', async () => {
  const same = finishedJobFixture({installed: true});
  const preview = await same.controller.preview(target);
  assert.equal(preview.receipt.phase, 'healthy', 'permissions and setup can still follow');
  assert.deepEqual(same.retired, []);
});

test('a finished job whose app was replaced is retired, not a lock-out', async () => {
  const changed = finishedJobFixture({installed: false});
  const preview = await changed.controller.preview(target);
  assert.equal(preview.receipt, null, 'the next install starts fresh');
  assert.deepEqual(changed.retired, [6], 'retired at the exact revision inspected');
  assert.deepEqual(changed.inspected, ['healthy', 'prepared'], 'then inspected as a new install');
});

test('a finished job for another release is retired without asking the panel about it', async () => {
  const other = finishedJobFixture({descriptor: {apkSha256: 'f'.repeat(64)}});
  const preview = await other.controller.preview(target);
  assert.equal(preview.receipt, null);
  assert.deepEqual(other.retired, [6]);
  assert.deepEqual(other.inspected, ['prepared']);
});
