import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { openJobStore } from '../src/job-store.mjs';
import { verifyReleaseBundle } from '../src/release-verifier.mjs';

// The smallest IndexedDB the job store needs: one object store, asynchronous
// request callbacks and a completing transaction. It does not roll back.
function fakeIndexedDB() {
  const rows = new Map();
  let created = false;
  const later = callback => setTimeout(callback, 0);
  const db = {
    objectStoreNames: { get length() { return created ? 1 : 0; }, contains: name => created && name === 'jobs' },
    createObjectStore() { created = true; },
    close() {},
    transaction() {
      let pending = 0, aborted = false;
      const tx = { abort() { aborted = true; later(() => tx.onabort?.()); } };
      const request = operation => {
        const req = {};
        pending++;
        later(() => {
          if (aborted) return;
          try { req.result = operation(); } catch (error) {
            req.error = error; pending--; req.onerror?.({ preventDefault() {} }); return;
          }
          pending--;
          req.onsuccess?.();
          later(() => { if (!aborted && pending === 0) tx.oncomplete?.(); });
        });
        return req;
      };
      tx.objectStore = () => ({
        get: key => request(() => rows.get(key)),
        count: key => request(() => (rows.has(key) ? 1 : 0)),
        add: (value, key) => request(() => {
          if (rows.has(key)) throw Object.assign(new Error('exists'), { name: 'ConstraintError' });
          rows.set(key, value);
        }),
        put: (value, key) => request(() => { rows.set(key, value); }),
        delete: key => request(() => { rows.delete(key); }),
      });
      return tx;
    },
  };
  return { rows, open() {
    const req = {};
    later(() => { req.result = db; if (!created) req.onupgradeneeded?.(); req.onsuccess?.(); });
    return req;
  } };
}

const fixture = name => new Uint8Array(readFileSync(new URL(`./fixtures/${name}`, import.meta.url)));
const pem = new TextDecoder().decode(fixture('build-feed-golden.pub.pem'));
const key = await crypto.subtle.importKey('spki',
  Buffer.from(pem.replace(/-----[A-Z ]+-----|\s/g, ''), 'base64'),
  { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
const { descriptor } = await verifyReleaseBundle({ tag: 'build-772', feed: fixture('build-feed-golden.json'),
  feedSignature: new Uint8Array(Buffer.from(new TextDecoder().decode(fixture('build-feed-golden.json.sig.b64')).trim(), 'base64')) }, { expectedRcTag: 'build-772' }, key);
const target = { model: 'Test panel', serial: 'serial', primaryAbi: 'arm64-v8a', androidSdk: 33,
  rootMode: 'rootless', usbVendorId: 1, usbProductId: 2, usbSerial: 'usb' };

test('the job store saves, reloads and advances a job for a feed build', async () => {
  const idb = fakeIndexedDB();
  globalThis.indexedDB = idb;
  try {
    const store = await openJobStore('feed-test');
    const receipt = await store.create(target, descriptor);
    assert.equal(receipt.artifact.releaseTag, 'build-772');
    assert.equal(receipt.artifact.apkName, `${descriptor.apkSha256}.apk`);
    assert.equal(idb.rows.size, 1);
    assert.deepEqual(await store.load(receipt.deviceKey), receipt);
    const staged = await store.advance(receipt.deviceKey, 0, 'staging');
    assert.equal(staged.revision, 1);
    assert.deepEqual(staged.artifact, receipt.artifact);
    store.close();
  } finally { delete globalThis.indexedDB; }
});

test('the job store refuses a feed descriptor whose identity does not match its tag', async () => {
  globalThis.indexedDB = fakeIndexedDB();
  try {
    const store = await openJobStore('feed-test');
    for (const change of [{ versionCode: 771 }, { releaseTag: 'build-771' },
      { apkName: 'ha-paneld-build-772-manual-setup-required.apk' }, { versionName: 'dev build' }]) {
      await assert.rejects(store.create(target, { ...descriptor, ...change }), { code: 'job_malformed' });
    }
  } finally { delete globalThis.indexedDB; }
});
