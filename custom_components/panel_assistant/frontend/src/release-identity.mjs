// The one definition of every release identity the installer accepts. A GitHub
// release is a v-tag; a dev build from the signed build feed is build-<versionCode>
// and is never a GitHub tag. Every tag, APK name and version name check imports
// these rules instead of repeating a pattern.
export const MAX_TAG_LENGTH = 64;
// The signed build feed document, in exact bytes.
export const MAX_FEED_BYTES = 256 * 1024;
const MAX_VERSION_CODE = 2147483647;
const STABLE = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const RC = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/;
const BUILD = /^build-([1-9][0-9]{0,9})$/;
const VERSION_NAME = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$/;
const matches = (pattern, value) => typeof value === 'string' && value.length <= MAX_TAG_LENGTH &&
  pattern.exec(value)?.[0] === value;

export const isStableTag = tag => matches(STABLE, tag);
export const isRcTag = tag => matches(RC, tag);
export const isGithubTag = tag => isStableTag(tag) || isRcTag(tag);
/** The versionCode a build tag names, or null when the value is not a build tag. */
export function buildTagVersionCode(tag) {
  if (!matches(BUILD, tag)) return null;
  const code = Number(BUILD.exec(tag)[1]);
  return code <= MAX_VERSION_CODE ? code : null;
}
export const isBuildTag = tag => buildTagVersionCode(tag) !== null;
/** A feed build's versionName: free-form within this pattern, unlike a GitHub tag. */
export const isBuildVersionName = value => matches(VERSION_NAME, value);
/** How Home Assistant names a feed build, and how the release list carries it. */
export const buildLabel = (versionName, versionCode) => `${versionName} build ${versionCode}`;
/** The exact asset name a GitHub release publishes its APK under. */
export const githubApkName = tag => `ha-paneld-${tag}-manual-setup-required.apk`;

/** Whether a v1 install descriptor carries the identity its tag requires. A
 * GitHub release names the APK and version after its tag; a feed build names the
 * APK by its content and its tag number is exactly its versionCode.
 */
export function descriptorIdentityValid(descriptor, tag, apkSha256) {
  if (isGithubTag(tag)) {
    return descriptor.releaseTag === tag && descriptor.versionName === tag.slice(1) &&
      descriptor.apkName === githubApkName(tag);
  }
  const code = buildTagVersionCode(tag);
  return code !== null && descriptor.releaseTag === tag && descriptor.versionCode === code &&
    isBuildVersionName(descriptor.versionName) && descriptor.apkName === `${apkSha256}.apk`;
}
