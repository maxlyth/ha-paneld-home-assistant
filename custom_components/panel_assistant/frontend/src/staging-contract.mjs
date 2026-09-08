export class StagingError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const fail = (code = 'staging_response_invalid') => { throw new StagingError(code); };
const exact = (pattern, value) => typeof value === 'string' && pattern.exec(value)?.[0] === value;
const validNonce = nonce => { if (!exact(/^[0-9a-f]{32}$/, nonce)) fail('invalid_request'); };

export function stagingPath(jobId) {
  validNonce(jobId);
  return `/data/local/tmp/ha-paneld-install-${jobId}.apk`;
}
export function buildPathState(nonce, jobId) {
  validNonce(nonce);
  const path = stagingPath(jobId);
  const parents = ['/data', '/data/local', '/data/local/tmp'].map(parent =>
    `[ -d ${parent} ] && [ ! -L ${parent} ] && [ -x ${parent} ]`).join(' && ');
  return [`echo HAPANELD_PATH_BEGIN:${nonce}`,
    `if ${parents} && [ -r /data/local/tmp ]; then if [ -e ${path} ] || [ -L ${path} ]; then echo present; else echo absent; fi; echo HAPANELD_PATH_END:${nonce}:0; else echo HAPANELD_PATH_END:${nonce}:1; fi`].join('; ');
}
// Observation only: unlike Python's post-upload preparation, this never chmods.
export function buildStagedObservation(nonce, jobId) {
  validNonce(nonce);
  const path = stagingPath(jobId);
  return [`echo HAPANELD_STAGED_BEGIN:${nonce}`,
    ...[['MODE', `stat -c %f ${path}`], ['SIZE', `wc -c < ${path}`], ['SHA', `sha256sum ${path}`]]
      .flatMap(([name, command]) => [`echo HAPANELD_STAGED_${name}_BEGIN:${nonce}`, command,
        `echo HAPANELD_STAGED_${name}_END:${nonce}:$?`]),
    `echo HAPANELD_STAGED_END:${nonce}`].join('; ');
}
function lines(body) {
  let text;
  if (typeof body === 'string') {
    if (!body.length || body.length > 32768 || new TextEncoder().encode(body).length > 32768) fail();
    text = body;
  } else if (body instanceof Uint8Array && body.byteLength > 0 && body.byteLength <= 32768) {
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); }
    catch { fail(); }
  } else fail();
  text = text.replaceAll('\r\n', '\n');
  if (!text.endsWith('\n') || /[^\x20-\x7e\t\n]/.test(text)) fail();
  return text.slice(0, -1).split('\n');
}
export function parsePathState(body, nonce) {
  validNonce(nonce);
  const output = lines(body);
  if (output.length !== 3 || output[0] !== `HAPANELD_PATH_BEGIN:${nonce}` ||
      output[2] !== `HAPANELD_PATH_END:${nonce}:0` || !['absent', 'present'].includes(output[1])) fail();
  return output[1] === 'present';
}
export function parseStagedFile(body, nonce, jobId) {
  validNonce(nonce);
  const path = stagingPath(jobId);
  const output = lines(body);
  if (output.length !== 11 || output[0] !== `HAPANELD_STAGED_BEGIN:${nonce}` ||
      output[10] !== `HAPANELD_STAGED_END:${nonce}`) fail();
  for (const [index, name] of ['MODE', 'SIZE', 'SHA'].entries()) {
    if (output[index * 3 + 1] !== `HAPANELD_STAGED_${name}_BEGIN:${nonce}` ||
        output[index * 3 + 3] !== `HAPANELD_STAGED_${name}_END:${nonce}:0`) fail();
  }
  if (!exact(/^[0-9a-fA-F]{1,8}$/, output[2]) || !exact(/^[ \t]*[0-9]{1,10}[ \t]*$/, output[5])) fail();
  // Exactly a regular 0644 file, not a symlink, special file or extra mode bits.
  const hashLine = /^([0-9a-f]{64})[ \t]+([^ \t]+)$/.exec(output[8]);
  const size = Number(output[5].trim());
  if (Number.parseInt(output[2], 16) !== 0x81a4 || size > 67108864 ||
      !hashLine || hashLine[2] !== path) {
    fail('staged_artifact_mismatch');
  }
  return Object.freeze({size, sha256: hashLine[1]});
}
export function parseStagedObservation(body, nonce, jobId, descriptor) {
  if (!Number.isSafeInteger(descriptor?.apkSize) || descriptor.apkSize < 1 || descriptor.apkSize > 67108864 ||
      !exact(/^[0-9a-f]{64}$/, descriptor.apkSha256)) fail('invalid_request');
  const file = parseStagedFile(body, nonce, jobId);
  if (file.size !== descriptor.apkSize || file.sha256 !== descriptor.apkSha256) fail('staged_artifact_mismatch');
  return true;
}
