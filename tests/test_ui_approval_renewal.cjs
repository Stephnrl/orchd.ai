// Run with: node tests/test_ui_approval_renewal.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    checked: false, hidden: false, disabled: false, textContent: "", handlers: {},
    addEventListener(name, handler) { this.handlers[name] = handler; },
    querySelector() { return element(id + "-button"); }
  });
  return elements.get(id);
}
const context = vm.createContext({document: {getElementById: element}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
vm.runInContext(`
  selected = "task-one";
  snapshot = {state: {state: "AWAITING_PLAN_APPROVAL", revision: 3, pending_approval: {id: "old"}}};
  approval = {id: "old", expires_at: "2000-01-01T00:00:00Z"};
  $("reviewed").checked = true;
  controls();
`, context);
assert.equal(element("approve").disabled, true);
assert.equal(element("reject").disabled, true);
assert.equal(element("renew-approval").hidden, false);
assert.equal(element("renew-approval").disabled, false);
assert.match(element("approval-expiry").textContent, /expired/);
vm.runInContext("busy = true; controls();", context);
assert.equal(element("renew-approval").disabled, true);
vm.runInContext(`
  busy = false;
  mutate = action => action();
  api = async (route, payload) => { globalThis.sent = {route, payload}; };
`, context);
element("renew-approval").handlers.click();
assert.equal(context.sent.route, "/tasks/task-one/renew-approval");
assert.equal(JSON.stringify(context.sent.payload), '{"request_id":"old","expected_revision":3}');
vm.runInContext('approval.expires_at = "2999-01-01T00:00:00Z"; $("reviewed").checked = false; controls();', context);
assert.equal(element("renew-approval").hidden, true);
assert.equal(element("approve").disabled, true);
assert.equal(element("approval-expiry").textContent, "");
vm.runInContext('$("reviewed").checked = true; controls();', context);
assert.equal(element("approve").disabled, false);
console.log("PASS: expired/fresh approval controls and renewal request binding");
