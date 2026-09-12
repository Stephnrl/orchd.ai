# Operator cancellation

An authenticated operator can now end a task between operations using the local UI,
API or CLI. Cancellation is a terminal workflow decision. It does not erase artifacts,
undo edits, reverse an external action or claim to terminate an active process.

In the UI, select a task, expand **Cancel this task**, enter a reason and select
**Cancel task**. The displayed revision is submitted with the request. Execution and
cancellation controls are disabled while that page is running an operation. The server
remains synchronous: a cancellation request cannot interrupt an in-flight HTTP run;
if the task changes before it is handled, its stale revision is rejected.

The authenticated JSON route is:

```text
POST /tasks/TASK_ID/cancel
Authorization: Bearer OPERATOR_SESSION
Content-Type: application/json

{"expected_revision": 3, "reason": "This task is no longer needed"}
```

Only those two fields are accepted. Identity comes from the local operator boundary,
not request JSON. The reason is trimmed, must be 1–500 characters on one line, and
must not contain recognized secrets. Missing authentication returns 401; invalid,
stale or unsafe requests return 409 without changing task state.

For an existing local database, the CLI needs no Docker image or provider installation:

```sh
python -m orch cancel --data .runtime/managed --task TASK_ID --expected-revision 3 --reason "This task is no longer needed"
```

The CLI's fixed local executor is constructed only for the existing Engine interface;
the cancellation path never dispatches a worker or provider. Missing CLI arguments or
a missing workflow database are rejected before opening task storage.

The engine acquires the same exclusive dispatcher lock used for advancement, then
checks the revision and all unresolved operations in a transaction. Active operation,
invocation or container markers also prevent cancellation. Uncertain work must first
be reconciled through the existing recovery path; cancellation is not a way to hide
an orphaned process or discard an uncertain outcome. A busy dispatcher is rejected.

A successful external-action receipt prevents cancellation even if a crash occurred
before the final task transition. The task must instead complete reconciliation.
Other terminal states cannot be changed into CANCELLED.

Successful cancellation records an `operator_cancellation` event with the trusted
principal, reason, original revision/state, timestamp and pending approval reference,
then transitions to CANCELLED atomically. The pending approval is no longer actionable.
Already recorded approvals, proposals, test results and artifacts remain immutable.
Repeating exactly the same cancellation request is idempotent; a different reason,
principal or original revision is rejected. Existing idempotent approval receipts
remain historical evidence and cannot restart a cancelled task.

`run`, `advance` and restore/replay preserve the terminal state. Creating a new task
is the way to restart work; there is no uncancel operation in this milestone.

Tests cover both approval pauses, no subsequent invocation/edit, stale and malformed
requests, busy/unresolved execution, a crash after successful external action, exact
retry behavior, CLI operation without Docker configuration and backup/restore replay.
This implements human task cancellation from the design; interrupting active provider
or worker processes remains a separate runtime capability.

## Verification

Verified on September 12, 2026 on Windows 11 with Python 3.12.14 and Docker
Desktop's Linux engine 29.7.2:

- Full suite: 83 tests passed, zero skips, including all five Docker gates.
- Contract validation: 19 examples and 366 negative cases passed; Python compilation,
  JavaScript syntax and whitespace checks passed.
- Browser check: created a task, ran to plan approval, cancelled with a reason, and
  confirmed CANCELLED, retained evidence, removed approval controls and disabled run.
- A fresh runtime verification passed all five required gates with no failures,
  errors or skips. Report ID: `6b9b744ea52a4af1aba54a15eab6bc97`.
  Source digest: `36fdcf9f7e6e1efaa793b690441bdd44b8ddbc0cebe53f42648313e76cb62826`.

The runtime report completed at `2026-09-12T15:42:20.438474Z` and expires after
24 hours. It is local, host/image/source-bound evidence, not permanent authorization.
`provider_authorized` remains false; live providers and external integrations remain
disabled.
