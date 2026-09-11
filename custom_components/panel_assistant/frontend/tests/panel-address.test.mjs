import test from 'node:test';
import assert from 'node:assert/strict';
import { buildAddressProbe, parsePanelAddress, setupUrl } from '../src/panel-address.mjs';

const nonce = 'a'.repeat(32);
const framed = lines => `HAPANELD_ADDR_BEGIN:${nonce}\n${lines.join('\n')}\nHAPANELD_ADDR_END:${nonce}\n`;
const inet = (index, iface, address) =>
  `${index}: ${iface}    inet ${address} brd 0.0.0.0 scope global ${iface}\\       valid_lft forever preferred_lft forever`;

test('the panel is found at its private Wi-Fi address', () => {
  assert.equal(parsePanelAddress(framed([inet(3, 'wlan0', '172.16.5.20/24')]), nonce), '172.16.5.20');
  assert.equal(setupUrl('172.16.5.20'), 'http://172.16.5.20:8888/setup');
});

test('Wi-Fi is preferred over Ethernet, and both over anything else', () => {
  const both = framed([inet(2, 'eth0', '10.20.30.5/24'), inet(3, 'wlan0', '192.168.1.40/24')]);
  assert.equal(parsePanelAddress(both, nonce), '192.168.1.40');
  assert.equal(parsePanelAddress(framed([inet(2, 'eth0', '10.20.30.5/24')]), nonce), '10.20.30.5');
  assert.equal(parsePanelAddress(framed([inet(4, 'tun0', '10.8.0.2/24')]), nonce), null);
});

test('a public, reserved or malformed address never becomes a destination', () => {
  for (const address of ['8.8.8.8/24', '100.64.0.1/32', '169.254.1.2/16', '172.32.0.1/16',
    '172.15.9.9/16', '300.1.1.1/24', '192.168.1.1/40']) {
    assert.equal(parsePanelAddress(framed([inet(3, 'wlan0', address)]), nonce), null, address);
  }
});

test('output without this exact nonce is ignored', () => {
  const other = 'b'.repeat(32);
  const forged = `HAPANELD_ADDR_BEGIN:${other}\n${inet(3, 'wlan0', '192.168.1.40/24')}\nHAPANELD_ADDR_END:${other}\n`;
  assert.equal(parsePanelAddress(forged, nonce), null);
  assert.equal(parsePanelAddress(inet(3, 'wlan0', '192.168.1.40/24'), nonce), null);
  assert.equal(parsePanelAddress('x'.repeat(9000), nonce), null);
});

test('the probe is a fixed program and refuses an unexpected nonce', () => {
  assert.match(buildAddressProbe(nonce), /^echo HAPANELD_ADDR_BEGIN:a{32}; ip -4 -o addr show scope global/);
  assert.throws(() => buildAddressProbe('; rm -rf /'), /invalid_nonce/);
  assert.equal(setupUrl(null), null);
});
