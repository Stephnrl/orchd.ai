// The panel that answers "what needs me now" before anything asks you to read JSON.
//
// The operator page was a column of cards with evidence behind <details>, which assumes you
// already know the workflow and can translate AWAITING_PLAN_APPROVAL in your head. These
// checks fix the two things that changed: a state is shown as a sentence, and the work that
// is stopped on a person is separated from the work that is merely in progress.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const nodes = new Map();
function node(tag) {
  return {tag, className: "", textContent: "", value: "", hidden: false, checked: false,
    children: [], handlers: {},
    get firstChild() { return this.children[0]; },
    replaceChildren() { this.children = []; },
    append(...kids) { for (const kid of kids) this.children.push(kid); },
    setAttribute() {}, querySelector() { return node("div"); },
    addEventListener(name, fn) { this.handlers[name] = fn; }};
}
function element(id) { if (!nodes.has(id)) nodes.set(id, node(id)); return nodes.get(id); }
const context = vm.createContext({document: {getElementById: element, createElement: node},
  Date, Map, Set, setInterval() {}, JSON, Number, String, Math});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
const run = code => vm.runInContext(code, context);

function text(list) {
  return list.children.map(item => item.children.map(part =>
    part.children.length ? part.children.map(inner => inner.textContent).join(" | ") : part.textContent).join(" | "));
}

function report(overrides) {
  return Object.assign({tasks: {waiting_on_a_person: []}, agents: {holding: [], lapsed_count: 0},
                        work: {at_bound: false}, summary: {tasks_active: 0}}, overrides);
}

async function withStatus(value) {
  run("api = () => Promise.resolve(" + JSON.stringify(value) + ");");
  await run("attention()");
}

(async () => {
  // A state name is a protocol detail; the page says what it means.
  assert.equal(run('plain("AWAITING_PLAN_APPROVAL")'), "Waiting for you to approve the plan");
  assert.equal(run('plain("AWAITING_CLARIFICATION")'), "Waiting for your answer to a question");
  assert.equal(run('plain("IMPLEMENTING")'), "Implementing");
  // An unknown state still reads as words rather than an identifier.
  assert.equal(run('plain("SOME_NEW_STATE")'), "SOME NEW STATE");

  // How long it has waited is the part that says whether it matters.
  const now = Date.now();
  assert.equal(run(`waited(${JSON.stringify(new Date(now - 30000).toISOString())})`), "30s");
  assert.equal(run(`waited(${JSON.stringify(new Date(now - 240000).toISOString())})`), "4m");
  assert.equal(run(`waited(${JSON.stringify(new Date(now - 7200000).toISOString())})`), "2h");
  assert.equal(run('waited("not a time")'), "");

  // Nothing waiting, nothing running.
  await withStatus(report());
  assert.match(element("attention-summary").textContent, /Nothing needs you, and nothing is in progress/);
  assert.equal(element("attention-list").children.length, 0);
  assert.equal(element("working").hidden, true);

  // Work in progress is not work that needs you.
  await withStatus(report({agents: {holding: [{task_id: "abcdef0123456789", agent: "project-manager",
                                               role: "project_manager", claimed_at: new Date(now - 60000).toISOString()}],
                                    lapsed_count: 0},
                           summary: {tasks_active: 1}}));
  assert.match(element("attention-summary").textContent, /Nothing needs you\. 1 task\(s\) in progress/);
  assert.equal(element("attention-list").children.length, 0);
  assert.equal(element("working").hidden, false);
  assert.match(text(element("working-list"))[0], /project-manager is holding this as project manager/);

  // Something stopped on a person says so, in words, with how long it has waited.
  await withStatus(report({tasks: {waiting_on_a_person: [
    {task_id: "11112222333344445555", state: "AWAITING_PLAN_APPROVAL", since: new Date(now - 300000).toISOString()},
    {task_id: "66667777888899990000", state: "AWAITING_CLARIFICATION", since: new Date(now - 45000).toISOString()}]},
    summary: {tasks_active: 2}}));
  assert.match(element("attention-summary").textContent, /2 things need you/);
  const rows = text(element("attention-list"));
  assert.equal(rows.length, 2);
  assert.match(rows[0], /Waiting for you to approve the plan/);
  assert.match(rows[0], /5m/);
  assert.match(rows[1], /Waiting for your answer to a question/);
  // Every row offers the task it names rather than making you find it.
  assert.equal(element("attention-list").children[0].children[2].textContent, "Open task");

  // One thing reads as one thing.
  await withStatus(report({tasks: {waiting_on_a_person: [
    {task_id: "aaaa", state: "BLOCKED", since: new Date(now - 1000).toISOString()}]}, summary: {tasks_active: 1}}));
  assert.match(element("attention-summary").textContent, /^1 thing needs you\./);

  // The two conditions that are not a task waiting, but still want a person.
  await withStatus(report({work: {at_bound: true}, summary: {tasks_active: 4}}));
  assert.match(element("attention-summary").textContent, /As many tasks are advancing as this workspace allows/);
  await withStatus(report({agents: {holding: [], lapsed_count: 2}, summary: {tasks_active: 0}}));
  assert.match(element("attention-summary").textContent, /2 lease\(s\) lapsed without being released/);

  // A workspace that cannot be read says so rather than showing a stale answer.
  run("api = () => Promise.reject(new Error('Session expired'));");
  await run("attention()");
  assert.match(element("attention-summary").textContent, /Could not read the workspace state: Session expired/);

  console.log("PASS: states read as sentences, waiting work is separated from work in progress, and every row opens its task");
})().catch(error => { console.error(error); process.exitCode = 1; });
