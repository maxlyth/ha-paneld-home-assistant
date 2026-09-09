import test from 'node:test';
import assert from 'node:assert/strict';
import {createUsbTransactionPorts} from '../src/usb-transaction-ports.mjs';
import {ACCESSIBILITY_SERVICE} from '../src/permission-contract.mjs';

function fixture({badTarget = false, badApk = false, badRead = false, failGrant = false,
  badVerify = false, rootMode = 'rootless', sdk = 33} = {}) {
  const target = {model: 'Test panel', serial: 'serial', primaryAbi: 'arm64-v8a', androidSdk: sdk,
    rootMode, usbVendorId: 1, usbProductId: 2, usbSerial: 'usb'};
  const artifact = {apkSize: 1234, apkSha256: 'a'.repeat(64)};
  const release = {kind: 'authenticated-apk-bytes', descriptor: artifact};
  const receipt = {phase: 'healthy', target, artifact};
  let quarantine = 0;
  const commands = [];
  const adb = {async createSocket(command) {
    commands.push(command);
    const n = command.match(/BEGIN:([a-f0-9]{32})/)[1];
    let body;
    if (command.includes('HAPANELD_POSTURE_BEGIN:')) {
      const fields = {MODEL: badTarget ? 'Different panel' : target.model, SERIAL: target.serial,
        ABI: target.primaryAbi, SDK: String(sdk), UID: rootMode === 'root_adbd' ? '0' : '2000',
        SECURE: '1', DEBUGGABLE: '0', SU: rootMode === 'root_su' ? 'present' : 'absent'};
      body = [`HAPANELD_POSTURE_BEGIN:${n}`, ...Object.entries(fields).flatMap(([key, value]) =>
        [`HAPANELD_POSTURE_${key}_BEGIN:${n}`, value, `HAPANELD_POSTURE_${key}_END:${n}:0`]),
      `HAPANELD_POSTURE_END:${n}`, ''].join('\n');
    } else if (command.includes('HAPANELD_INSTALLED_BEGIN:')) {
      const path = '/data/app/test/base.apk';
      body = [`HAPANELD_INSTALLED_BEGIN:${n}`, 'present', path,
        `HAPANELD_INSTALLED_MODE_BEGIN:${n}`, '81a4', `HAPANELD_INSTALLED_MODE_END:${n}:0`,
        `HAPANELD_INSTALLED_SIZE_BEGIN:${n}`, '1234', `HAPANELD_INSTALLED_SIZE_END:${n}:0`,
        `HAPANELD_INSTALLED_SHA_BEGIN:${n}`, `${badApk ? 'b'.repeat(64) : artifact.apkSha256}  ${path}`,
        `HAPANELD_INSTALLED_SHA_END:${n}:0`, `HAPANELD_INSTALLED_END:${n}`, ''].join('\n');
    } else if (command.includes('HAPANELD_PERMISSIONS_READ_BEGIN:')) {
      body = `HAPANELD_PERMISSIONS_READ_BEGIN:${n}\n${badRead ? "cannot read settings" : 'com.other/.Reader'}\nHAPANELD_PERMISSIONS_READ_END:${n}:0\n`;
    } else if (command.includes('HAPANELD_PERMISSIONS_GRANT_BEGIN:')) {
      body = `HAPANELD_PERMISSIONS_GRANT_BEGIN:${n}\nHAPANELD_PERMISSIONS_GRANT_END:${n}:${failGrant ? 1 : 0}\n`;
    } else {
      assert.ok(command.includes('HAPANELD_PERMISSIONS_VERIFY_BEGIN:'));
      body = [`HAPANELD_PERMISSIONS_VERIFY_BEGIN:${n}`, `com.other/.Reader:${ACCESSIBILITY_SERVICE}`, '1',
        `WRITE_SETTINGS: ${badVerify ? 'deny' : 'allow'}`, 'SYSTEM_ALERT_WINDOW: allow',
        sdk >= 33 ? 'android.permission.POST_NOTIFICATIONS: granted=true, flags=[]' : 'not_required',
        `HAPANELD_PERMISSIONS_VERIFY_END:${n}:0`, ''].join('\n');
    }
    return {readable: new ReadableStream({start(controller) {
      controller.enqueue(new TextEncoder().encode(body)); controller.close();
    }}), close: async () => {}};
  }};
  const ports = createUsbTransactionPorts({adb,
    usbDevice: {vendorId: 1, productId: 2, serialNumber: 'usb'}, authenticate: async () => release,
    quarantine: () => {quarantine++;}});
  return {ports, receipt, release, commands, get quarantined() {return quarantine;}};
}

test('permissions require current USB posture and exact installed APK before any grants', async () => {
  for (const rootMode of ['rootless', 'root_adbd', 'root_su']) {
    for (const sdk of [32, 33]) {
      const f = fixture({rootMode, sdk});
      await f.ports.authenticate();
      assert.deepEqual(await f.ports.commissionPermissions(f.receipt, f.release),
        {permissionsVerified: true, notificationsRequired: sdk >= 33});
      assert.equal(f.commands.length, 5);
      assert.match(f.commands[0], /HAPANELD_POSTURE_BEGIN/);
      assert.match(f.commands[1], /HAPANELD_INSTALLED_BEGIN/);
      assert.match(f.commands[3], /HAPANELD_PERMISSIONS_GRANT_BEGIN/);
      assert.equal(f.quarantined, 0);
      assert.ok(!f.commands.some(command => /su -c|reboot|force-stop|tcpip|setprop/.test(command)));
    }
  }
});

test('mismatched target, APK or malformed preservation read cannot reach grant', async () => {
  for (const options of [{badTarget: true}, {badApk: true}, {badRead: true}]) {
    const f = fixture(options); await f.ports.authenticate();
    await assert.rejects(f.ports.commissionPermissions(f.receipt, f.release));
    assert.equal(f.quarantined, 1);
    assert.ok(!f.commands.some(command => command.includes('PERMISSIONS_GRANT_BEGIN')));
    await assert.rejects(f.ports.authenticate(), /transaction_unavailable/);
  }
});

test('grant failure or denied readback quarantines connection and never claims completion', async () => {
  for (const options of [{failGrant: true}, {badVerify: true}]) {
    const f = fixture(options); await f.ports.authenticate();
    await assert.rejects(f.ports.commissionPermissions(f.receipt, f.release), /permissions_unverified/);
    assert.equal(f.quarantined, 1);
  }
  const f = fixture(); await f.ports.authenticate();
  await assert.rejects(f.ports.commissionPermissions({...f.receipt, phase: 'installed'}, f.release), /transaction_invalid/);
  assert.equal(f.commands.length, 0);
});
