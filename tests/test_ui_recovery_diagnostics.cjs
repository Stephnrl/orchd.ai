// Run with: node tests/test_ui_recovery_diagnostics.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {checked: false, hidden: false, textContent: "",
    addEventListener() {}, querySelector() { return element(id + "-button"); }});
  return nodes.get(id);
}
const context = vm.createContext({document: {getElementById: element}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
(async () => {
  await vm.runInContext(`
    selected = "task-one";
    globalThis.blocked = true;
    api = async route => {
      if (route.endsWith("recovery-diagnostics")) return {reasons: ["Inspect <script>untrusted</script> evidence."], next_step: "No runtime checks.", runtime_checked: false};
      if (route.includes("/records/")) return {title: "Fixture"};
      return {state: {state: blocked ? "BLOCKED" : "COMPLETED", spec: {id: "spec"}, revision: 4, pending_approval: null}};
    };
    state();
  `, context);
  assert.equal(element("recovery-diagnostics").hidden, false);
  assert.equal(element("recovery-guidance").textContent, "Inspect <script>untrusted</script> evidence. No runtime checks.");
  assert.match(element("recovery-json").textContent, /"runtime_checked": false/);
  await vm.runInContext("blocked = false; state();", context);
  assert.equal(element("recovery-diagnostics").hidden, true);
  console.log("PASS: blocked-task diagnostic text and non-blocked hiding");
})().catch(error => { console.error(error); process.exitCode = 1; });
