import { verifyReleaseBundle } from './release-verifier.mjs';

const MAX_APK_SIZE = 64 * 1024 * 1024;
const blobSize = Object.getOwnPropertyDescriptor(Blob.prototype, 'size').get;
const blobSlice = Blob.prototype.slice;
const blobArrayBuffer = Blob.prototype.arrayBuffer;
const typedArrayPrototype = Object.getPrototypeOf(Uint8Array.prototype);
const byteLength = Object.getOwnPropertyDescriptor(typedArrayPrototype, 'byteLength').get;
const arrayBuffer = Object.getOwnPropertyDescriptor(typedArrayPrototype, 'buffer').get;

export class ApkVerificationError extends Error {
  constructor() { super('APK bytes do not match the authenticated release'); }
}

/** Authenticate metadata first, then snapshot and hash bounded APK bytes.
 * This authenticates exact content against the signed release descriptor, not
 * Android's APK signing block, target compatibility or installation readiness.
 * No network retrieval is performed. A Uint8Array must not use shared memory.
 */
export async function verifyApkBundle(bundle, apk, options = {}) {
  const { descriptor } = await verifyReleaseBundle(bundle, options);
  try {
    let size;
    if (apk instanceof Blob) {
      // Use native methods, not a caller's overridden size/read implementation.
      size = blobSize.call(apk);
    } else if (apk instanceof Uint8Array) {
      if (typeof SharedArrayBuffer !== 'undefined' && arrayBuffer.call(apk) instanceof SharedArrayBuffer) {
        throw new ApkVerificationError();
      }
      size = byteLength.call(apk);
    } else {
      throw new ApkVerificationError();
    }
    if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_APK_SIZE || size !== descriptor.apkSize) {
      throw new ApkVerificationError();
    }
    // Blob snapshots typed-array contents synchronously; native Blob slicing
    // preserves immutable contents without accepting overridden reader methods.
    const snapshot = apk instanceof Blob ? blobSlice.call(apk, 0, size) : new Blob([apk]);
    const bytes = await blobArrayBuffer.call(snapshot);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
    if (hash !== descriptor.apkSha256) throw new ApkVerificationError();
    return Object.freeze({ kind: 'authenticated-apk-bytes', descriptor, apk: Object.freeze(snapshot) });
  } catch {
    throw new ApkVerificationError();
  }
}
