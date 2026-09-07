import {parseStagedFile, StagingError} from './staging-contract.mjs';

// Internal read-only recovery evidence. The caller must freshly authenticate
// this release and bind the observation to the same clean live target/job.
// This result alone never authorizes deletion, retry or installation.
export async function verifyStagedPrefix(body, nonce, jobId, release) {
  const fail = () => {throw new StagingError('staged_prefix_mismatch');};
  if (release?.kind !== 'authenticated-apk-bytes' || !(release.apk instanceof Blob)) fail();
  const size = Object.getOwnPropertyDescriptor(Blob.prototype, 'size').get.call(release.apk);
  if (size !== release.descriptor?.apkSize || size < 1 || size > 67108864) fail();
  const observed = parseStagedFile(body, nonce, jobId);
  if (observed.size > size) fail();
  const prefix = Blob.prototype.slice.call(release.apk, 0, observed.size);
  const hash = await crypto.subtle.digest('SHA-256', await Blob.prototype.arrayBuffer.call(prefix));
  const actual = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  if (actual !== observed.sha256) fail();
  return Object.freeze({size: observed.size, sha256: actual, complete: observed.size === size});
}
