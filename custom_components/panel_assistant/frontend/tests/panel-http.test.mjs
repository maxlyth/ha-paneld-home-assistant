import test from 'node:test';
import assert from 'node:assert/strict';
import { PANEL_HTTP_SERVICE } from '../src/panel-http.mjs';
import { readUsbHealth } from '../src/usb-health.mjs';
import { readUsbSetup } from '../src/usb-setup.mjs';

// A panel whose adbd only accepts the NUL-terminated form, as hall-class adbd does.
function strictPanel(response) {
  const opened = [];
  return {
    opened,
    async createSocket(service) {
      opened.push(service);
      if (service !== 'tcp:8888\0') throw new Error('Socket open failed');
      const bytes = new TextEncoder().encode(response);
      return {
        writable: new WritableStream(),
        readable: new ReadableStream({ start(c) { c.enqueue(bytes); c.close(); } }),
        close: async () => {},
      };
    },
  };
}

const health = 'ha-paneld 1.2.3 panel=hall_panel build=42 cfg=0123abcd\n';
const ok = body => `HTTP/1.1 200 OK\r\nContent-Length: ${body.length}\r\n\r\n${body}`;

test('the app port is opened NUL-terminated, the way host adb opens it', () => {
  assert.equal(PANEL_HTTP_SERVICE, 'tcp:8888\0');
});

test('the health read reaches a panel that refuses an unterminated service name', async () => {
  const adb = strictPanel(ok(health));
  assert.equal(await readUsbHealth(adb, { versionName: '1.2.3' }, { quarantine: () => {} }), true);
  assert.deepEqual(adb.opened, [PANEL_HTTP_SERVICE]);
});

test('the setup read opens the same service as the health read', async () => {
  const adb = strictPanel(ok('{}'));
  await readUsbSetup(adb, { quarantine: () => {} }).catch(() => {});
  assert.deepEqual(adb.opened, [PANEL_HTTP_SERVICE]);
});
