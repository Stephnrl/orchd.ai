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
// The panel now asks two things: what the store knows about each agent, and what Docker
// says about its container. The stub answers by route, because the roster's whole point is
// what those two say together.
async function withStatus(value, containers) {
  const lifecycle = containers || {agents: [], docker: "available", reason: null};
  run("api = path => Promise.resolve(path === '/agents' ? " + JSON.stringify(lifecycle)
      + " : " + JSON.stringify(value) + ");");
  await run("attention()");
}
function container(agent, fields) {
  return {agent, declared: true, role: "lead_planner", image: "sha256:" + "a".repeat(64),
          state: "no container", container: null, status: null, reason: null, ...fields};
}
function rows() {
  return element("agent-list").children.map(item => ({
    dot: item.children[0].className,
    name: item.children[1].children[0].textContent,
    detail: item.children[1].children[1].textContent,
    when: item.children[2].textContent,
    highlighted: item.className,
    buttons: item.children.slice(3).map(control => control.textContent)}));
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
  assert.deepEqual(listed[2].buttons, ["Open task", "Ask"], "a working agent offers the task it holds");
  assert.deepEqual(listed[0].buttons, ["Ask"],
                   "an idle agent holds nothing to open, and is still the one you would ask");
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

  // Asking the agent in front of you: the button puts its role into the question form,
  // and asks nothing by itself.
  await withStatus(report([
    {agent: "lead-devops", roles: ["lead_planner"], last_seen: recently, holding: "task-1",
     state: "working", needs_you: false}]));
  assert.deepEqual(rows()[0].buttons, ["Open task", "Ask"]);
  run("globalThis.sent = null; api = (route, body) => { globalThis.sent = [route, body]; return Promise.resolve({}); };");
  element("agent-list").children[0].children[4].handlers.click();
  assert.equal(context.sent, null, "choosing who to ask asks nothing");
  assert.equal(element("consult-role").value, "lead_planner", "the form is pointed at that agent's role");

  // Running a declared container, which is the thing this window could not do before.
  const enrolled = [{agent: "lead-devops", roles: ["lead_planner"], last_seen: null, holding: null,
                     state: "not seen", needs_you: false}];
  await withStatus(report(enrolled), {agents: [container("lead-devops")], docker: "available", reason: null});
  assert.match(rows()[0].detail, /Not started/, "a declared agent that is not running says so");
  assert.deepEqual(rows()[0].buttons, ["Ask", "Run"]);

  // The pair of signals presence alone could not give: the container is up and nothing calls.
  await withStatus(report(enrolled), {agents: [container("lead-devops", {state: "running", container: "abc123"})],
                                      docker: "available", reason: null});
  assert.match(rows()[0].detail, /Container running, but not calling in/);
  assert.equal(rows()[0].highlighted, "asking", "up and silent is the one to look at");
  assert.deepEqual(rows()[0].buttons, ["Ask", "Stop"]);

  // Running and calling in is ordinary, and reads the way it always did.
  await withStatus(report([{...enrolled[0], last_seen: recently, state: "idle"}]),
                   {agents: [container("lead-devops", {state: "running", container: "abc123"})],
                    docker: "available", reason: null});
  assert.match(rows()[0].detail, /Idle, waiting for work/);
  assert.deepEqual(rows()[0].buttons, ["Ask", "Stop"]);

  // An agent with no declaration is not offered a button that cannot work.
  await withStatus(report(enrolled), {agents: [container("lead-devops", {declared: false, role: null, image: null})],
                                      docker: "available", reason: null});
  assert.match(rows()[0].detail, /No container declared/);
  assert.deepEqual(rows()[0].buttons, ["Ask"]);

  // Nor is one offered while Docker cannot be asked; the summary says why instead.
  await withStatus(report(enrolled), {agents: [container("lead-devops")], docker: "unavailable",
                                      reason: "docker_cli_missing"});
  assert.deepEqual(rows()[0].buttons, ["Ask"]);
  assert.match(element("agents-summary").textContent, /Docker is not on this machine's PATH\./);

  // Run posts to that agent's own route and sends nothing else.
  await withStatus(report(enrolled), {agents: [container("lead-devops")], docker: "available", reason: null});
  run("globalThis.calls = []; api = (route, body) => { globalThis.calls.push([route, body]); "
      + "return Promise.resolve({status: 'started', agent: 'lead-devops', reason: null}); };");
  await element("agent-list").children[0].children[4].handlers.click();
  assert.equal(context.calls[0][0], "/agents/lead-devops/start");
  assert.equal(JSON.stringify(context.calls[0][1]), "{}", "nothing from this page is part of a command");
  assert.match(element("notice").textContent, /lead-devops started\./);

  // A refusal is shown as a sentence rather than a code.
  await withStatus(report(enrolled), {agents: [container("lead-devops")], docker: "available", reason: null});
  run("api = () => Promise.resolve({status: 'refused', agent: 'lead-devops', reason: 'image_not_present'});");
  await element("agent-list").children[0].children[4].handlers.click();
  assert.match(element("notice").textContent, /That image is not on this machine/);

  console.log("PASS: presence separates idle from gone, a container that is up while nothing calls is marked, and Run acts only on what was declared");
})().catch(error => { console.error(error); process.exitCode = 1; });
