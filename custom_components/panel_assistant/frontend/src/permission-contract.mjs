// Fixed, explicitly confirmed post-install grants for the authenticated APK.
// Nothing here configures MQTT, networking, device ownership or app data.
const PACKAGE = 'io.github.maxlyth.hapaneld';
export const ACCESSIBILITY_SERVICE = `${PACKAGE}/.input.PanelAccessibilityService`;
const FULL_SERVICE = `${PACKAGE}/${PACKAGE}.input.PanelAccessibilityService`;
const fail = () => { throw new Error('permissions_unverified'); };
const checkNonce = nonce => { if (!/^[a-f0-9]{32}$/.test(nonce)) fail(); };
const checkSdk = sdk => { if (!Number.isInteger(sdk) || sdk < 1 || sdk > 100) fail(); };

function validateServices(value) {
  if (typeof value !== 'string' || value.length > 4096) fail();
  if (value === '' || value === 'null') return [];
  const services = value.split(':');
  if (services.length > 64 || new Set(services).size !== services.length ||
      services.some(service => !/^[A-Za-z][A-Za-z0-9_.]*\/[A-Za-z.][A-Za-z0-9_.$]*$/.test(service))) fail();
  return services;
}

function frame(body, nonce, kind) {
  checkNonce(nonce);
  if (typeof body !== 'string' || body.length > 16384 || /[^\x20-\x7e\r\n\t]/.test(body)) fail();
  const lines = body.replaceAll('\r\n', '\n').split('\n');
  if (lines.shift() !== `HAPANELD_${kind}_BEGIN:${nonce}` || lines.pop() !== '' ||
      lines.pop() !== `HAPANELD_${kind}_END:${nonce}:0` || lines.some(line => line.includes('HAPANELD_'))) fail();
  return lines;
}

export function buildPermissionRead(nonce) {
  checkNonce(nonce);
  return `echo HAPANELD_PERMISSIONS_READ_BEGIN:${nonce}; settings get secure enabled_accessibility_services; echo HAPANELD_PERMISSIONS_READ_END:${nonce}:$?`;
}

export function parsePermissionRead(body, nonce) {
  const lines = frame(body, nonce, 'PERMISSIONS_READ');
  if (lines.length !== 1) fail();
  validateServices(lines[0]);
  return lines[0];
}

export function expectedServices(existing) {
  const services = validateServices(existing);
  return services.some(service => [ACCESSIBILITY_SERVICE, FULL_SERVICE].includes(service))
    ? existing : [...services, ACCESSIBILITY_SERVICE].join(':');
}

export function buildPermissionGrant(nonce, sdk, existing) {
  checkNonce(nonce); checkSdk(sdk);
  const expected = expectedServices(existing);
  // Validated component characters cannot escape single-quoted shell literals.
  // Check again immediately before the list write: unrelated services must not
  // disappear if another setup operation changed this device-wide setting.
  return `echo HAPANELD_PERMISSIONS_GRANT_BEGIN:${nonce}; (
current=$(settings get secure enabled_accessibility_services) || exit 1
[ "$current" = '${existing}' ] || exit 1
${sdk >= 33 ? `pm grant ${PACKAGE} android.permission.POST_NOTIFICATIONS || exit 1` : ':'}
appops set ${PACKAGE} WRITE_SETTINGS allow || exit 1
appops set ${PACKAGE} SYSTEM_ALERT_WINDOW allow || exit 1
current=$(settings get secure enabled_accessibility_services) || exit 1
[ "$current" = '${existing}' ] || exit 1
${expected === existing ? ':' : `settings put secure enabled_accessibility_services '${expected}' || exit 1`}
settings put secure accessibility_enabled 1 || exit 1
); echo HAPANELD_PERMISSIONS_GRANT_END:${nonce}:$?`;
}

export function parsePermissionGrant(body, nonce) {
  if (frame(body, nonce, 'PERMISSIONS_GRANT').length !== 0) fail();
}

export function buildPermissionVerification(nonce, sdk) {
  checkNonce(nonce); checkSdk(sdk);
  return `echo HAPANELD_PERMISSIONS_VERIFY_BEGIN:${nonce}; (
settings get secure enabled_accessibility_services || exit 1
settings get secure accessibility_enabled || exit 1
appops get ${PACKAGE} WRITE_SETTINGS || exit 1
appops get ${PACKAGE} SYSTEM_ALERT_WINDOW || exit 1
${sdk >= 33 ? `dumpsys package ${PACKAGE} | grep 'android.permission.POST_NOTIFICATIONS: granted=' || exit 1` : "echo not_required"}
); echo HAPANELD_PERMISSIONS_VERIFY_END:${nonce}:$?`;
}

export function parsePermissionVerification(body, nonce, sdk, existing) {
  checkSdk(sdk);
  const lines = frame(body, nonce, 'PERMISSIONS_VERIFY');
  if (lines.length !== 5 || lines[0] !== expectedServices(existing) || lines[1] !== '1' ||
      !/^WRITE_SETTINGS: allow(?:; [\x20-\x7e]{1,1024})?$/.test(lines[2]) ||
      !/^SYSTEM_ALERT_WINDOW: allow(?:; [\x20-\x7e]{1,1024})?$/.test(lines[3]) ||
      (sdk >= 33 ? !/^\s*android\.permission\.POST_NOTIFICATIONS: granted=true, flags=\[[ A-Z0-9_|]*\]\s*$/.test(lines[4])
        : lines[4] !== 'not_required')) fail();
  return Object.freeze({permissionsVerified: true, notificationsRequired: sdk >= 33});
}
