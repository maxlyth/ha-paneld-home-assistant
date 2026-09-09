import { deviceKeyForIdentity } from './job-store.mjs';
import { advanceTransaction, TransactionError } from './transaction.mjs';
import { recoverTransaction } from './recovery-transaction.mjs';

const fail = code => { throw new TransactionError(code); };
const canonical = value => JSON.stringify(value, Object.keys(value).sort());

// UI controller for an already authenticated, identity-checked connection.
// The caller supplies actual usb-transaction-ports, storage, and a session guard.
// No jobs or mutations are created during preview. No receipt deletion exists.
export function createInstallController({ store, ports, locks = globalThis.navigator?.locks,
  ensureCurrent = () => {}, onReceipt = () => {} }) {
  let preview, expectedJobId, busy = false;
  const guard = () => { ensureCurrent(); };
  const setupOperation = async operation => {
    if (busy) fail('transaction_busy');
    if (!preview) fail('confirmation_required');
    busy = true;
    try {
      return await locks.request(`ha-paneld-usb:${preview.deviceKey}`,
        {mode: 'exclusive', ifAvailable: true}, async lock => {
          if (!lock) fail('transaction_busy');
          guard();
          const receipt = await store.load(preview.deviceKey);
          if (!receipt || receipt.phase !== 'healthy' ||
              canonical(receipt.artifact) !== canonical(preview.descriptor)) fail('transaction_invalid');
          if (receipt.id !== expectedJobId) fail('job_conflict');
          const release = await ports.authenticate();
          guard();
          if (release?.kind !== 'authenticated-apk-bytes' ||
              canonical(release.descriptor) !== canonical(receipt.artifact)) fail('artifact_changed');
          const result = await operation(receipt, release);
          guard();
          return result;
        });
    } finally { busy = false; }
  };
  return Object.freeze({
    async preview(target) {
      if (busy) fail('transaction_busy');
      busy = true;
      preview = undefined;
      try {
        guard();
        const snapshot = Object.freeze({ ...target });
        const deviceKey = await deviceKeyForIdentity(snapshot);
        const release = await ports.authenticate();
        guard();
        if (release?.kind !== 'authenticated-apk-bytes') fail('artifact_changed');
        const receipt = await store.load(deviceKey);
        if (receipt && canonical(receipt.artifact) !== canonical(release.descriptor)) fail('artifact_changed');
        // Actual ports validate the live target and installed/clean state. A
        // not-yet-created job may use the target only for read-only inspection.
        const proposed = receipt ?? { phase: 'prepared', target: snapshot, artifact: release.descriptor };
        if (['recovery_required', 'cleanup_pending'].includes(proposed.phase)) await ports.inspectRecovery(proposed, release);
        else await ports.inspect(proposed, release);
        guard();
        preview = Object.freeze({ target: snapshot, descriptor: release.descriptor, deviceKey, receipt });
        expectedJobId = receipt?.id;
        return preview;
      } finally { busy = false; }
    },
    async run(confirmed = false) {
      if (busy) fail('transaction_busy');
      if (!preview || confirmed !== true) fail('confirmation_required');
      busy = true;
      try {
        guard();
        const selected = preview;
        // Verify selection again before first durable job creation; changed
        // release selection requires a new preview and confirmation.
        const release = await ports.authenticate();
        guard();
        if (release?.kind !== 'authenticated-apk-bytes' ||
            canonical(release.descriptor) !== canonical(selected.descriptor)) fail('artifact_changed');
        let receipt = await store.load(selected.deviceKey);
        if (receipt && (!selected.receipt || receipt.id !== selected.receipt.id)) fail('job_conflict');
        if (!receipt) {
          if (selected.receipt) fail('transaction_missing');
          receipt = await store.create(selected.target, release.descriptor);
        }
        expectedJobId = receipt.id;
        onReceipt(receipt);
        // A reconciliation button promises observation only. Stop after that
        // transition; require a separate continue action before any mutation.
        const limit = ['staging', 'installing', 'launching'].includes(receipt.phase) ? 1 : 3;
        for (let step = 0; step < limit && !['healthy', 'recovery_required'].includes(receipt.phase); step++) {
          guard();
          receipt = await advanceTransaction({ store, ports, locks,
            deviceKey: selected.deviceKey, ensureCurrent: guard });
          onReceipt(receipt);
        }
        return receipt;
      } finally { busy = false; }
    },
    async recover(confirmed = false) {
      if (busy) fail('transaction_busy');
      if (!preview || !confirmed) fail('confirmation_required');
      busy = true;
      try {
        guard();
        const current = await store.load(preview.deviceKey);
        if (!current || canonical(current.artifact) !== canonical(preview.descriptor)) fail('artifact_changed');
        if (!preview.receipt || current.id !== preview.receipt.id) fail('job_conflict');
        const result = await recoverTransaction({store, deviceKey: preview.deviceKey, ports,
          locks, ensureCurrent: guard, confirmed, expectedJobId: preview.receipt.id});
        guard(); onReceipt(result); return result;
      } finally {busy = false;}
    },
    async observeSetup() {
      return setupOperation((receipt, release) => ports.setup(receipt, release));
    },
    async commissionPermissions(confirmed = false) {
      if (confirmed !== true) fail('confirmation_required');
      return setupOperation((receipt, release) => ports.commissionPermissions(receipt, release));
    },
    // Cancellation of active I/O belongs to the owning session's guard/close.
    invalidate() { if (busy) fail('transaction_busy'); preview = undefined; },
  });
}
