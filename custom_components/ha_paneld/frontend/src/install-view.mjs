// Presentation only. Receipt phases describe saved progress, never permission
// to mutate a panel; the transaction ports must repeat their admission checks.
export const INSTALL_MESSAGES = Object.freeze({
  installPreviewTitle: 'Install ha-paneld over USB',
  installPreviewBody: 'Install the verified release on a compatible panel with no existing or retained ha-paneld installation. Existing panel data is preserved. This step does not complete provisioning or Home Assistant setup.',
  installConfirmation: 'I want to install this verified release on the selected panel.',
  installUsbBoundary: 'Connect the panel by USB to the computer or mobile device running this browser, never to the Home Assistant server.',
  installMqttBoundary: 'Existing MQTT configuration and integrations remain unchanged.',
  installPrepare: 'Install verified release',
  installPreparedTitle: 'Installation prepared',
  installPreparedBody: 'Saved progress is ready. Check the connected panel and verified release again, then upload the installation files.',
  installStage: 'Continue installation',
  installStagedTitle: 'Installation files uploaded',
  installStagedBody: 'Check the uploaded files and confirm that the panel still has no existing or retained installation before installing.',
  installApply: 'Continue installation',
  installInstalledTitle: 'App installation verified',
  installInstalledBody: 'The installed app matches the selected release. Start it and check its health. Provisioning and Home Assistant registration are still unfinished.',
  installLaunch: 'Start app and check health',
  installStagingTitle: 'Upload outcome needs review',
  installStagingBody: 'An upload was started without a saved completion. Check whether the complete verified file is already on this panel, without repeating the upload. Missing or incomplete files require recovery review; existing files will not be overwritten.',
  installInstallingTitle: 'Installation outcome needs checking',
  installInstallingBody: 'Installation was started without a saved completion. Inspect the connected panel to establish the outcome without repeating installation.',
  installLaunchingTitle: 'App startup needs checking',
  installLaunchingBody: 'Startup was requested without a saved health result. Check the connected panel without repeating startup. The app may still be starting.',
  installReconcile: 'Check outcome without repeating the step',
  installHealthyTitle: 'Installation and app health verified',
  installHealthyBody: 'The selected release is installed and its health check passed. Panel commissioning and Home Assistant registration are not complete.',
  installRecoveryTitle: 'Recovery review required',
  installRecoveryBody: 'Recovery checks that no ha-paneld app or retained data exists, and removes only this job’s temporary APK if its bytes match the verified release. It does not uninstall an app or change configuration. Upload and installation need a separate action afterwards.',
  installRecover: 'Confirm temporary-upload recovery',
  installCleanupTitle: 'Temporary-upload cleanup needs checking',
  installCleanupBody: 'Cleanup was requested without a saved completion. Check whether the temporary file is absent; this action does not repeat removal, upload or installation.',
  installInvalidTitle: 'Saved installation progress unavailable',
  installInvalidBody: 'Saved progress could not be understood. No installation action is available. Review the saved installation before continuing.',
  installErrorGeneric: 'The operation could not be completed. Keep the saved progress and check the connection before reviewing the outcome.',
  installErrorBusy: 'Another operation is using this panel. Wait for it to finish before checking saved progress.',
  installErrorStorage: 'Installation progress could not be read or saved. Keep this browser’s site data and review the installation before continuing.',
  installErrorTarget: 'The connected panel or its access permissions changed. Reconnect the intended panel and check saved progress.',
  installErrorArtifact: 'The selected release or panel files could not be verified. Review the release selection and saved progress before continuing.',
  installErrorNotClean: 'An existing or retained installation was found. Clean installation is refused to preserve panel data.',
  installErrorIncompatible: 'The verified release is not compatible with this panel.',
  installErrorConnection: 'The connection did not complete safely. Reconnect before checking saved progress; the previous operation may have taken effect.',
  installErrorHealth: 'App health could not yet be verified. The app may still be starting. Keep saved progress and check the outcome after reconnecting.',
});

const PHASES = Object.freeze({
  prepared: ['installPreparedTitle', 'installPreparedBody', 'installStage'],
  staged: ['installStagedTitle', 'installStagedBody', 'installApply'],
  installed: ['installInstalledTitle', 'installInstalledBody', 'installLaunch'],
  staging: ['installStagingTitle', 'installStagingBody', 'installReconcile'],
  installing: ['installInstallingTitle', 'installInstallingBody', 'installReconcile'],
  launching: ['installLaunchingTitle', 'installLaunchingBody', 'installReconcile'],
  healthy: ['installHealthyTitle', 'installHealthyBody', null],
  recovery_required: ['installRecoveryTitle', 'installRecoveryBody', 'installRecover'],
  cleanup_pending: ['installCleanupTitle', 'installCleanupBody', 'installReconcile'],
});

export function installationView(receiptOrNull, { connected = false, confirmed = false, busy = false } = {}) {
  let entry = ['installInvalidTitle', 'installInvalidBody', null];
  const showConfirmation = receiptOrNull === null;
  if (showConfirmation) entry = ['installPreviewTitle', 'installPreviewBody', 'installPrepare'];
  else if (receiptOrNull && typeof receiptOrNull === 'object' && !Array.isArray(receiptOrNull)) {
    // Read only an own data property; never invoke a getter or display peer data.
    const phase = Object.getOwnPropertyDescriptor(receiptOrNull, 'phase')?.value;
    if (typeof phase === 'string' && Object.hasOwn(PHASES, phase)) entry = PHASES[phase];
  }
  const [titleKey, bodyKey, actionKey] = entry;
  return Object.freeze({ titleKey, bodyKey, actionKey,
    actionEnabled: actionKey !== null && connected === true && busy === false &&
      (!showConfirmation || confirmed === true),
    showConfirmation });
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
