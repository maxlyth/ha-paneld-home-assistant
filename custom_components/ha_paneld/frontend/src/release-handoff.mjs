import { verifyApkBundle } from './apk-verifier.mjs';

const fail = () => { throw new Error('handoff_invalid'); };
const keys = (value, expected) => value && typeof value === 'object' && !Array.isArray(value) &&
  Object.keys(value).sort().join(',') === [...expected].sort().join(',');

export function handoffOptions(hash) {
  if (!hash) return null;
  const query = new URLSearchParams(hash.slice(1));
  if ([...query.keys()].sort().join(',') !== 'ha_origin,nonce,rc') fail();
  const origin = query.get('ha_origin'), nonce = query.get('nonce'), rc = query.get('rc');
  const url = new URL(origin);
  if (!['http:', 'https:'].includes(url.protocol) || url.origin !== origin ||
      !/^[0-9a-f]{32}$/.test(nonce) || nonce.length !== 32 ||
      (rc !== '' && (!/^v[0-9]+\.[0-9]+\.[0-9]+-rc[1-9][0-9]*$/.test(rc) || rc.length > 64))) fail();
  return Object.freeze({ origin, nonce, expectedRcTag: rc || null });
}

function snapshot(message) {
  if (!keys(message, ['type', 'nonce', 'bundle', 'apk']) ||
      !keys(message.bundle, ['tag', 'checksum', 'checksumSignature', 'descriptor', 'descriptorSignature'])) fail();
  const bundle = { tag: message.bundle.tag };
  if (typeof bundle.tag !== 'string' || bundle.tag.length > 64) fail();
  for (const [key, maximum] of Object.entries({ checksum: 512, checksumSignature: 256,
    descriptor: 4096, descriptorSignature: 256 })) {
    const value = message.bundle[key];
    if (!(value instanceof Uint8Array) || value.byteLength < 1 || value.byteLength > maximum ||
        (typeof SharedArrayBuffer !== 'undefined' && value.buffer instanceof SharedArrayBuffer)) fail();
    bundle[key] = value.slice();
  }
  if (!(message.apk instanceof Blob)) fail();
  const size = Object.getOwnPropertyDescriptor(Blob.prototype, 'size').get.call(message.apk);
  if (size < 1 || size > 64 * 1024 * 1024) fail();
  const apk = Blob.prototype.slice.call(message.apk, 0, size);
  return { bundle, apk };
}

// The opener is a delivery source, not a release authority. Authenticate again
// locally before enabling connection, then again at transaction boundaries.
export function receiveReleaseHandoff({ windowObject = window,
  options = handoffOptions(windowObject.location.hash), timeoutMs = 300000 } = {}) {
  if (!options || !windowObject.opener || !windowObject.isSecureContext ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 300000) fail();
  const source = windowObject.opener;
  let stopped = false, received = false, timer, rejectCompletion;
  const cleanup = () => { clearTimeout(timer); windowObject.removeEventListener('message', receive); };
  const reply = type => source.postMessage({ type, nonce: options.nonce }, options.origin);
  const cancel = () => {
    stopped = true;
    cleanup();
    rejectCompletion(new Error('handoff_cancelled'));
  };
  let resolveCompletion;
  const completion = new Promise((resolve, reject) => {
    resolveCompletion = resolve; rejectCompletion = reject;
  });
  void completion.catch(() => {});
  async function receive(event) {
    if (stopped || received || event.source !== source || event.origin !== options.origin ||
        event.data?.type !== 'ha-paneld/usb-bundle' || event.data?.nonce !== options.nonce) return;
    received = true;
    try {
      const selected = snapshot(event.data);
      const authenticate = async () => {
        if (stopped) fail();
        const release = await verifyApkBundle(selected.bundle, selected.apk,
          { expectedRcTag: options.expectedRcTag });
        if (stopped) fail();
        return release;
      };
      const release = await authenticate();
      if (stopped) return;
      cleanup();
      reply('ha-paneld/usb-verified');
      resolveCompletion(Object.freeze({ release, authenticate }));
    } catch {
      if (stopped) return;
      stopped = true;
      cleanup();
      try { reply('ha-paneld/usb-error'); } catch { /* No credentials or diagnostics are sent. */ }
      rejectCompletion(new Error('handoff_invalid'));
    }
  }
  windowObject.addEventListener('message', receive);
  timer = setTimeout(cancel, timeoutMs);
  try { reply('ha-paneld/usb-ready'); } catch { cancel(); }
  return Object.freeze({ completion, cancel });
}
