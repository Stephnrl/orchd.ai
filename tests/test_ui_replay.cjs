const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map();
function node() {
  return {textContent: '', children: [], handlers: {},
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    addEventListener(name, fn) { this.handlers[name] = fn; }, querySelector: node};
}
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const pending = [];
const context = vm.createContext({document: {getElementById: element, createElement: node},
  Date, Map, Set, setInterval() {}, fetch: (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject}))});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../orch/ui/app.js'), 'utf8'), context);
let refreshes = 0, recordReads = 0;
context.refresh = async () => { refreshes++; };
context.readRecords = async () => { recordReads++; };
vm.runInContext('token = "session"; selected = "one"; state = refresh; records = readRecords;', context);
function response(...sequences) {
  return {ok: true, text: async () => sequences.map(sequence => 'data: ' + JSON.stringify({sequence,
    event_type: 'workflow_completed', payload: {artifact_id: 'payload'}, created_at: 'time', actor: {role: 'human'}})).join('\n\n')};
}
(async () => {
  const initial = vm.runInContext('replay()', context);
  await vm.runInContext('replay()', context);
  assert.equal(pending.length, 1); // Serialize replay; never dispatch a duplicate cursor.
  pending.at(-1).resolve(response(1)); await initial;
  assert.equal(element('events').children.length, 1);
  assert.equal(refreshes, 1);
  const failed = vm.runInContext('replay()', context);
  assert.equal(pending.at(-1).options.headers['Last-Event-ID'], '1');
  pending.at(-1).reject(new Error('offline'));
  await assert.rejects(failed, /offline/);
  assert.match(element('event-status').textContent, /Retained events may be incomplete/);
  assert.equal(element('events').children.length, 1);
  const retry = vm.runInContext('replay()', context);
  pending.at(-1).resolve(response(1, 2)); await retry;
  assert.equal(element('events').children.length, 2); // Duplicate replay is ignored.
  assert.match(element('event-status').textContent, /through event 2/);
  const gap = vm.runInContext('replay()', context);
  pending.at(-1).resolve(response(4)); await assert.rejects(gap, /Event gap/);
  assert.equal(vm.runInContext('cursor', context), 2);
  for (const failure of ['network', 'http', 'body', 'success']) {
    const stale = vm.runInContext('replay()', context);
    vm.runInContext('epoch++; selected = "other";', context);
    element('event-status').textContent = 'Current task';
    if (failure === 'network') pending.at(-1).reject(new Error('obsolete'));
    else if (failure === 'http') pending.at(-1).resolve({ok: false});
    else if (failure === 'body') pending.at(-1).resolve({ok: true, text: async () => { throw new Error('obsolete body'); }});
    else pending.at(-1).resolve(response(3));
    await stale;
    assert.equal(element('event-status').textContent, 'Current task');
    assert.equal(vm.runInContext('cursor', context), 2);
  }
  let finishRefresh;
  context.refresh = () => new Promise(resolve => { finishRefresh = resolve; });
  vm.runInContext('state = refresh;', context);
  const duringRefresh = vm.runInContext('replay()', context);
  pending.at(-1).resolve(response(3));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof finishRefresh, 'function');
  const before = recordReads;
  vm.runInContext('epoch++; selected = "latest";', context);
  finishRefresh(); await duringRefresh;
  assert.equal(recordReads, before); // Old replay must not refresh the new task's records.
  console.log('PASS: replay failure status, cursor retry, gaps, stale responses and task switches');
})().catch(error => { console.error(error); process.exitCode = 1; });
