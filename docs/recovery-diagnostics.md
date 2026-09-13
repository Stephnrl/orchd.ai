# Recovery diagnostics

Blocked tasks now show read-only recovery guidance in the local management UI.
The same report is available from the authenticated
`GET /tasks/TASK_ID/recovery-diagnostics` API. No query parameters are accepted.

The report includes the task revision, blocked/resume state, active operation ID and
up to 20 unresolved operation summaries. It excludes operation results and broker
journal contents. `operations_truncated` indicates additional unresolved operations.

`recovery_status` has three values:

- `not_applicable`: the task is not BLOCKED. Use its ordinary workflow controls.
- `unavailable`: persisted preconditions fail. The explanation identifies an
  inconsistent/missing active operation, unsupported recovery stage, or unusable plan
  approval. Inspect retained evidence; do not delete journals or redispatch uncertain work.
- `preconditions_met`: one unresolved operation matches the active operation, the
  resume stage is implementation/testing, and the persisted plan approval passes
  the existing binding and expiry checks. An operator can attempt recovery, but it
  may still be rejected by runtime checks.

Diagnostics acquire the dispatcher lock and read persisted evidence. They do not
open broker journals, inspect or remove containers, probe broker process locks,
create retirement markers, advance generations, record events, or invoke recovery.
`runtime_checked` is always false. A busy dispatcher returns 409 rather than an
uncoordinated report. The report is advisory and can become stale immediately.

Recovery remains an explicit action using the existing API or CLI:

The local UI now also provides **Attempt recovery**. Review the evidence and select
the acknowledgement checkbox first. This action may stop the exact task container
and retire the interrupted attempt. It is available only when diagnostics match the
selected task and displayed revision and report `preconditions_met`. Refreshing task
state clears the checkbox. Stale diagnostics, unavailable recovery and a busy UI
disable the action; the server independently rechecks the submitted revision.

A rejection refreshes the task view and leaves the operator to inspect the current
evidence. Successful recovery also refreshes state and records; it does not call
`run`. Review the resulting state before explicitly choosing to continue execution.

Recovery now persists its original revision, operator identity and cleanup status
with the committed reconciliation. An exact retry while the task remains at that
recovered revision returns the same outcome without repeating runtime reconciliation
or advancing the operation generation again. A different identity or a request after
workflow advancement is rejected. This retry record survives backup/restore.

Workspace cleanup is a separate post-commit step. Unexpected files or filesystem
errors produce an audited `deferred` cleanup status while recovery still returns
success; the UI tells the operator to inspect retained files. An ordinary retry does
not repeat deferred cleanup. If the process crashes before cleanup is recorded, the
status remains `pending`; retrying the original API/CLI recovery request finishes
only that cleanup step. The diagnostic panel is for BLOCKED tasks, so this pending
post-commit retry uses the API or CLI with the original revision in task context.
Completed cleanup is also recorded, and the UI shows the outcome. No unexpected
files are implicitly removed and no task is automatically run afterward.

## Explicit cleanup retry after inspection

After inspecting retained workspace files and resolving the cause of deferred cleanup,
the operator can explicitly retry it while the task remains at its recovered
`CHANGES_REQUESTED` revision:

```sh
python -m orch retry-cleanup --data .runtime/managed --task TASK_ID --operation-id OPERATION_ID --expected-revision RECOVERED_REVISION
```

The authenticated API equivalent is `POST /tasks/TASK_ID/retry-cleanup` with exactly
`{"operation_id": "OPERATION_ID", "expected_revision": RECOVERED_REVISION}`.
Use `operator_recovery.operation_id` and `operator_recovery.completed_revision` from
task context. This revision is different from the original BLOCKED recovery revision.
The local UI continues to show the cleanup outcome; this explicit retry is available
through the CLI/API.

The command requires an existing database, the original recovery operator, the matching
operation, completed reconciliation, and no active or unresolved task operations.
It takes the dispatcher lock, records a human `operator_cleanup_retry` event with the
authenticated principal, then retries the existing nonrecursive workspace cleanup.
Only the expected fixture file is eligible for removal; unexpected contents are retained
and the outcome remains `deferred`. Filesystem errors also leave an audited deferred
outcome. Inspect the returned cleanup status even when the request succeeds.

A completed cleanup retry returns the saved outcome without another deletion or event.
An interruption after recording intent leaves `pending`, which can be retried. No
broker journal, process or container is probed, no generation advances, and no provider
or workflow execution starts. An expired plan approval does not authorize new work;
this command only cleans the already reconciled operation. Once the workflow advances,
this cleanup route rejects the request. General cleanup of historical workspaces is
outside this command's scope.

## Original recovery request

The recovery API request is:

```text
POST /tasks/TASK_ID/recover
Authorization: Bearer OPERATOR_SESSION
Content-Type: application/json

{"expected_revision": CURRENT_REVISION}
```

It rechecks state and approval, validates the broker result, and where supported
verifies Docker cleanup. A diagnostic result never proves that an orphan process
stopped. It does not select an executor mode or substitute for correct runtime
configuration on the recovery process.

Expired granted approvals still prevent recovery. Pending-request renewal cannot
extend a decision already granted. General remediation of that case is not provided
by this diagnostic milestone. No live providers or external integrations are enabled.

Validation on September 12, 2026: all 108 Python tests passed with zero skips,
including all five real Docker gates. Five new tests cover persisted preconditions,
expired approval, unsupported/mismatched recovery state, bounded operation summaries,
API authorization and proof that diagnostics do not probe or mutate runtime state.
Both Node UI regression checks passed, including diagnostic text rendering and hiding
using a DOM stub (not a rendered-browser test). Contract validation passed 19 examples
and 366 negative cases; compilation, JavaScript syntax and whitespace checks passed.

The subsequent UI recovery-control change passed 27 focused Python tests covering
recovery, runtime reconciliation, management and HTTP boundaries, plus both Node UI
checks. The expanded UI check covers explicit review, stale revisions, mismatched
tasks, busy/unavailable states and the exact recovery request. The API regression
checks failed runtime verification, stale requests and successful recovery without
automatic continuation. These UI changes did not rerun the full Docker gate suite;
the Node tests use DOM stubs rather than a rendered browser.

The subsequent recovery-retry change passed the full 113-test Python suite with no
skips, including all five real Docker gates. Four new tests cover deferred cleanup
with retained files, interruption immediately after commit, exact retries after
restore, and rejection of extra unresolved operations before runtime probes. Both
Node UI checks passed, including all three cleanup messages. Contract validation
passed 19 examples and 366 negative cases; compilation and whitespace checks passed.
