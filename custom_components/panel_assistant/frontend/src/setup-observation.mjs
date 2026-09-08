const STAGES = new Set(['identity', 'renderer', 'ha_url', 'ha_credentials', 'home_dashboard',
  'mqtt_broker', 'mqtt_credentials', 'mqtt_connection', 'entity_filter', 'render_proof']);
const STATUSES = new Set(['satisfied', 'blocked', 'in_flight', 'unknown', 'skipped']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const token = value => typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value) && !value.includes('\n');
const fail = () => { throw new Error('setup_response_invalid'); };

// Advisory only: setup completion is not proof of permissions, HA integration
// registration, installed APK identity or authority to change MQTT/configuration.
// Never retain panel names, dashboard paths, discovery prose or step details.
export function parseSetupObservation(bytes) {
  if (!(bytes instanceof Uint8Array) || !bytes.byteLength || bytes.byteLength > 32768) fail();
  let value;
  try {
    const text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes);
    value = JSON.parse(text);
  } catch { fail(); }
  if (!object(value) || typeof value.complete !== 'boolean' ||
      (value.repair !== undefined && typeof value.repair !== 'boolean') ||
      !(value.next === null || token(value.next)) || !Array.isArray(value.steps) ||
      !value.steps.length || value.steps.length > 32) fail();
  const seen = new Set();
  let hasUnknownStage = false;
  const steps = value.steps.map(step => {
    if (!object(step) || !token(step.stage) || seen.has(step.stage) ||
        !STATUSES.has(step.status) || typeof step.blocking !== 'boolean') fail();
    seen.add(step.stage);
    const known = STAGES.has(step.stage);
    if (!known) hasUnknownStage = true;
    return Object.freeze({stage: known ? step.stage : 'unsupported', status: step.status,
      blocking: step.blocking});
  });
  // Ignore additive fields, but do not call an unfamiliar journey understood.
  // Only a BLOCKED step is actionable; IN_FLIGHT and UNKNOWN never become repair.
  const nextStep = value.steps.find(step => step.stage === value.next);
  if (value.next !== null && (!nextStep || !['blocked', 'in_flight'].includes(nextStep.status))) fail();
  return Object.freeze({reportedComplete: value.complete, repair: value.repair ?? false,
    next: value.next !== null && STAGES.has(value.next) ? value.next : null,
    actionRequired: Boolean(nextStep && STAGES.has(nextStep.stage) && nextStep.status === 'blocked'),
    needsUpdatedClient: hasUnknownStage, steps: Object.freeze(steps)});
}
