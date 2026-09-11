import { buildLabel, buildTagVersionCode, isBuildVersionName, isRcTag, isStableTag } from './release-identity.mjs';

// GitHub lists at most 30 releases; a signed build feed holds at most 500 builds.
const MAX_GITHUB_CHOICES = 30;
const MAX_FEED_CHOICES = 500;
const MAX_BYTES = 128 * 1024;
const keys = (value, expected) => value !== null && typeof value === 'object' &&
  !Array.isArray(value) && Object.keys(value).length === expected.length &&
  expected.every(key => Object.hasOwn(value, key));
function requireValid(value) { if (!value) throw new Error('Invalid release catalogue'); }

// A feed build is always a test build, named "<versionName> build <versionCode>".
function feedName(release) {
  const code = buildTagVersionCode(release.tag);
  const versionName = typeof release.name === 'string' ? release.name.split(' ')[0] : null;
  return code !== null && release.prerelease === true && isBuildVersionName(versionName) &&
    release.name === buildLabel(versionName, code);
}

export function parseReleaseCatalog(value) {
  requireValid(keys(value, ['releases']) && Array.isArray(value.releases) &&
    value.releases.length <= MAX_GITHUB_CHOICES + MAX_FEED_CHOICES);
  const seen = new Set();
  let stableCount = 0, githubCount = 0, feedCount = 0;
  return Object.freeze(value.releases.map(release => {
    if (keys(release, ['tag', 'prerelease', 'name'])) {
      requireValid(feedName(release) && !seen.has(release.tag) && ++feedCount <= MAX_FEED_CHOICES);
      seen.add(release.tag);
      return Object.freeze({ tag: release.tag, prerelease: true, name: release.name });
    }
    requireValid(keys(release, ['tag', 'prerelease']) && typeof release.prerelease === 'boolean' &&
      (release.prerelease ? isRcTag(release.tag) : isStableTag(release.tag)) && !seen.has(release.tag) &&
      ++githubCount <= MAX_GITHUB_CHOICES);
    seen.add(release.tag);
    if (!release.prerelease) requireValid(++stableCount <= 1);
    return Object.freeze({ tag: release.tag, prerelease: release.prerelease });
  }));
}

export async function fetchReleaseCatalog(hass, { signal, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, timeoutMs);
  let reader;
  let rejectAborted;
  const aborted = new Promise((_, reject) => { rejectAborted = () => reject(new Error('Release catalogue cancelled')); });
  controller.signal.addEventListener('abort', rejectAborted, { once: true });
  try {
    requireValid(!controller.signal.aborted);
    return await Promise.race([aborted, (async () => {
      const response = await hass.fetchWithAuth('/api/panel_assistant/usb/releases', {
        method: 'GET', redirect: 'error', signal: controller.signal,
      });
      requireValid(!controller.signal.aborted && response.status === 200 && !response.redirected && response.body &&
        response.headers.get('content-type')?.split(';')[0].trim() === 'application/json');
      const length = response.headers.get('content-length');
      requireValid(length === null || (/^(0|[1-9][0-9]*)$/.exec(length)?.[0] === length && Number(length) <= MAX_BYTES));
      reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      while (true) {
        const next = await reader.read();
        requireValid(!controller.signal.aborted);
        if (next.done) break;
        size += next.value.byteLength;
        requireValid(size <= MAX_BYTES);
        chunks.push(next.value);
      }
      requireValid(size > 0 && (length === null || size === Number(length)));
      return parseReleaseCatalog(JSON.parse(await new Blob(chunks).text()));
    })()]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    controller.signal.removeEventListener('abort', rejectAborted);
    controller.abort();
    if (reader) void reader.cancel().catch(() => {});
  }
}
