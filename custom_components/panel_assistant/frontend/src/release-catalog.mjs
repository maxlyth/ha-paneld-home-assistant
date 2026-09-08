const MAX_CHOICES = 30;
const MAX_BYTES = 8192;
const STABLE = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const RC = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/;
const keys = (value, expected) => value !== null && typeof value === 'object' &&
  !Array.isArray(value) && Object.keys(value).length === expected.length &&
  expected.every(key => Object.hasOwn(value, key));
function requireValid(value) { if (!value) throw new Error('Invalid release catalogue'); }

export function parseReleaseCatalog(value) {
  requireValid(keys(value, ['releases']) && Array.isArray(value.releases) && value.releases.length <= MAX_CHOICES);
  const seen = new Set();
  let stableCount = 0;
  return Object.freeze(value.releases.map(release => {
    requireValid(keys(release, ['tag', 'prerelease']) && typeof release.prerelease === 'boolean' &&
      typeof release.tag === 'string' && release.tag.length <= 64 &&
      (release.prerelease ? RC : STABLE).exec(release.tag)?.[0] === release.tag && !seen.has(release.tag));
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
