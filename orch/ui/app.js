"use strict";
const $ = id => document.getElementById(id);
let token = "", selected = null, snapshot = null, approval = null, recovery = null;
let epoch = 0, cursor = 0, taskNext = null, recordNext = null, busy = false;
let stateRequest = 0;
let evidenceRequest = 0;
let evidenceDownload = null;
let claimRequest = 0, claimLoading = false, claimResult = null;
let claimDownloadController = null;
let bundleReviewVersion = 0, bundleReviewController = null, bundleReviewResult = null;
let claimChoices = {review: new Map(), policy: new Map()};
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
  if (typeof batchControls === 'function') batchControls();
  for (const id of ["session-check", "session-rotate", "session-revoke"]) $(id).disabled = busy || !token;
  bundleReviewControls();
  claimControls();
  $("save-evidence").disabled = busy || !evidenceDownload || evidenceDownload.epoch !== epoch;
  $("more-tasks").disabled = tasksLoading || taskNext === null;
  $("more-records").disabled = recordsLoading || recordNext === null;
  $("refresh-task").disabled = busy || !token || !selected;
  $("storage-report").disabled = $("pilot-usage-report").disabled = $("integrity-report").disabled = busy || !token;
  $("cleanup-retry").hidden = !cleanupAvailable();
  $("retry-cleanup").disabled = !canRetryCleanup();
  $("recover-task").disabled = !canRecover();
  $("run").disabled = busy || !snapshot || !!snapshot.state.pending_approval || ["COMPLETED", "FAILED", "CANCELLED", "BLOCKED"].includes(snapshot.state.state);
  const expired = !!approval && Date.parse(approval.expires_at) <= Date.now();
  $("approve").disabled = $("reject").disabled = busy || !approval || expired || !$("reviewed").checked;
  $("renew-approval").hidden = !expired;
  $("renew-approval").disabled = busy || !expired || (snapshot?.context?.workload === "repository-json-v1" && approval?.kind === "plan");
  $("approval-expiry").textContent = expired ? (snapshot?.context?.workload === "repository-json-v1" && approval?.kind === "plan" ? "Execution scope expired. Cancel this task and create a newly prepared repository task." : "This request has expired. Request a fresh approval, then review its evidence and decide separately.") : "";
  $("create").querySelector("button").disabled = busy;
  $("cancellation").hidden = !snapshot || ["COMPLETED", "FAILED", "CANCELLED", "PR_CREATED"].includes(snapshot.state.state);
  $("cancel-task").disabled = busy || !snapshot || !!snapshot.state.active_operation_id || !!snapshot.state.active_invocation_id || !!snapshot.state.container_id;
}
function clearSessionView() {
  if (typeof resetBatches === 'function') resetBatches();
  epoch++; stateRequest++; evidenceRequest++; taskListRequest++; recordListRequest++;
  selected = snapshot = approval = recovery = evidenceDownload = null;
  cursor = 0; taskNext = recordNext = null; tasksLoading = recordsLoading = false;
  names.clear(); resetClaimReview(); clearBundleReview("No bundle checked.");
  for (const id of ["tasks", "records", "events", "evidence-links", "approval-links", "consult-list"]) $(id).replaceChildren();
  $("consult-question").value = ""; $("consult-about").checked = false;
  $("consult-status").textContent = "No questions asked.";
  for (const id of ["state-json", "evidence-json", "approval-json", "maintenance-json", "maintenance-summary", "task-title", "task-id", "recovery-json"]) $(id).textContent = "";
  $("reviewed").checked = $("recovery-reviewed").checked = $("cleanup-reviewed").checked = false;
  $("bundle-file").value = $("bundle-expected").value = $("token").value = "";
  $("session-status").textContent = "Session status has not been checked.";
  $("detail").hidden = $("evidence").hidden = true; $("empty").hidden = false;
  controls();
}
function endLocalSession(message) {
  token = ""; clearSessionView();
  $("workspace").hidden = true; $("login").hidden = false; $("disconnect").hidden = true;
  notice(message);
}
async function sessionFetch(path, options) {
  const requestedToken = token;
  const response = await fetch(path, options);
  if (requestedToken !== token) throw new Error("Session changed; previous response discarded.");
  if (response.status === 401) {
    const message = "Session expired or revoked. Use a current token, or restart the server for a new session.";
    endLocalSession(message);
    throw new Error(message);
  }
  return response;
}
async function api(path, payload) {
  const requestedToken = token;
  const response = await sessionFetch(path, {method: payload === undefined ? "GET" : "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, body: payload === undefined ? undefined : JSON.stringify(payload), cache: "no-store", credentials: "omit"});
  const data = await response.json();
  if (requestedToken !== token) throw new Error("Session changed; previous response discarded.");
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
  evidenceDownload = null; $("save-evidence").disabled = true;
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
    evidenceDownload = {epoch: version, text: $("evidence-json").textContent,
      filename: type === "records" ? "orchd-record.json" : "orchd-artifact.txt"};
    $("save-evidence").disabled = busy;
  } catch (error) {
    if (version === epoch && request === evidenceRequest) {
      $("evidence-title").textContent = "Evidence unavailable. Select the reference to retry.";
      $("evidence-json").textContent = "";
      $("evidence-links").replaceChildren();
      throw error;
    }
  }
}
function bundleReviewControls() {
  const file = $("bundle-file").files?.[0];
  $("verify-bundle").disabled = busy || !token || !!bundleReviewController || !file || file.size < 1 || file.size > 16 * 1024 * 1024 || !/^[a-f0-9]{64}$/.test($("bundle-expected").value);
  $("save-bundle-review").disabled = busy || !token || !bundleReviewResult;
}
function clearBundleReview(message) {
  bundleReviewVersion++;
  bundleReviewController?.abort(); bundleReviewController = null; bundleReviewResult = null;
  $("bundle-review-status").textContent = message;
  $("bundle-review-json").textContent = ""; $("bundle-review-details").hidden = true;
  bundleReviewControls();
}
for (const [id, event] of [["bundle-file", "change"], ["bundle-expected", "input"]]) $(id).addEventListener(event, () => clearBundleReview("Inputs changed. Verify the selected file against its expected digest."));
$("clear-bundle-review").addEventListener("click", () => {
  $("bundle-file").value = ""; $("bundle-expected").value = "";
  clearBundleReview("Review cleared. Select a bundle and enter its expected SHA-256.");
});
$("verify-bundle").addEventListener("click", async () => {
  bundleReviewControls();
  if ($("verify-bundle").disabled) return;
  const file = $("bundle-file").files[0], expected = $("bundle-expected").value;
  clearBundleReview("Checking file digest and verifying bundle…");
  const version = bundleReviewVersion, controller = new AbortController();
  bundleReviewController = controller; bundleReviewControls();
  try {
    const bytes = await file.arrayBuffer();
    if (version !== bundleReviewVersion || controller.signal.aborted) return;
    if (bytes.byteLength !== file.size) throw new Error("File size changed.");
    const sha = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
    if (version !== bundleReviewVersion || controller.signal.aborted) return;
    if (sha !== expected) throw new Error("The file does not match the expected SHA-256. Nothing was uploaded.");
    const response = await sessionFetch('/evidence-bundles/verify', {method: "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/octet-stream", "X-Orch-Expected-SHA256": expected}, body: bytes, cache: "no-store", credentials: "omit", signal: controller.signal});
    if (!response.ok) throw new Error(response.status === 401 ? "Session expired. Reconnect." : "The server rejected the bundle. Check the file and expected digest.");
    const report = await response.json();
    if (version !== bundleReviewVersion || controller.signal.aborted) return;
    if (report.kind !== "GitHubEvidenceBundleAssessment" || report.binding?.bundle_sha256 !== expected || !["claims_consistent", "blocked"].includes(report.status) || report.live_authorized !== false || report.evidence_verified !== false) throw new Error("Unexpected verification response.");
    bundleReviewResult = report;
    $("bundle-review-status").textContent = (report.status === "claims_consistent" ? "Bundle bytes verified; retained claims are consistent." : "Bundle bytes verified; retained claims are blocked.") + " This does not authenticate the author or approve execution. See the diagnostic details.";
    $("bundle-review-json").textContent = pretty(report); $("bundle-review-details").hidden = false;
  } catch (error) {
    if (version === bundleReviewVersion && !controller.signal.aborted) clearBundleReview("Verification unavailable. " + error.message);
  } finally {
    if (bundleReviewController === controller) bundleReviewController = null;
    bundleReviewControls();
  }
});
$("save-bundle-review").addEventListener("click", () => {
  if (busy || !token || !bundleReviewResult) return;
  const url = URL.createObjectURL(new Blob([pretty(bundleReviewResult)], {type: "application/json"})), link = document.createElement("a");
  link.href = url; link.download = "orchd-bundle-assessment.json"; document.body.append(link);
  try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
});
$("save-evidence").addEventListener("click", () => {
  if (busy || !evidenceDownload || evidenceDownload.epoch !== epoch) return;
  const url = URL.createObjectURL(new Blob([evidenceDownload.text], {type: "text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url; link.download = evidenceDownload.filename;
  document.body.append(link);
  try { link.click(); }
  finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
});
function claimControls() {
  const unavailable = busy || claimLoading || !token || !selected || !snapshot;
  $("load-claims").disabled = unavailable;
  for (const role of ["review", "policy"]) $("claim-" + role).disabled = unavailable || !claimChoices[role].size;
  for (const role of ["review", "policy"]) $("inspect-claim-" + role).disabled = unavailable || !claimChoices[role].has($("claim-" + role).value);
  $("assess-claims").disabled = unavailable || !claimChoices.review.has($("claim-review").value) || !claimChoices.policy.has($("claim-policy").value);
  $("save-selection").disabled = unavailable || !claimResult || claimResult.epoch !== epoch;
  $("download-bundle").disabled = unavailable || !!claimDownloadController || !claimResult || claimResult.epoch !== epoch || claimResult.status !== "claims_consistent";
}
function clearClaimResult(message) {
  claimDownloadController?.abort(); claimDownloadController = null;
  $("bundle-digest").textContent = "";
  $("download-bundle").disabled = true;
  claimResult = null;
  $("claims-status").textContent = message;
  $("claims-blockers").replaceChildren();
  $("claims-json").textContent = "";
  $("claims-details").hidden = true;
  $("save-selection").disabled = true;
}
function resetClaimReview() {
  claimRequest++; claimLoading = false;
  claimChoices = {review: new Map(), policy: new Map()};
  for (const role of ["review", "policy"]) {
    const select = $("claim-" + role), placeholder = document.createElement("option");
    placeholder.value = ""; placeholder.textContent = "Choose a " + role;
    select.replaceChildren(); select.append(placeholder); select.value = "";
  }
  clearClaimResult("Load choices to select an evidence chain.");
}
async function loadClaimChoices() {
  if (busy || !selected || !token || !snapshot) return;
  resetClaimReview();
  const version = epoch, task = selected, request = ++claimRequest;
  claimLoading = true; claimControls();
  $("claims-status").textContent = "Loading review and policy choices…";
  try {
    const catalog = await api(`/tasks/${task}/evidence-catalog`);
    if (version !== epoch || request !== claimRequest) return;
    for (const item of catalog.binding.records) {
      if (!["review", "policy"].includes(item.role)) continue;
      claimChoices[item.role].set(item.record.id, item.record);
      const option = document.createElement("option");
      option.value = item.record.id;
      option.textContent = `${item.created_at} · ${item.record.id}`;
      $("claim-" + item.role).append(option);
    }
    $("claims-status").textContent = claimChoices.review.size && claimChoices.policy.size ?
      "Choose a review and policy explicitly. No records are selected automatically." :
      "No complete review and policy pair is available yet. Reload choices after the workflow reaches review.";
  } catch (error) {
    if (version === epoch && request === claimRequest) clearClaimResult("Choices unavailable. " + error.message);
  } finally { if (version === epoch && request === claimRequest) { claimLoading = false; claimControls(); } }
}
async function assessClaimSelection() {
  if (busy || claimLoading || !snapshot || !token) return;
  const review = claimChoices.review.get($("claim-review").value), policy = claimChoices.policy.get($("claim-policy").value);
  if (!review || !policy) return;
  const version = epoch, task = selected, request = ++claimRequest;
  clearClaimResult("Checking selected evidence…"); claimLoading = true; claimControls();
  try {
    const result = await api(`/tasks/${task}/assess-evidence`, {review, policy});
    if (version !== epoch || request !== claimRequest) return;
    claimResult = {epoch: version, selection: result.selection, status: result.assessment.status};
    const assessment = result.assessment, claims = assessment.binding.claims;
    $("claims-status").textContent = (assessment.status === "claims_consistent" ? "Selected evidence claims are consistent." : "Selected evidence is blocked.") +
      " Checked " + claims.checked_at + ". This does not approve or run the task.";
    for (const reason of claims.blockers) {
      const item = document.createElement("li"); item.textContent = reason; $("claims-blockers").append(item);
    }
    $("claims-json").textContent = pretty(result);
    $("claims-details").hidden = false;
  } catch (error) {
    if (version === epoch && request === claimRequest) clearClaimResult("Evidence assessment unavailable. " + error.message);
  } finally { if (version === epoch && request === claimRequest) { claimLoading = false; claimControls(); } }
}
$("load-claims").addEventListener("click", loadClaimChoices);
async function readBundleResponse(response) {
  const limit = 16 * 1024 * 1024, length = response.headers.get("Content-Length"), sha = response.headers.get("X-Orch-Bundle-SHA256");
  if (!response.ok) throw new Error(response.status === 401 ? "Session expired. Reconnect." : "Fresh bundle assessment rejected. Check the evidence again.");
  if (response.headers.get("Content-Type") !== "application/json" || !/^[0-9]+$/.test(length || "") || Number(length) < 1 || Number(length) > limit || !/^[a-f0-9]{64}$/.test(sha || "")) {
    await response.body?.cancel(); throw new Error("Invalid bundle response headers.");
  }
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > Number(length) || size > limit) throw new Error("Bundle exceeds its declared size.");
      chunks.push(value);
    }
    if (size !== Number(length)) throw new Error("Incomplete bundle response.");
  } catch (error) { await reader.cancel(); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  if (actual !== sha) throw new Error("Bundle digest mismatch.");
  return {bytes, sha};
}
$("download-bundle").addEventListener("click", async () => {
  if (busy || claimLoading || claimDownloadController || !snapshot || !token || !claimResult || claimResult.epoch !== epoch || claimResult.status !== "claims_consistent") return;
  const version = epoch, request = claimRequest, task = selected, controller = new AbortController();
  const {task_id, ...selection} = claimResult.selection;
  claimDownloadController = controller; claimControls();
  $("bundle-digest").textContent = "Preparing and verifying bundle…";
  try {
    const response = await sessionFetch(`/tasks/${task}/evidence-bundle`, {method: "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, body: JSON.stringify(selection), cache: "no-store", credentials: "omit", signal: controller.signal});
    const {bytes, sha} = await readBundleResponse(response);
    if (controller.signal.aborted || version !== epoch || request !== claimRequest) return;
    const url = URL.createObjectURL(new Blob([bytes], {type: "application/json"})), link = document.createElement("a");
    link.href = url; link.download = "orchd-evidence-bundle.json"; document.body.append(link);
    try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    $("bundle-digest").textContent = "Bundle prepared for download. Keep this SHA-256 for verification: " + sha;
  } catch (error) {
    if (!controller.signal.aborted && version === epoch && request === claimRequest) clearClaimResult("Bundle download unavailable. " + error.message);
  } finally {
    if (claimDownloadController === controller) claimDownloadController = null;
    claimControls();
  }
});
$("assess-claims").addEventListener("click", assessClaimSelection);
for (const role of ["review", "policy"]) $("inspect-claim-" + role).addEventListener("click", async () => {
  const chosen = claimChoices[role].get($("claim-" + role).value), version = epoch;
  if (busy || claimLoading || !snapshot || !chosen) return;
  try {
    await evidence('records', chosen.id);
    if (version === epoch) $("evidence").scrollIntoView({block: "start", behavior: "smooth"});
  } catch (error) { if (version === epoch) notice(error.message); }
});
for (const role of ["review", "policy"]) $("claim-" + role).addEventListener("change", () => {
  claimRequest++; claimLoading = false;
  clearClaimResult("Selection changed. Check the selected evidence again."); claimControls();
});
$("save-selection").addEventListener("click", () => {
  if (busy || claimLoading || !snapshot || !claimResult || claimResult.epoch !== epoch) return;
  const url = URL.createObjectURL(new Blob([pretty(claimResult.selection)], {type: "application/json;charset=utf-8"}));
  const link = document.createElement("a"); link.href = url; link.download = "orchd-record-selection.json";
  document.body.append(link);
  try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
});
// A state name is a protocol detail. An operator should not have to translate one in their
// head to learn whether a task is waiting on them, so every place that shows a state shows
// this instead, and the raw name stays available in the evidence.
const PLAIN = {
  DRAFT_SPEC: "Being specified",
  AWAITING_CLARIFICATION: "Waiting for your answer to a question",
  SPEC_READY: "Ready to plan",
  PLANNING: "Planning",
  AWAITING_PLAN_APPROVAL: "Waiting for you to approve the plan",
  READY_FOR_IMPLEMENTATION: "Ready to implement",
  CHANGES_REQUESTED: "Changes requested",
  IMPLEMENTING: "Implementing",
  TESTING: "Running tests",
  REVIEWING: "Under review",
  AWAITING_ACTION_APPROVAL: "Waiting for you to approve the action",
  READY_FOR_PR: "Ready to open a pull request",
  PR_CREATED: "Pull request created",
  BLOCKED: "Blocked, and waiting on you",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};
function plain(state) { return PLAIN[state] || String(state || "").replaceAll("_", " "); }

// How long something has been waiting, which is the part that tells you whether it matters.
function waited(since) {
  const started = Date.parse(since || "");
  if (!Number.isFinite(started)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  if (seconds < 60) return seconds + "s";
  if (seconds < 3600) return Math.round(seconds / 60) + "m";
  if (seconds < 86400) return Math.round(seconds / 3600) + "h";
  return Math.round(seconds / 86400) + "d";
}

function queueEntry(list, name, why, since, open) {
  const item = document.createElement("li");
  const what = document.createElement("div");
  what.className = "what";
  const label = document.createElement("div");
  label.className = "name";
  label.textContent = name;
  const reason = document.createElement("div");
  reason.className = "why";
  reason.textContent = why;
  what.append(label, reason);
  const age = document.createElement("span");
  age.className = "waited";
  age.textContent = waited(since);
  item.append(what, age);
  if (open) item.append(open);
  list.append(item);
  return item;
}

// Which agents exist, and which of them are actually there. An agent that is running and
// correctly idle holds nothing, so before presence it was indistinguishable from one that
// stopped; the dot is the difference an operator acts on.
const PRESENCE = {working: "Working", idle: "Idle, waiting for work", "not seen": "Not seen"};
// Why a start or a stop did not happen, in words. The server answers with a code from a
// closed set and never with the daemon's own output, which carries host paths.
const LIFECYCLE = {
  not_enrolled: "That agent is not enrolled on this host.",
  not_declared: "That agent has no container declaration yet. Declare one with agent-declare on the host.",
  already_running: "That agent is already running.",
  not_running: "That agent is not running.",
  docker_cli_missing: "Docker is not on this machine's PATH.",
  daemon_unavailable: "Docker is not answering.",
  image_not_present: "That image is not on this machine. Build or pull it first; nothing is pulled for you.",
  previous_container_not_removed: "The previous container could not be removed. Inspect it with docker ps -a.",
  start_failed: "Docker refused to start it. Read the container log with docker logs.",
  stop_failed: "Docker refused to stop it. Inspect it with docker ps.",
  registry_unreadable: "This server's agent registry could not be read.",
};
let processes = new Map(), dockerNote = "";
async function lifecycle() {
  // Its own request rather than part of /status: this one asks Docker, and the status view
  // has to keep answering while everything else is busy.
  processes = new Map(); dockerNote = "";
  try {
    const report = await api("/agents");
    for (const row of report.agents || []) processes.set(row.agent, row);
    if (report.docker !== "available") dockerNote = LIFECYCLE[report.reason] || "Docker could not be asked what is running.";
  } catch (error) {
    dockerNote = "";   // No registry, or no session: the roster still renders without this.
  }
}
async function lifecycleAction(agent, what) {
  const result = await api("/agents/" + encodeURIComponent(agent) + "/" + what, {});
  notice(result.status === "refused"
    ? agent + ": " + (LIFECYCLE[result.reason] || "That could not be done.")
    : agent + " " + result.status + ".");
  await attention();
}
function renderAgents(report) {
  const declared = report.enrolled || [];
  const list = $("agent-list");
  list.replaceChildren();
  for (const row of declared) {
    const item = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = "dot " + (row.state === "working" ? "working" : row.state === "idle" ? "idle" : "gone");
    const who = document.createElement("div");
    who.className = "who";
    const name = document.createElement("b");
    name.textContent = row.agent;
    const detail = document.createElement("span");
    const process = processes.get(row.agent);
    // Presence says whether the agent is calling; the container says whether it is even
    // there. Apart they are two half-answers, and one pair of them is the useful one: a
    // container that is up while nothing calls in is broken rather than idle.
    const stopped = process && process.state !== "running";
    const silent = process && process.state === "running" && row.state === "not seen";
    detail.textContent = row.needs_you ? "Waiting on you before it can go on"
      : silent ? "Container running, but not calling in"
      : process && !process.declared ? "No container declared \u00b7 " + row.roles.join(", ")
      : stopped ? "Not started \u00b7 " + row.roles.join(", ")
      : PRESENCE[row.state] + " \u00b7 " + row.roles.join(", ");
    who.append(name, detail);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = row.last_seen ? waited(row.last_seen) + " ago" : "never";
    item.className = row.needs_you || silent ? "asking" : "";
    item.append(dot, who, when);
    if (row.holding) item.append(button("Open task", async () => { await select(row.holding); }));
    item.append(button("Ask", async () => {
      $("consult-role").value = row.roles[0];
      const question = $("consult-question");
      if (typeof question.focus === "function") question.focus();
    }));
    // Only when this window knows what the container is doing. Offering Run without that
    // would be offering to do something whose outcome it cannot report.
    // ...and only while Docker can be asked: a Run that is certain to be refused is worse
    // than no button, because it looks like the thing that is broken.
    if (dockerNote) { /* nothing to offer */ }
    else if (process && process.state === "running") item.append(button("Stop", () => lifecycleAction(row.agent, "stop")));
    else if (process && process.declared) item.append(button("Run", () => lifecycleAction(row.agent, "start")));
    list.append(item);
  }
  const present = declared.filter(row => row.state !== "not seen").length;
  const asking = declared.filter(row => row.needs_you).length;
  $("agents-summary").textContent = !declared.length
    ? "No agent registry was given to this server. Start it with --agents to see them here."
    : present + " of " + declared.length + " present"
      + (asking ? ", and " + asking + " waiting on you." : ".")
      + (dockerNote ? " " + dockerNote : "");
}

// Questions and what came back. A question is not a task: there is no approval here, no
// evidence record and no way from an answer into the workflow. See docs/consultations.md.
const CONSULT_STATE = {asked: "Waiting for an agent to pick this up", answering: "An agent is writing an answer",
                       answered: "Answered", withdrawn: "Withdrawn", closed: "Read"};
let rolesFilled = false;
function roleChoices() {
  if (rolesFilled) return;
  rolesFilled = true;
  const select = $("consult-role");
  for (const role of ["lead_planner", "project_manager", "junior", "reviewer", "guardian",
                      "lead_clarifier", "test_runner", "orchestrator", "action_broker"]) {
    const option = document.createElement("option");
    option.value = role;
    option.textContent = plain_role(role);
    select.append(option);
  }
  select.value = "lead_planner";
}
function line(className, content) {
  const element = document.createElement("div");
  element.className = className;
  element.textContent = content;
  return element;
}
async function consultations() {
  const version = epoch;
  try {
    const report = await api("/consultations");
    if (version !== epoch) return;
    const list = $("consult-list");
    list.replaceChildren();
    // Questions that are finished with — read, or taken back — stay in the store and leave
    // the window. A panel that only grows is one people stop looking at, and neither of
    // those has anything left for anybody to do.
    const done = ["closed", "withdrawn"];
    const shown = report.consultations.filter(row => !done.includes(row.state)).reverse();
    for (const row of shown) {
      const item = document.createElement("li");
      item.className = row.state === "answered" ? "answered" : "";
      const what = document.createElement("div");
      what.className = "what";
      what.append(line("name", "To the " + plain_role(row.role) + (row.about ? " \u00b7 about " + row.about.slice(0, 12) : "")),
                  line("why", row.question));
      if (row.answer) {
        what.append(line("answer", row.answer),
                    line("hint", (row.agent || "An agent") + " answered. This is an opinion: it approves nothing and changes nothing."));
      } else {
        what.append(line("hint", CONSULT_STATE[row.state] || row.state));
      }
      if (row.redacted) what.append(line("hint", "Text that looked like a credential was replaced before this was stored."));
      const age = document.createElement("span");
      age.className = "waited";
      age.textContent = waited(row.answered_at || row.asked_at);
      item.append(what, age);
      if (row.state === "answered") item.append(button("Mark read", () => settle(row.consultation_id, "close")));
      else if (row.state === "asked" || row.state === "answering") item.append(button("Withdraw", () => settle(row.consultation_id, "withdraw")));
      list.append(item);
    }
    const unread = report.consultations.filter(row => row.state === "answered").length;
    const open = report.open;
    $("consult-status").textContent = !report.count ? "No questions asked."
      : (unread ? unread + " answer(s) to read. " : "") + (open ? open + " question(s) waiting for an agent." : "Nothing is waiting for an agent.");
  } catch (error) {
    if (version === epoch) $("consult-status").textContent = "Could not read the questions: " + error.message;
  }
}
async function settle(consultation, what) {
  await api("/consultations/" + encodeURIComponent(consultation) + "/" + what, {});
  await consultations();
}

async function attention() {
  const version = epoch;
  try {
    const report = await api("/status");
    if (version !== epoch) return;
    await lifecycle();
    if (version !== epoch) return;
    const waiting = report.tasks.waiting_on_a_person || [];
    const holding = report.agents.holding || [];
    const list = $("attention-list");
    list.replaceChildren();
    for (const row of waiting) {
      const open = button("Open task", async () => { await select(row.task_id); });
      queueEntry(list, names.has(row.task_id) ? names.get(row.task_id).firstChild.textContent : row.task_id.slice(0, 12),
                 plain(row.state), row.since, open);
    }
    const work = $("working-list");
    work.replaceChildren();
    for (const row of holding) {
      queueEntry(work, row.task_id.slice(0, 12), (row.agent || "a worker") + " is holding this as " + plain_role(row.role), row.claimed_at, null);
    }
    $("working").hidden = holding.length === 0;
    const parts = [];
    if (waiting.length) parts.push(waiting.length === 1 ? "1 thing needs you." : waiting.length + " things need you.");
    if (report.work.at_bound) parts.push("As many tasks are advancing as this workspace allows; new work will wait.");
    if (report.agents.lapsed_count) parts.push(report.agents.lapsed_count + " lease(s) lapsed without being released.");
    if (report.summary.answers_to_read) parts.push(report.summary.answers_to_read + " answer(s) to read.");
    if (!parts.length) parts.push(report.summary.tasks_active ? "Nothing needs you. " + report.summary.tasks_active + " task(s) in progress." : "Nothing needs you, and nothing is in progress.");
    renderAgents(report);
    $("attention-summary").textContent = parts.join(" ");
    $("attention-summary").className = waiting.length ? "" : "settled";
  } catch (error) {
    if (version === epoch) $("attention-summary").textContent = "Could not read the workspace state: " + error.message;
  }
}

const ROLES = {project_manager: "project manager", lead_planner: "lead planner", lead_clarifier: "lead clarifier",
               junior: "junior", reviewer: "reviewer", guardian: "guardian", test_runner: "test runner",
               orchestrator: "orchestrator", action_broker: "action broker", human: "a person"};
function plain_role(role) { return ROLES[role] || String(role || "an agent").replaceAll("_", " "); }

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
      const small = document.createElement("small"); small.textContent = plain(item.state.state) + " · " + item.task_id.slice(0, 12); b.append(small);
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
  resetClaimReview();
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
    $("state").textContent = data.context?.workload === "repository-json-v1" && data.state.state === "COMPLETED" ? "LOCAL SNAPSHOT ACCEPTED" : data.state.state.replaceAll("_", " "); $("revision").textContent = "Revision " + data.state.revision;
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
      $("approval-summary").textContent = `${data.context?.workload === "repository-json-v1" ? pending.summary : (pending.kind === "plan" ? "Plan approval" : "Simulated action approval")} · Expires ${pending.expires_at}`;
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
  $("events").replaceChildren(); $("event-status").textContent = "Loading event history…"; $("evidence").hidden = true; $("empty").hidden = true; $("detail").hidden = false; $("approval").hidden = true;
  for (const [id, b] of names) b.setAttribute("aria-current", String(id === task));
  controls(); await state(); await records(); await replay();
}
let replaying = false;
async function replay() {
  if (!selected || !token || replaying || busy) return;
  replaying = true; const version = epoch, task = selected;
  try {
    const response = await sessionFetch(`/tasks/${task}/stream`, {headers: {Authorization: "Bearer " + token, "Last-Event-ID": String(cursor)}, cache: "no-store", credentials: "omit"});
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
    if (added) {
      await state();
      if (version === epoch) await records();
    }
  } catch (error) {
    if (version === epoch) {
      $("event-status").textContent = "Event history update failed. Retained events may be incomplete; polling will retry. Reselect the task to replay from the start.";
      throw error;
    }
  } finally { replaying = false; }
}
async function mutate(action) {
  if (busy) return; busy = true; controls();
  try { await action(); notice("Saved. Review the current state before the next action."); if (selected) { await state(); await records(); } await tasks(); }
  catch (e) { notice(e.message); if (selected && token) await state(); }
  finally { busy = false; controls(); }
}
async function maintenanceReport(kind) {
  if (busy || !token || !["storage", "pilots", "integrity"].includes(kind)) return;
  busy = true; controls();
  $("maintenance-summary").hidden = $("maintenance-details").hidden = true;
  $("maintenance-summary").textContent = $("maintenance-json").textContent = "";
  $("maintenance-status").textContent = kind === "storage" ? "Checking storage usage…" : kind === "pilots" ? "Checking repository pilot usage…" : "Checking persisted evidence…";
  try {
    const report = await api(kind === "storage" ? "/storage" : kind === "pilots" ? "/pilot-usage" : "/integrity-report");
    if (kind === "pilots") {
      if (!Number.isInteger(report.journal_rows) || !Number.isInteger(report.live_pilots) || !Array.isArray(report.pilots)) throw new Error("Unrecognized pilot usage report.");
      $("maintenance-status").textContent = "Pilot usage report ready. Reclamation is a separate reviewed CLI action; nothing was deleted.";
      const counts = Object.entries(report.status_counts || {}).map(([status, count]) => `${status} ${count}`).join(" · ") || "none";
      $("maintenance-summary").textContent = `Checked ${report.generated_at}\nJournal rows: ${report.journal_rows} / ${report.journal_cap}\nLive pilots: ${report.live_pilots} / ${report.live_cap} (headroom ${report.live_headroom})\nWorkspace bytes: ${report.workspace_bytes} · Reclaimable workspaces: ${report.reclaimable}\nStatus counts: ${counts}`;
    } else if (kind === "integrity") {
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
$("pilot-usage-report").addEventListener("click", () => maintenanceReport("pilots"));
$("integrity-report").addEventListener("click", () => maintenanceReport("integrity"));
$("connect").addEventListener("submit", async e => {
  e.preventDefault(); token = $("token").value.trim(); $("token").value = "";
  const connectingToken = token;
  try { await tasks(); if (connectingToken !== token) return; $("login").hidden = true; $("workspace").hidden = false; $("disconnect").hidden = false; notice("Connected to the local operator session."); roleChoices(); attention().catch(() => {}); consultations().catch(() => {}); }
  catch (error) { if (connectingToken === token) { token = ""; notice(error.message); } }
});
$("disconnect").addEventListener("click", () => location.reload());
function showSessionStatus(status) {
  $("session-status").textContent = `Session generation ${status.generation}. Expires in at most ${Math.ceil(status.expires_in_seconds / 60)} minutes. Rotation does not extend the eight-hour limit.`;
}
async function sessionAction(action) {
  if (busy || !token) return;
  busy = true; controls();
  try {
    const result = await api(action === "check" ? "/session" : `/session/${action}`, action === "check" ? undefined : {});
    if (action === "revoke") {
      endLocalSession("Server session ended. Restart the server to reconnect. Previously admitted work is not cancelled.");
    } else if (action === "rotate") {
      if (typeof result.session !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(result.session)) {
        endLocalSession("Invalid rotation response; restart the server to reconnect."); return;
      }
      token = result.session; clearSessionView(); showSessionStatus(result.status);
      await tasks(); notice("Session rotated. Old tokens no longer work. Select a task and review it again.");
    } else showSessionStatus(result);
  } catch (error) { notice(error.message); }
  finally { busy = false; controls(); }
}
for (const action of ["check", "rotate", "revoke"]) $("session-" + action).addEventListener("click", () => sessionAction(action));
$("refresh").addEventListener("click", () => tasks().then(attention).then(consultations).catch(e => notice(e.message)));
$("attention-refresh").addEventListener("click", () => attention().catch(e => notice(e.message)));
$("consult-refresh").addEventListener("click", () => consultations().catch(e => notice(e.message)));
$("consult-form").addEventListener("submit", e => {
  e.preventDefault();
  mutate(async () => {
    const body = {role: $("consult-role").value, question: $("consult-question").value};
    // Read-only context, and only when there is a task selected to be context for.
    if ($("consult-about").checked && selected) body.about = selected;
    await api("/consultations", body);
    $("consult-question").value = "";
    await consultations();
  }).catch(e => notice(e.message));
});
$("agents-refresh").addEventListener("click", () => attention().catch(e => notice(e.message)));
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
  e.preventDefault(); mutate(async () => { const result = await api("/tasks", {title: $("title").value, draft: $("create-draft").checked}); epoch++; selected = result.task_id; cursor = 0; $("cancel-reason").value = ""; $("cancellation").open = false; $("events").replaceChildren(); $("event-status").textContent = "Loading event history…"; $("evidence").hidden = true; $("empty").hidden = true; $("detail").hidden = false; attention().catch(() => {}); }).catch(e => notice(e.message));
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
