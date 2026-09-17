// Asking an agent a question, and reading what it thinks.
//
// The panel's whole claim is that a question is not a task: nothing it shows is an approval,
// nothing it offers advances anything, and the only decision it records is a person saying
// they have read an answer. These checks hold it to that, and to saying out loud that an
// answer is an opinion rather than authority.
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
  Date, Map, Set, setInterval() {}, JSON, Number, String, Math, encodeURIComponent});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../orch/ui/app.js"), "utf8"), context);
const run = code => vm.runInContext(code, context);

function consultation(fields) {
  return {kind: "Consultation", consultation_id: "c".repeat(32), role: "lead_planner",
          question: "What are your inputs if I deployed this?", about: null,
          asked_by: "local-operator", asked_at: new Date(Date.now() - 60000).toISOString(),
          state: "asked", agent: null, answer: null, answered_at: null,
          answer_is_untrusted: true, approves_nothing: true, redacted: false,
          live_authorized: false, retry_allowed: false, ...fields};
}
function listing(rows) {
  return {kind: "Consultations", consultations: rows, count: rows.length,
          open: rows.filter(row => ["asked", "answering"].includes(row.state)).length,
          answered: rows.filter(row => row.state === "answered").length,
          live_authorized: false, retry_allowed: false};
}
async function withConsultations(rows) {
  run("api = () => Promise.resolve(" + JSON.stringify(listing(rows)) + ");");
  await run("consultations()");
}
function shown() {
  return element("consult-list").children.map(item => ({
    highlighted: item.className,
    lines: item.children[0].children.map(part => part.className + ": " + part.textContent),
    age: item.children[1].textContent,
    buttons: item.children.slice(2).map(control => control.textContent)}));
}

(async () => {
  // Nothing asked yet says so, rather than showing an empty box.
  await withConsultations([]);
  assert.equal(element("consult-list").children.length, 0);
  assert.equal(element("consult-status").textContent, "No questions asked.");

  // A question that has not been picked up: who it went to, and that it is waiting.
  await withConsultations([consultation({})]);
  const waiting = shown()[0];
  assert.match(waiting.lines[0], /name: To the lead planner/);
  assert.match(waiting.lines[1], /why: What are your inputs if I deployed this\?/);
  assert.match(waiting.lines[2], /hint: Waiting for an agent to pick this up/);
  assert.deepEqual(waiting.buttons, ["Withdraw"], "a question not yet answered can be taken back");
  assert.match(element("consult-status").textContent, /1 question\(s\) waiting for an agent\./);

  // An answer is shown as an answer, and said out loud to be an opinion.
  await withConsultations([consultation({
    state: "answered", agent: "lead-devops", answer: "Three inputs: the VPC id, the instance type and the AMI.",
    answered_at: new Date(Date.now() - 30000).toISOString()})]);
  const answered = shown()[0];
  assert.equal(answered.highlighted, "answered");
  assert.match(answered.lines[2], /answer: Three inputs: the VPC id/);
  assert.match(answered.lines[3], /hint: lead-devops answered\. This is an opinion: it approves nothing and changes nothing\./);
  assert.deepEqual(answered.buttons, ["Mark read"], "the only decision here is having read it");
  assert.match(element("consult-status").textContent, /1 answer\(s\) to read\./);

  // Redaction is disclosed, because text a person will read as quoted was altered.
  await withConsultations([consultation({redacted: true})]);
  assert.match(shown()[0].lines[3], /looked like a credential was replaced/);

  // Questions finished with leave the window; a panel that only grows is one people stop
  // reading, and neither a read answer nor a withdrawn question has anything left to do.
  await withConsultations([consultation({consultation_id: "d".repeat(32), state: "closed",
                                         answer: "Read already", answered_at: new Date().toISOString()}),
                           consultation({consultation_id: "e".repeat(32), state: "withdrawn"})]);
  assert.equal(element("consult-list").children.length, 0);
  assert.equal(element("consult-status").textContent, "Nothing is waiting for an agent.");

  // Newest question first: the one you just asked is the one you are looking for.
  await withConsultations([consultation({consultation_id: "a".repeat(32), question: "First"}),
                           consultation({consultation_id: "b".repeat(32), question: "Second"})]);
  assert.match(shown()[0].lines[1], /why: Second/);

  // Asking sends the role and the question, and nothing else.
  // Every call is kept, not only the last: asking reloads the list, so the interesting
  // call is the first one rather than the one that happens to be most recent.
  const record = "globalThis.calls = []; api = (route, body) => { globalThis.calls.push([route, body]); " +
    "return Promise.resolve(" + JSON.stringify(listing([])) + "); };";
  run("mutate = action => action(); selected = null; " + record);
  element("consult-role").value = "junior";
  element("consult-question").value = "Is this terraform module ours?";
  element("consult-about").checked = false;
  await element("consult-form").handlers.submit({preventDefault() {}});
  // Compared as JSON: the object is made inside the vm, so its prototype is not this realm's.
  assert.equal(context.calls[0][0], "/consultations");
  assert.equal(JSON.stringify(context.calls[0][1]), '{"role":"junior","question":"Is this terraform module ours?"}');
  assert.equal(element("consult-question").value, "", "the box is cleared once the question is asked");

  // A question may name a task as read-only context, and only when one is selected.
  element("consult-question").value = "What do you think of this?";
  element("consult-about").checked = true;
  run(record);
  await element("consult-form").handlers.submit({preventDefault() {}});
  assert.equal(JSON.stringify(context.calls[0][1]),
               '{"role":"junior","question":"What do you think of this?"}',
               "about is left out when nothing is selected to be about");
  run("selected = 'task-9'; " + record);
  element("consult-question").value = "What do you think of this?";
  element("consult-about").checked = true;
  await element("consult-form").handlers.submit({preventDefault() {}});
  assert.equal(JSON.parse(JSON.stringify(context.calls[0][1])).about, "task-9");

  // Withdrawing and marking read post to their own routes and nothing else.
  await withConsultations([consultation({})]);
  run(record);
  await element("consult-list").children[0].children[2].handlers.click();
  assert.equal(context.calls[0][0], "/consultations/" + "c".repeat(32) + "/withdraw");

  await withConsultations([consultation({state: "answered", agent: "lead-devops", answer: "Yes",
                                         answered_at: new Date().toISOString()})]);
  run(record);
  await element("consult-list").children[0].children[2].handlers.click();
  assert.equal(context.calls[0][0], "/consultations/" + "c".repeat(32) + "/close");

  console.log("PASS: a question is asked, an answer is read as an opinion, and neither approves anything");
})().catch(error => { console.error(error); process.exitCode = 1; });
