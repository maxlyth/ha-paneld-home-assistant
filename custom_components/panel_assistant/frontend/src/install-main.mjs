import { Adb, AdbDaemonTransport } from '@yume-chan/adb';
import AdbWebCredentialStore from '@yume-chan/adb-credential-web';
import { AdbDaemonWebUsbDeviceManager } from '@yume-chan/adb-daemon-webusb';
import { boundedUsb } from './usb-bounds.mjs';
import { verifySelectedBundle } from './selected-bundle.mjs';
import { inspectSessionTarget } from './session-target.mjs';
import { openJobStore } from './job-store.mjs';
import { createUsbTransactionPorts } from './usb-transaction-ports.mjs';
import { createInstallController } from './install-controller.mjs';
import { INSTALL_MESSAGES, installationView, errorView } from './install-view.mjs';
import { INSTALL_SCREEN_MESSAGES as screen } from './install-screen-messages.mjs';
import { handoffOptions, receiveReleaseHandoff } from './release-handoff.mjs';

const element = id => document.getElementById(id);
const files = element('bundle-files');
const rc = element('bundle-rc');
const verify = element('verify-bundle');
const connect = element('connect');
const cancel = element('cancel');
const confirmation = element('confirmation');
const action = element('install-action');
for (const node of document.querySelectorAll('[data-message]')) {
  node.textContent = INSTALL_MESSAGES[node.dataset.message];
}
for (const node of document.querySelectorAll('[data-screen-message]')) {
  node.textContent = screen[node.dataset.screenMessage];
}
for (const node of document.querySelectorAll('[data-screen-placeholder]')) {
  node.placeholder = screen[node.dataset.screenPlaceholder];
}
const manager = window.isSecureContext && navigator.usb
  ? new AdbDaemonWebUsbDeviceManager(boundedUsb(navigator.usb)) : undefined;
const supported = Boolean(manager && navigator.locks && globalThis.indexedDB);
let pinned, release, raw, adb, store, controller;
let handoff, incomingAuthenticate;
let busy = false, connected = false, quarantined = false, hasPreview = false;
let refineFailure = false;
let receipt = null;
let deadline;
let rejectStop;
const stopPromise = new Promise((_, reject) => { rejectStop = reject; });
void stopPromise.catch(() => {});
const status = text => { element('status').textContent = text; };

function render() {
  files.disabled = rc.disabled = verify.disabled = Boolean(handoff) || busy || connected || quarantined;
  connect.disabled = !supported || !release || busy || connected || quarantined;
  cancel.disabled = quarantined || (!busy && !connected);
  confirmation.disabled = busy || !connected || quarantined;
  element('installation').hidden = !hasPreview;
  if (!hasPreview) return;
  const view = installationView(receipt, {
    connected: connected && !quarantined, confirmed: confirmation.checked, busy,
  });
  element('install-title').textContent = INSTALL_MESSAGES[view.titleKey];
  element('install-body').textContent = INSTALL_MESSAGES[view.bodyKey];
  element('confirmation-label').hidden = !view.showConfirmation;
  action.hidden = view.actionKey === null;
  action.textContent = view.actionKey ? INSTALL_MESSAGES[view.actionKey] : '';
  action.disabled = !view.actionEnabled;
  element('receipt-result').textContent = receipt ? JSON.stringify(receipt, null, 2) : '';
  element('setup-observation').hidden = receipt?.phase !== 'healthy';
  element('setup-check').disabled = receipt?.phase !== 'healthy' || busy || !connected || quarantined;
  element('permissions-confirmation').disabled = element('setup-check').disabled;
  element('permissions-grant').disabled = element('setup-check').disabled || !element('permissions-confirmation').checked;
}

// Close each acquired resource independently. Late chooser/authentication
// completions call this again, so resources acquired after cancellation close too.
function closeResources() {
  const closingAdb = adb, closingRaw = raw;
  for (const resource of [closingAdb, closingRaw]) {
    if (!resource) continue;
    let timer;
    void Promise.race([
      Promise.resolve().then(() => resource.close()),
      new Promise(resolve => { timer = setTimeout(resolve, 5000); }),
    ]).catch(() => {}).finally(() => clearTimeout(timer));
  }
  try { store?.close(); } catch { /* The connection is already quarantined. */ }
}

function quarantine(message = INSTALL_MESSAGES.installErrorConnection) {
  const first = !quarantined;
  quarantined = true;
  connected = false;
  clearTimeout(deadline);
  handoff?.cancel();
  if (first) {
    refineFailure = message === INSTALL_MESSAGES.installErrorConnection;
    status(`${message} ${screen.reloadRequired}`);
    rejectStop(new Error('session_closed'));
  }
  render();
  closeResources();
}

function ensureCurrent() {
  if (quarantined) throw new Error('session_closed');
  if (incomingAuthenticate) return;
  if (!pinned || rc.value !== pinned.tag || files.files.length !== pinned.files.length ||
      pinned.files.some((file, index) => files.files[index] !== file)) {
    quarantine(INSTALL_MESSAGES.installErrorArtifact);
    throw new Error('selection_changed');
  }
}

// Guard every command entry, including calls following an awaited observation.
// Resource cleanup uses the original Adb instance and remains allowed after stop.
function guardedAdb(value) {
  return new Proxy(value, {
    get(target, key) {
      const member = Reflect.get(target, key, target);
      return typeof member === 'function' ? (...args) => {
        ensureCurrent();
        return member.apply(target, args);
      } : member;
    },
  });
}

function fail(error) {
  const key = errorView(error?.code);
  if (!quarantined) quarantine(INSTALL_MESSAGES[key]);
  else if (refineFailure && key !== 'installErrorGeneric') {
    // Ports quarantine synchronously before their rejected promise reaches the
    // UI. Preserve their specific diagnosis without restoring any permission.
    status(`${INSTALL_MESSAGES[key]} ${screen.reloadRequired}`);
    refineFailure = false;
  }
}
function selectionChanged() {
  if (handoff || busy || connected) { quarantine(INSTALL_MESSAGES.installErrorArtifact); return; }
  pinned = release = undefined;
  confirmation.checked = false;
  element('bundle-status').textContent = '';
  element('bundle-result').textContent = '';
  render();
}
files.addEventListener('change', selectionChanged);
rc.addEventListener('input', selectionChanged);
confirmation.addEventListener('change', render);
cancel.addEventListener('click', () => quarantine(screen.cancelled));
window.addEventListener('pagehide', () => quarantine(screen.pageClosed));

verify.addEventListener('click', async () => {
  if (verify.disabled) return;
  busy = true;
  release = undefined;
  pinned = Object.freeze({ files: Object.freeze(Array.from(files.files)), tag: rc.value });
  element('bundle-status').textContent = screen.bundleVerifying;
  render();
  try {
    const verified = await Promise.race([stopPromise,
      verifySelectedBundle(pinned.files, { expectedRcTag: pinned.tag || null })]);
    ensureCurrent();
    release = verified;
    element('bundle-result').textContent = JSON.stringify(release.descriptor, null, 2);
    element('bundle-status').textContent = screen.bundleVerified;
  } catch (error) {
    element('bundle-status').textContent = screen.bundleFailure;
    fail(error);
  } finally { busy = false; render(); }
});

connect.addEventListener('click', async () => {
  if (connect.disabled) return;
  busy = true;
  confirmation.checked = false;
  render();
  status(screen.selecting);
  deadline = setTimeout(() => quarantine(screen.connectionTimeout), 45000);
  try {
    // requestDevice is invoked directly within this user gesture, never on load.
    const chosen = manager.requestDevice();
    await Promise.race([stopPromise, (async () => {
      const device = await chosen;
      if (device) raw = device.raw;
      if (quarantined) { closeResources(); return; }
      if (!device) { quarantine(screen.noSelection); return; }
      ensureCurrent();
      status(screen.authenticating);
      const connection = await device.connect();
      if (quarantined) { closeResources(); return; }
      ensureCurrent();
      const transport = await AdbDaemonTransport.authenticate({
        serial: device.serial, connection,
        credentialStore: new AdbWebCredentialStore('ha-paneld-usb-installer'),
        readTimeLimit: 10000,
      });
      adb = new Adb(transport);
      if (quarantined) { closeResources(); return; }
      ensureCurrent();
      void adb.disconnected.then(() => quarantine(screen.disconnected), fail);
      const sessionAdb = guardedAdb(adb);
      status(screen.inspecting);
      store = await openJobStore();
      if (quarantined) { closeResources(); return; }
      ensureCurrent();
      const target = await inspectSessionTarget(sessionAdb, release.descriptor, raw, ensureCurrent);
      ensureCurrent();
      const ports = createUsbTransactionPorts({
        adb: sessionAdb, usbDevice: raw, ensureCurrent,
        authenticate: async () => {
          ensureCurrent();
          const verified = incomingAuthenticate ? await incomingAuthenticate() :
            await verifySelectedBundle(pinned.files, { expectedRcTag: pinned.tag || null });
          ensureCurrent();
          return verified;
        },
        quarantine: () => quarantine(),
      });
      controller = createInstallController({ store, ports, ensureCurrent,
        onReceipt(value) { receipt = value; render(); },
      });
      const preview = await controller.preview(target);
      ensureCurrent();
      receipt = preview.receipt;
      hasPreview = connected = true;
      element('target-result').textContent = JSON.stringify(target, null, 2);
      status(screen.previewReady);
    })().catch(error => { fail(error); throw error; })]);
  } catch (error) { fail(error); }
  finally { clearTimeout(deadline); busy = false; render(); }
});

action.addEventListener('click', async () => {
  if (action.disabled || !controller) return;
  busy = true;
  render();
  status(screen.advancing);
  try {
    // Each click is explicit permission for the displayed action. Pending
    // intents reconcile once, read-only; the controller never replays them.
    receipt = await Promise.race([stopPromise,
      (['recovery_required', 'cleanup_pending'].includes(receipt?.phase)
        ? controller.recover(true) : controller.run(true)).catch(error => { fail(error); throw error; })]);
    ensureCurrent();
    status(receipt.phase === 'healthy'
      ? `${INSTALL_MESSAGES.installHealthyBody} ${INSTALL_MESSAGES.installMqttBoundary}`
      : screen.progressUpdated);
  } catch (error) { fail(error); }
  finally { busy = false; render(); }
});

element('setup-check').addEventListener('click', async () => {
  if (element('setup-check').disabled || !controller) return;
  busy = true; render();
  element('setup-summary').textContent = screen.setupChecking;
  try {
    const result = await Promise.race([stopPromise,
      controller.observeSetup().catch(error => { fail(error); throw error; })]);
    ensureCurrent();
    element('setup-summary').textContent = result.needsUpdatedClient ? screen.setupUnsupported :
      result.actionRequired ? screen.setupAction : result.reportedComplete ? screen.setupComplete : screen.setupWaiting;
  } catch (error) { element('setup-summary').textContent = screen.setupWaiting; fail(error); }
  finally { busy = false; render(); }
});

element('permissions-confirmation').addEventListener('change', render);
element('permissions-grant').addEventListener('click', async () => {
  if (element('permissions-grant').disabled || !controller) return;
  busy = true; render();
  element('permissions-summary').textContent = screen.permissionsChecking;
  try {
    const result = await Promise.race([stopPromise,
      controller.commissionPermissions(element('permissions-confirmation').checked)
        .catch(error => { fail(error); throw error; })]);
    ensureCurrent();
    if (result?.permissionsVerified !== true) throw new Error('permissions_unverified');
    element('permissions-summary').textContent = screen.permissionsVerified;
    element('permissions-confirmation').checked = false;
  } catch (error) {
    element('permissions-summary').textContent = screen.permissionsUnverified;
    fail(error);
  } finally { busy = false; render(); }
});

status(supported ? screen.ready : screen.unsupported);
if (window.location.hash) {
  try {
    const options = handoffOptions(window.location.hash);
    busy = true;
    handoff = receiveReleaseHandoff({ options });
    files.closest('label').hidden = rc.closest('label').hidden = verify.hidden = true;
    document.querySelector('[data-screen-message="releaseRcHelp"]').hidden = true;
    document.querySelector('[data-screen-message="releaseAdapter"]').textContent = screen.handoffScope;
    document.querySelector('[data-screen-message="progressHelp"]').textContent = screen.handoffProgressHelp;
    element('bundle-status').textContent = screen.handoffWaiting;
    void handoff.completion.then(({ release: verified, authenticate }) => {
      if (quarantined) return;
      release = verified;
      incomingAuthenticate = authenticate;
      element('bundle-result').textContent = JSON.stringify(release.descriptor, null, 2);
      element('bundle-status').textContent = screen.bundleVerified;
      status(supported ? screen.handoffReady : screen.unsupported);
    }, () => {
      if (!quarantined) quarantine(screen.handoffFailure);
    }).finally(() => { busy = false; render(); });
  } catch { quarantine(screen.handoffFailure); busy = false; }
}
render();
