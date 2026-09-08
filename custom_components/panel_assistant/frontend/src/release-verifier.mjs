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
const FIELDS = ['schema', 'releaseTag', 'versionName', 'versionCode', 'apkName',
  'apkSize', 'apkSha256', 'packageId', 'signerCertificateSha256', 'minSdk',
  'supportedAbis', 'databaseCompatibility', 'launchComponent'].sort();
const STABLE = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const RC = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/;
const HASH = /^[0-9a-f]{64}$/;
const ALGORITHM = 'RSASSA-PKCS1-v1_5';

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
function tagValid(tag) {
  return typeof tag === 'string' && tag.length <= 64 &&
    (fullMatch(STABLE, tag) || fullMatch(RC, tag));
}
function integer(value, maximum) {
  return Number.isSafeInteger(value) && value >= 1 && value <= maximum;
}

/** Strict parsing only: returned data is NOT authenticated. For format conformance. */
export function parseUnauthenticatedDescriptor(body, { tag, apkSha256 }) {
  try {
    requireValid(tagValid(tag) && fullMatch(HASH, apkSha256));
    const text = ascii(bytes(body, 4096));
    const d = JSON.parse(text);
    requireValid(d !== null && !Array.isArray(d) && typeof d === 'object');
    requireValid(JSON.stringify(Object.keys(d).sort()) === JSON.stringify(FIELDS));
    const apkName = `ha-paneld-${tag}-manual-setup-required.apk`;
    requireValid(d.schema === `${PACKAGE}.install.v1` && d.releaseTag === tag &&
      d.versionName === tag.slice(1) && d.apkName === apkName &&
      d.apkSha256 === apkSha256 && d.packageId === PACKAGE &&
      d.signerCertificateSha256 === SIGNER && d.launchComponent === `${PACKAGE}/.MainActivity`);
    requireValid(integer(d.apkSize, 64 * 1024 * 1024) &&
      integer(d.versionCode, 2147483647) && integer(d.minSdk, 100));
    requireValid(Array.isArray(d.supportedAbis) && d.supportedAbis.length === 2 &&
      d.supportedAbis[0] === 'arm64-v8a' && d.supportedAbis[1] === 'armeabi-v7a');
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

/** Authenticate both signatures and cross-bind exact tag/checksum/descriptor.
 * Stable releases are default; an RC requires its explicit expectedRcTag.
 * The caller supplies bytes, not URLs. No release discovery or network trust is implied.
 */
export async function verifyReleaseBundle(bundle, { expectedRcTag = null } = {}) {
  try {
    const { tag } = bundle;
    requireValid(tagValid(tag) && (expectedRcTag === null ? fullMatch(STABLE, tag) :
      tagValid(expectedRcTag) && fullMatch(RC, expectedRcTag) && tag === expectedRcTag));
    const checksum = bytes(bundle.checksum, 512);
    const checksumSignature = bytes(bundle.checksumSignature, 256, true);
    const descriptor = bytes(bundle.descriptor, 4096);
    const descriptorSignature = bytes(bundle.descriptorSignature, 256, true);
    const key = await crypto.subtle.importKey('spki',
      Uint8Array.from(atob(KEY.replace(/\s/g, '')), (c) => c.charCodeAt(0)),
      { name: ALGORITHM, hash: 'SHA-256' }, false, ['verify']);
    requireValid(await crypto.subtle.verify(ALGORITHM, key, checksumSignature, checksum));
    requireValid(await crypto.subtle.verify(ALGORITHM, key, descriptorSignature, descriptor));
    const record = ascii(checksum);
    const hash = record.slice(0, 64);
    requireValid(fullMatch(HASH, hash) &&
      record === `${hash}  ha-paneld-${tag}-manual-setup-required.apk\n`);
    const authenticated = parseUnauthenticatedDescriptor(descriptor, { tag, apkSha256: hash });
    return Object.freeze({ kind: 'authenticated-release-metadata', descriptor: authenticated });
  } catch { throw new ReleaseVerificationError(); }
}
