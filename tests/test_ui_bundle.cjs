const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const nodes = new Map();
let clicks = 0;
function node() { return {value: '', textContent: '', handlers: {}, append() {}, replaceChildren() {}, remove() {}, click() { clicks++; }, addEventListener(name, fn) { this.handlers[name] = fn; }}; }
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const pending = [];
const context = vm.createContext({document: {getElementById: element, createElement: node, body: node()}, setInterval() {}, setTimeout(fn) { fn(); }, Date, Map, Set, Uint8Array, crypto: webcrypto, AbortController, Blob, URL: {createObjectURL() { return 'blob:test'; }, revokeObjectURL() {}}, fetch(route, options) { return new Promise(resolve => pending.push({route, options, resolve})); }});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../orch/ui/app.js'), 'utf8'), context);
const bytes = Buffer.from('{"bundle":"example"}');
const sha = createHash('sha256').update(bytes).digest('hex');
function response(headers = {}, chunks = [bytes]) {
  return new Response(new ReadableStream({start(c) { for (const chunk of chunks) c.enqueue(chunk); c.close(); }}), {headers: {'Content-Type': 'application/json', 'Content-Length': String(bytes.length), 'X-Orch-Bundle-SHA256': sha, ...headers}});
}
function ready() { vm.runInContext('token="session"; selected="one"; snapshot={}; claimResult={epoch, status:"claims_consistent", selection:{task_id:"one", patch:{}, test:{}, review:{}, policy:{}}}; claimControls();', context); }
(async () => {
  context.response = response();
  const good = await vm.runInContext('readBundleResponse(response)', context);
  assert.equal(good.sha, sha);
  for (const [headers, chunks, message] of [
    [{'Content-Length': '16777217'}, [bytes], /headers/],
    [{'Content-Length': '1'}, [bytes], /declared size/],
    [{'Content-Length': String(bytes.length + 1)}, [bytes], /Incomplete/],
    [{'X-Orch-Bundle-SHA256': 'a'.repeat(64)}, [bytes], /digest mismatch/],
    [{'Content-Type': 'text/html'}, [bytes], /headers/],
  ]) {
    context.response = response(headers, chunks);
    await assert.rejects(vm.runInContext('readBundleResponse(response)', context), message);
  }
  ready();
  const first = element('download-bundle').handlers.click();
  assert.equal(element('download-bundle').disabled, true);
  await element('download-bundle').handlers.click();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].route, '/tasks/one/evidence-bundle');
  assert.equal(pending[0].options.credentials, 'omit');
  assert.deepEqual(Object.keys(JSON.parse(pending[0].options.body)).sort(), ['patch','policy','review','test']);
  pending[0].resolve(response()); await first;
  assert.equal(clicks, 1); assert.match(element('bundle-digest').textContent, new RegExp(sha));
  const stale = element('download-bundle').handlers.click();
  vm.runInContext('epoch++; selected="two"; resetClaimReview();', context);
  assert.equal(pending[1].options.signal.aborted, true);
  pending[1].resolve(response()); await stale;
  assert.equal(clicks, 1); assert.equal(element('bundle-digest').textContent, '');
  ready();
  const rejected = element('download-bundle').handlers.click();
  pending[2].resolve(new Response('{}', {status:409})); await rejected;
  assert.equal(clicks, 1); assert.equal(element('download-bundle').disabled, true);
  assert.match(element('claims-status').textContent, /Fresh bundle assessment rejected/);
  console.log('PASS: bounded bundle streams, digest verification, request binding, cancellation and failures');
})().catch(error => { console.error(error); process.exitCode = 1; });
