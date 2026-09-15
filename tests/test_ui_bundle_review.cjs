const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const nodes = new Map(), pending = [], waiters = new Set();
function node() { return {value: '', textContent: '', handlers: {}, append() {}, replaceChildren() {}, querySelector() { return node(); }, addEventListener(name, fn) { this.handlers[name] = fn; }}; }
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const context = vm.createContext({document: {getElementById: element, createElement: node}, setInterval() {}, Date, Map, Set, Uint8Array, crypto: webcrypto, AbortController, fetch(route, options) { return new Promise(resolve => { pending.push({route, options, resolve}); for (const waiter of [...waiters]) waiter(); }); }});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../orch/ui/app.js'), 'utf8'), context);
const bytes = new TextEncoder().encode('{}');
const sha = createHash('sha256').update(bytes).digest('hex');
const file = {size: bytes.length, arrayBuffer: async () => bytes.buffer};
const report = {kind: 'GitHubEvidenceBundleAssessment', binding: {bundle_sha256: sha}, status: 'blocked', evidence_verified: false, live_authorized: false};
const click = () => element('verify-bundle').handlers.click();
function ready() {
  vm.runInContext('token="session"; selected=null; snapshot=null;', context);
  element('bundle-file').files = [file]; element('bundle-expected').value = sha;
  element('bundle-expected').handlers.input();
}
// The click path awaits crypto.subtle.digest on the libuv threadpool before it calls
// fetch, so tick polling is racy. Wait for the mocked fetch to register the request
// and keep only a wall-clock bound as the failure timeout.
function fetched(count) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { waiters.delete(check); reject(new Error(`Expected ${count} fetch calls, observed ${pending.length}`)); }, 5000);
    function check() { if (pending.length >= count) { clearTimeout(timer); waiters.delete(check); resolve(); } }
    waiters.add(check); check();
  }).then(() => assert.equal(pending.length, count));
}
(async () => {
  ready(); assert.equal(element('verify-bundle').disabled, false);
  element('bundle-file').files = [{...file, size: 16777217}];
  await click(); assert.equal(pending.length, 0);
  ready(); element('bundle-expected').value = 'a'.repeat(64);
  await click(); assert.equal(pending.length, 0);
  assert.match(element('bundle-review-status').textContent, /Nothing was uploaded/);
  ready(); const first = click(); await fetched(1);
  assert.equal(pending[0].route, '/evidence-bundles/verify');
  assert.equal(pending[0].options.headers['X-Orch-Expected-SHA256'], sha);
  assert.equal(pending[0].options.credentials, 'omit');
  await click(); assert.equal(pending.length, 1);
  pending[0].resolve({ok: true, json: async () => report}); await first;
  assert.match(element('bundle-review-status').textContent, /claims are blocked/);
  assert.equal(element('save-bundle-review').disabled, false);
  const stale = click(); await fetched(2);
  element('bundle-expected').handlers.input();
  assert.equal(pending[1].options.signal.aborted, true);
  pending[1].resolve({ok: true, json: async () => report}); await stale;
  assert.equal(element('bundle-review-json').textContent, '');
  assert.equal(element('save-bundle-review').disabled, true);
  const wrong = click(); await fetched(3);
  pending[2].resolve({ok: true, json: async () => ({...report, binding: {bundle_sha256: 'a'.repeat(64)}})}); await wrong;
  assert.match(element('bundle-review-status').textContent, /Unexpected verification response/);
  const auth = click(); await fetched(4);
  pending[3].resolve({ok:false, status:401}); await auth;
  assert.match(element('notice').textContent, /Session expired/);
  assert.equal(element('workspace').hidden, true);
  assert.equal(element('bundle-review-json').textContent, '');
  ready();
  let finish;
  element('bundle-file').files = [{size:2, arrayBuffer: () => new Promise(resolve => { finish = resolve; })}];
  const cancelled = click(); element('clear-bundle-review').handlers.click();
  finish(bytes.buffer); await cancelled; assert.equal(pending.length, 4);
  assert.equal(element('bundle-expected').value, '');
  console.log('PASS: received bundle size/digest guards, independent review, stale results, cancellation and auth failure');
})().catch(error => {console.error(error); process.exitCode = 1;});
