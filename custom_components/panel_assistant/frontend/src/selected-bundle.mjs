import { verifyApkBundle } from './apk-verifier.mjs';
import { isGithubTag } from './release-identity.mjs';

// Advanced manual release selection: GitHub release files only, never a feed
// build. Never accept a partial or mixed bundle.
export async function verifySelectedBundle(selection, options = {}) {
  const files = Array.from(selection);
  if (files.length !== 5 || files.some(file => !(file instanceof File))) throw new Error('bundle');
  const byName = new Map(files.map(file => [file.name, file]));
  if (byName.size !== 5) throw new Error('bundle');
  const apk = files.find(file => file.name.endsWith('.apk'));
  const match = apk?.name.match(/^ha-paneld-(.+)-manual-setup-required\.apk$/);
  if (!match || !isGithubTag(match[1])) throw new Error('bundle');
  const tag = match[1];
  const descriptorName = `ha-paneld-${tag}-install.json`;
  const requirements = [
    ['checksum', `${apk.name}.sha256`, 512],
    ['checksumSignature', `${apk.name}.sha256.sig`, 256],
    ['descriptor', descriptorName, 4096],
    ['descriptorSignature', `${descriptorName}.sig`, 256],
  ];
  // Check every file before reading any bytes. The cryptographic verifier then
  // enforces exact signature lengths, channel, metadata and APK size/hash.
  if (apk.size < 1 || apk.size > 64 * 1024 * 1024) throw new Error('bundle');
  for (const [, name, max] of requirements) {
    const file = byName.get(name);
    if (!file || file.size < 1 || file.size > max) throw new Error('bundle');
  }
  const bundle = { tag };
  for (const [key, name] of requirements) bundle[key] = new Uint8Array(await byName.get(name).arrayBuffer());
  return verifyApkBundle(bundle, apk, options);
}
