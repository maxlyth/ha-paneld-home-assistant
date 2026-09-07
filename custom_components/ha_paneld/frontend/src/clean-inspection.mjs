import { buildPreflight, parsePreflight, PreflightError } from './preflight.mjs';
import { SU_PREFIXES, buildDelegatedProof, parseDelegatedProof } from './delegated-proof.mjs';
import { readShell } from './shell-session.mjs';

const newNonce = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');

// Same-connection observation only; every future actuator still needs its own
// fresh identity/custody checks. The guard prevents work after cancellation or
// release selection changes, including between failed dialect attempts.
export async function inspectCleanTarget(adb, descriptor, ensureCurrent = () => {}) {
  async function inventory() {
    ensureCurrent();
    const nonce = newNonce();
    const body = await readShell(adb, buildPreflight(nonce), { timeoutMs: 30000 });
    ensureCurrent();
    return parsePreflight(body, nonce, descriptor);
  }
  const initial = await inventory();
  if (initial.rootMode !== 'root_su') return initial;
  let proved = false;
  for (const prefix of SU_PREFIXES) {
    ensureCurrent();
    const nonce = newNonce();
    const body = await readShell(adb, buildDelegatedProof(prefix, nonce));
    ensureCurrent();
    if (parseDelegatedProof(body, nonce)) { proved = true; break; }
  }
  if (!proved) throw new PreflightError('root_state_ambiguous');
  const final = await inventory();
  for (const key of ['model', 'serial', 'primaryAbi', 'androidSdk', 'rootMode']) {
    if (initial[key] !== final[key]) throw new PreflightError('target_changed');
  }
  return Object.freeze({ ...final, installationAdmission: 'clean_preflight', delegatedRootVerified: true });
}
