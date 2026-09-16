# One owner per task

Until now a single store-wide lock served as the dispatcher: `Store.exclusive()` locked
`dispatcher.lock`, every engine mutation took it, and so exactly one process could advance
anything in the store at a time. That is why [serial batches](workflow-batches.md) were
possible and parallel work was not, and it is the first thing that has to change before
there can be more than one worker.

Ownership replaces it with two mechanisms that do different jobs. **The lock is the
enforcement**: task work holds `locks/<task>.lock`, so two workers may advance two
different tasks at once and never the same one. **The recorded claim is the evidence**: a
kernel lock vanishes with the process that held it, leaving nothing behind, while a
`task_owners` row survives to show that a worker was interrupted mid-advance.

This milestone does not add parallel scheduling, admission slots or a second worker
process. It makes one possible, and nothing in the repository starts one yet.
[Worker admission](worker-admission.md) then bounds how many may advance at once, and
[agent sessions](agent-sessions.md) add the lease an agent holds when it has no resident
process to hold a lock.

## What takes which lock

| Operation | Task lock | Store-wide lock |
| --- | --- | --- |
| `advance` / `run` | held throughout | taken briefly, to register the claim |
| approve, cancel, renew, recover, retry-cleanup, diagnostics | held | held (these are short and transactional) |
| create task | — | held |
| backup, restore, gc, integrity audit, batch review | — | held, then `quiescent()` |

Operator decisions hold both, so whole-store maintenance still excludes **every** task
mutation rather than only advancement. Only `advance` holds a lock for as long as real work
takes, and it releases the store-wide lock as soon as its claim is registered, so a long
container run never blocks another task.

Because claims register under the store-wide lock, maintenance that holds it stops new
claims, and `quiescent()` then refuses while any task is *genuinely* being advanced —
"genuinely" meaning a recorded claim whose task lock is still held by a live process. A
claim left behind by a crash does not block maintenance; that distinction is the whole
reason both mechanisms exist.

## What a crash leaves behind

A worker that dies mid-advance releases its lock to the kernel but leaves its claim. The
next worker to reach that task sees a claim it does not own, takes it over, and records a
`task_ownership_taken` event naming the previous owner, when it claimed, and the new
generation. `recovery-diagnostics` reports the same claim as `interrupted_claim`, which is
safe to read there because that call holds the task's lock: nothing can be advancing it.

**Takeover never re-runs anything.** An operation left `started` still hits the existing
guard that moves the task to `BLOCKED` and requires explicit reconciliation, exactly as
before. Ownership decides who may advance a task; it has never decided whether interrupted
work may be repeated, and it does not begin to.

An owner is the account, the process id and a session identifier generated once per engine,
so two processes under the same account are distinguishable and a recycled process id
cannot be mistaken for the original.

## Boundaries

This is a **single-host** mechanism. Advisory file locks are reliable between processes on
one machine and are not a distributed lock; nothing here makes two hosts safe against each
other, and no part of the system attempts it. Multi-host operation needs a different
primitive and its own review.

The claim is current state, not evidence: it is written and deleted as work starts and
stops. The append-only event log keeps the history, which is why a takeover is recorded as
an event rather than inferred from the row.

Nothing here changes approvals, revisions, receipts or reconciliation. Two workers on two
tasks are subject to exactly the invariants one worker was, and a task's own fences —
content hash, expected revision, one attempt per operation — are untouched. Scheduling,
resource admission and same-repository conflict handling remain unimplemented; they need
their own milestone, and this one deliberately stops short of them.
