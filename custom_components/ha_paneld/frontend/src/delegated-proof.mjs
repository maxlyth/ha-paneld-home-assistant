/** Fixed read-only delegated-root proof; never elevates an installation command. */
export const SU_PREFIXES = Object.freeze(['su 0', 'su 0 sh -c', 'su root', 'su root sh -c', 'su -c']);
export const MAX_DELEGATED_BYTES = 32 * 1024;
const BASES = ['/data/user/0', '/data/data', '/data/user_de/0'];
const PACKAGE = 'io.github.maxlyth.hapaneld';
const NAMES = ['UID', 'BASE0', 'BASE1', 'BASE2', 'RESIDUE0', 'RESIDUE1', 'RESIDUE2'];
export class DelegatedProofError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code = 'target_response_invalid') { throw new DelegatedProofError(code); }
function checkNonce(nonce) {
  if (typeof nonce !== 'string' || !/^[0-9a-f]{32}$/.test(nonce) || nonce.length !== 32) fail('invalid_request');
}

export function buildDelegatedProof(prefix, nonce) {
  checkNonce(nonce);
  if (!SU_PREFIXES.includes(prefix)) fail('invalid_request');
  const sections = [['UID', 'id -u'],
    ...BASES.map((path, i) => [`BASE${i}`,
      `if [ -d ${path} ] && ls -1A ${path} >/dev/null 2>&1; then echo readable; else echo unreadable; fi`]),
    ...BASES.map((path, i) => [`RESIDUE${i}`,
      `if [ -e ${path}/${PACKAGE} ] || [ -L ${path}/${PACKAGE} ]; then echo present; else echo absent; fi`]),
  ];
  const payload = [`echo HAPANELD_SU_BEGIN:${nonce}`,
    ...sections.flatMap(([name, command]) => [
      `echo HAPANELD_SU_${name}_BEGIN:${nonce}`, command,
      `echo HAPANELD_SU_${name}_END:${nonce}:$?`,
    ]), `echo HAPANELD_SU_END:${nonce}`].join('; ');
  return `echo HAPANELD_DELEGATE_BEGIN:${nonce}; ${prefix} '${payload}'; echo HAPANELD_DELEGATE_END:${nonce}:$?`;
}

function decode(body) {
  let text;
  if (typeof body === 'string') {
    if (!body.length || body.length > MAX_DELEGATED_BYTES ||
        new TextEncoder().encode(body).length > MAX_DELEGATED_BYTES) fail();
    text = body;
  } else if (body instanceof Uint8Array) {
    if (!body.byteLength || body.byteLength > MAX_DELEGATED_BYTES) fail();
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); }
    catch { fail(); }
  } else fail();
  text = text.replaceAll('\r\n', '\n');
  if (!text.endsWith('\n') || /[\p{C}\p{Zl}\p{Zp}]/u.test(text.replaceAll('\n', ''))) fail();
  return text.slice(0, -1).split('\n');
}

/** False permits another fixed dialect, never mutation. True proves only this response. */
export function parseDelegatedProof(body, nonce) {
  checkNonce(nonce);
  const lines = decode(body);
  if (lines[0] !== `HAPANELD_DELEGATE_BEGIN:${nonce}`) fail();
  const end = new RegExp(`^HAPANELD_DELEGATE_END:${nonce}:([0-9]{1,3})$`).exec(lines.at(-1));
  if (!end) fail();
  // Reject duplicate or injected outer frames even when a dialect reports failure.
  if (lines.slice(1, -1).some(line => line.startsWith('HAPANELD_DELEGATE_'))) fail();
  if (Number(end[1]) !== 0) return false;
  if (lines[1] !== `HAPANELD_SU_BEGIN:${nonce}` || lines.at(-2) !== `HAPANELD_SU_END:${nonce}`) fail();
  let offset = 2;
  const sections = {};
  for (const name of NAMES) {
    if (lines[offset++] !== `HAPANELD_SU_${name}_BEGIN:${nonce}`) fail();
    const pattern = new RegExp(`^HAPANELD_SU_${name}_END:${nonce}:([0-9]{1,3})$`);
    const values = [];
    let status;
    while (offset < lines.length - 2) {
      const line = lines[offset++];
      const match = pattern.exec(line);
      if (match) { status = Number(match[1]); break; }
      if (line.startsWith('HAPANELD_')) fail();
      values.push(line);
    }
    if (status === undefined) fail();
    sections[name] = { values, status };
  }
  if (offset !== lines.length - 2) fail();
  const isOne = (section, value) => section.status === 0 && section.values.length === 1 && section.values[0] === value;
  if (!isOne(sections.UID, '0')) fail('root_state_ambiguous');
  for (let i = 0; i < 3; i++) {
    if (!isOne(sections[`BASE${i}`], 'readable')) fail('root_state_ambiguous');
  }
  for (let i = 0; i < 3; i++) {
    const section = sections[`RESIDUE${i}`];
    if (isOne(section, 'present')) fail('target_not_clean');
    if (!isOne(section, 'absent')) fail();
  }
  return true;
}
