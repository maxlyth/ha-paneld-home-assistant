import { inspectCleanTarget } from './clean-inspection.mjs';
import { buildPosture, parsePosture } from './posture.mjs';
import { readShell } from './shell-session.mjs';
import { buildPathState, parsePathState, buildStagedObservation, parseStagedObservation } from './staging-contract.mjs';
import { uploadApk } from './usb-upload.mjs';
import { buildInstall, parseInstall, buildLaunch, parseLaunch } from './install-contract.mjs';
import { inspectInstalledApk } from './installed-observation.mjs';
import { readUsbHealth } from './usb-health.mjs';
import { readUsbSetup } from './usb-setup.mjs';
import { buildPermissionRead, parsePermissionRead, buildPermissionGrant, parsePermissionGrant,
  buildPermissionVerification, parsePermissionVerification } from './permission-contract.mjs';
import { verifyStagedPrefix } from './staged-prefix.mjs';
import { buildPrefixCleanup, parsePrefixCleanup } from './cleanup-contract.mjs';
import { TransactionError } from './transaction.mjs';

const nonce = () => [...crypto.getRandomValues(new Uint8Array(16))].map(v => v.toString(16).padStart(2, '0')).join('');
const fail = code => { throw new TransactionError(code); };

// Internal composition for a single live connection. Caller owns verified file
// selection, confirmation, receipt creation and device-wide transaction lock.
// No USB discovery, network ADB activation, HA credentials or entry creation.
export function createUsbTransactionPorts({ adb, usbDevice, authenticate,
  ensureCurrent = () => {}, quarantine, onUploadProgress = () => {} }) {
  if (!adb || !usbDevice || typeof authenticate !== 'function' ||
      typeof ensureCurrent !== 'function' || typeof quarantine !== 'function') fail('invalid_request');
  let stopped = false;
  let authenticated;
  let uploadedJob;
  const stop = () => { stopped = true; quarantine(); };
  const guard = () => { if (stopped) fail('transaction_unavailable'); ensureCurrent(); };
  const binding = (receipt, release) => {
    guard();
    if (release !== authenticated || release?.kind !== 'authenticated-apk-bytes') fail('artifact_changed');
    if (receipt.target.usbVendorId !== usbDevice.vendorId || receipt.target.usbProductId !== usbDevice.productId ||
        receipt.target.usbSerial !== (usbDevice.serialNumber ?? '')) fail('target_changed');
  };
  const posture = async (receipt, release) => {
    binding(receipt, release);
    const n = nonce();
    const observed = parsePosture(await readShell(adb, buildPosture(n)), n, receipt.target);
    binding(receipt, release);
    return { ...observed, usbVendorId: usbDevice.vendorId, usbProductId: usbDevice.productId,
      usbSerial: usbDevice.serialNumber ?? '' };
  };
  const clean = async (receipt, release) => {
    const result = await inspectCleanTarget(adb, release.descriptor, guard);
    for (const key of ['model', 'serial', 'primaryAbi', 'androidSdk', 'rootMode']) {
      if (result[key] !== receipt.target[key]) fail('target_changed');
    }
    binding(receipt, release);
  };
  const staged = async (receipt, release) => {
    const n = nonce();
    return parseStagedObservation(await readShell(adb, buildStagedObservation(n, receipt.id),
      { timeoutMs: 30000 }), n, receipt.id, release.descriptor);
  };
  const prefix = async (receipt, release) => {
    const n = nonce();
    return verifyStagedPrefix(await readShell(adb, buildStagedObservation(n, receipt.id),
      {timeoutMs: 30000}), n, receipt.id, release);
  };
  const recovery = async (receipt, release) => {
    const target = await posture(receipt, release);
    await clean(receipt, release);
    const n = nonce();
    const present = parsePathState(await readShell(adb, buildPathState(n, receipt.id)), n);
    if (present) await prefix(receipt, release);
    binding(receipt, release);
    return {target, clean: true, absent: !present, removable: present};
  };
  const protect = action => async (...args) => {
    try { return await action(...args); }
    catch (error) { stop(); throw error; }
  };
  return Object.freeze({
    authenticate: protect(async () => {
      guard();
      authenticated = await authenticate();
      guard();
      return authenticated;
    }),
    inspect: protect(async (receipt, release) => {
      const target = await posture(receipt, release);
      let isClean = false, isStaged = false, installed = false, healthy = false;
      if (receipt.phase === 'staging' && uploadedJob !== receipt.id) {
        // A lost completion receipt does not necessarily mean a partial upload.
        // Missing files need recovery; present files must pass all fresh clean,
        // regular-file, size and signed-hash checks below. Never upload again.
        const n = nonce();
        if (!parsePathState(await readShell(adb, buildPathState(n, receipt.id)), n)) {
          binding(receipt, release);
          return { target, clean: false, staged: false, installed: false, healthy: false };
        }
      }
      if (['prepared', 'staging', 'staged'].includes(receipt.phase)) {
        await clean(receipt, release);
        isClean = true;
        if (receipt.phase !== 'prepared') isStaged = receipt.phase === 'staging' && uploadedJob !== receipt.id
          ? (await prefix(receipt, release)).complete : await staged(receipt, release);
      } else {
        installed = await inspectInstalledApk(adb, release.descriptor, guard);
        if (installed && receipt.phase === 'launching') {
          healthy = await readUsbHealth(adb, release.descriptor, { ensureCurrent: guard, quarantine: stop });
        }
      }
      binding(receipt, release);
      return { target, clean: isClean, staged: isStaged, installed, healthy };
    }),
    inspectRecovery: protect(recovery),
    cleanup: protect(async (receipt, release) => {
      if (receipt.phase !== 'cleanup_pending') fail('transaction_invalid');
      await posture(receipt, release);
      await clean(receipt, release);
      const observation = await prefix(receipt, release);
      binding(receipt, release);
      const n = nonce();
      parsePrefixCleanup(await readShell(adb, buildPrefixCleanup(n, receipt.id, observation),
        {timeoutMs: 30000}), n);
      binding(receipt, release);
    }),
    stage: protect(async (receipt, release) => {
      if (receipt.phase !== 'staging') fail('transaction_invalid');
      await posture(receipt, release);
      await clean(receipt, release);
      const n = nonce();
      if (parsePathState(await readShell(adb, buildPathState(n, receipt.id)), n)) fail('staging_path_exists');
      binding(receipt, release);
      await uploadApk(adb, receipt.id, release, { ensureCurrent: guard, quarantine: stop,
        onProgress: onUploadProgress });
      uploadedJob = receipt.id;
    }),
    install: protect(async (receipt, release) => {
      if (receipt.phase !== 'installing') fail('transaction_invalid');
      await posture(receipt, release);
      await clean(receipt, release);
      await staged(receipt, release);
      binding(receipt, release);
      const n = nonce();
      const result = parseInstall(await readShell(adb, buildInstall(n, receipt.id, receipt.target.androidSdk),
        { timeoutMs: 180000 }), n);
      if (result !== 'installed') fail('install_refused');
      binding(receipt, release);
    }),
    launch: protect(async (receipt, release) => {
      if (receipt.phase !== 'launching') fail('transaction_invalid');
      await posture(receipt, release);
      if (!await inspectInstalledApk(adb, release.descriptor, guard)) fail('installed_artifact_mismatch');
      binding(receipt, release);
      const n = nonce();
      if (parseLaunch(await readShell(adb, buildLaunch(n), { timeoutMs: 30000 }), n) !== 'started') fail('launch_refused');
      binding(receipt, release);
    }),
    setup: protect(async (receipt, release) => {
      if (receipt.phase !== 'healthy') fail('transaction_invalid');
      await posture(receipt, release);
      if (!await inspectInstalledApk(adb, release.descriptor, guard)) fail('installed_artifact_mismatch');
      const result = await readUsbSetup(adb, {ensureCurrent: guard, quarantine: stop});
      binding(receipt, release);
      return result;
    }),
    commissionPermissions: protect(async (receipt, release) => {
      if (receipt.phase !== 'healthy') fail('transaction_invalid');
      await posture(receipt, release);
      if (!await inspectInstalledApk(adb, release.descriptor, guard)) fail('installed_artifact_mismatch');
      binding(receipt, release);
      let n = nonce();
      const existing = parsePermissionRead(await readShell(adb, buildPermissionRead(n), {maximum: 8192}), n);
      binding(receipt, release);
      n = nonce();
      parsePermissionGrant(await readShell(adb, buildPermissionGrant(n, receipt.target.androidSdk, existing),
        {timeoutMs: 30000, maximum: 16384}), n);
      binding(receipt, release);
      n = nonce();
      const result = parsePermissionVerification(await readShell(adb,
        buildPermissionVerification(n, receipt.target.androidSdk), {timeoutMs: 30000, maximum: 16384}),
      n, receipt.target.androidSdk, existing);
      binding(receipt, release);
      return result;
    }),
  });
}
