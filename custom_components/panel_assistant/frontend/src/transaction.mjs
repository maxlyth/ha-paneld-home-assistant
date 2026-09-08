// Internal orchestration only. Ports must authenticate release bytes and inspect
// the same live USB connection; receipt contents are never authorization.
// Composed by usb-transaction-ports and the explicit installation controller.
export class TransactionError extends Error {
  constructor(code) { super(code); this.code = code; }
}

function same(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length &&
    keys.every(key => Object.hasOwn(right, key) && same(left[key], right[key]));
}

/** Advance at most one actuator under a browser-wide exclusive device lock.
 * ports.authenticate() freshly verifies the selected signed bundle and APK.
 * ports.inspect(receipt, release) is read-only and returns exact target plus
 * booleans clean, staged, installed, healthy, all bound to this artifact.
 * ports.stage/install/launch must implement bounded operations on that same
 * connection, and settle only after local I/O is closed or quarantined. A timeout
 * alone must not release the lock while late local I/O can resume. Installed/
 * healthy mean verified outcomes, not command exit codes.
 */
export async function advanceTransaction({ store, deviceKey, ports,
  locks = globalThis.navigator?.locks, ensureCurrent = () => {} }) {
  if (typeof deviceKey !== 'string' || !/^[0-9a-f]{64}$/.test(deviceKey) || deviceKey.length !== 64 ||
      !locks || typeof locks.request !== 'function') throw new TransactionError('transaction_unavailable');
  return locks.request(`ha-paneld-usb:${deviceKey}`, { mode: 'exclusive', ifAvailable: true }, async lock => {
    if (!lock) throw new TransactionError('transaction_busy');
    ensureCurrent();
    let receipt = await store.load(deviceKey);
    if (!receipt) throw new TransactionError('transaction_missing');
    if (['healthy', 'recovery_required'].includes(receipt.phase)) return receipt;
    const release = await ports.authenticate();
    ensureCurrent();
    if (release?.kind !== 'authenticated-apk-bytes' || !same(receipt.artifact, release.descriptor)) {
      throw new TransactionError('artifact_changed');
    }
    const observe = async () => {
      ensureCurrent();
      const state = await ports.inspect(receipt, release);
      ensureCurrent();
      if (!same(receipt.target, state?.target)) throw new TransactionError('target_changed');
      return state;
    };
    const advance = async phase => {
      ensureCurrent();
      receipt = await store.advance(deviceKey, receipt.revision, phase);
      return receipt;
    };
    const state = await observe();
    // Pending mutations are reconciled with observations, never replayed.
    if (receipt.phase === 'staging') return advance(
      state.clean === true && state.staged === true ? 'staged' : 'recovery_required');
    if (receipt.phase === 'installing') return advance(state.installed === true ? 'installed' : 'recovery_required');
    if (receipt.phase === 'launching') return advance(state.healthy === true ? 'healthy' : 'recovery_required');

    const transitions = {
      prepared: ['staging', 'stage', 'staged', state.clean === true],
      staged: ['installing', 'install', 'installed', state.clean === true && state.staged === true],
      installed: ['launching', 'launch', 'healthy', state.installed === true],
    };
    const transition = transitions[receipt.phase];
    if (!transition) throw new TransactionError('transaction_invalid');
    const [pending, operation, completed, admitted] = transition;
    if (!admitted) return advance('recovery_required');
    // Commit intent before any side effect. Failure/cancellation leaves pending
    // durable, including a successful actuator whose completion write failed.
    await advance(pending);
    ensureCurrent();
    await ports[operation](receipt, release);
    const after = await observe();
    const proved = operation === 'stage' ? after.clean === true && after.staged === true :
      operation === 'install' ? after.installed === true : after.healthy === true;
    return advance(proved ? completed : 'recovery_required');
  });
}
