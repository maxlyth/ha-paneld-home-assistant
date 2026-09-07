import { parseUnauthenticatedDescriptor } from './release-verifier.mjs';

// Receipts are local progress records, never authenticated release or target proof.
const STORE = 'jobs';
const MAX_BYTES = 8192;
const FIELDS = ['schema', 'id', 'deviceKey', 'revision', 'phase', 'target', 'artifact'];
const TARGET_FIELDS = ['model', 'serial', 'primaryAbi', 'androidSdk', 'rootMode',
  'usbVendorId', 'usbProductId', 'usbSerial'];
const EDGES = Object.freeze({
  prepared: ['staging', 'recovery_required'],
  staging: ['staged', 'recovery_required'],
  staged: ['installing', 'recovery_required'],
  installing: ['installed', 'recovery_required'],
  installed: ['launching', 'recovery_required'],
  launching: ['healthy', 'recovery_required'],
  healthy: [], recovery_required: ['cleanup_pending', 'prepared'],
  cleanup_pending: ['prepared', 'recovery_required'],
});
const encoder = new TextEncoder();
class JobStoreError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code = 'job_malformed') { throw new JobStoreError(code); }
function keys(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(value)) ||
      Reflect.ownKeys(value).length !== fields.length ||
      !fields.every(key => Object.hasOwn(value, key) &&
        Object.getOwnPropertyDescriptor(value, key)?.get === undefined &&
        Object.getOwnPropertyDescriptor(value, key)?.set === undefined)) fail();
}
function match(pattern, value) {
  return typeof value === 'string' && pattern.exec(value)?.[0] === value;
}
function integer(value, min, max) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}
function printable(value, minimum) {
  return typeof value === 'string' && [...value].length >= minimum &&
    [...value].length <= 128 && !/[\p{C}\p{Zl}\p{Zp}]/u.test(value);
}
function targetSnapshot(target) {
  keys(target, TARGET_FIELDS);
  if (!printable(target.model, 1) || target.model !== target.model.trim() ||
      !match(/^[A-Za-z0-9._:-]{1,128}$/, target.serial) ||
      !['arm64-v8a', 'armeabi-v7a'].includes(target.primaryAbi) ||
      !integer(target.androidSdk, 1, 100) ||
      !['rootless', 'root_adbd', 'root_su'].includes(target.rootMode) ||
      !integer(target.usbVendorId, 0, 65535) ||
      !integer(target.usbProductId, 0, 65535) || !printable(target.usbSerial, 0)) fail();
  return Object.freeze(Object.fromEntries(TARGET_FIELDS.map(key => [key, target[key]])));
}
function artifactSnapshot(artifact, target) {
  try {
    // Preserve the parser's closed schema: do not drop unknown descriptor fields.
    keys(artifact, Object.keys(artifact));
    const text = JSON.stringify(Object.fromEntries(Object.keys(artifact).sort()
      .map(key => [key, artifact[key]]))) + '\n';
    const descriptor = parseUnauthenticatedDescriptor(encoder.encode(text), {
      tag: artifact.releaseTag, apkSha256: artifact.apkSha256,
    });
    if (descriptor.minSdk > target.androidSdk ||
        !descriptor.supportedAbis.includes(target.primaryAbi)) fail();
    return descriptor;
  } catch { fail(); }
}
function validate(value) {
  keys(value, FIELDS);
  if (value.schema !== 1 || !match(/^[0-9a-f]{32}$/, value.id) ||
      !match(/^[0-9a-f]{64}$/, value.deviceKey) ||
      !integer(value.revision, 0, Number.MAX_SAFE_INTEGER) ||
      typeof value.phase !== 'string' || !Object.hasOwn(EDGES, value.phase)) fail();
  const target = targetSnapshot(value.target);
  const artifact = artifactSnapshot(value.artifact, target);
  const receipt = Object.freeze({ schema: 1, id: value.id, deviceKey: value.deviceKey,
    revision: value.revision, phase: value.phase, target, artifact });
  if (encoder.encode(JSON.stringify(receipt)).length > MAX_BYTES) fail();
  return receipt;
}
function decode(raw, deviceKey) {
  if (typeof raw !== 'string' || raw.length > MAX_BYTES ||
      encoder.encode(raw).length > MAX_BYTES) fail();
  try {
    const receipt = validate(JSON.parse(raw));
    // Also reject duplicate properties and noncanonical stored representations.
    if (receipt.deviceKey !== deviceKey || JSON.stringify(receipt) !== raw) fail();
    return receipt;
  } catch { fail(); }
}
async function keyFor(target) {
  const canonical = JSON.stringify([target.usbVendorId, target.usbProductId,
    target.usbSerial, target.serial]);
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(canonical)))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
}
// Lookup a receipt before deciding between clean preflight and installed-job
// reconciliation. Only USB metadata plus Android serial form this stable key.
export async function deviceKeyForIdentity(identity) {
  if (!identity || !match(/^[A-Za-z0-9._:-]{1,128}$/, identity.serial) ||
      !integer(identity.usbVendorId, 0, 65535) || !integer(identity.usbProductId, 0, 65535) ||
      !printable(identity.usbSerial, 0)) fail();
  return keyFor(identity);
}
function keyValid(deviceKey) {
  if (!match(/^[0-9a-f]{64}$/, deviceKey)) fail();
}

/** IndexedDB completion is required before success; browser eviction is still possible. */
export async function openJobStore(name = 'ha-paneld-usb-jobs-v1') {
  let db;
  try {
    db = await new Promise((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      let abandoned = false;
      request.onupgradeneeded = () => request.result.createObjectStore(STORE);
      request.onsuccess = () => {
        if (abandoned) request.result.close(); else resolve(request.result);
      };
      request.onerror = () => reject(new JobStoreError('job_storage_failed'));
      request.onblocked = () => {
        abandoned = true;
        reject(new JobStoreError('job_storage_failed'));
      };
    });
    if (db.objectStoreNames.length !== 1 || !db.objectStoreNames.contains(STORE)) {
      db.close(); fail('job_storage_failed');
    }
  } catch { fail('job_storage_failed'); }
  db.onversionchange = () => db.close();

  function transaction(mode, operation) {
    return new Promise((resolve, reject) => {
      let tx;
      let result;
      let failure;
      const abort = error => {
        failure = error instanceof JobStoreError ? error : new JobStoreError('job_storage_failed');
        try { tx.abort(); } catch { reject(failure); }
      };
      try {
        tx = db.transaction(STORE, mode, { durability: 'strict' });
        tx.oncomplete = () => resolve(result);
        tx.onabort = () => reject(failure ?? new JobStoreError('job_storage_failed'));
        tx.onerror = () => { /* onabort owns settlement after rollback. */ };
        operation(tx.objectStore(STORE), value => { result = value; }, abort);
      } catch (error) {
        if (tx) abort(error); else reject(new JobStoreError('job_storage_failed'));
      }
    });
  }
  async function load(deviceKey) {
    keyValid(deviceKey);
    const receipt = await transaction('readonly', (store, done, abort) => {
      const request = store.get(deviceKey);
      request.onsuccess = () => {
        try {
          if (request.result !== undefined) done(decode(request.result, deviceKey));
          else {
            // IDB permits an explicit undefined value; that is corruption, not absence.
            const count = store.count(deviceKey);
            count.onsuccess = () => count.result === 0 ? done(null) : abort(new JobStoreError('job_malformed'));
          }
        }
        catch (error) { abort(error); }
      };
    });
    if (receipt && await keyFor(receipt.target) !== deviceKey) fail();
    return receipt;
  }
  return Object.freeze({
    async create(target, artifact) {
      const snapshot = targetSnapshot(target);
      const descriptor = artifactSnapshot(artifact, snapshot);
      const id = [...crypto.getRandomValues(new Uint8Array(16))]
        .map(byte => byte.toString(16).padStart(2, '0')).join('');
      const receipt = validate({ schema: 1, id, deviceKey: await keyFor(snapshot),
        revision: 0, phase: 'prepared', target: snapshot, artifact: descriptor });
      return transaction('readwrite', (store, done, abort) => {
        const request = store.add(JSON.stringify(receipt), receipt.deviceKey);
        request.onerror = event => {
          event.preventDefault();
          abort(new JobStoreError(request.error?.name === 'ConstraintError' ?
            'job_conflict' : 'job_storage_failed'));
        };
        request.onsuccess = () => done(receipt);
      });
    },
    load,
    async advance(deviceKey, expectedRevision, nextPhase) {
      keyValid(deviceKey);
      if (!integer(expectedRevision, 0, Number.MAX_SAFE_INTEGER)) fail();
      const snapshot = await load(deviceKey);
      if (!snapshot || snapshot.revision !== expectedRevision) fail('job_conflict');
      return transaction('readwrite', (store, done, abort) => {
        const request = store.get(deviceKey);
        request.onsuccess = () => {
          try {
            if (request.result === undefined) fail('job_conflict');
            const current = decode(request.result, deviceKey);
            if (current.revision !== expectedRevision ||
                JSON.stringify(current) !== JSON.stringify(snapshot)) fail('job_conflict');
            if (!EDGES[current.phase].includes(nextPhase)) fail('job_transition_invalid');
            const updated = validate({ ...current, revision: current.revision + 1, phase: nextPhase });
            store.put(JSON.stringify(updated), deviceKey);
            done(updated);
          } catch (error) { abort(error); }
        };
      });
    },
    close() { db.close(); },
  });
}
