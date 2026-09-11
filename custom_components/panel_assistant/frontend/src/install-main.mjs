import { Adb, AdbDaemonTransport } from '@yume-chan/adb';
import AdbWebCredentialStore from '@yume-chan/adb-credential-web';
import { AdbDaemonWebUsbDeviceManager } from '@yume-chan/adb-daemon-webusb';
import { boundedUsb } from './usb-bounds.mjs';
import { verifySelectedBundle } from './selected-bundle.mjs';
import { inspectSessionTarget } from './session-target.mjs';
import { openJobStore } from './job-store.mjs';
import { createUsbTransactionPorts } from './usb-transaction-ports.mjs';
import { createInstallController } from './install-controller.mjs';
import { INSTALL_MESSAGES, installProgress, errorView } from './install-view.mjs';
import { INSTALL_SCREEN_MESSAGES as screen } from './install-screen-messages.mjs';
import { handoffOptions, receiveReleaseHandoff } from './release-handoff.mjs';
import { readSetupUrl } from './panel-address.mjs';

// A guided wizard for people who have never used a terminal. One step is on
// screen at a time; the single Install press is the consent for everything
// that follows, including granting the app its permissions. Every safety check
// below still runs, and technical detail only reaches "Details for support".

const element = id => document.getElementById(id);
const files = element('bundle-files');
const rc = element('bundle-rc');
const verify = element('verify-bundle');
const connect = element('connect');
const install = element('install');
for (const node of document.querySelectorAll('[data-screen-message]')) {
  node.textContent = screen[node.dataset.screenMessage];
}
for (const node of document.querySelectorAll('[data-screen-placeholder]')) {
  node.placeholder = screen[node.dataset.screenPlaceholder];
}

const STEPS = ['preparing', 'connect', 'allow', 'confirm', 'progress', 'done', 'error'];
const manager = window.isSecureContext && navigator.usb
  ? new AdbDaemonWebUsbDeviceManager(boundedUsb(navigator.usb)) : undefined;
const supported = Boolean(manager && navigator.locks && globalThis.indexedDB);
let pinned, release, raw, adb, sessionAdb, store, controller;
let handoff, incomingAuthenticate;
let busy = false, quarantined = false;
let receipt = null;
let deadline;
let rejectStop;
const stopPromise = new Promise((_, reject) => { rejectStop = reject; });
void stopPromise.catch(() => {});
const newNonce = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
  byte => byte.toString(16).padStart(2, '0')).join('');

function show(step) {
  for (const name of STEPS) element(`step-${name}`).hidden = name !== step;
}

// Support detail is collected, never shown unless the person opens it.
const supportLines = [];
function support(label, value) {
  supportLines.push(`${label}\n${typeof value === 'string' ? value : JSON.stringify(value, null, 2)}`);
  element('support-log').textContent = supportLines.join('\n\n');
}

function progress(stepKey, percent) {
  element('activity').textContent = screen[stepKey];
  element('progress-fill').style.width = `${percent}%`;
  element('step-progress').querySelector('.bar').setAttribute('aria-valuenow', String(percent));
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
  try { store?.close(); } catch { /* The connection is already closed. */ }
}

// Any safety fault ends this connection. The saved job survives in this
// browser, so pressing Try again reconnects and carries on from where it stopped.
function quarantine(message = INSTALL_MESSAGES.installErrorConnection) {
  if (!quarantined) {
    quarantined = true;
    clearTimeout(deadline);
    handoff?.cancel();
    rejectStop(new Error('session_closed'));
    element('error-text').textContent = message;
    show('error');
  }
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
  support('Error', String(error?.code ?? error?.message ?? error));
  quarantine(INSTALL_MESSAGES[errorView(error?.code)]);
}

function releaseReady(verified) {
  release = verified;
  support('Release', release.descriptor);
  show(supported ? 'connect' : 'error');
  if (!supported) element('error-text').textContent = screen.unsupported;
}

function selectionChanged() {
  if (handoff || busy) { quarantine(INSTALL_MESSAGES.installErrorArtifact); return; }
  pinned = release = undefined;
}
files.addEventListener('change', selectionChanged);
rc.addEventListener('input', selectionChanged);
// The release arrives from the Home Assistant tab, so starting again means going
// back there; the saved job then resumes from where it stopped.
element('retry').addEventListener('click', () => {
  if (handoff && window.opener) { window.close(); return; }
  window.location.reload();
});
let leaving = false;
window.addEventListener('pagehide', () => { if (!leaving) quarantine(screen.pageClosed); });

verify.addEventListener('click', async () => {
  if (busy) return;
  busy = true;
  release = undefined;
  pinned = Object.freeze({ files: Object.freeze(Array.from(files.files)), tag: rc.value });
  show('preparing');
  try {
    const verified = await Promise.race([stopPromise,
      verifySelectedBundle(pinned.files, { expectedRcTag: pinned.tag || null })]);
    ensureCurrent();
    releaseReady(verified);
  } catch (error) { fail(error); } finally { busy = false; }
});

connect.addEventListener('click', async () => {
  if (busy || !release || quarantined) return;
  busy = true;
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
      show('allow');
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
      sessionAdb = guardedAdb(adb);
      element('allow-text').textContent = screen.checkingPanel;
      store = await openJobStore();
      if (quarantined) { closeResources(); return; }
      ensureCurrent();
      const target = await inspectSessionTarget(sessionAdb, release.descriptor, raw, ensureCurrent);
      ensureCurrent();
      support('Panel', target);
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
        onReceipt(value) {
          receipt = value;
          const { stepKey, percent } = installProgress(receipt);
          progress(stepKey, percent);
        },
      });
      const preview = await controller.preview(target);
      ensureCurrent();
      receipt = preview.receipt;
      support('Saved progress', receipt ?? 'none');
      // A job already under way resumes without asking again: the person
      // agreed to install when they started it.
      if (receipt) await installAll(); else show('confirm');
    })().catch(error => { fail(error); throw error; })]);
  } catch (error) { fail(error); }
  finally { clearTimeout(deadline); busy = false; }
});

install.addEventListener('click', async () => {
  if (busy || !controller || quarantined) return;
  busy = true;
  try { await installAll(); } catch (error) { fail(error); } finally { busy = false; }
});

// Run the saved job to a healthy app, then permissions, then hand over to the
// panel's own setup. Each advance goes through the same locked, durable
// transaction a click used to; the loop only removes the clicks. The bound
// stops a job that can never settle from spinning forever.
async function installAll() {
  clearTimeout(deadline);
  show('progress');
  const { stepKey, percent } = installProgress(receipt);
  progress(stepKey, percent);
  for (let round = 0; round < 12 && receipt?.phase !== 'healthy'; round++) {
    receipt = await Promise.race([stopPromise,
      ['recovery_required', 'cleanup_pending'].includes(receipt?.phase)
        ? controller.recover(true) : controller.run(true)]);
    ensureCurrent();
    support('Saved progress', receipt);
  }
  if (receipt?.phase !== 'healthy') throw Object.assign(new Error('install_incomplete'), { code: 'health_unavailable' });

  progress('stepPermissions', 92);
  const granted = await Promise.race([stopPromise, controller.commissionPermissions(true)]);
  ensureCurrent();
  support('Permissions', granted);
  if (granted?.permissionsVerified !== true) {
    throw Object.assign(new Error('permissions_unverified'), { code: 'health_unavailable' });
  }

  progress('stepOpening', 100);
  const url = await readSetupUrl(sessionAdb, newNonce);
  support('Setup address', url ?? 'not found; setup continues on the panel');
  finish(url);
}

function finish(url) {
  show('done');
  const link = element('open-setup');
  if (!url) {
    element('done-text').textContent = screen.doneManual;
    return;
  }
  element('done-text').textContent = screen.doneOpening;
  link.href = url;
  link.hidden = false;
  // Same tab, so the panel's own wizard simply takes over. The visible button
  // stays in case this computer cannot reach the panel's network.
  setTimeout(() => {
    // Leaving on success is not a failure: release USB quietly, then go.
    leaving = true;
    closeResources();
    window.location.assign(url);
  }, 1200);
}

if (!supported) {
  element('error-text').textContent = screen.unsupported;
  show('error');
  element('retry').hidden = true;
} else if (window.location.hash) {
  try {
    const options = handoffOptions(window.location.hash);
    busy = true;
    const slow = setTimeout(() => { element('preparing-text').textContent = screen.preparingSlow; }, 15000);
    handoff = receiveReleaseHandoff({ options });
    void handoff.completion.then(({ release: verified, authenticate }) => {
      if (quarantined) return;
      incomingAuthenticate = authenticate;
      releaseReady(verified);
    }, () => {
      if (!quarantined) quarantine(screen.handoffFailure);
    }).finally(() => { clearTimeout(slow); busy = false; });
  } catch { quarantine(screen.handoffFailure); busy = false; }
  element('retry').textContent = screen.backToHa;
} else {
  // No Home Assistant handover: only the advanced file route is available.
  show('connect');
  connect.disabled = true;
  element('advanced').hidden = false;
  element('advanced').open = true;
  verify.addEventListener('click', () => { connect.disabled = false; }, { once: true });
}
