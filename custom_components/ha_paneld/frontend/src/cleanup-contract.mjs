import {stagingPath, StagingError} from './staging-contract.mjs';
const valid = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value) && value.length === 32;
export function buildPrefixCleanup(nonce, jobId, observation) {
  if (!valid(nonce) || !Number.isSafeInteger(observation?.size) || observation.size < 0 ||
      observation.size > 67108864 || typeof observation.sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/.test(observation.sha256) || observation.sha256.length !== 64) throw new StagingError('invalid_request');
  const path = stagingPath(jobId);
  // Only a task-scoped temporary file. No recursive removal, overwrite, package
  // mutation or elevated command. Recheck exact content immediately before rm.
  const checks = ['/data', '/data/local', '/data/local/tmp', path].map(p => `[ ! -L ${p} ]`);
  checks.push(`[ -f ${path} ]`, `[ "$(stat -c %f ${path})" = 81a4 ]`,
    `[ "$(stat -c %h ${path})" = 1 ]`,
    `{ [ "$(stat -c %u ${path})" = 0 ] || [ "$(stat -c %u ${path})" = 2000 ]; }`,
    `[ "$(stat -c %s ${path})" = ${observation.size} ]`,
    `[ "$(sha256sum ${path})" = '${observation.sha256}  ${path}' ]`);
  return `echo HAPANELD_CLEANUP_BEGIN:${nonce}; if ${checks.join(' && ')}; then rm ${path}; else false; fi; echo HAPANELD_CLEANUP_END:${nonce}:$?`;
}
export function parsePrefixCleanup(body, nonce) {
  if (!valid(nonce) || typeof body !== 'string' || body.length > 1024) throw new StagingError('cleanup_response_invalid');
  const text = body.replaceAll('\r\n', '\n');
  if (text !== `HAPANELD_CLEANUP_BEGIN:${nonce}\nHAPANELD_CLEANUP_END:${nonce}:0\n`) throw new StagingError('cleanup_refused');
  return true;
}
