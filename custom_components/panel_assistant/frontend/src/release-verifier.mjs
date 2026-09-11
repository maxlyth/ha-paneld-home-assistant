import { MAX_FEED_BYTES, buildTagVersionCode, descriptorIdentityValid, githubApkName,
  isBuildTag, isBuildVersionName, isGithubTag, isRcTag, isStableTag } from './release-identity.mjs';

/** Byte-only metadata authentication. This does not validate an APK signing block. */
const KEY = `MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3LH+db6kzNld/ERP612x
UOOG6TINFvuKJKinQAWi6Gfm2jCmW4plhw+w4vXgP8B8FpY0SLatUVo3EeAi+f1K
EHj0syPi7Sx781o1oc9LicQG4LjWVZPe+m4AkPl9ByopobQwYTXOjaq6ZFpFgAZe
NwQ44hg5o9iVKtxpnnjHEc/m6o9TBySQvxDWF3RxCDyPLNBqhrsgKsDlAyh+dtA8
aJpQsDUJoX42xsRvA1hkRCpnWdEs1Bwfyv0ztlOxj7MxeFrFxWc3mnUyGhsn6rCT
O+ygQ2m7FHp3D5t1+wFIendluEzUC+y9MpUHmoyq/lFrVuA8EOiy1U+z7Lr1vBWf
LQIDAQAB`;
const PACKAGE = 'io.github.maxlyth.hapaneld';
const SIGNER = 'ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339';
const LAUNCH = `${PACKAGE}/.MainActivity`;
const FIELDS = ['schema', 'releaseTag', 'versionName', 'versionCode', 'apkName',
  'apkSize', 'apkSha256', 'packageId', 'signerCertificateSha256', 'minSdk',
  'supportedAbis', 'databaseCompatibility', 'launchComponent'].sort();
// The build feed mirrors build_feed.parse_build_feed and _parse_build exactly.
const FEED_SCHEMA = `${PACKAGE}.buildfeed.v1`;
const FEED_CHANNELS = ['maintainer', 'beta'];
const FEED_FIELDS = ['builds', 'channel', 'schema'];
const BUILD_FIELDS = ['apkPath', 'apkSha256', 'apkSize', 'commit', 'databaseCompatibility',
  'launchComponent', 'minSdk', 'packageId', 'published', 'signerCertificateSha256',
  'supportedAbis', 'versionCode', 'versionName'];
const MAX_FEED_BUILDS = 500;
const COMMIT = /^[0-9a-f]{40}$/;
const PUBLISHED = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;
// The feed's own bound; the descriptor built from the chosen build is then held
// to the stricter install descriptor rule below.
const FEED_DATABASE = /^hapaneld-db:v1:ha-paneld\.db:([1-9][0-9]*):([1-9][0-9]*)$/;
const HASH = /^[0-9a-f]{64}$/;
const ALGORITHM = 'RSASSA-PKCS1-v1_5';
const encoder = new TextEncoder();

export class ReleaseVerificationError extends Error {
  constructor() { super('Release metadata could not be authenticated'); }
}
function requireValid(condition) { if (!condition) throw new ReleaseVerificationError(); }
function fullMatch(pattern, value) {
  return typeof value === 'string' && pattern.exec(value)?.[0] === value;
}
function bytes(value, maximum, exact = false) {
  requireValid(value instanceof Uint8Array && value.length > 0 &&
    (exact ? value.length === maximum : value.length <= maximum));
  // Snapshot before async work: callers cannot change authenticated input mid-flight.
  return Uint8Array.from(value);
}
function ascii(value) {
  requireValid(value.every((byte) => byte < 128));
  return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(value);
}
function integer(value, maximum) {
  return Number.isSafeInteger(value) && value >= 1 && value <= maximum;
}
function record(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function exactKeys(value, fields) {
  return record(value) && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(fields);
}
function supportedAbis(value) {
  return Array.isArray(value) && value.length === 2 &&
    value[0] === 'arm64-v8a' && value[1] === 'armeabi-v7a';
}
// Python's json.dumps(sort_keys=True, separators=(",", ":")). Every value a
// valid feed can hold is an ASCII string, a safe integer, a list or an object,
// where both encodings coincide; anything else fails validation regardless.
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (record(value)) {
    return `{${Object.keys(value).sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
// Test seam only: production callers never pass a key, so neither a URL nor a
// posted message can choose the key that authenticates a release.
async function releaseKey(injected) {
  if (injected === undefined) {
    return crypto.subtle.importKey('spki',
      Uint8Array.from(atob(KEY.replace(/\s/g, '')), (c) => c.charCodeAt(0)),
      { name: ALGORITHM, hash: 'SHA-256' }, false, ['verify']);
  }
  requireValid(injected instanceof CryptoKey && injected.type === 'public' &&
    injected.algorithm.name === ALGORITHM && injected.algorithm.hash.name === 'SHA-256' &&
    injected.usages.includes('verify'));
  return injected;
}

/** Strict parsing only: returned data is NOT authenticated. For format conformance. */
export function parseUnauthenticatedDescriptor(body, { tag, apkSha256 }) {
  try {
    requireValid((isGithubTag(tag) || isBuildTag(tag)) && fullMatch(HASH, apkSha256));
    const text = ascii(bytes(body, 4096));
    const d = JSON.parse(text);
    requireValid(record(d));
    requireValid(JSON.stringify(Object.keys(d).sort()) === JSON.stringify(FIELDS));
    requireValid(d.schema === `${PACKAGE}.install.v1` && descriptorIdentityValid(d, tag, apkSha256) &&
      d.apkSha256 === apkSha256 && d.packageId === PACKAGE &&
      d.signerCertificateSha256 === SIGNER && d.launchComponent === LAUNCH);
    requireValid(integer(d.apkSize, 64 * 1024 * 1024) &&
      integer(d.versionCode, 2147483647) && integer(d.minSdk, 100));
    requireValid(supportedAbis(d.supportedAbis));
    const dbPattern = /^hapaneld-db:v1:ha-paneld\.db:([1-9][0-9]{0,9}):([1-9][0-9]{0,9})$/;
    requireValid(fullMatch(dbPattern, d.databaseCompatibility));
    const db = dbPattern.exec(d.databaseCompatibility);
    requireValid(integer(Number(db[1]), 2147483647) &&
      integer(Number(db[2]), 2147483647) && Number(db[1]) <= Number(db[2]));
    // All accepted strings are ASCII, integers bounded: JS and Python canonical
    // encodings coincide here. Exact comparison also rejects duplicate keys,
    // escaped spellings, floats/exponents, extra whitespace and missing newline.
    const canonical = JSON.stringify(Object.fromEntries(FIELDS.map((k) => [k, d[k]]))) + '\n';
    requireValid(text === canonical);
    Object.freeze(d.supportedAbis);
    return Object.freeze(d);
  } catch { throw new ReleaseVerificationError(); }
}

function feedBuild(entry) {
  requireValid(exactKeys(entry, BUILD_FIELDS));
  requireValid(integer(entry.versionCode, 2147483647) &&
    integer(entry.apkSize, 64 * 1024 * 1024) && integer(entry.minSdk, 100));
  requireValid(fullMatch(HASH, entry.apkSha256) &&
    // Content-addressed: the path can only ever name the bytes the feed signs.
    entry.apkPath === `apks/${entry.apkSha256}.apk` &&
    isBuildVersionName(entry.versionName) && fullMatch(COMMIT, entry.commit) &&
    fullMatch(PUBLISHED, entry.published) &&
    fullMatch(FEED_DATABASE, entry.databaseCompatibility) &&
    entry.packageId === PACKAGE && entry.signerCertificateSha256 === SIGNER &&
    entry.launchComponent === LAUNCH && supportedAbis(entry.supportedAbis));
  return entry;
}

// The feed is authenticated as exact bytes, then held to its closed canonical
// shape. The chosen build becomes the same v1 descriptor a GitHub release
// carries, so everything after this point handles both alike.
async function verifyFeedBundle(bundle, expectedTag, verificationKey) {
  requireValid(exactKeys(bundle, ['feed', 'feedSignature', 'tag']) && bundle.tag === expectedTag);
  const code = buildTagVersionCode(expectedTag);
  requireValid(code !== null);
  const feed = bytes(bundle.feed, MAX_FEED_BYTES);
  const signature = bytes(bundle.feedSignature, 256, true);
  const key = await releaseKey(verificationKey);
  requireValid(await crypto.subtle.verify(ALGORITHM, key, signature, feed));
  const text = ascii(feed);
  const document = JSON.parse(text);
  requireValid(exactKeys(document, FEED_FIELDS) && document.schema === FEED_SCHEMA &&
    FEED_CHANNELS.includes(document.channel) && Array.isArray(document.builds) &&
    document.builds.length <= MAX_FEED_BUILDS);
  requireValid(text === `${canonicalJson(document)}\n`);
  const builds = document.builds.map(feedBuild);
  requireValid(new Set(builds.map((build) => build.versionCode)).size === builds.length);
  const build = builds.find((candidate) => candidate.versionCode === code);
  requireValid(build);
  const descriptor = {
    apkName: `${build.apkSha256}.apk`, apkSha256: build.apkSha256, apkSize: build.apkSize,
    databaseCompatibility: build.databaseCompatibility, launchComponent: build.launchComponent,
    minSdk: build.minSdk, packageId: build.packageId, releaseTag: expectedTag,
    schema: `${PACKAGE}.install.v1`, signerCertificateSha256: build.signerCertificateSha256,
    supportedAbis: build.supportedAbis, versionCode: build.versionCode, versionName: build.versionName,
  };
  // The job store re-reads this exact descriptor later: prove now that it will.
  const canonical = `${JSON.stringify(Object.fromEntries(FIELDS.map((k) => [k, descriptor[k]])))}\n`;
  return parseUnauthenticatedDescriptor(encoder.encode(canonical), { tag: expectedTag, apkSha256: build.apkSha256 });
}

async function verifyGithubBundle(bundle, expectedRcTag, verificationKey) {
  const { tag } = bundle;
  requireValid(isGithubTag(tag) && (expectedRcTag === null ? isStableTag(tag) :
    isRcTag(expectedRcTag) && tag === expectedRcTag));
  const checksum = bytes(bundle.checksum, 512);
  const checksumSignature = bytes(bundle.checksumSignature, 256, true);
  const descriptor = bytes(bundle.descriptor, 4096);
  const descriptorSignature = bytes(bundle.descriptorSignature, 256, true);
  const key = await releaseKey(verificationKey);
  requireValid(await crypto.subtle.verify(ALGORITHM, key, checksumSignature, checksum));
  requireValid(await crypto.subtle.verify(ALGORITHM, key, descriptorSignature, descriptor));
  const line = ascii(checksum);
  const hash = line.slice(0, 64);
  requireValid(fullMatch(HASH, hash) && line === `${hash}  ${githubApkName(tag)}\n`);
  return parseUnauthenticatedDescriptor(descriptor, { tag, apkSha256: hash });
}

/** Authenticate the signed metadata and cross-bind exact tag, checksum and descriptor.
 * Stable releases are default; an RC or a feed build requires its explicit
 * expectedRcTag, and a build tag accepts only a signed feed bundle.
 * The caller supplies bytes, not URLs. No release discovery or network trust is implied.
 * `verificationKey` replaces the embedded release key in tests only.
 */
export async function verifyReleaseBundle(bundle, { expectedRcTag = null } = {}, verificationKey = undefined) {
  try {
    const authenticated = expectedRcTag !== null && isBuildTag(expectedRcTag)
      ? await verifyFeedBundle(bundle, expectedRcTag, verificationKey)
      : await verifyGithubBundle(bundle, expectedRcTag, verificationKey);
    return Object.freeze({ kind: 'authenticated-release-metadata', descriptor: authenticated });
  } catch { throw new ReleaseVerificationError(); }
}
