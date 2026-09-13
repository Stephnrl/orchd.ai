const { test: base, expect } = require('@playwright/test');
const { spawn } = require('node:child_process');
const { mkdtemp, rm } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createInterface } = require('node:readline');

const test = base.extend({
  operator: async ({ page }, use) => {
    const data = await mkdtemp(path.join(tmpdir(), 'orchd-browser-'));
    const server = spawn(process.env.ORCH_TEST_PYTHON || 'python', [
      '-m', 'orch', 'serve', '--trusted-fixture', '--data', data, '--port', '0',
    ], { cwd: path.resolve(__dirname, '../..'), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    // Read only startup metadata; never print the session or server output.
    server.stderr.resume();
    const lines = createInterface({ input: server.stdout });
    const closed = new Promise(resolve => server.once('close', resolve));
    let startupTimer;
    try {
      const session = await new Promise((resolve, reject) => {
        let url;
        startupTimer = setTimeout(() => reject(new Error('Local test server startup timed out')), 10_000);
        server.once('error', () => reject(new Error('Cannot start Python; set ORCH_TEST_PYTHON')));
        server.once('exit', () => reject(new Error('Local test server exited during startup')));
        lines.on('line', line => {
          if (line.startsWith('Local API: ')) url = line.slice('Local API: '.length);
          if (url && line.startsWith('Operator session (this process only): ')) {
            resolve({ url, token: line.split(': ').at(-1) });
          }
        });
      });
      clearTimeout(startupTimer);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(session.url);
      await use(session);
      expect(errors).toEqual([]);
    } finally {
      clearTimeout(startupTimer);
      lines.close();
      server.kill();
      await closed;
      // Only remove the unique directory allocated by this fixture.
      await rm(data, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  },
});

async function connect(page, token) {
  await page.locator('#token').fill(token);
  await page.locator('#connect button').click();
  await expect(page.locator('#workspace')).toBeVisible();
  await expect(page.locator('#token')).toHaveValue('');
}

test('session rejection and disconnect clear access', async ({ page, operator }) => {
  await page.locator('#token').fill('invalid-test-session');
  await page.locator('#connect button').click();
  await expect(page.locator('#notice')).toContainText('Session expired');
  await expect(page.locator('#workspace')).toBeHidden();
  await connect(page, operator.token);
  await page.locator('#disconnect').click();
  await expect(page.locator('#login')).toBeVisible();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#token')).toHaveValue('');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('operator reviews evidence, refreshes, approves and inspects maintenance', async ({ page, operator }) => {
  await connect(page, operator.token);
  await page.locator('#title').fill('Browser regression fixture');
  await page.locator('#create button').click();
  await expect(page.locator('#task-title')).toHaveText('Browser regression fixture');
  await page.locator('#run').click();
  await expect(page.locator('#state')).toHaveText('AWAITING PLAN APPROVAL');
  await expect(page.locator('#approve')).toBeDisabled();
  await expect(page.locator('#run')).toBeDisabled();
  await page.locator('#approval-links button').first().click();
  await expect(page.locator('#evidence-json')).not.toBeEmpty();
  await expect(page.locator('#evidence-title')).not.toHaveText('Loading evidence…');
  await page.locator('#reviewed').check();
  await expect(page.locator('#approve')).toBeEnabled();
  await page.locator('#refresh-task').click();
  await expect(page.locator('#reviewed')).not.toBeChecked();
  await expect(page.locator('#approve')).toBeDisabled();
  await expect(page.locator('#task-refresh-status')).toContainText('Task refreshed.');
  await page.locator('#reviewed').check();
  await page.locator('#approve').click();
  await expect(page.locator('#approval')).toBeHidden();
  await expect(page.locator('#run')).toBeEnabled();
  // Approval must not itself run the next operation.
  await expect(page.locator('#state')).toHaveText('READY FOR IMPLEMENTATION');
  await page.locator('#run').click();
  await expect(page.locator('#state')).toHaveText('AWAITING ACTION APPROVAL');
  await page.locator('#storage-report').click();
  await expect(page.locator('#maintenance-status')).toContainText('Storage report ready');
  await expect(page.locator('#maintenance-summary')).toContainText('Artifact bytes:');
  await page.locator('#integrity-report').click();
  await expect(page.locator('#maintenance-status')).toHaveText('Integrity audit passed.');
  await expect(page.locator('#state')).toHaveText('AWAITING ACTION APPROVAL');
});
