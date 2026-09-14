const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {checked: false, hidden: false, textContent: "", handlers: {},
    addEventListener(name, handler) { this.handlers[name] = handler; }, querySelector() { return element(id + "-button"); }});
  return nodes.get(id);
}
const calls = [];
let report, responseOK = true, responseStatus = 200, release;
const context = vm.createContext({document: {getElementById: element}, setInterval() {}, Date, Map, Set,
  fetch: async (route, options) => {
    calls.push({route, options});
    if (release) await new Promise(resolve => { release = resolve; });
    return {ok: responseOK, status: responseStatus, json: async () => report};
  }});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
(async () => {
  assert.equal(calls.length, 0);
  await element("storage-report").handlers.click();
  assert.equal(calls.length, 0); // No session, no request.
  vm.runInContext('token = "operator-session"; controls();', context);
  report = {generated_at: "snapshot-time", physical_artifact_bytes: 12, disk_quota_bytes: 100,
    disk_headroom_bytes: 88, orphan_files: 2, gc_eligible_files: 1,
    missing_referenced_files: 0, size_mismatches: 0, unexpected_entries: 0};
  release = true;
  const pending = element("storage-report").handlers.click();
  assert.equal(element("storage-report").disabled, true);
  assert.equal(element("integrity-report").disabled, true);
  await element("integrity-report").handlers.click();
  assert.equal(calls.length, 1);
  const resume = release; release = null; resume();
  await pending;
  assert.equal(calls[0].route, "/storage");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.headers.Authorization, "Bearer operator-session");
  assert.match(element("maintenance-summary").textContent, /headroom: 88 bytes/);
  assert.match(element("maintenance-status").textContent, /not an integrity audit/);
  for (const status of ["passed", "failed"]) {
    report = {status, generated_at: "snapshot-time", tasks: 1, records: 2, artifact_references: 3, reason: "<script>untrusted</script>"};
    await element("integrity-report").handlers.click();
    assert.equal(calls.at(-1).route, "/integrity-report");
    assert.equal(calls.at(-1).options.method, "GET");
    assert.match(element("maintenance-status").textContent, new RegExp(`audit ${status}`));
    if (status === "failed") assert.match(element("maintenance-summary").textContent, /<script>untrusted<\/script>/);
  }
  report = {status: "unknown"};
  await element("integrity-report").handlers.click();
  assert.match(element("maintenance-status").textContent, /No verdict available/);
  assert.equal(element("maintenance-details").hidden, true);
  for (const status of [409, 401]) {
    responseOK = false; responseStatus = status; report = {error: "Dispatcher busy"};
    await element("integrity-report").handlers.click();
    assert.match(element("maintenance-status").textContent, /Report unavailable/);
    assert.equal(element("maintenance-summary").hidden, true);
    assert.equal(element("maintenance-json").textContent, "");
    assert.equal(element("integrity-report").disabled, status === 401);
  }
  assert.ok(calls.every(call => call.options.method === "GET"));
  console.log("PASS: manual authenticated maintenance reads, busy guards, verdicts and stale-report clearing");
})().catch(error => { console.error(error); process.exitCode = 1; });
