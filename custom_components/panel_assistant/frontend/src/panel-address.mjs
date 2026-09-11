import { readShell } from './shell-session.mjs';

// Where to send the browser once the app is running: the panel's own setup
// wizard, served by the app on the local network. The address is read over the
// existing USB session with a fixed program. Only a private IPv4 on a Wi-Fi or
// Ethernet interface is accepted; anything else returns null and the person
// finishes setup on the panel's own screen instead.

const LINE = /^\d+:\s+([a-z][a-z0-9]{0,14})\s+inet\s+(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})\s/;
const PREFERENCE = [/^wlan\d+$/, /^eth\d+$/];
export const SETUP_PORT = 8888;

function isPrivate([a, b]) {
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
}

export function buildAddressProbe(nonce) {
  if (!/^[0-9a-f]{32}$/.test(nonce)) throw new Error('invalid_nonce');
  return `echo HAPANELD_ADDR_BEGIN:${nonce}; ip -4 -o addr show scope global 2>/dev/null; ` +
    `echo HAPANELD_ADDR_END:${nonce}`;
}

export function parsePanelAddress(text, nonce) {
  if (typeof text !== 'string' || text.length > 8192) return null;
  const begin = text.indexOf(`HAPANELD_ADDR_BEGIN:${nonce}\n`);
  const end = text.indexOf(`HAPANELD_ADDR_END:${nonce}`);
  if (begin < 0 || end < begin) return null;
  const body = text.slice(begin + `HAPANELD_ADDR_BEGIN:${nonce}\n`.length, end);
  const found = [];
  for (const line of body.split('\n')) {
    const match = LINE.exec(line.trim() + ' ');
    if (!match) continue;
    const octets = match.slice(2, 6).map(Number);
    if (octets.some(value => value > 255) || Number(match[6]) > 32 || !isPrivate(octets)) continue;
    found.push({ iface: match[1], address: octets.join('.') });
  }
  for (const preferred of PREFERENCE) {
    const hit = found.find(entry => preferred.test(entry.iface));
    if (hit) return hit.address;
  }
  return null;
}

export function setupUrl(address) {
  return address ? `http://${address}:${SETUP_PORT}/setup` : null;
}

export async function readSetupUrl(adb, newNonce) {
  const nonce = newNonce();
  try {
    const bytes = await readShell(adb, buildAddressProbe(nonce), { maximum: 8192, timeoutMs: 5000 });
    return setupUrl(parsePanelAddress(new TextDecoder().decode(bytes), nonce));
  } catch {
    // Not knowing the address is never a failure: setup continues on the panel.
    return null;
  }
}
