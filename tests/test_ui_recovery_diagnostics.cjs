// Run with: node tests/test_ui_recovery_diagnostics.cjs
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
(async () => {
  await vm.runInContext(`
    selected = "task-one";
    globalThis.blocked = true;
    api = async route => {
      if (route.endsWith("recovery-diagnostics")) return {task_id: "task-one", revision: 4, recovery_status: "preconditions_met", reasons: ["Inspect <script>untrusted</script> evidence."], next_step: "No runtime checks.", runtime_checked: false};
      if (route.includes("/records/")) return {title: "Fixture"};
      return {state: {state: blocked ? "BLOCKED" : "COMPLETED", spec: {id: "spec"}, revision: 4, pending_approval: null}};
    };
    state();
  `, context);
  assert.equal(element("recovery-diagnostics").hidden, false);
  assert.equal(element("recovery-guidance").textContent, "Inspect <script>untrusted</script> evidence. No runtime checks.");
  assert.match(element("recovery-json").textContent, /"runtime_checked": false/);
  assert.equal(element("recover-task").disabled, true);
  element("recovery-reviewed").checked = true;
  element("recovery-reviewed").handlers.change();
  assert.equal(element("recover-task").disabled, false);
  await vm.runInContext("state();", context);
  assert.equal(element("recovery-reviewed").checked, false);
  assert.equal(element("recover-task").disabled, true);
  vm.runInContext(`
    globalThis.calls = [];
    mutate = action => action();
    api = async (route, payload) => calls.push({route, payload});
  `, context);
  element("recover-task").handlers.click();
  assert.equal(context.calls.length, 0);
  element("recovery-reviewed").checked = true;
  for (const invalid of ['recovery.revision = 3', 'recovery.task_id = "other"', 'recovery.recovery_status = "unavailable"', 'busy = true', 'snapshot.state.state = "COMPLETED"']) {
    vm.runInContext('recovery.revision = 4; recovery.task_id = "task-one"; recovery.recovery_status = "preconditions_met"; busy = false; snapshot.state.state = "BLOCKED";', context);
    vm.runInContext(invalid + "; controls();", context);
    assert.equal(element("recover-task").disabled, true);
    element("recover-task").handlers.click();
    assert.equal(context.calls.length, 0);
  }
  vm.runInContext('snapshot.state.state = "BLOCKED"; controls();', context);
  assert.equal(element("recover-task").disabled, false);
  element("recover-task").handlers.click();
  assert.equal(context.calls.length, 1);
  assert.equal(context.calls[0].route, "/tasks/task-one/recover");
  assert.equal(JSON.stringify(context.calls[0].payload), '{"expected_revision":4}');
  vm.runInContext(`api = async route => route.includes("/records/") ? {title: "Fixture"} : {state: {state: "COMPLETED", spec: {id: "spec"}, revision: 5, pending_approval: null}};`, context);
  await vm.runInContext("blocked = false; state();", context);
  assert.equal(element("recovery-diagnostics").hidden, true);
  assert.equal(element("recover-task").disabled, true);
  for (const cleanup of ["pending", "deferred", "completed"]) {
    context.cleanupStatus = cleanup;
    await vm.runInContext(`api = async route => route.includes("/records/") ? {title: "Fixture"} : {state: {state: "CHANGES_REQUESTED", spec: {id: "spec"}, revision: 6, pending_approval: null}, context: {operator_recovery: {cleanup: cleanupStatus}}}; state();`, context);
    assert.equal(element("recovery-outcome").hidden, false);
    assert.match(element("recovery-outcome").textContent, new RegExp(cleanup));
  }
  vm.runInContext(`
    globalThis.resetCleanup = () => {
      selected = "task-one"; busy = false;
      snapshot = {state: {task_id: selected, state: "CHANGES_REQUESTED", revision: 6, active_operation_id: null},
        context: {operator_recovery: {operation_id: "a".repeat(32), completed_revision: 6, cleanup: "deferred"}}};
    };
    resetCleanup(); calls = [];
    api = async (route, payload) => calls.push({route, payload});
    controls();
  `, context);
  assert.equal(element("cleanup-retry").hidden, false);
  assert.equal(element("retry-cleanup").disabled, true);
  element("retry-cleanup").handlers.click();
  assert.equal(context.calls.length, 0);
  element("cleanup-reviewed").checked = true;
  element("cleanup-reviewed").handlers.change();
  assert.equal(element("retry-cleanup").disabled, false);
  for (const invalid of ['busy = true', 'selected = "other"', 'snapshot.state.revision = 7',
    'snapshot.state.state = "COMPLETED"', 'snapshot.state.active_operation_id = "active"',
    'snapshot.context.operator_recovery.cleanup = "completed"', 'snapshot.context.operator_recovery.operation_id = "../bad"',
    'snapshot.context.operator_recovery = null']) {
    vm.runInContext('resetCleanup(); ' + invalid + '; controls();', context);
    assert.equal(element("retry-cleanup").disabled, true, invalid);
    element("retry-cleanup").handlers.click();
    assert.equal(context.calls.length, 0, invalid);
  }
  for (const cleanup of ["pending", "deferred"]) {
    vm.runInContext(`resetCleanup(); snapshot.context.operator_recovery.cleanup = "${cleanup}"; controls();`, context);
    assert.equal(element("retry-cleanup").disabled, false);
    element("retry-cleanup").handlers.click();
    const call = context.calls.at(-1);
    assert.equal(call.route, "/tasks/task-one/retry-cleanup");
    assert.equal(JSON.stringify(call.payload), JSON.stringify({operation_id: "a".repeat(32), expected_revision: 6}));
  }
  assert.equal(context.calls.length, 2); // No automatic run request.
  await vm.runInContext(`snapshot.state.spec = {id: "spec"}; globalThis.savedSnapshot = snapshot; api = async route => route.includes("/records/") ? {title: "Fixture"} : savedSnapshot; state();`, context);
  assert.equal(element("cleanup-reviewed").checked, false);
  element("cleanup-reviewed").checked = true;
  await assert.rejects(vm.runInContext(`api = async () => { throw new Error("refresh failed"); }; state();`, context), /refresh failed/);
  // Even a failed refresh clears acknowledgement before awaiting the server.
  assert.equal(element("cleanup-reviewed").checked, false);
  assert.equal(element("retry-cleanup").disabled, true);
  console.log("PASS: recovery and cleanup text, inspection, stale-state guards and request binding");
})().catch(error => { console.error(error); process.exitCode = 1; });
