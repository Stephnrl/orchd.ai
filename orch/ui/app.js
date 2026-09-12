"use strict";
const $ = id => document.getElementById(id);
let token = "", selected = null, snapshot = null, approval = null;
let epoch = 0, cursor = 0, taskNext = null, recordNext = null, busy = false;
const names = new Map();
const pretty = value => JSON.stringify(value, null, 2);
function notice(message) { $("notice").textContent = message; }
function controls() {
  $("run").disabled = busy || !snapshot || !!snapshot.state.pending_approval || ["COMPLETED", "FAILED", "CANCELLED", "BLOCKED"].includes(snapshot.state.state);
  const expired = !!approval && Date.parse(approval.expires_at) <= Date.now();
  $("approve").disabled = $("reject").disabled = busy || !approval || expired || !$("reviewed").checked;
  $("renew-approval").hidden = !expired;
  $("renew-approval").disabled = busy || !expired;
  $("approval-expiry").textContent = expired ? "This request has expired. Request a fresh approval, then review its evidence and decide separately." : "";
  $("create").querySelector("button").disabled = busy;
  $("cancellation").hidden = !snapshot || ["COMPLETED", "FAILED", "CANCELLED", "PR_CREATED"].includes(snapshot.state.state);
  $("cancel-task").disabled = busy || !snapshot || !!snapshot.state.active_operation_id || !!snapshot.state.active_invocation_id || !!snapshot.state.container_id;
}
async function api(path, payload) {
  const response = await fetch(path, {method: payload === undefined ? "GET" : "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, body: payload === undefined ? undefined : JSON.stringify(payload), cache: "no-store", credentials: "omit"});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? "Session expired. Disconnect and paste the session from the running server." : data.error || "Request failed.");
  return data;
}
function button(text, action) { const b = document.createElement("button"); b.type = "button"; b.textContent = text; b.addEventListener("click", () => action().catch(e => notice(e.message))); return b; }
function references(value, target) {
  target.replaceChildren(); const seen = new Set();
  function visit(v, label) {
    if (!v || typeof v !== "object") return;
    const type = v.artifact_id && v.sha256 ? "artifacts" : v.id && v.sha256 ? "records" : null;
    if (type) {
      const id = v.artifact_id || v.id, key = type + id;
      if (!seen.has(key)) { seen.add(key); target.append(button(label + " · " + id.slice(0, 8), () => evidence(type, id))); }
    } else for (const [key, child] of Object.entries(v)) visit(child, label ? label + "." + key : key);
  }
  visit(value, "");
}
async function evidence(type, id) {
  const version = epoch, task = selected;
  const data = await api(`/tasks/${task}/${type}/${encodeURIComponent(id)}`);
  if (version !== epoch) return;
  $("evidence").hidden = false;
  $("evidence-title").textContent = type === "records" ? data.contract_type : "Artifact content";
  $("evidence-trust").textContent = type === "artifacts" ? `Trust: ${data.reference.trust} · Producer: ${data.reference.producer_id} · SHA-256: ${data.reference.sha256}` : "Persisted contract · " + id;
  $("evidence-json").textContent = type === "artifacts" ? data.content : pretty(data);
  let linked = data;
  if (type === "artifacts") { try { linked = JSON.parse(data.content); } catch { linked = null; } }
  references(linked, $("evidence-links"));
}
async function tasks(more = false) {
  const version = epoch;
  const data = await api("/tasks?after=" + (more ? taskNext : 0));
  if (version !== epoch) return;
  if (!more) { $("tasks").replaceChildren(); names.clear(); }
  for (const item of data.items) {
    const b = button(item.task_id.slice(0, 12), () => select(item.task_id));
    b.className = "task-button"; b.setAttribute("aria-current", String(item.task_id === selected));
    const small = document.createElement("small"); small.textContent = item.state.state.replaceAll("_", " "); b.append(small);
    $("tasks").append(b); names.set(item.task_id, b);
  }
  taskNext = data.next; $("more-tasks").hidden = taskNext === null;
}
async function records(more = false) {
  const version = epoch, task = selected;
  const data = await api(`/tasks/${task}/records?after=${more ? recordNext : 0}`);
  if (version !== epoch) return;
  if (!more) $("records").replaceChildren();
  for (const item of data.items) $("records").append(button(item.kind + " · " + item.id.slice(0, 8), () => evidence("records", item.id)));
  recordNext = data.next; $("more-records").hidden = recordNext === null;
}
async function state() {
  const version = epoch, task = selected;
  const data = await api(`/tasks/${task}`);
  const spec = await api(`/tasks/${task}/records/${data.state.spec.id}`);
  const pending = data.state.pending_approval ? await api(`/tasks/${task}/records/${data.state.pending_approval.id}`) : null;
  const diagnostics = data.state.state === "BLOCKED" ? await api(`/tasks/${task}/recovery-diagnostics`) : null;
  if (version !== epoch) return;
  snapshot = data; approval = pending;
  $("task-title").textContent = spec.title; $("task-id").textContent = task;
  $("state").textContent = data.state.state.replaceAll("_", " "); $("revision").textContent = "Revision " + data.state.revision;
  $("state-json").textContent = pretty(data); $("approval").hidden = !pending; $("reviewed").checked = false;
  $("recovery-diagnostics").hidden = !diagnostics;
  if (diagnostics) {
    $("recovery-guidance").textContent = diagnostics.reasons.join(" ") + " " + diagnostics.next_step;
    $("recovery-json").textContent = pretty(diagnostics);
  }
  if (pending) {
    $("approval-summary").textContent = `${pending.kind === "plan" ? "Plan" : "Simulated action"} approval · Expires ${pending.expires_at}`;
    $("approval-json").textContent = pretty(pending); references(pending, $("approval-links"));
  }
  controls();
}
async function select(task) {
  if (busy) return;
  epoch++; selected = task; cursor = 0; snapshot = null; approval = null;
  $("cancel-reason").value = ""; $("cancellation").open = false;
  $("events").replaceChildren(); $("evidence").hidden = true; $("empty").hidden = true; $("detail").hidden = false; $("approval").hidden = true;
  for (const [id, b] of names) b.setAttribute("aria-current", String(id === task));
  controls(); await state(); await records(); await replay();
}
let replaying = false;
async function replay() {
  if (!selected || !token || replaying || busy) return;
  replaying = true; const version = epoch, task = selected;
  try {
    const response = await fetch(`/tasks/${task}/stream`, {headers: {Authorization: "Bearer " + token, "Last-Event-ID": String(cursor)}, cache: "no-store", credentials: "omit"});
    if (!response.ok) throw new Error("Event connection unavailable. Reconnect if the server restarted.");
    const text = await response.text(); if (version !== epoch) return;
    let added = false;
    for (const block of text.split("\n\n")) {
      const line = block.split("\n").find(l => l.startsWith("data: ")); if (!line) continue;
      const e = JSON.parse(line.slice(6)); if (e.sequence <= cursor) continue;
      if (e.sequence !== cursor + 1) throw new Error("Event gap detected. Reselect this task to replay.");
      cursor = e.sequence; added = true;
      const li = document.createElement("li");
      li.append(button(`${e.sequence}. ${e.event_type.replaceAll("_", " ")}`, () => evidence("artifacts", e.payload.artifact_id)));
      const small = document.createElement("small"); small.textContent = `${e.created_at} · ${e.actor.role}`; li.append(small); $("events").append(li);
    }
    $("event-status").textContent = `Replayed through event ${cursor}`;
    if (added) { await state(); await records(); }
  } finally { replaying = false; }
}
async function mutate(action) {
  if (busy) return; busy = true; controls();
  try { await action(); notice("Saved. Review the current state before the next action."); if (selected) { await state(); await records(); } await tasks(); }
  catch (e) { notice(e.message); if (selected && token) await state(); }
  finally { busy = false; controls(); }
}
$("connect").addEventListener("submit", async e => {
  e.preventDefault(); token = $("token").value.trim(); $("token").value = "";
  try { await tasks(); $("login").hidden = true; $("workspace").hidden = false; $("disconnect").hidden = false; notice("Connected to the local operator session."); }
  catch (error) { token = ""; notice(error.message); }
});
$("disconnect").addEventListener("click", () => location.reload());
$("refresh").addEventListener("click", () => tasks().catch(e => notice(e.message)));
$("more-tasks").addEventListener("click", () => tasks(true).catch(e => notice(e.message)));
$("more-records").addEventListener("click", () => records(true).catch(e => notice(e.message)));
$("reviewed").addEventListener("change", controls);
$("renew-approval").addEventListener("click", () => {
  if (busy || !approval || !snapshot) return;
  const task = selected, payload = {request_id: approval.id, expected_revision: snapshot.state.revision};
  mutate(() => api(`/tasks/${task}/renew-approval`, payload)).catch(e => notice(e.message));
});
$("create").addEventListener("submit", e => {
  e.preventDefault(); mutate(async () => { const result = await api("/tasks", {title: $("title").value}); epoch++; selected = result.task_id; cursor = 0; $("cancel-reason").value = ""; $("cancellation").open = false; $("events").replaceChildren(); $("evidence").hidden = true; $("empty").hidden = true; $("detail").hidden = false; }).catch(e => notice(e.message));
});
$("run").addEventListener("click", () => mutate(() => api(`/tasks/${selected}/run`, {})).catch(e => notice(e.message)));
$("cancel-form").addEventListener("submit", e => {
  e.preventDefault(); if (busy || !snapshot) return;
  const task = selected, payload = {expected_revision: snapshot.state.revision, reason: $("cancel-reason").value};
  mutate(() => api(`/tasks/${task}/cancel`, payload)).catch(e => notice(e.message));
});
for (const decision of ["approve", "reject"]) $(decision).addEventListener("click", () => {
  if (!approval || !$("reviewed").checked || busy) return;
  const task = selected, payload = {request_id: approval.id, decision, expected_revision: snapshot.state.revision};
  mutate(() => api(`/tasks/${task}/approvals`, payload)).catch(e => notice(e.message));
});
setInterval(() => { controls(); replay().catch(e => notice(e.message)); }, 2000);
