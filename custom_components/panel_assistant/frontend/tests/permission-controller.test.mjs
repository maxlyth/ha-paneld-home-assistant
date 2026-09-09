import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createInstallController} from '../src/install-controller.mjs';
import {INSTALL_SCREEN_MESSAGES} from '../src/install-screen-messages.mjs';

const target = {model: 'Test panel', serial: 'serial', primaryAbi: 'arm64-v8a', androidSdk: 33,
  rootMode: 'rootless', usbVendorId: 1, usbProductId: 2, usbSerial: 'usb'};

async function fixture() {
  const artifact = {apkSha256: 'a'.repeat(64)};
  let receipt = {id: 'b'.repeat(32), phase: 'healthy', target, artifact};
  let release = {kind: 'authenticated-apk-bytes', descriptor: artifact};
  let current = true, grants = 0, lockAvailable = true;
  let grant = async () => {grants++; return {permissionsVerified: true};};
  const controller = createInstallController({store: {load: async () => receipt},
    ports: {authenticate: async () => release, inspect: async () => {},
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

test('frontend includes an independent permission checkbox and action, with honest retry wording', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const source = readFileSync(new URL('../src/install-main.mjs', import.meta.url), 'utf8');
  assert.match(html, /id="permissions-confirmation" type="checkbox"/);
  assert.match(html, /id="permissions-grant"[^>]+disabled/);
  assert.match(source, /controller\.commissionPermissions\(element\('permissions-confirmation'\)\.checked\)/);
  assert.match(source, /permissions-grant'\)\.disabled = .*permissions-confirmation'\)\.checked/);
  assert.match(INSTALL_SCREEN_MESSAGES.permissionsVerified, /not yet verified/);
  assert.match(INSTALL_SCREEN_MESSAGES.permissionsUnverified, /reload/i);
  assert.match(INSTALL_SCREEN_MESSAGES.permissionsHelp, /MQTT settings are preserved/);
});
