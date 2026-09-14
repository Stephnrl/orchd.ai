const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map();
function node() {
  return {value: '', children: [], handlers: {}, textContent: '',
    replaceChildren() { this.children = []; }, append(child) { this.children.push(child); },
    addEventListener(name, handler) { this.handlers[name] = handler; }};
}
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const context = vm.createContext({document: {getElementById: element, createElement: node}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../orch/ui/app.js'), 'utf8'), context);
const pending = [];
context.request = (route, payload) => new Promise((resolve, reject) => pending.push({route, payload, resolve, reject}));
const catalog = {binding: {records: ['review', 'policy'].map(role => ({role, created_at: '2026-09-14T12:00:00Z', record: {id: role + '<img>', sha256: 'a'.repeat(64)}}))}};
const result = {selection: {task_id: 'one'}, assessment: {status: 'blocked', binding: {claims: {checked_at: '2026-09-14T12:01:00Z', blockers: ['<script>blocked</script>']}}}};
function choose() {
  element('claim-review').value = 'review<img>'; element('claim-policy').value = 'policy<img>';
  element('claim-review').handlers.change();
}
(async () => {
  vm.runInContext('selected = "one"; token = "session"; snapshot = {}; api = request;', context);
  const load = vm.runInContext('loadClaimChoices()', context);
  assert.equal(element('assess-claims').disabled, true);
  pending[0].resolve(catalog); await load;
  assert.equal(element('claim-review').value, '');
  assert.equal(element('assess-claims').disabled, true);
  assert.match(element('claim-review').children[1].textContent, /review<img>/);
  choose(); assert.equal(element('assess-claims').disabled, false);
  const check = vm.runInContext('assessClaimSelection()', context);
  assert.equal(element('save-selection').disabled, true);
  assert.equal(pending[1].route, '/tasks/one/assess-evidence');
  assert.deepEqual(Object.keys(pending[1].payload), ['review', 'policy']);
  pending[1].resolve(result); await check;
  assert.equal(element('claims-blockers').children[0].textContent, '<script>blocked</script>');
  assert.equal(element('save-selection').disabled, false);
  assert.match(element('claims-status').textContent, /does not approve or run/);
  choose(); assert.equal(element('save-selection').disabled, true);
  assert.equal(element('claims-json').textContent, '');
  const obsolete = vm.runInContext('assessClaimSelection()', context);
  choose();
  const latest = vm.runInContext('assessClaimSelection()', context);
  pending[3].resolve(result); await latest;
  pending[2].reject(new Error('obsolete failure')); await obsolete;
  assert.match(element('claims-status').textContent, /blocked/);
  const switched = vm.runInContext('loadClaimChoices()', context);
  vm.runInContext('epoch++; selected = "two"; resetClaimReview();', context);
  pending[4].resolve(catalog); await switched;
  assert.equal(element('claim-review').children.length, 1);
  const fresh = vm.runInContext('loadClaimChoices()', context);
  pending[5].resolve(catalog); await fresh; choose();
  const failure = vm.runInContext('assessClaimSelection()', context);
  pending[6].reject(new Error('unavailable')); await failure;
  assert.match(element('claims-status').textContent, /unavailable/);
  assert.equal(element('save-selection').disabled, true);
  assert.equal(element('claims-json').textContent, '');
  const empty = vm.runInContext('loadClaimChoices()', context);
  pending[7].resolve({binding: {records: []}}); await empty;
  assert.match(element('claims-status').textContent, /No complete/);
  assert.equal(element('assess-claims').disabled, true);
  vm.runInContext('busy = true;', context);
  await vm.runInContext('loadClaimChoices(); assessClaimSelection();', context);
  assert.equal(pending.length, 8);
  console.log('PASS: explicit evidence selection, stale responses, literal rendering, errors and busy guards');
})().catch(error => { console.error(error); process.exitCode = 1; });
