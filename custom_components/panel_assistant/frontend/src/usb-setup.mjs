import {parseSetupObservation} from './setup-observation.mjs';
const fail = () => { throw new Error('setup_response_invalid'); };
const encode = new TextEncoder();
const ascii = bytes => {
  if (bytes.some(value => value > 126 || (value < 32 && ![9, 10, 13].includes(value)))) fail();
  return new TextDecoder().decode(bytes);
};
const index = (bytes, pattern, start = 0) => {
  for (let i = start; i <= bytes.length - pattern.length; i++) {
    if (pattern.every((value, j) => bytes[i + j] === value)) return i;
  }
  return -1;
};

export function parseSetupHttp(wire) {
  if (!(wire instanceof Uint8Array) || wire.length > 65536) fail();
  const boundary = index(wire, [13, 10, 13, 10]);
  if (boundary < 0 || boundary > 8192) fail();
  const lines = ascii(wire.subarray(0, boundary)).split('\r\n');
  if (!/^HTTP\/1\.[01] 200(?: [\x20-\x7e]*)?$/.test(lines.shift()) || lines.length > 64) fail();
  const headers = new Map();
  for (const line of lines) {
    const match = /^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*([\x20-\x7e\t]*)$/.exec(line);
    if (!match || line.length > 2048 || headers.has(match[1].toLowerCase())) fail();
    headers.set(match[1].toLowerCase(), match[2].trim());
  }
  if (headers.has('content-encoding') && headers.get('content-encoding').toLowerCase() !== 'identity') fail();
  if (headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') fail();
  let body = wire.subarray(boundary + 4);
  const length = headers.get('content-length'), transfer = headers.get('transfer-encoding');
  if (transfer !== undefined) {
    if (length !== undefined || transfer.toLowerCase() !== 'chunked') fail();
    const decoded = new Uint8Array(32768);
    let at = 0, size = 0;
    for (;;) {
      const end = index(body, [13, 10], at);
      if (end < at + 1 || end - at > 8) fail();
      const token = ascii(body.subarray(at, end));
      if (!/^[a-fA-F0-9]+$/.test(token)) fail();
      const count = Number.parseInt(token, 16);
      at = end + 2;
      if (!count) {
        if (at + 2 !== body.length || body[at] !== 13 || body[at + 1] !== 10) fail();
        break;
      }
      if (size + count > 32768 || at + count + 2 > body.length ||
          body[at + count] !== 13 || body[at + count + 1] !== 10) fail();
      decoded.set(body.subarray(at, at + count), size);
      size += count; at += count + 2;
    }
    body = decoded.subarray(0, size);
  } else if (length !== undefined && (!/^(0|[1-9][0-9]{0,4})$/.test(length) ||
      Number(length) !== body.length)) fail();
  return parseSetupObservation(body);
}

export async function readUsbSetup(adb, {ensureCurrent = () => {}, quarantine,
  timeoutMs = 5000, closeMs = 2000} = {}) {
  if (typeof quarantine !== 'function' || typeof ensureCurrent !== 'function' ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 5000 ||
      !Number.isSafeInteger(closeMs) || closeMs < 1 || closeMs > 2000) fail();
  let socket, writer, reader, stopped = false, timer;
  const guard = () => { if (stopped) fail(); ensureCurrent(); };
  const operation = (async () => {
    guard(); socket = await adb.createSocket('tcp:8888');
    if (stopped) { void Promise.resolve().then(() => socket.close()).catch(() => {}); fail(); }
    guard(); writer = socket.writable.getWriter();
    await writer.write(encode.encode('GET /api/v1/setup HTTP/1.1\r\nHost: 127.0.0.1:8888\r\nConnection: close\r\n\r\n'));
    guard(); reader = socket.readable.getReader();
    const bytes = new Uint8Array(65536); let size = 0;
    for (;;) {
      const next = await reader.read(); guard();
      if (next.done) break;
      if (!(next.value instanceof Uint8Array) || size + next.value.length > bytes.length) fail();
      bytes.set(next.value, size); size += next.value.length;
    }
    return parseSetupHttp(bytes.subarray(0, size));
  })();
  let result;
  try {
    result = await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('setup_unavailable')), timeoutMs);
    })]);
  } catch (error) {
    stopped = true; quarantine();
    throw new Error(error?.message === 'setup_response_invalid' ? 'setup_response_invalid' : 'setup_unavailable');
  } finally {
    stopped = true; clearTimeout(timer);
    if (socket) {
      let deadline;
      try {
        await Promise.race([Promise.resolve().then(() => socket.close()), new Promise((_, reject) => {
          deadline = setTimeout(() => reject(new Error('setup_unavailable')), closeMs);
        })]);
      } catch { quarantine(); throw new Error('setup_unavailable'); }
      finally {
        clearTimeout(deadline);
        try { reader?.releaseLock(); } catch { /* Owning connection is quarantined. */ }
        try { writer?.releaseLock(); } catch { /* Owning connection is quarantined. */ }
      }
    }
  }
  try { ensureCurrent(); } catch { quarantine(); throw new Error('setup_unavailable'); }
  return result;
}
