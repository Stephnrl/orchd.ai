"use strict";
let batchDraft = new Map(), batchView = null, batchId = null;
let batchVersion = 0, batchListVersion = 0, batchNext = null, batchLoading = false, batchListing = false;
const batchPaused = new Set(["AWAITING_PLAN_APPROVAL", "AWAITING_ACTION_APPROVAL", "BLOCKED", "FAILED", "COMPLETED", "CANCELLED"]);

function clearBatchInspection() {
  batchVersion++; batchView = null; batchLoading = false;
  $("batch-reviewed").checked = $("batch-abandon-reviewed").checked = false;
  $("batch-json").textContent = $("batch-summary").textContent = "";
  $("batch-entries").replaceChildren(); $("batch-abandon-task").replaceChildren();
  const option = document.createElement("option"); option.value = ""; option.textContent = "Choose an entry";
  $("batch-abandon-task").append(option); $("batch-abandon-task").value = "";
  $("batch-detail").hidden = true;
}
function resetBatches() {
  clearBatchInspection(); batchId = null; batchDraft.clear(); batchListVersion++;
  batchNext = null; batchLoading = batchListing = false;
  $("batch-list").replaceChildren(); $("batch-more").hidden = true;
  $("batch-status").textContent = "No batch loaded."; renderBatchDraft();
}
function renderBatchDraft() {
  $("batch-draft").replaceChildren();
  for (const [task, item] of batchDraft) {
    const row = document.createElement("li"), label = document.createElement("span");
    label.textContent = `${item.title} · ${task.slice(0,12)} · revision ${item.expected_revision} `;
    row.append(label, button("Remove", async () => {if (busy) return; batchDraft.delete(task); renderBatchDraft(); controls();}));
    $("batch-draft").append(row);
  }
  $("batch-draft-status").textContent = batchDraft.size ? `${batchDraft.size} of 16 tasks selected, in execution order.` : "No tasks selected for a batch.";
}
function batchCanRun() {
  if (!batchView || !Number.isFinite(Date.parse(batchView.batch.scope.expires_at)) || Date.parse(batchView.batch.scope.expires_at) <= Date.now()) return false;
  const entries = batchView.batch.entries;
  if (entries.some(e => e.status === "running" || (e.status === "stopped" && ["FAILED","BLOCKED"].includes(e.result)))) return false;
  return entries.some(e => e.status === "pending") && entries.every((e,i) => e.status !== "pending" || batchView.observations[i]?.matches_checkpoint === true);
}
function batchAbandonObservation() {
  if (!batchView) return null;
  const index = batchView.batch.scope.tasks.findIndex(t => t.task_id === $("batch-abandon-task").value);
  if (index < 0) return null;
  const entry = batchView.batch.entries[index], observed = batchView.observations[index];
  return (entry.status === "pending" || entry.status === "running" || (entry.status === "stopped" && ["FAILED","BLOCKED"].includes(entry.result))) && observed?.snapshot_sha256 ? observed : null;
}
function batchControls() {
  const blocked = busy || !token || batchLoading;
  const state = snapshot?.state;
  $("batch-add").disabled = blocked || !state || snapshot?.context?.workload === "repository-json-v1" || batchPaused.has(state.state) || !!state.active_operation_id || !!state.active_invocation_id || !!state.container_id || (batchDraft.size >= 16 && !batchDraft.has(selected));
  $("batch-create").disabled = blocked || !batchDraft.size;
  $("batch-clear").disabled = busy || !batchDraft.size;
  $("batch-list-refresh").disabled = busy || !token || batchListing;
  $("batch-more").disabled = busy || !token || batchListing || batchNext === null;
  $("batch-refresh").disabled = blocked || !batchId;
  $("batch-reviewed").disabled = blocked || !batchView;
  $("batch-run").disabled = blocked || !$("batch-reviewed").checked || !batchCanRun();
  $("batch-abandon-task").disabled = $("batch-abandon-reviewed").disabled = blocked || !batchView;
  $("batch-abandon").disabled = blocked || !$("batch-abandon-reviewed").checked || !batchAbandonObservation();
}
function renderBatchInspection(report) {
  batchView = report;
  const batch = report.batch;
  $("batch-detail").hidden = false;
  $("batch-json").textContent = pretty(report);
  $("batch-summary").textContent = `${batch.id} · journal revision ${batch.revision} · scope ${batch.scope_sha256}`;
  for (let i=0; i<batch.entries.length; i++) {
    const task = batch.scope.tasks[i], entry = batch.entries[i], observed = report.observations[i];
    const row = document.createElement("li"), label = document.createElement("span");
    label.textContent = `${task.task_id.slice(0,12)} · ${entry.status}${entry.result ? " · "+entry.result : ""} · current ${observed?.state || "unavailable"} `;
    row.append(label, button("Inspect task", async () => {if (busy) return; $("batch-abandon-reviewed").checked = false; await select(task.task_id);}));
    $("batch-entries").append(row);
    if (entry.status === "pending" || entry.status === "running" || (entry.status === "stopped" && ["FAILED","BLOCKED"].includes(entry.result))) {
      const option = document.createElement("option"); option.value = task.task_id; option.textContent = task.task_id.slice(0,12)+" · "+entry.status; $("batch-abandon-task").append(option);
    }
  }
  $("batch-status").textContent = batchCanRun() ? "Inspection loaded. Review the scope before running." : "Inspection loaded. No runnable pending scope: review checkpoints, expiry or recovery state.";
}
async function inspectBatch(id, internal = false) {
  if (!token || (busy && !internal)) return;
  batchListVersion++; batchListing = false;
  clearBatchInspection(); batchId = id; batchLoading = true;
  const version = batchVersion;
  $("batch-status").textContent = "Loading batch inspection…"; controls();
  try {
    const report = await api(`/batches/${id}`);
    if (version !== batchVersion) return;
    renderBatchInspection(report);
  } catch (error) {
    if (version === batchVersion) $("batch-status").textContent = "Batch inspection failed. Refresh before taking action.";
  } finally {if (version === batchVersion) {batchLoading = false; controls();}}
}
async function listBatches(more = false) {
  if (busy || !token || batchListing || (more && batchNext === null)) return;
  const after = more ? batchNext : null, version = ++batchListVersion;
  if (!more) {clearBatchInspection(); batchId = null; batchNext = null; $("batch-list").replaceChildren();}
  batchListing = true; controls();
  try {
    const result = await api("/batches"+(after ? "?after="+encodeURIComponent(after) : ""));
    if (version !== batchListVersion) return;
    for (const item of result.items) $("batch-list").append(button(`${item.id.slice(0,12)} · ${item.counts.pending} pending · ${item.counts.running} interrupted`, async () => inspectBatch(item.id)));
    batchNext = result.next; $("batch-more").hidden = batchNext === null;
    $("batch-status").textContent = "Batch list loaded. Select a batch to inspect.";
  } catch (error) {if (version === batchListVersion) $("batch-status").textContent = "Batch list failed. Retry to load the same page.";}
  finally {if (version === batchListVersion) {batchListing = false; controls();}}
}
async function changeBatch(action) {
  if (busy || !token || batchLoading) return;
  const current = batchView?.batch, observed = batchAbandonObservation();
  if (action === "create" && !batchDraft.size) return;
  if (action === "run" && (!$("batch-reviewed").checked || !batchCanRun())) return;
  if (action === "abandon" && (!$("batch-abandon-reviewed").checked || !observed)) return;
  const payload = action === "create" ? {tasks:Array.from(batchDraft, ([task_id,item]) => ({task_id,expected_revision:item.expected_revision}))} : {scope_sha256:current.scope_sha256,expected_revision:current.revision};
  if (action === "abandon") Object.assign(payload, {task_id:observed.task_id,expected_snapshot_sha256:observed.snapshot_sha256});
  busy = true; batchListVersion++; batchListing = false; clearBatchInspection(); batchLoading = false;
  const version = batchVersion;
  $("batch-status").textContent = "Saving batch operation…"; controls();
  try {
    const result = await api(action === "create" ? "/batches" : `/batches/${current.id}/${action}`, payload);
    if (version !== batchVersion) return;
    if (action === "create") {batchDraft.clear(); renderBatchDraft();}
    await inspectBatch(result.id, true);
  } catch (error) {
    if (version === batchVersion) $("batch-status").textContent = "Batch operation failed or its result is uncertain. Load batches and inspect before retrying.";
  } finally {
    if (token) {try {if (selected) {await state(); await records();} await tasks();} catch (error) {notice(error.message);}}
    busy = false; controls();
  }
}
$("batch-add").addEventListener("click", () => {
  batchControls(); if ($("batch-add").disabled) return;
  batchDraft.set(selected, {expected_revision:snapshot.state.revision,title:$("task-title").textContent});
  $("batch-panel").open = true; renderBatchDraft(); controls();
});
$("batch-clear").addEventListener("click", () => {if (busy) return; batchDraft.clear(); renderBatchDraft(); controls();});
$("batch-list-refresh").addEventListener("click", () => listBatches());
$("batch-more").addEventListener("click", () => listBatches(true));
$("batch-refresh").addEventListener("click", () => inspectBatch(batchId));
$("batch-reviewed").addEventListener("change", batchControls);
$("batch-abandon-reviewed").addEventListener("change", batchControls);
$("batch-abandon-task").addEventListener("change", () => {$("batch-abandon-reviewed").checked = false; batchControls();});
for (const action of ["create","run","abandon"]) $("batch-"+action).addEventListener("click", () => changeBatch(action));
resetBatches(); batchControls();
