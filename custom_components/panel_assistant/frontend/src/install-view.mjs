// Presentation only. Receipt phases describe saved progress, never permission
// to mutate a panel; the transaction ports repeat their own admission checks.
// The person installing never sees a phase name: they see one activity line
// and a progress bar, and an error tells them the single thing to do next.

export const INSTALL_MESSAGES = Object.freeze({
  installErrorGeneric: 'Something went wrong. Unplug the panel, plug it back in and try again.',
  installErrorBusy: 'The panel is busy with another install. Wait a minute, then try again.',
  installErrorStorage: 'This browser couldn’t save its progress. Allow this site to store data, then try again.',
  installErrorTarget: 'A different panel was connected. Plug in the same panel and try again.',
  installErrorArtifact: 'The app download couldn’t be checked. Go back to Home Assistant and start again.',
  installErrorNotClean: 'This panel already has the app. Update it from Home Assistant instead.',
  installErrorIncompatible: 'This panel can’t run this version of the app.',
  installErrorConnection: 'The connection to the panel dropped. Unplug it, plug it back in and try again.',
  installErrorHealth: 'The app is installed but hasn’t started yet. Wait a moment, then try again.',
});

// Each phase maps to what the person sees: the activity line and how far along
// the bar is. Phases that mean "checking where we left off" show the same
// activity as the step they belong to, because to the person it is that step.
const PROGRESS = Object.freeze({
  prepared: ['stepCopying', 10],
  staging: ['stepCopying', 25],
  staged: ['stepInstalling', 50],
  installing: ['stepInstalling', 60],
  installed: ['stepStarting', 70],
  launching: ['stepStarting', 80],
  healthy: ['stepPermissions', 90],
  recovery_required: ['stepCopying', 10],
  cleanup_pending: ['stepCopying', 10],
});

export function installProgress(receiptOrNull) {
  // Read only an own data property; never invoke a getter or display peer data.
  const phase = receiptOrNull && typeof receiptOrNull === 'object' && !Array.isArray(receiptOrNull)
    ? Object.getOwnPropertyDescriptor(receiptOrNull, 'phase')?.value : undefined;
  const [stepKey, percent] = typeof phase === 'string' && Object.hasOwn(PROGRESS, phase)
    ? PROGRESS[phase] : ['stepCopying', 5];
  return Object.freeze({ stepKey, percent });
}

const ERRORS = Object.freeze({
  transaction_busy: 'installErrorBusy',
  job_malformed: 'installErrorStorage',
  job_storage_failed: 'installErrorStorage',
  job_conflict: 'installErrorStorage',
  job_unavailable: 'installErrorStorage',
  transaction_missing: 'installErrorStorage',
  target_changed: 'installErrorTarget',
  root_state_ambiguous: 'installErrorTarget',
  artifact_changed: 'installErrorArtifact',
  installed_artifact_mismatch: 'installErrorArtifact',
  target_not_clean: 'installErrorNotClean',
  target_incompatible: 'installErrorIncompatible',
  shell_timeout: 'installErrorConnection',
  shell_cleanup_failed: 'installErrorConnection',
  health_unavailable: 'installErrorHealth',
  health_malformed: 'installErrorHealth',
});

export function errorView(code) {
  return typeof code === 'string' && Object.hasOwn(ERRORS, code)
    ? ERRORS[code] : 'installErrorGeneric';
}
