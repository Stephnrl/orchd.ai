// Which agents exist, and which of them are actually there.
//
// Holding a task was the only way an agent was visible, so a container that is running and
// correctly idle — because nothing for its role is waiting — looked exactly like one that
// crashed an hour ago. These checks fix the dot that tells them apart, and the one case that
// should catch the eye: an agent whose task is stopped on a person.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const nodes = new Map();
function node(tag) {
  return {tag, className: "", textContent: "", value: "", hidden: false, checked: false,
    children: [], handlers: {},
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

function report(enrolled) {
  return {tasks: {waiting_on_a_person: []}, agents: {holding: [], lapsed_count: 0},
          work: {at_bound: false}, summary: {tasks_active: 0}, enrolled};
}
async function withStatus(value) {
  run("api = () => Promise.resolve(" + JSON.stringify(value) + ");");
  await run("attention()");
}
function rows() {
  return element("agent-list").children.map(item => ({
    dot: item.children[0].className,
    name: item.children[1].children[0].textContent,
    detail: item.children[1].children[1].textContent,
    when: item.children[2].textContent,
    highlighted: item.className,
    opens: item.children.length > 3 ? item.children[3].textContent : null}));
}

(async () => {
  const now = Date.now();
  const recently = new Date(now - 20000).toISOString();
  const ages = new Date(now - 7200000).toISOString();

  // No registry given: say so rather than implying there are no agents.
  await withStatus(report([]));
  assert.match(element("agents-summary").textContent, /No agent registry was given/);
  assert.equal(element("agent-list").children.length, 0);

  // Running and idle is not the same as gone, and the dot is the difference.
  await withStatus(report([
    {agent: "project-manager", roles: ["project_manager"], last_seen: recently, holding: null,
     state: "idle", needs_you: false},
    {agent: "junior-devops", roles: ["junior"], last_seen: null, holding: null,
     state: "not seen", needs_you: false},
    {agent: "lead-devops", roles: ["lead_planner"], last_seen: recently, holding: "abc123abc123",
     state: "working", needs_you: false}]));
  const listed = rows();
  assert.equal(listed.length, 3);
  assert.equal(listed[0].dot, "dot idle");
  assert.match(listed[0].detail, /Idle, waiting for work · project_manager/);
  assert.equal(listed[1].dot, "dot gone");
  assert.equal(listed[1].when, "never", "an agent never seen says so rather than showing a time");
  assert.equal(listed[2].dot, "dot working");
  assert.equal(listed[2].opens, "Open task", "a working agent offers the task it holds");
  assert.equal(listed[0].opens, null, "an idle agent holds nothing to open");
  assert.match(element("agents-summary").textContent, /2 of 3 present\./);

  // The case that should catch the eye.
  await withStatus(report([
    {agent: "lead-devops", roles: ["lead_planner"], last_seen: recently, holding: "task-1",
     state: "working", needs_you: true}]));
  const asking = rows()[0];
  assert.equal(asking.highlighted, "asking");
  assert.match(asking.detail, /Waiting on you before it can go on/);
  assert.match(element("agents-summary").textContent, /1 of 1 present, and 1 waiting on you\./);

  // How long ago it was last heard from, which is what makes "not seen" believable.
  await withStatus(report([
    {agent: "stale", roles: ["junior"], last_seen: ages, holding: null, state: "not seen", needs_you: false}]));
  assert.equal(rows()[0].when, "2h ago");

  // The window opens work an agent will take, not only the fixed fixture.
  element("title").value = "Add a rate limit";
  element("create-draft").checked = true;
  run("mutate = action => action(); api = (route, body) => { globalThis.sent = body; return Promise.resolve({task_id: 'new-task'}); };");
  element("create").handlers.submit({preventDefault() {}});
  // Compared as JSON: the object is made inside the vm, so its prototype is not this realm's.
  assert.equal(JSON.stringify(context.sent), '{"title":"Add a rate limit","draft":true}');
  element("create-draft").checked = false;
  element("create").handlers.submit({preventDefault() {}});
  assert.equal(context.sent.draft, false, "unchecked still creates the greeting fixture");

  console.log("PASS: presence separates idle from gone, an agent waiting on you is marked, and the window can open work an agent will take");
})().catch(error => { console.error(error); process.exitCode = 1; });
