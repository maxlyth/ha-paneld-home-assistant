import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildStagedPreparation, parseStagedPreparation, stagingPath } from '../src/staging-contract.mjs';
import { INSTALL_MESSAGES } from '../src/install-view.mjs';
import { INSTALL_SCREEN_MESSAGES } from '../src/install-screen-messages.mjs';

const nonce = 'c'.repeat(32);
const job = 'd'.repeat(32);

test('preparation sets exactly 0644 on this job\'s own regular file and nothing else', () => {
  const program = buildStagedPreparation(nonce, job);
  const path = stagingPath(job);
  assert.ok(program.includes(`if [ -f ${path} ] && [ ! -L ${path} ]; then chmod 0644 ${path}; fi`));
  assert.equal((program.match(/chmod/g) ?? []).length, 1, 'one chmod, on one fixed path');
  assert.ok(!/rm |mv |cp |>/.test(program), 'preparation never moves, copies or writes content');
  assert.throws(() => buildStagedPreparation(nonce, '../../etc'), /invalid_request/);
});

test('preparation succeeds only on a clean, correctly framed zero exit', () => {
  const ok = `HAPANELD_PREPARE_BEGIN:${nonce}\nHAPANELD_PREPARE_END:${nonce}:0\n`;
  assert.equal(parseStagedPreparation(ok, nonce), true);
  for (const body of [
    `HAPANELD_PREPARE_BEGIN:${nonce}\nHAPANELD_PREPARE_END:${nonce}:1\n`,
    `HAPANELD_PREPARE_BEGIN:${nonce}\nchmod: Operation not permitted\nHAPANELD_PREPARE_END:${nonce}:0\n`,
    `HAPANELD_PREPARE_BEGIN:${'e'.repeat(32)}\nHAPANELD_PREPARE_END:${nonce}:0\n`,
  ]) {
    assert.throws(() => parseStagedPreparation(body, nonce), /staged_preparation_failed/);
  }
});

test('both staged-file checks prepare the file before reading its mode', () => {
  const source = readFileSync(new URL('../src/usb-transaction-ports.mjs', import.meta.url), 'utf8');
  for (const name of ['staged', 'prefix']) {
    const start = source.indexOf(`const ${name} = async`);
    const body = source.slice(start, source.indexOf('};', start));
    assert.ok(start >= 0, `${name} exists`);
    assert.ok(body.indexOf('await prepare(receipt)') >= 0, `${name} prepares first`);
    assert.ok(body.indexOf('await prepare(receipt)') < body.indexOf('buildStagedObservation'),
      `${name} prepares before it observes`);
  }
});

test('no message ever tells a person to unplug a panel that may be powered by that cable', () => {
  const shown = [...Object.values(INSTALL_MESSAGES), ...Object.values(INSTALL_SCREEN_MESSAGES)].join('\n');
  assert.ok(!/unplug/i.test(shown), 'unplugging a USB-powered panel cuts its power mid-install');
});
