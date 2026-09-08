import { readShell } from './shell-session.mjs';

export class InstalledObservationError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const fail = () => { throw new InstalledObservationError('installed_response_invalid'); };
const validNonce = value => {
  if (typeof value !== 'string' || value.length !== 32 || !/^[0-9a-f]{32}$/.test(value)) {
    throw new InstalledObservationError('invalid_request');
  }
};
const PACKAGE = 'io.github.maxlyth.hapaneld';

// No elevated access, writes or peer-controlled shell text. The package manager
// selects the path; quoted expansion plus a fixed character allowlist prevent
// path output from becoming shell syntax or a second command argument.
export function buildInstalledObservation(nonce) {
  validNonce(nonce);
  return [`echo HAPANELD_INSTALLED_BEGIN:${nonce}`,
    `hapaneld_package=$(pm path ${PACKAGE})`, 'hapaneld_pm_status=$?',
    'if [ "$hapaneld_pm_status" -ne 0 ] && [ "$hapaneld_pm_status" -ne 1 ]; then echo invalid',
    'elif [ -z "$hapaneld_package" ]; then echo absent',
    'elif [ "$hapaneld_pm_status" -ne 0 ]; then echo invalid',
    'else case "$hapaneld_package" in package:/data/app/*/base.apk)',
    'hapaneld_apk=${hapaneld_package#package:}',
    'case "$hapaneld_apk" in *[!A-Za-z0-9_./=+~-]*) echo invalid ;; *)',
    'echo present', 'echo "$hapaneld_apk"',
    `echo HAPANELD_INSTALLED_MODE_BEGIN:${nonce}`, 'stat -c %f "$hapaneld_apk"',
    `echo HAPANELD_INSTALLED_MODE_END:${nonce}:$?`,
    `echo HAPANELD_INSTALLED_SIZE_BEGIN:${nonce}`, 'wc -c < "$hapaneld_apk"',
    `echo HAPANELD_INSTALLED_SIZE_END:${nonce}:$?`,
    `echo HAPANELD_INSTALLED_SHA_BEGIN:${nonce}`, 'sha256sum "$hapaneld_apk"',
    `echo HAPANELD_INSTALLED_SHA_END:${nonce}:$?`,
    ';; esac ;; *) echo invalid ;; esac; fi',
    `echo HAPANELD_INSTALLED_END:${nonce}`].join('\n');
}

export function parseInstalledObservation(body, nonce, descriptor) {
  validNonce(nonce);
  if (!Number.isSafeInteger(descriptor?.apkSize) || descriptor.apkSize < 1 || descriptor.apkSize > 67108864 ||
      typeof descriptor.apkSha256 !== 'string' || descriptor.apkSha256.length !== 64 ||
      !/^[0-9a-f]{64}$/.test(descriptor.apkSha256)) throw new InstalledObservationError('invalid_request');
  let text = body;
  if (body instanceof Uint8Array) {
    if (body.byteLength > 32768) fail();
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); } catch { fail(); }
  }
  if (typeof text !== 'string' || !text.length || text.length > 32768 ||
      new TextEncoder().encode(text).length > 32768) fail();
  text = text.replaceAll('\r\n', '\n');
  if (!text.endsWith('\n') || /[^\x20-\x7e\t\n]/.test(text)) fail();
  const lines = text.slice(0, -1).split('\n');
  if (lines[0] !== `HAPANELD_INSTALLED_BEGIN:${nonce}` || lines.at(-1) !== `HAPANELD_INSTALLED_END:${nonce}`) fail();
  if (lines.length === 3 && lines[1] === 'absent') return false;
  if (lines.length !== 13 || lines[1] !== 'present' || lines[2].length > 1024 ||
      !/^\/data\/app\/[A-Za-z0-9_./=+~-]+\/base\.apk$/.test(lines[2]) ||
      lines[2].includes('//') || lines[2].split('/').some(part => part === '.' || part === '..')) fail();
  for (const [index, name] of ['MODE', 'SIZE', 'SHA'].entries()) {
    if (lines[index * 3 + 3] !== `HAPANELD_INSTALLED_${name}_BEGIN:${nonce}` ||
        lines[index * 3 + 5] !== `HAPANELD_INSTALLED_${name}_END:${nonce}:0`) fail();
  }
  if (!/^[0-9a-fA-F]{1,8}$/.test(lines[4]) || !/^[ \t]*[0-9]{1,10}[ \t]*$/.test(lines[7])) fail();
  const mode = Number.parseInt(lines[4], 16);
  const hash = /^([0-9a-f]{64})[ \t]+([^ \t]+)$/.exec(lines[10]);
  if ((mode & 0xf000) !== 0x8000 || mode > 0xffff || !hash || hash[2] !== lines[2]) fail();
  return Number(lines[7].trim()) === descriptor.apkSize && hash[1] === descriptor.apkSha256;
}

export async function inspectInstalledApk(adb, descriptor, ensureCurrent = () => {}) {
  ensureCurrent();
  const nonce = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('');
  const body = await readShell(adb, buildInstalledObservation(nonce), { timeoutMs: 30000 });
  ensureCurrent();
  return parseInstalledObservation(body, nonce, descriptor);
}
