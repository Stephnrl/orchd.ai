"use strict";
const $ = id => document.getElementById(id);
let token = "", selected = null, snapshot = null, approval = null, recovery = null;
let epoch = 0, cursor = 0, taskNext = null, recordNext = null, busy = false;
let stateRequest = 0;
let evidenceRequest = 0;
let taskListRequest = 0, recordListRequest = 0, tasksLoading = false, recordsLoading = false;
const names = new Map();
const pretty = value => JSON.stringify(value, null, 2);
function notice(message) { $("notice").textContent = message; }
function canRecover() {
  return !busy && !!snapshot && snapshot.state.state === "BLOCKED" && !!recovery &&
    recovery.task_id === selected && recovery.revision === snapshot.state.revision &&
    recovery.recovery_status === "preconditions_met" && $("recovery-reviewed").checked;
}
function cleanupAvailable() {
  const outcome = snapshot?.context?.operator_recovery;
  return !!outcome && snapshot.state.task_id === selected && snapshot.state.state === "CHANGES_REQUESTED" &&
    snapshot.state.revision === outcome.completed_revision && !snapshot.state.active_operation_id &&
    typeof outcome.operation_id === "string" && /^[a-f0-9]{32}$/.test(outcome.operation_id) &&
    ["pending", "deferred"].includes(outcome.cleanup);
}
function canRetryCleanup() {
  return !busy && cleanupAvailable() && $("cleanup-reviewed").checked;
}
function controls() {
  $("more-tasks").disabled = tasksLoading || taskNext === null;
  $("more-records").disabled = recordsLoading || recordNext === null;
  $("refresh-task").disabled = busy || !token || !selected;
  $("storage-report").disabled = $("integrity-report").disabled = busy || !token;
  $("cleanup-retry").hidden = !cleanupAvailable();
  $("retry-cleanup").disabled = !canRetryCleanup();
  $("recover-task").disabled = !canRecover();
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
  const version = epoch, task = selected, request = ++evidenceRequest;
  $("evidence").hidden = false;
  $("evidence-title").textContent = "Loading evidence…";
  $("evidence-trust").textContent = type + " · " + id;
  $("evidence-json").textContent = "";
  $("evidence-links").replaceChildren();
  try {
    const data = await api(`/tasks/${task}/${type}/${encodeURIComponent(id)}`);
    if (version !== epoch || request !== evidenceRequest) return;
    $("evidence-title").textContent = type === "records" ? data.contract_type : "Artifact content";
    $("evidence-trust").textContent = type === "artifacts" ? `Trust: ${data.reference.trust} · Producer: ${data.reference.producer_id} · SHA-256: ${data.reference.sha256}` : "Persisted contract · " + id;
    $("evidence-json").textContent = type === "artifacts" ? data.content : pretty(data);
    let linked = data;
    if (type === "artifacts") { try { linked = JSON.parse(data.content); } catch { linked = null; } }
    references(linked, $("evidence-links"));
  } catch (error) {
    if (version === epoch && request === evidenceRequest) {
      $("evidence-title").textContent = "Evidence unavailable. Select the reference to retry.";
      $("evidence-json").textContent = "";
      $("evidence-links").replaceChildren();
      throw error;
    }
  }
}
async function tasks(more = false) {
  if (more && (tasksLoading || taskNext === null)) return;
  const version = epoch, request = ++taskListRequest, after = more ? taskNext : 0;
  if (!more) taskNext = null;
  tasksLoading = true; controls();
  try {
    const data = await api("/tasks?after=" + after);
    if (version !== epoch || request !== taskListRequest) return;
    if (!more) { $("tasks").replaceChildren(); names.clear(); }
    for (const item of data.items) {
      const b = button(item.title, () => select(item.task_id));
      b.className = "task-button"; b.setAttribute("aria-current", String(item.task_id === selected));
      const small = document.createElement("small"); small.textContent = item.task_id.slice(0, 12) + " · " + item.state.state.replaceAll("_", " "); b.append(small);
      $("tasks").append(b); names.set(item.task_id, b);
    }
    taskNext = data.next; $("more-tasks").hidden = taskNext === null;
  } catch (error) {
    if (version === epoch && request === taskListRequest) throw error;
  } finally {
    if (request === taskListRequest) { tasksLoading = false; controls(); }
  }
}
async function records(more = false) {
  if (more && (recordsLoading || recordNext === null)) return;
  const version = epoch, task = selected, request = ++recordListRequest, after = more ? recordNext : 0;
  if (!more) recordNext = null;
  recordsLoading = true; controls();
  try {
    const data = await api(`/tasks/${task}/records?after=${after}`);
    if (version !== epoch || request !== recordListRequest) return;
    if (!more) $("records").replaceChildren();
    for (const item of data.items) $("records").append(button(item.kind + " · " + item.id.slice(0, 8), () => evidence("records", item.id)));
    recordNext = data.next; $("more-records").hidden = recordNext === null;
  } catch (error) {
    if (version === epoch && request === recordListRequest) throw error;
  } finally {
    if (request === recordListRequest) { recordsLoading = false; controls(); }
  }
}
async function state() {
  const version = epoch, task = selected, request = ++stateRequest;
  snapshot = null; approval = null; recovery = null;
  $("task-refresh-status").textContent = "Refreshing task… Previous details may be out of date. Actions are disabled until refresh succeeds.";
  $("reviewed").checked = $("recovery-reviewed").checked = $("cleanup-reviewed").checked = false;
  controls();
  try {
    const data = await api(`/tasks/${task}`);
    const spec = await api(`/tasks/${task}/records/${data.state.spec.id}`);
    const pending = data.state.pending_approval ? await api(`/tasks/${task}/records/${data.state.pending_approval.id}`) : null;
    const diagnostics = data.state.state === "BLOCKED" ? await api(`/tasks/${task}/recovery-diagnostics`) : null;
    if (version !== epoch || request !== stateRequest) return;
    snapshot = data; approval = pending; recovery = diagnostics;
    $("task-title").textContent = spec.title; $("task-id").textContent = task;
    $("state").textContent = data.state.state.replaceAll("_", " "); $("revision").textContent = "Revision " + data.state.revision;
    $("state-json").textContent = pretty(data); $("approval").hidden = !pending; $("reviewed").checked = false;
    $("recovery-diagnostics").hidden = !diagnostics;
    const outcome = data.context?.operator_recovery;
    $("recovery-outcome").hidden = !outcome;
    if (outcome) $("recovery-outcome").textContent = outcome.cleanup === "deferred" ?
      "Recovery committed. Workspace cleanup was deferred; inspect the retained files before continuing." :
      outcome.cleanup === "pending" ? "Recovery committed. Cleanup is pending; inspect the workspace before retrying cleanup." :
      "Recovery committed and workspace cleanup completed.";
    if (diagnostics) {
      $("recovery-guidance").textContent = diagnostics.reasons.join(" ") + " " + diagnostics.next_step;
      $("recovery-json").textContent = pretty(diagnostics);
    }
    if (pending) {
      $("approval-summary").textContent = `${pending.kind === "plan" ? "Plan" : "Simulated action"} approval · Expires ${pending.expires_at}`;
      $("approval-json").textContent = pretty(pending); references(pending, $("approval-links"));
    }
    $("task-refresh-status").textContent = "Task refreshed. Review the current evidence before taking action.";
    controls();
  } catch (error) {
    // An obsolete request must not replace a newer view with an error either.
    if (version === epoch && request === stateRequest) {
      $("task-refresh-status").textContent = "Task refresh failed. Previous details may be out of date. Use Refresh task to try again.";
      throw error;
    }
  }
}
async function select(task) {
  if (busy) return;
  epoch++; selected = task; cursor = 0; snapshot = null; approval = null; recovery = null;
  recordNext = null; $("records").replaceChildren();
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
async function maintenanceReport(kind) {
  if (busy || !token || !["storage", "integrity"].includes(kind)) return;
  busy = true; controls();
  $("maintenance-summary").hidden = $("maintenance-details").hidden = true;
  $("maintenance-summary").textContent = $("maintenance-json").textContent = "";
  $("maintenance-status").textContent = kind === "storage" ? "Checking storage usage…" : "Checking persisted evidence…";
  try {
    const report = await api(kind === "storage" ? "/storage" : "/integrity-report");
    if (kind === "integrity") {
      if (!["passed", "failed"].includes(report.status)) throw new Error("Unrecognized audit result. No verdict available.");
      $("maintenance-status").textContent = report.status === "passed" ? "Integrity audit passed." : "Integrity audit failed. Inspect retained evidence before continuing.";
      $("maintenance-summary").textContent = report.status === "passed" ?
        `Checked ${report.generated_at}\nTasks: ${report.tasks} · Records: ${report.records} · Artifact references: ${report.artifact_references}` :
        `Checked ${report.generated_at}\nReason: ${report.reason}`;
    } else {
      $("maintenance-status").textContent = "Storage report ready. This is not an integrity audit.";
      $("maintenance-summary").textContent = `Checked ${report.generated_at}\nArtifact bytes: ${report.physical_artifact_bytes} / ${report.disk_quota_bytes}\nOrdinary quota headroom: ${report.disk_headroom_bytes} bytes\nOrphan files: ${report.orphan_files} · Eligible for GC: ${report.gc_eligible_files}\nMissing referenced files: ${report.missing_referenced_files} · Size mismatches: ${report.size_mismatches}\nUnexpected entries: ${report.unexpected_entries}`;
    }
    $("maintenance-json").textContent = pretty(report);
    $("maintenance-summary").hidden = $("maintenance-details").hidden = false;
  } catch (error) {
    $("maintenance-status").textContent = "Report unavailable. " + error.message;
  } finally { busy = false; controls(); }
}
$("storage-report").addEventListener("click", () => maintenanceReport("storage"));
$("integrity-report").addEventListener("click", () => maintenanceReport("integrity"));
$("connect").addEventListener("submit", async e => {
  e.preventDefault(); token = $("token").value.trim(); $("token").value = "";
  try { await tasks(); $("login").hidden = true; $("workspace").hidden = false; $("disconnect").hidden = false; notice("Connected to the local operator session."); }
  catch (error) { token = ""; notice(error.message); }
});
$("disconnect").addEventListener("click", () => location.reload());
$("refresh").addEventListener("click", () => tasks().catch(e => notice(e.message)));
$("refresh-task").addEventListener("click", () => {
  if (busy || !token || !selected) return;
  return state().catch(e => notice(e.message));
});
$("more-tasks").addEventListener("click", () => tasks(true).catch(e => notice(e.message)));
$("more-records").addEventListener("click", () => records(true).catch(e => notice(e.message)));
$("reviewed").addEventListener("change", controls);
$("recovery-reviewed").addEventListener("change", controls);
$("cleanup-reviewed").addEventListener("change", controls);
$("retry-cleanup").addEventListener("click", () => {
  if (!canRetryCleanup()) return;
  const task = selected, payload = {operation_id: snapshot.context.operator_recovery.operation_id, expected_revision: snapshot.state.revision};
  mutate(() => api(`/tasks/${task}/retry-cleanup`, payload)).catch(e => notice(e.message));
});
$("recover-task").addEventListener("click", () => {
  if (!canRecover()) return;
  const task = selected, payload = {expected_revision: recovery.revision};
  mutate(() => api(`/tasks/${task}/recover`, payload)).catch(e => notice(e.message));
});
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
