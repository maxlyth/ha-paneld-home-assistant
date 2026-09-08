/** Read-only identity/root observation; USB session binding belongs to the caller. */
export const MAX_POSTURE_BYTES = 32 * 1024;
export class PostureError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code = 'target_response_invalid') { throw new PostureError(code); }
function fullMatch(pattern, value) {
  return typeof value === 'string' && pattern.exec(value)?.[0] === value;
}
function checkNonce(nonce) {
  if (!fullMatch(/^[0-9a-f]{32}$/, nonce)) fail('invalid_request');
}
const SECTIONS = [
  ['MODEL', 'getprop ro.product.model'], ['SERIAL', 'getprop ro.serialno'],
  ['ABI', 'getprop ro.product.cpu.abi'], ['SDK', 'getprop ro.build.version.sdk'],
  ['UID', 'id -u'], ['SECURE', 'getprop ro.secure'],
  ['DEBUGGABLE', 'getprop ro.debuggable'],
  ['SU', 'if command -v su >/dev/null 2>&1; then echo present; ' +
    'else hapaneld_su_status=$?; if [ "$hapaneld_su_status" -eq 1 ]; then echo absent; ' +
    'else echo abnormal; fi; fi'],
];
export function buildPosture(nonce) {
  checkNonce(nonce);
  return [`echo HAPANELD_POSTURE_BEGIN:${nonce}`,
    ...SECTIONS.flatMap(([name, command]) => [
      `echo HAPANELD_POSTURE_${name}_BEGIN:${nonce}`, command,
      `echo HAPANELD_POSTURE_${name}_END:${nonce}:$?`,
    ]), `echo HAPANELD_POSTURE_END:${nonce}`].join('; ');
}
function decode(body) {
  let text;
  if (typeof body === 'string') {
    if (!body.length || body.length > MAX_POSTURE_BYTES ||
        new TextEncoder().encode(body).length > MAX_POSTURE_BYTES) fail();
    text = body;
  } else if (body instanceof Uint8Array) {
    if (!body.byteLength || body.byteLength > MAX_POSTURE_BYTES) fail();
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); }
    catch { fail(); }
  } else fail();
  text = text.replaceAll('\r\n', '\n');
  if (!text.endsWith('\n') || /[\p{C}\p{Zl}\p{Zp}]/u.test(text.replaceAll('\n', ''))) fail();
  return text.slice(0, -1).split('\n');
}
function sections(body, nonce) {
  const lines = decode(body);
  if (lines[0] !== `HAPANELD_POSTURE_BEGIN:${nonce}` ||
      lines.at(-1) !== `HAPANELD_POSTURE_END:${nonce}`) fail();
  let offset = 1;
  const result = {};
  for (const [name] of SECTIONS) {
    if (lines[offset++] !== `HAPANELD_POSTURE_${name}_BEGIN:${nonce}`) fail();
    const values = [];
    const pattern = new RegExp(`^HAPANELD_POSTURE_${name}_END:${nonce}:([0-9]{1,3})$`);
    let status;
    while (offset < lines.length - 1) {
      const line = lines[offset++];
      const match = pattern.exec(line);
      if (match) { status = Number(match[1]); break; }
      if (line.startsWith('HAPANELD_')) fail();
      values.push(line);
    }
    if (status !== 0 || values.length !== 1) fail();
    result[name] = values[0];
  }
  if (offset !== lines.length - 1) fail();
  return result;
}
function validIdentity({ model, serial, primaryAbi, androidSdk }) {
  return typeof model === 'string' && model === model.trim() &&
    Array.from(model).length >= 1 && Array.from(model).length <= 128 &&
    !/[\p{C}\p{Zl}\p{Zp}]|[^\S ]/u.test(model) &&
    fullMatch(/^[A-Za-z0-9._:-]{1,128}$/, serial) &&
    fullMatch(/^[A-Za-z0-9_.-]{1,64}$/, primaryAbi) &&
    Number.isSafeInteger(androidSdk) && androidSdk >= 1 && androidSdk <= 100;
}

/** Root SU here means presence only: this command never executes su or proves UID0. */
export function parsePosture(body, nonce, expectedTarget) {
  checkNonce(nonce);
  if (!expectedTarget || !validIdentity(expectedTarget) ||
      !['rootless', 'root_adbd', 'root_su'].includes(expectedTarget.rootMode)) fail('invalid_request');
  const observed = parseObservedPosture(body, nonce);
  for (const key of Object.keys(observed)) {
    if (observed[key] !== expectedTarget[key]) fail('target_changed');
  }
  return observed;
}

// Discovery only: not clean-install admission or proof that delegated root works.
export function parseObservedPosture(body, nonce) {
  checkNonce(nonce);
  const s = sections(body, nonce);
  const identity = { model: s.MODEL, serial: s.SERIAL, primaryAbi: s.ABI, androidSdk: Number(s.SDK) };
  if (!fullMatch(/^[0-9]{1,3}$/, s.SDK) || !validIdentity(identity)) fail();
  if (!['0', '1'].includes(s.SECURE) || !['0', '1'].includes(s.DEBUGGABLE) ||
      !['absent', 'present'].includes(s.SU)) fail();
  let rootMode;
  if (s.UID === '0') rootMode = 'root_adbd';
  else if (s.UID === '2000' && s.SU === 'present') rootMode = 'root_su';
  else if (s.UID === '2000' && s.SECURE === '1' && s.DEBUGGABLE === '0' && s.SU === 'absent') rootMode = 'rootless';
  else fail('root_state_ambiguous');
  return Object.freeze({ ...identity, rootMode });
}
