const { test: base, expect } = require('@playwright/test');
const { spawn } = require('node:child_process');
const { mkdtemp, rm, readFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createInterface } = require('node:readline');

const test = base.extend({
  operator: async ({ page }, use) => {
    const data = await mkdtemp(path.join(tmpdir(), 'orchd-browser-'));
    let server, lines, closed, startupTimer;
    async function stop() {
      clearTimeout(startupTimer);
      lines?.close();
      if (server) {
        server.kill();
        await closed;
        server = null;
      }
    }
    async function start() {
      server = spawn(process.env.ORCH_TEST_PYTHON || 'python', [
        '-m', 'orch', 'serve', '--trusted-fixture', '--data', data, '--port', '0',
      ], { cwd: path.resolve(__dirname, '../..'), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      // Read only startup metadata; never print the session or server output.
      server.stderr.resume();
      lines = createInterface({ input: server.stdout });
      closed = new Promise(resolve => server.once('close', resolve));
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
      return session;
    }
    try {
      const session = await start();
      session.restart = async () => {
        // Stop browser polling before replacing the process; preserve this test's store.
        await page.goto('about:blank');
        await stop();
        Object.assign(session, await start());
        await page.goto(session.url);
      };
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(session.url);
      await use(session);
      expect(errors).toEqual([]);
    } finally {
      await stop();
      // Only remove the unique directory allocated by this fixture.
      await rm(data, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  },
});

async function readTask(page, operator, task) {
  const response = await page.request.get(`${operator.url}/tasks/${task}`, {
    headers: { Authorization: `Bearer ${operator.token}` },
  });
  expect(response.status()).toBe(200);
  return response.json();
}

for (const outcome of ['COMPLETED', 'CANCELLED']) {
  test(`${outcome.toLowerCase()} task survives server restart with evidence and replay`, async ({ page, operator }) => {
    await connect(page, operator.token);
    await page.locator('#create button').click();
    await expect(page.locator('#task-id')).not.toBeEmpty();
    const task = await page.locator('#task-id').textContent();
    await page.locator('#run').click();
    await expect(page.locator('#state')).toHaveText('AWAITING PLAN APPROVAL');
    if (outcome === 'COMPLETED') {
      for (const next of ['AWAITING ACTION APPROVAL', 'COMPLETED']) {
        // Replay refreshes intentionally clear review; wait for this pause's events first.
        const pause = await readTask(page, operator, task);
        await expect(page.locator('#events li')).toHaveCount(pause.state.last_event_sequence);
        await expect(page.locator('#task-refresh-status')).toContainText('Task refreshed.');
        await page.locator('#reviewed').check();
        await page.locator('#approve').click();
        await expect(page.locator('#approval')).toBeHidden();
        await page.locator('#run').click();
        await expect(page.locator('#state')).toHaveText(next);
      }
    } else {
      await page.locator('#cancellation summary').click();
      await page.locator('#cancel-reason').fill('Stop this disposable browser fixture');
      await page.locator('#cancel-task').click();
      await expect(page.locator('#state')).toHaveText(outcome);
    }
    const before = await readTask(page, operator, task);
    await expect(page.locator('#events li')).toHaveCount(before.state.last_event_sequence);
    const events = await page.locator('#events button').allTextContents();
    await page.locator('#records button').first().click();
    await expect(page.locator('#evidence-json')).not.toBeEmpty();
    const evidence = await page.locator('#evidence-json').textContent();
    await page.locator('#events button').last().click();
    await expect(page.locator('#evidence-title')).toHaveText('Artifact content');
    await expect(page.locator('#evidence-json')).not.toBeEmpty();
    const eventPayload = await page.locator('#evidence-json').textContent();
    if (outcome === 'CANCELLED') expect(eventPayload).toContain('Stop this disposable browser fixture');
    const oldToken = operator.token;

    await operator.restart();
    await page.locator('#token').fill(oldToken);
    await page.locator('#connect button').click();
    await expect(page.locator('#notice')).toContainText('Session expired');
    await expect(page.locator('#workspace')).toBeHidden();
    await connect(page, operator.token);
    await page.locator('#tasks button').click();
    await expect(page.locator('#task-id')).toHaveText(task);
    await expect(page.locator('#state')).toHaveText(outcome);
    await expect(page.locator('#run')).toBeDisabled();
    await expect(page.locator('#approval')).toBeHidden();
    await expect(page.locator('#cancellation')).toBeHidden();
    await expect(page.locator('#events button')).toHaveText(events);
    await page.locator('#records button').first().click();
    await expect(page.locator('#evidence-json')).toHaveText(evidence);
    await page.locator('#events button').last().click();
    await expect(page.locator('#evidence-json')).toHaveText(eventPayload);
    // Reconnection and replay must not advance the workflow or rewrite its context.
    expect(await readTask(page, operator, task)).toEqual(before);
  });
}

async function connect(page, token) {
  await page.locator('#token').fill(token);
  await page.locator('#connect button').click();
  await expect(page.locator('#workspace')).toBeVisible();
  await expect(page.locator('#token')).toHaveValue('');
}

test('task titles remain literal and duplicate titles select distinct tasks', async ({ page, operator }) => {
  await connect(page, operator.token);
  const title = '<img src=x onerror=alert(1)> Same title';
  const ids = [];
  for (let index = 0; index < 2; index++) {
    await page.locator('#title').fill(title);
    const created = page.waitForResponse(response => response.url().endsWith('/tasks') && response.request().method() === 'POST');
    await page.locator('#create button').click();
    ids.push((await (await created).json()).task_id);
    await expect(page.locator('#task-id')).toHaveText(ids[index]);
    await expect(page.locator('#tasks button')).toHaveCount(index + 1);
  }
  await expect(page.locator('#tasks img')).toHaveCount(0);
  for (let index = 0; index < 2; index++) {
    const entry = page.locator('#tasks button').nth(index);
    await expect(entry).toContainText(title);
    await expect(entry.locator('small')).toContainText(ids[index].slice(0, 12));
    await expect(entry.locator('small')).toContainText('SPEC READY');
    await entry.click();
    await expect(page.locator('#task-id')).toHaveText(ids[index]);
    await expect(page.locator('#task-title')).toHaveText(title);
    await expect(entry).toHaveAttribute('aria-current', 'true');
  }
});

test('displayed evidence downloads match the view and failed reads disable saving', async ({ page, operator }, testInfo) => {
  await connect(page, operator.token);
  await page.locator('#create button').click();
  await expect(page.locator('#task-id')).not.toBeEmpty();
  const task = await page.locator('#task-id').textContent();
  const before = await readTask(page, operator, task);
  for (const [selector, filename] of [['#records button', 'orchd-record.json'], ['#events button', 'orchd-artifact.txt']]) {
    await page.locator(selector).first().click();
    await expect(page.locator('#save-evidence')).toBeEnabled();
    const displayed = await page.locator('#evidence-json').textContent();
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#save-evidence').click();
    const download = await downloadEvent;
    expect(download.suggestedFilename()).toBe(filename);
    const destination = testInfo.outputPath(filename);
    await download.saveAs(destination);
    expect(await readFile(destination, 'utf8')).toBe(displayed);
  }
  await page.route('**/records/*', route => route.fulfill({status: 409, contentType: 'application/json', body: '{"error":"Read unavailable"}'}));
  await page.locator('#records button').first().click();
  await expect(page.locator('#evidence-title')).toContainText('Evidence unavailable');
  await expect(page.locator('#save-evidence')).toBeDisabled();
  expect(await readTask(page, operator, task)).toEqual(before);
});

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

test('event history reports an outage and retries without duplicate events', async ({ page, operator }) => {
  await connect(page, operator.token);
  await page.locator('#create button').click();
  await expect(page.locator('#task-id')).not.toBeEmpty();
  const task = await page.locator('#task-id').textContent();
  const snapshot = await readTask(page, operator, task);
  await expect(page.locator('#events li')).toHaveCount(snapshot.state.last_event_sequence);
  const labels = await page.locator('#events button').allTextContents();
  await page.route('**/stream', route => route.abort());
  await expect(page.locator('#event-status')).toContainText('Event history update failed.');
  await expect(page.locator('#events button')).toHaveText(labels);
  await page.unroute('**/stream');
  await expect(page.locator('#event-status')).toHaveText(`Replayed through event ${snapshot.state.last_event_sequence}`);
  await expect(page.locator('#events button')).toHaveText(labels);
  expect(await readTask(page, operator, task)).toEqual(snapshot);
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
