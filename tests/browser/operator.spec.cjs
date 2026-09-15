const { test: base, expect } = require('@playwright/test');
const { spawn, execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
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

test('explicit evidence review checks and saves a selection without advancing', async ({ page, operator }) => {
  await connect(page, operator.token);
  await page.locator('#create button').click();
  await expect(page.locator('#task-id')).not.toBeEmpty();
  const task = await page.locator('#task-id').textContent();
  await page.locator('#run').click();
  await expect(page.locator('#state')).toHaveText('AWAITING PLAN APPROVAL');
  const plan = await readTask(page, operator, task);
  await expect(page.locator('#events li')).toHaveCount(plan.state.last_event_sequence);
  await page.locator('#reviewed').check();
  await page.locator('#approve').click();
  await expect(page.locator('#approval')).toBeHidden();
  await page.locator('#run').click();
  await expect(page.locator('#state')).toHaveText('AWAITING ACTION APPROVAL');
  const before = await readTask(page, operator, task);
  await expect(page.locator('#events li')).toHaveCount(before.state.last_event_sequence);
  await page.locator('#load-claims').click();
  await expect(page.locator('#claims-status')).toContainText('Choose a review and policy explicitly');
  await expect(page.locator('#assess-claims')).toBeDisabled();
  await page.locator('#claim-review').selectOption({index: 1});
  // The catalog also contains implementation/test policies; choose the action policy explicitly.
  const policyIds = await page.locator('#claim-policy option').evaluateAll(options => options.map(option => option.value).filter(Boolean));
  let actionPolicy;
  for (const id of policyIds) {
    const response = await page.request.get(`${operator.url}/tasks/${task}/records/${id}`, {headers: {Authorization: `Bearer ${operator.token}`}});
    if ((await response.json()).decision === 'require_human_approval') actionPolicy = id;
  }
  expect(actionPolicy).toBeTruthy();
  await page.locator('#claim-policy').selectOption(actionPolicy);
  await page.locator('#inspect-claim-policy').click();
  await expect(page.locator('#evidence-title')).toHaveText('ToolDecision');
  await expect(page.locator('#evidence-json')).toContainText(actionPolicy);
  await page.locator('#assess-claims').click();
  await expect(page.locator('#claims-status')).toContainText('Selected evidence claims are consistent');
  const downloaded = page.waitForEvent('download');
  await page.locator('#save-selection').click();
  const file = await downloaded;
  expect(file.suggestedFilename()).toBe('orchd-record-selection.json');
  const selection = JSON.parse(await readFile(await file.path(), 'utf8'));
  expect(selection.task_id).toBe(task);
  expect(Object.keys(selection).sort()).toEqual(['patch', 'policy', 'review', 'task_id', 'test']);
  const bundleDownload = page.waitForEvent('download');
  const bundleResponse = page.waitForResponse(response => response.url().endsWith('/evidence-bundle'));
  await page.locator('#download-bundle').click();
  const bundleFile = await bundleDownload, http = await bundleResponse;
  expect(bundleFile.suggestedFilename()).toBe('orchd-evidence-bundle.json');
  expect(http.status()).toBe(200);
  expect(http.headers()['cache-control']).toBe('no-store');
  expect(http.headers()['content-disposition']).toContain('attachment;');
  const bundlePath = await bundleFile.path(), bytes = await readFile(bundlePath);
  const sha = createHash('sha256').update(bytes).digest('hex');
  expect(http.headers()['x-orch-bundle-sha256']).toBe(sha);
  expect(Number(http.headers()['content-length'])).toBe(bytes.length);
  await expect(page.locator('#bundle-digest')).toContainText(sha);
  await page.locator('#bundle-file').setInputFiles(bundlePath);
  await page.locator('#bundle-expected').fill(sha);
  await page.locator('#verify-bundle').click();
  await expect(page.locator('#bundle-review-status')).toContainText('retained claims are consistent');
  const assessmentDownload = page.waitForEvent('download');
  await page.locator('#save-bundle-review').click();
  const assessmentFile = await assessmentDownload;
  expect(assessmentFile.suggestedFilename()).toBe('orchd-bundle-assessment.json');
  const assessment = JSON.parse(await readFile(await assessmentFile.path(), 'utf8'));
  expect(assessment.binding.bundle_sha256).toBe(sha);
  expect(assessment.live_authorized).toBe(false);
  const verified = JSON.parse(execFileSync(process.env.ORCH_TEST_PYTHON || 'python', ['-c', 'import json,sys; from orch.github_bundle import verify_bundle; print(json.dumps(verify_bundle(sys.argv[1],sys.argv[2])))', bundlePath, sha], {encoding: 'utf8', windowsHide: true}));
  expect(verified.status).toBe('claims_consistent');
  expect(verified.live_authorized).toBe(false);
  expect(await readTask(page, operator, task)).toEqual(before);
  // Save a review image when requested, without the session input or token.
  if (process.env.ORCH_REVIEW_SCREENSHOT) {
    const panel = page.locator('section[aria-labelledby="claims-title"]');
    await panel.screenshot({path: process.env.ORCH_REVIEW_SCREENSHOT});
    await page.setViewportSize({width: 390, height: 844});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({path: process.env.ORCH_REVIEW_SCREENSHOT.replace('.png', '-mobile.png')});
  }
  await page.locator('#refresh-task').click();
  await expect(page.locator('#save-selection')).toBeDisabled();
  await expect(page.locator('#download-bundle')).toBeDisabled();
  await expect(page.locator('#bundle-digest')).toBeEmpty();
  await expect(page.locator('#claim-review')).toHaveValue('');
  await expect(page.locator('#claims-details')).toBeHidden();
});

test('received bundle review works in an empty store and rejects changed bytes', async ({page, operator}) => {
  const bundle = execFileSync(process.env.ORCH_TEST_PYTHON || 'python', ['-c', [
    'import sys; sys.path.insert(0,"tests")',
    'from unittest.mock import patch',
    'from test_github_evidence_claims import EvidenceClaimsTests',
    'from orch.github_bundle import build_bundle',
    'fixture=EvidenceClaimsTests(); fixture.setUp()',
    'try:',
    ' fixture.persist()',
    ' with patch("orch.github_evidence.now", return_value="2026-09-12T14:01:00Z"):',
    '  raw,report=build_bundle(fixture.store,fixture.selection())',
    ' sys.stdout.buffer.write(raw)',
    'finally: fixture.tearDown()',
  ].join('\n')], {windowsHide: true});
  const sha = createHash('sha256').update(bundle).digest('hex');
  await connect(page, operator.token);
  await expect(page.locator('#tasks button')).toHaveCount(0);
  await page.locator('#bundle-file').setInputFiles({name:'received.json', mimeType:'application/json', buffer:bundle});
  await page.locator('#bundle-expected').fill(sha);
  const response = page.waitForResponse(r => r.url().endsWith('/evidence-bundles/verify'));
  await page.locator('#verify-bundle').click();
  const received = await response;
  expect(received.status()).toBe(200);
  expect(received.headers()['cache-control']).toBe('no-store');
  await expect(page.locator('#bundle-review-status')).toContainText('retained claims are blocked');
  await expect(page.locator('#bundle-review-json')).toContainText('policy_not_current');
  const tasks = await page.request.get(`${operator.url}/tasks`, {headers:{Authorization:`Bearer ${operator.token}`}});
  expect((await tasks.json()).items).toEqual([]);
  if (process.env.ORCH_REVIEW_SCREENSHOT) {
    const panel = page.locator('section[aria-labelledby="bundle-review-title"]');
    await panel.screenshot({path:process.env.ORCH_REVIEW_SCREENSHOT.replace('.png','-received.png')});
    await page.setViewportSize({width:390,height:844});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({path:process.env.ORCH_REVIEW_SCREENSHOT.replace('.png','-received-mobile.png')});
  }
  let uploads = 0;
  page.on('request', request => { if (request.url().endsWith('/evidence-bundles/verify')) uploads++; });
  await page.locator('#bundle-file').setInputFiles({name:'changed.json',mimeType:'application/json',buffer:Buffer.concat([bundle,Buffer.from(' ')])});
  await expect(page.locator('#save-bundle-review')).toBeDisabled();
  await page.locator('#verify-bundle').click();
  await expect(page.locator('#bundle-review-status')).toContainText('Nothing was uploaded');
  expect(uploads).toBe(0);
  await page.locator('#clear-bundle-review').click();
  await expect(page.locator('#bundle-expected')).toHaveValue('');
  await expect(page.locator('#bundle-review-details')).toBeHidden();
});

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

test('rotation retains this tab, invalidates old tokens and revocation ends access', async ({ page, operator }) => {
  await connect(page, operator.token);
  await page.locator('#session-check').click();
  await expect(page.locator('#session-status')).toContainText('generation 1');
  const old = operator.token;
  const rotated = page.waitForResponse(response => response.url().endsWith('/session/rotate'));
  await page.locator('#session-rotate').click();
  operator.token = (await (await rotated).json()).session;
  await expect(page.locator('#session-status')).toContainText('generation 2');
  await expect(page.locator('#workspace')).toBeVisible();
  await expect(page.locator('#token')).toHaveValue('');
  const rejected = await page.request.get(operator.url+'/tasks', {headers:{Authorization:'Bearer '+old}});
  expect(rejected.status()).toBe(401);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await page.locator('#session-revoke').click();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#login')).toBeVisible();
  await expect(page.locator('#notice')).toContainText('Server session ended');
  expect((await page.request.get(operator.url+'/session', {headers:{Authorization:'Bearer '+operator.token}})).status()).toBe(401);
});

test('revocation in another client clears displayed evidence on the next request', async ({ page, operator }) => {
  await connect(page, operator.token);
  await page.locator('#create button').click();
  await expect(page.locator('#detail')).toBeVisible();
  await page.locator('#records button').first().click();
  await expect(page.locator('#evidence-json')).not.toBeEmpty();
  const response = await page.request.post(operator.url+'/session/revoke', {headers:{Authorization:'Bearer '+operator.token}, data:{}});
  expect(response.status()).toBe(200);
  await page.locator('#session-check').click();
  await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#evidence-json')).toBeEmpty();
  await expect(page.locator('#reviewed')).not.toBeChecked();
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

test('batch UI prepares two tasks, requires review and stops at individual approvals', async ({page,operator}) => {
  await connect(page,operator.token);
  const ids=[];
  for(const title of ['Batch <script>literal</script>','Second batch fixture']) {
    await page.locator('#title').fill(title);await page.locator('#create button').click();
    await expect(page.locator('#task-title')).toHaveText(title);
    ids.push(await page.locator('#task-id').textContent());
    await page.locator('#batch-add').click();
  }
  await expect(page.locator('#batch-draft li')).toHaveCount(2);
  await expect(page.locator('#batch-draft')).toContainText('Batch <script>literal</script>');
  await page.locator('#batch-create').click();
  await expect(page.locator('#batch-detail')).toBeVisible();
  await expect(page.locator('#batch-entries li')).toHaveCount(2);
  for(const task of ids) expect((await readTask(page,operator,task)).state.state).toBe('SPEC_READY');
  await expect(page.locator('#batch-run')).toBeDisabled();
  await page.locator('#batch-reviewed').check();await expect(page.locator('#batch-run')).toBeEnabled();
  await page.locator('#batch-refresh').click();await expect(page.locator('#batch-reviewed')).not.toBeChecked();
  await expect(page.locator('#batch-status')).toContainText('Inspection loaded');
  await page.locator('#batch-reviewed').check();await page.locator('#batch-run').click();
  await expect(page.locator('#batch-entries')).toContainText('AWAITING_PLAN_APPROVAL');
  await expect(page.locator('#batch-reviewed')).not.toBeChecked();await expect(page.locator('#batch-run')).toBeDisabled();
  for(const task of ids) expect((await readTask(page,operator,task)).state.state).toBe('AWAITING_PLAN_APPROVAL');
  await operator.restart();await connect(page,operator.token);
  await page.locator('#batch-panel summary').first().click();await page.locator('#batch-list-refresh').click();
  await page.locator('#batch-list button').first().click();await expect(page.locator('#batch-entries li')).toHaveCount(2);
  await expect(page.locator('#batch-run')).toBeDisabled();
});

test('batch UI inspects stale reservation and abandons tracking without advancing the task', async ({page,operator}) => {
  await connect(page,operator.token);await page.locator('#create button').click();
  await expect(page.locator('#batch-add')).toBeEnabled();const task=await page.locator('#task-id').textContent();
  await page.locator('#batch-add').click();await page.locator('#batch-create').click();
  await expect(page.locator('#batch-status')).toContainText('Inspection loaded');
  const response=await page.request.post(`${operator.url}/tasks/${task}/advance`,{headers:{Authorization:'Bearer '+operator.token},data:{}});
  expect(response.status()).toBe(200);const before=await readTask(page,operator,task);
  await page.locator('#batch-reviewed').check();await page.locator('#batch-run').click();
  await expect(page.locator('#batch-entries')).toContainText('running');await expect(page.locator('#batch-run')).toBeDisabled();
  await page.locator('#batch-entries button').first().click();
  await page.locator('#batch-abandon-task').selectOption(task);await expect(page.locator('#batch-abandon')).toBeDisabled();
  await page.locator('#batch-abandon-reviewed').check();await page.locator('#batch-abandon').click();
  await expect(page.locator('#batch-entries')).toContainText('abandoned');
  expect(await readTask(page,operator,task)).toEqual(before);
});

test('session revocation clears batch drafts and inspected scope', async ({page,operator}) => {
  await connect(page,operator.token);await page.locator('#create button').click();await expect(page.locator('#batch-add')).toBeEnabled();
  await page.locator('#batch-add').click();await page.locator('#batch-create').click();await expect(page.locator('#batch-json')).not.toBeEmpty();
  await page.locator('#batch-add').click();await expect(page.locator('#batch-draft li')).toHaveCount(1);
  await page.locator('#session-revoke').click();await expect(page.locator('#workspace')).toBeHidden();
  await expect(page.locator('#batch-json')).toBeEmpty();await expect(page.locator('#batch-draft li')).toHaveCount(0);
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

test('repository task uses execution approval and local-result sign-off across restart', async ({ page, operator }) => {
  const { writeFile } = require('node:fs/promises');
  const repo = await mkdtemp(path.join(tmpdir(), 'orchd-repository-browser-'));
  const gitEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('GIT_')));
  gitEnv.GIT_CONFIG_NOSYSTEM = '1';
  gitEnv.GIT_CONFIG_GLOBAL = process.platform === 'win32' ? 'NUL' : '/dev/null';
  const git = (...args) => execFileSync('git', ['-c', 'core.autocrlf=false', '-c', 'core.hooksPath=' + gitEnv.GIT_CONFIG_GLOBAL, '-C', repo, ...args], {env: gitEnv, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']}).toString().trim();
  try {
    git('init');
    await writeFile(path.join(repo, 'settings.json'), '{"retries":1}\n');
    git('add', '.');
    git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Disposable baseline');
    const response = await page.request.post(`${operator.url}/repository-tasks`, {
      headers: {Authorization: `Bearer ${operator.token}`},
      data: {title: 'Repository JSON task', intent: {repository: repo, base_commit: git('rev-parse', 'HEAD'),
        replacements: {'settings.json': '{"retries":3}\n'}, checks: [{path: 'settings.json', keys: ['retries'], equals: 3}]}},
    });
    expect(response.status()).toBe(201);
    const task = (await response.json()).state.task_id;
    await connect(page, operator.token);
    await page.locator('#tasks button').click();
    await expect(page.locator('#approval-summary')).toContainText('Approve exact repository pilot execution');
    let state = await readTask(page, operator, task);
    await expect(page.locator('#events li')).toHaveCount(state.state.last_event_sequence);
    await page.locator('#reviewed').check();
    await page.locator('#approve').click();
    await expect(page.locator('#state')).toHaveText('READY FOR IMPLEMENTATION');
    await expect(page.locator('#batch-add')).toBeDisabled();
    await page.locator('#run').click();
    await expect(page.locator('#state')).toHaveText('AWAITING ACTION APPROVAL');
    await expect(page.locator('#approval-summary')).toContainText('Accept validated local snapshot; no remote action');
    state = await readTask(page, operator, task);
    await expect(page.locator('#events li')).toHaveCount(state.state.last_event_sequence);
    await page.locator('#reviewed').check();
    await page.locator('#approve').click();
    await expect(page.locator('#state')).toHaveText('LOCAL SNAPSHOT ACCEPTED');
    await expect(page.locator('#run')).toBeDisabled();
    const completed = await readTask(page, operator, task);
    expect(completed.context.external).toBeUndefined();
    await operator.restart();
    await connect(page, operator.token);
    await page.locator('#tasks button').click();
    await expect(page.locator('#state')).toHaveText('LOCAL SNAPSHOT ACCEPTED');
    expect(await readTask(page, operator, task)).toEqual(completed);
    expect(await readFile(path.join(repo, 'settings.json'), 'utf8')).toBe('{"retries":1}\n');
  } finally {
    if (path.dirname(repo) !== path.resolve(tmpdir()) || !path.basename(repo).startsWith('orchd-repository-browser-')) throw new Error('Unexpected disposable repository path');
    await rm(repo, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
  }
});
