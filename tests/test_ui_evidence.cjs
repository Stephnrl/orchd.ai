const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function node() {
  return {hidden: false, textContent: "", children: [], handlers: {},
    replaceChildren() { this.children = []; }, append(child) { this.children.push(child); },
    addEventListener(name, fn) { this.handlers[name] = fn; }};
}
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const pending = [];
const context = vm.createContext({document: {getElementById: element, createElement: node}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
context.request = route => new Promise((resolve, reject) => pending.push({route, resolve, reject}));
const record = {contract_type: "Plan", title: "older record"};
const artifact = {reference: {trust: "untrusted", producer_id: "junior", sha256: "a".repeat(64)}, content: "<script>untrusted</script>"};
(async () => {
  vm.runInContext('selected = "task-one"; api = request;', context);
  element("evidence-json").textContent = "previous evidence";
  element("evidence-links").append(node());
  const first = vm.runInContext('evidence("records", "old")', context);
  assert.equal(element("evidence-json").textContent, "");
  assert.equal(element("evidence-links").children.length, 0);
  assert.match(element("evidence-title").textContent, /Loading/);
  const latest = vm.runInContext('evidence("artifacts", "new")', context);
  pending[1].resolve(artifact); await latest;
  pending[0].resolve(record); await first;
  assert.equal(element("evidence-title").textContent, "Artifact content");
  assert.equal(element("evidence-json").textContent, artifact.content);
  assert.match(element("evidence-trust").textContent, /Trust: untrusted/);

  const obsolete = vm.runInContext('evidence("records", "old")', context);
  const fresh = vm.runInContext('evidence("artifacts", "new")', context);
  pending[3].resolve(artifact); await fresh;
  pending[2].reject(new Error("old failure")); await obsolete;
  assert.equal(element("evidence-json").textContent, artifact.content);

  const failed = vm.runInContext('evidence("records", "bad")', context);
  pending[4].reject(new Error("missing"));
  await assert.rejects(failed, /missing/);
  assert.match(element("evidence-title").textContent, /Evidence unavailable/);
  assert.equal(element("evidence-json").textContent, "");
  assert.equal(element("evidence-links").children.length, 0);
  const retry = vm.runInContext('evidence("records", "bad")', context);
  pending[5].resolve(record); await retry;
  assert.equal(element("evidence-title").textContent, "Plan");

  const switched = vm.runInContext('evidence("records", "previous-task")', context);
  vm.runInContext('epoch++; selected = "task-two"; $("evidence").hidden = true;', context);
  pending[6].resolve(record); await switched;
  assert.equal(element("evidence").hidden, true);
  assert.equal(element("evidence-json").textContent, "");
  assert.ok(pending.every(p => p.route.startsWith("/tasks/task-one/")));
  console.log("PASS: latest evidence selection, stale errors, loading/failure clearing, retry and task switches");
})().catch(error => { console.error(error); process.exitCode = 1; });
