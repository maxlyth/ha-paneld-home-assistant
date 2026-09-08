import { stagingPath, StagingError } from './staging-contract.mjs';

const encoder = new TextEncoder();
const fail = code => { throw new StagingError(code); };
function frame(id, value) {
  const body = value instanceof Uint8Array ? value : null;
  const output = new Uint8Array(8 + (body?.byteLength ?? 0));
  output.set(encoder.encode(id));
  new DataView(output.buffer).setUint32(4, body ? body.byteLength : value, true);
  if (body) output.set(body, 8);
  return output;
}

// Internal actuator: caller owns transaction intent, fresh target/clean/path
// admission and quarantine. No path supplied by the panel or arbitrary caller.
export async function uploadApk(adb, jobId, release, {
  ensureCurrent = () => {}, quarantine, timeoutMs = 180000, closeMs = 2000,
} = {}) {
  const path = stagingPath(jobId);
  if (typeof quarantine !== 'function' || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 180000 ||
      !Number.isSafeInteger(closeMs) || closeMs < 1 || closeMs > 2000) fail('invalid_request');
  let blob, size;
  try {
    const nativeSize = Object.getOwnPropertyDescriptor(Blob.prototype, 'size').get;
    size = nativeSize.call(release?.apk);
    if (release.kind !== 'authenticated-apk-bytes' || size !== release.descriptor.apkSize ||
        size < 1 || size > 67108864) fail('invalid_request');
    blob = Blob.prototype.slice.call(release.apk, 0, size);
  } catch { fail('invalid_request'); }
  let socket, writer, reader, stopped = false, timer;
  const guard = () => { if (stopped) fail('upload_stopped'); ensureCurrent(); };
  const operation = (async () => {
    guard();
    socket = await adb.createSocket('sync:');
    if (stopped) {
      void Promise.resolve().then(() => socket.close()).catch(() => {});
      fail('upload_stopped');
    }
    guard();
    writer = socket.writable.getWriter();
    reader = socket.readable.getReader();
    const send = async () => {
      guard();
      await writer.write(frame('SEND', encoder.encode(`${path},33188`)));
      for (let offset = 0; offset < size; offset += 65536) {
        guard();
        const chunk = new Uint8Array(await Blob.prototype.arrayBuffer.call(
          Blob.prototype.slice.call(blob, offset, Math.min(size, offset + 65536))));
        guard();
        await writer.write(frame('DATA', chunk));
      }
      guard();
      await writer.write(frame('DONE', 0));
    };
    const receive = async () => {
      // Only the eight-byte success/failure header is needed. Failure text is
      // never allocated or exposed, regardless of its declared length.
      const header = new Uint8Array(8);
      let used = 0;
      while (used < 8) {
        guard();
        const result = await reader.read();
        guard();
        if (result.done || !(result.value instanceof Uint8Array) || !result.value.byteLength ||
            result.value.byteLength > 1048576) fail('upload_response_invalid');
        const take = Math.min(8 - used, result.value.byteLength);
        header.set(result.value.subarray(0, take), used);
        used += take;
        if (used === 8) {
          if (String.fromCharCode(...header.subarray(0, 4)) === 'FAIL') fail('upload_refused');
          if (result.value.byteLength !== take) fail('upload_response_invalid');
        }
      }
      if (String.fromCharCode(...header.subarray(0, 4)) !== 'OKAY' ||
          new DataView(header.buffer).getUint32(4, true) !== 0) fail('upload_response_invalid');
    };
    // An early OKAY cannot claim completion while local writes are pending.
    await Promise.all([send(), receive()]);
    guard();
  })();
  try {
    await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new StagingError('upload_timeout')), timeoutMs);
    })]);
  } catch (error) {
    stopped = true;
    quarantine();
    if (error instanceof StagingError) throw error;
    fail('upload_failed');
  } finally {
    stopped = true;
    clearTimeout(timer);
    if (socket) {
      let closeTimer;
      try {
        await Promise.race([Promise.resolve().then(() => socket.close()), new Promise((_, reject) => {
          closeTimer = setTimeout(() => reject(new StagingError('upload_cleanup_failed')), closeMs);
        })]);
      } catch {
        quarantine();
        fail('upload_cleanup_failed');
      } finally {
        clearTimeout(closeTimer);
        // Pending stream reads/writes belong to the quarantined connection.
        // Avoid awaiting stream cancellation indefinitely here.
        try { reader?.releaseLock(); } catch { /* Pending read settles on close. */ }
        try { writer?.releaseLock(); } catch { /* Pending write settles on close. */ }
      }
    }
  }
}
