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

The API request is:

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
