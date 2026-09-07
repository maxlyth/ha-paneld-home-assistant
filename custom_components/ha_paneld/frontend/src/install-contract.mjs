/** Fixed package-manager/activity-manager commands and bounded response contracts. */
export const MAX_INSTALL_RESPONSE_BYTES = 32 * 1024;
const PACKAGE = 'io.github.maxlyth.hapaneld';

export class InstallContractError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code = 'target_response_invalid') { throw new InstallContractError(code); }
function checkId(value) {
  if (typeof value !== 'string' || value.length !== 32 || !/^[0-9a-f]{32}$/.test(value)) fail('invalid_request');
}

/** Only the job-owned staging pathname is representable; never accepts shell text. */
export function buildInstall(nonce, jobId, sdk) {
  checkId(nonce);
  checkId(jobId);
  if (!Number.isInteger(sdk) || sdk < 1 || sdk > 100) fail('invalid_request');
  const path = `/data/local/tmp/ha-paneld-install-${jobId}.apk`;
  return `echo HAPANELD_INSTALL_BEGIN:${nonce}; pm install ${sdk >= 28 ? '-R ' : ''}${path}; echo HAPANELD_INSTALL_END:${nonce}:$?`;
}

export function buildLaunch(nonce) {
  checkId(nonce);
  return `echo HAPANELD_LAUNCH_BEGIN:${nonce}; am start -W -n ${PACKAGE}/.MainActivity -p ${PACKAGE}; echo HAPANELD_LAUNCH_END:${nonce}:$?`;
}

function decode(body) {
  let text;
  if (typeof body === 'string') {
    if (!body.length || body.length > MAX_INSTALL_RESPONSE_BYTES ||
        new TextEncoder().encode(body).length > MAX_INSTALL_RESPONSE_BYTES ||
        /[\uD800-\uDFFF]/u.test(body)) fail();
    text = body;
  } else if (body instanceof Uint8Array) {
    if (!body.byteLength || body.byteLength > MAX_INSTALL_RESPONSE_BYTES) fail();
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); }
    catch { fail(); }
  } else fail();
  text = text.replaceAll('\r\n', '\n');
  if (text.includes('\r') || !text.endsWith('\n')) fail();
  // Match Python str.splitlines(), preserving empty interior lines only.
  return text.slice(0, -1).split(/[\n\v\f\x1c-\x1e\x85\u2028\u2029]/u);
}

function parseSection(body, nonce, prefix) {
  checkId(nonce);
  const lines = decode(body);
  if (lines[0] !== `HAPANELD_${prefix}_BEGIN:${nonce}`) fail();
  const match = new RegExp(`^HAPANELD_${prefix}_END:${nonce}:([0-9]{1,3})$`).exec(lines.at(-1));
  if (!match || lines.slice(1, -1).some(line => line.startsWith('HAPANELD_'))) fail();
  return { lines: lines.slice(1, -1), status: Number(match[1]) };
}

export function parseInstall(body, nonce) {
  const { lines, status } = parseSection(body, nonce, 'INSTALL');
  if (status === 0 && lines.length === 1 && lines[0] === 'Success') return 'installed';
  if (status === 1 && lines.length === 1 && /^Failure \[INSTALL_[A-Z0-9_]+(?:: [ -~]{1,1024})?\]$/.test(lines[0])) return 'refused';
  fail();
}

/** As in the reference, activity-manager diagnostic text is never returned. */
export function parseLaunch(body, nonce) {
  const { status } = parseSection(body, nonce, 'LAUNCH');
  if (status === 0) return 'started';
  if (status === 1) return 'refused';
  fail();
}
