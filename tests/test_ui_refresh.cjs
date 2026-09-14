const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {checked: false, hidden: false, textContent: "", handlers: {},
    replaceChildren() {}, append() {}, addEventListener(name, handler) { this.handlers[name] = handler; }, querySelector() { return element(id + "-button"); }});
  return nodes.get(id);
}
const context = vm.createContext({document: {getElementById: element, createElement() { return {}; }}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
const pending = [];
context.request = route => route.includes("/records/") ? Promise.resolve({title: "Fixture"}) :
  new Promise((resolve, reject) => pending.push({route, resolve, reject}));
function data(revision) {
  return {state: {task_id: "task-one", state: "CHANGES_REQUESTED", spec: {id: "spec"}, revision, pending_approval: null}};
}
(async () => {
  vm.runInContext(`selected = "task-one"; api = request; snapshot = {state: {state: "AWAITING_PLAN_APPROVAL", revision: 1}};
    approval = {id: "old", expires_at: "2999-01-01T00:00:00Z"}; $("reviewed").checked = true; controls();`, context);
  assert.equal(element("approve").disabled, false);
  const old = vm.runInContext("state()", context);
  assert.match(element("task-refresh-status").textContent, /Refreshing task/);
  assert.equal(element("approve").disabled, true);
  assert.equal(element("run").disabled, true);
  assert.equal(element("reviewed").checked, false);
  const latest = vm.runInContext("state()", context);
  pending[1].resolve(data(3)); await latest;
  assert.equal(element("revision").textContent, "Revision 3");
  pending[0].resolve(data(2)); await old;
  assert.match(element("task-refresh-status").textContent, /Task refreshed/);
  assert.equal(element("revision").textContent, "Revision 3");
  assert.equal(vm.runInContext("snapshot.state.revision", context), 3);

  const obsolete = vm.runInContext("state()", context);
  const newer = vm.runInContext("state()", context);
  pending[3].resolve(data(4)); await newer;
  pending[2].reject(new Error("old error")); await obsolete;
  assert.equal(vm.runInContext("snapshot.state.revision", context), 4);

  const failure = vm.runInContext("state()", context);
  pending[4].reject(new Error("refresh failed"));
  await assert.rejects(failure, /refresh failed/);
  assert.equal(vm.runInContext("snapshot", context), null);
  assert.equal(element("approve").disabled, true);
  assert.equal(element("run").disabled, true);
  assert.equal(element("retry-cleanup").disabled, true);
  assert.match(element("task-refresh-status").textContent, /Task refresh failed/);

  const switched = vm.runInContext("state()", context);
  vm.runInContext('epoch++; selected = "task-two";', context);
  pending[5].resolve(data(5)); await switched;
  assert.equal(vm.runInContext("snapshot", context), null);
  assert.notEqual(element("revision").textContent, "Revision 5");
  const beforeRetry = pending.length;
  vm.runInContext('busy = true; token = "session"; controls();', context);
  await element("refresh-task").handlers.click();
  assert.equal(pending.length, beforeRetry);
  assert.equal(element("refresh-task").disabled, true);
  vm.runInContext('busy = false; selected = "task-one"; token = ""; controls();', context);
  await element("refresh-task").handlers.click();
  assert.equal(pending.length, beforeRetry);
  vm.runInContext('token = "session"; controls();', context);
  assert.equal(element("refresh-task").disabled, false);
  const retry = element("refresh-task").handlers.click();
  assert.equal(pending.at(-1).route, "/tasks/task-one");
  pending.at(-1).resolve(data(6)); await retry;
  assert.equal(vm.runInContext("snapshot.state.revision", context), 6);
  assert.equal(element("run").disabled, false);
  assert.match(element("task-refresh-status").textContent, /Task refreshed/);
  assert.equal(pending.length, beforeRetry + 1); // Refresh only; no automatic run.
  console.log("PASS: latest refresh wins, stale errors are ignored and failed refresh disables actions");
})().catch(error => { console.error(error); process.exitCode = 1; });
