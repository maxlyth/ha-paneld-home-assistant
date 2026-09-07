import { TransactionError } from './transaction.mjs';

function same(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length &&
    keys.every(key => Object.hasOwn(right, key) && same(left[key], right[key]));
}

// Recovery permits one exact staged-file cleanup, never installation. Ports own
// mutation-time reinspection and bounded I/O shutdown before releasing this lock.
export async function recoverTransaction({ store, deviceKey, ports, expectedJobId,
  locks = globalThis.navigator?.locks, ensureCurrent = () => {}, confirmed = false }) {
  if (confirmed !== true) throw new TransactionError('recovery_confirmation_required');
  if (typeof deviceKey !== 'string' || deviceKey.length !== 64 || !/^[0-9a-f]{64}$/.test(deviceKey) ||
      !locks || typeof locks.request !== 'function') throw new TransactionError('transaction_unavailable');
  return locks.request(`ha-paneld-usb:${deviceKey}`, { mode: 'exclusive', ifAvailable: true }, async lock => {
    if (!lock) throw new TransactionError('transaction_busy');
    ensureCurrent();
    let receipt = await store.load(deviceKey);
    ensureCurrent();
    if (!receipt) throw new TransactionError('transaction_missing');
    if (typeof expectedJobId !== 'string' || receipt.id !== expectedJobId) throw new TransactionError('job_conflict');
    if (!['recovery_required', 'cleanup_pending'].includes(receipt.phase)) {
      throw new TransactionError('transaction_invalid');
    }
    const release = await ports.authenticate();
    ensureCurrent();
    if (release?.kind !== 'authenticated-apk-bytes' || !same(receipt.artifact, release.descriptor)) {
      throw new TransactionError('artifact_changed');
    }
    const observe = async () => {
      ensureCurrent();
      const state = await ports.inspectRecovery(receipt, release);
      ensureCurrent();
      if (!same(receipt.target, state?.target)) throw new TransactionError('target_changed');
      if (state.clean !== true) throw new TransactionError('recovery_not_clean');
      return state;
    };
    const advance = async phase => {
      ensureCurrent();
      receipt = await store.advance(deviceKey, receipt.revision, phase);
      ensureCurrent();
      return receipt;
    };
    const state = await observe();
    // A prior cleanup attempt is only reconciled, including lost completion.
    if (receipt.phase === 'cleanup_pending') {
      return advance(state.absent === true ? 'prepared' : 'recovery_required');
    }
    if (state.absent === true) return advance('prepared');
    if (state.removable !== true) throw new TransactionError('recovery_not_removable');
    await advance('cleanup_pending');
    ensureCurrent();
    await ports.cleanup(receipt, release);
    const after = await observe();
    return advance(after.absent === true ? 'prepared' : 'recovery_required');
  });
}
