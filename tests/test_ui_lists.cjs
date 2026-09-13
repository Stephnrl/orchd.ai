const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map();
function node() {
  return {checked: false, hidden: false, textContent: "", children: [], handlers: {},
    replaceChildren() { this.children = []; }, append(child) { this.children.push(child); },
    setAttribute() {}, querySelector() { return node(); }, addEventListener(name, fn) { this.handlers[name] = fn; }};
}
function element(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }
const pending = [];
const context = vm.createContext({document: {getElementById: element, createElement: node}, setInterval() {}, Date, Map, Set});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
context.request = route => new Promise((resolve, reject) => pending.push({route, resolve, reject}));
vm.runInContext('selected = "task-one"; api = request;', context);
(async () => {
  for (const kind of ["tasks", "records"]) {
    function page(id, next) {
      return {items: [kind === "tasks" ? {task_id: id, state: {state: "COMPLETED"}} : {id, kind: "Plan"}], next};
    }
    const start = pending.length;
    const older = vm.runInContext(`${kind}()`, context);
    const newer = vm.runInContext(`${kind}()`, context);
    await vm.runInContext(`${kind}(true)`, context);
    assert.equal(pending.length, start + 2); // No paging during reset.
    pending[start + 1].resolve(page("new", 10)); await newer;
    pending[start].resolve(page("old", 20)); await older;
    assert.equal(element(kind).children.length, 1);
    assert.match(element(kind).children[0].textContent, /new/);
    const more = vm.runInContext(`${kind}(true)`, context);
    assert.match(pending.at(-1).route, /after=10$/);
    assert.equal(element("more-" + kind).disabled, true);
    await vm.runInContext(`${kind}(true)`, context);
    assert.equal(pending.length, start + 3);
    pending.at(-1).resolve(page("next", null)); await more;
    assert.equal(element(kind).children.length, 2);
    await vm.runInContext(`${kind}(true)`, context);
    assert.equal(pending.length, start + 3); // Exhausted cursor.
    const stale = vm.runInContext(`${kind}()`, context);
    const latest = vm.runInContext(`${kind}()`, context);
    pending.at(-1).resolve(page("latest", 30)); await latest;
    pending.at(-2).reject(new Error("stale failure")); await stale;
    assert.match(element(kind).children[0].textContent, /latest/);
    const failedPage = vm.runInContext(`${kind}(true)`, context);
    pending.at(-1).reject(new Error("page unavailable"));
    await assert.rejects(failedPage, /page unavailable/);
    assert.equal(element("more-" + kind).disabled, false);
    const retry = vm.runInContext(`${kind}(true)`, context);
    assert.match(pending.at(-1).route, /after=30$/);
    pending.at(-1).resolve(page("retry", null)); await retry;
    assert.equal(element(kind).children.length, 2);
  }
  const oldRecords = vm.runInContext("records()", context);
  vm.runInContext('epoch++; selected = "task-two";', context);
  const currentRecords = vm.runInContext("records()", context);
  pending.at(-1).resolve({items: [{id: "current", kind: "Plan"}], next: null}); await currentRecords;
  pending.at(-2).resolve({items: [{id: "previous", kind: "Plan"}], next: 9}); await oldRecords;
  assert.match(element("records").children[0].textContent, /current/);
  assert.equal(element("records").children.length, 1);
  console.log("PASS: latest lists, single-page dispatch, failed-page retry and task-scoped records");
})().catch(error => { console.error(error); process.exitCode = 1; });
