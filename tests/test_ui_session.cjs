const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map(), pending = [];
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', hidden:false, checked:false, handlers:{},
    replaceChildren() {}, append() {}, querySelector() { return element(id+'-button'); },
    addEventListener(name, fn) { this.handlers[name] = fn; }});
  return nodes.get(id);
}
const context = vm.createContext({document:{getElementById:element, createElement:() => element('new-option')},
  Date, Map, Set, setInterval() {}, fetch(path, options) { return new Promise(resolve => pending.push({path, options, resolve})); }});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../orch/ui/app.js'), 'utf8'), context);
const execute = code => vm.runInContext(code, context);
(async () => {
  execute('token="old"; snapshot={state:{state:"AWAITING_PLAN_APPROVAL", revision:1}}; approval={id:"pending",expires_at:"2999-01-01T00:00:00Z"}; selected="task";');
  element('reviewed').checked = true;
  const stale = execute('api("/tasks")');
  execute('token="new";');
  pending[0].resolve({status:401, ok:false});
  await assert.rejects(stale, /previous response discarded/);
  assert.equal(execute('token'), 'new');
  const rejected = execute('api("/tasks")');
  pending[1].resolve({status:401, ok:false});
  await assert.rejects(rejected, /Session expired/);
  assert.equal(execute('token'), '');
  assert.equal(element('workspace').hidden, true);
  assert.equal(element('login').hidden, false);
  assert.equal(execute('approval'), null);
  assert.equal(element('reviewed').checked, false);

  // A late failure from an earlier login must not clear a newer session.
  element('token').value = 'first';
  const first = element('connect').handlers.submit({preventDefault(){}});
  element('token').value = 'second';
  const second = element('connect').handlers.submit({preventDefault(){}});
  pending[3].resolve({status:200, ok:true, json:async () => ({items:[], next:null})});
  await second;
  // Connecting now also fills the "what needs you" panel, so that request has to be drained
  // before the rotation below, which is what this section is actually about.
  for (let i=0; i<20 && !pending.some(p => p.path === '/status'); i++) await Promise.resolve();
  const settled = pending.find(p => p.path === '/status');
  assert.ok(settled, 'connecting loads the attention panel');
  settled.resolve({status:200, ok:true, json:async () => ({tasks:{waiting_on_a_person:[]},
    agents:{holding:[], lapsed_count:0}, work:{at_bound:false}, summary:{tasks_active:0}})});
  // The roster asks Docker what is running once the status it belongs to has arrived.
  for (let i=0; i<20 && !pending.some(p => p.path === '/agents'); i++) await Promise.resolve();
  const containers = pending.find(p => p.path === '/agents');
  assert.ok(containers, 'connecting asks what is running');
  containers.resolve({status:200, ok:true, json:async () => ({agents:[], docker:'available', reason:null})});
  // Connecting also loads the questions panel; that request is drained by name too, so the
  // indices below stay about what they are testing rather than about how many panels exist.
  for (let i=0; i<20 && !pending.some(p => p.path === '/consultations'); i++) await Promise.resolve();
  const questions = pending.find(p => p.path === '/consultations');
  assert.ok(questions, 'connecting loads the questions panel');
  questions.resolve({status:200, ok:true, json:async () => ({consultations:[], count:0, open:0, answered:0})});
  pending[2].resolve({status:401, ok:false});
  await first;
  assert.equal(execute('token'), 'second');
  assert.equal(element('workspace').hidden, false);

  const rotating = execute('sessionAction("rotate")');
  const newToken = 'r'.repeat(43);
  pending[7].resolve({status:200, ok:true, json:async () => ({session:newToken, status:{generation:2, expires_in_seconds:120}})});
  for (let i=0; i<10 && pending.length<9; i++) await Promise.resolve();
  pending[8].resolve({status:200, ok:true, json:async () => ({items:[], next:null})});
  await rotating;
  assert.equal(execute('token'), newToken);
  assert.match(element('session-status').textContent, /generation 2/);
  assert.equal(element('token').value, '');
  assert.equal(execute('selected'), null);
  // Headers can arrive before rotation while the body is still being read.
  let finishBody;
  const delayed = execute('api("/session")');
  pending[9].resolve({status:200, ok:true, json:() => new Promise(resolve => { finishBody = resolve; })});
  for (let i=0; i<10 && !finishBody; i++) await Promise.resolve();
  assert.equal(typeof finishBody, 'function');
  execute('token="newer"');
  finishBody({generation:2});
  await assert.rejects(delayed, /previous response discarded/);
  assert.equal(execute('token'), 'newer');
  console.log('PASS: session expiry clears access, stale authentication cannot clear a newer session, rotation resets review state');
})().catch(error => {console.error(error); process.exitCode=1;});
