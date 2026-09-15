# Serial workflow batches

Create a reviewed list of up to 16 existing runnable fixture tasks and advance them
in that order to their next approval pause or terminal state. Each task keeps its
normal specification, policy, evidence and human approval requirements. Batches do
not approve tasks, create tasks, enable live providers, or run concurrent workers.

## Local UI

Select a runnable task and choose **Add to batch**. The **Task batches** panel opens
with an ordered draft; add up to 16 tasks or remove entries before creating scope.
The draft holds the revisions you inspected. If a task changes, creation is rejected;
refresh that task and add it again to update its draft revision.

**Create reviewed scope** persists the list without running it. Inspect its entries
and scope details, check the separate execution acknowledgement, then choose
**Run reviewed batch**. Every task still stops for its individual approvals. Review
checkboxes clear whenever inspection is replaced or refreshed. Loading a saved batch
after restart never dispatches it.

Use **Load batches** and **Load more batches** to find retained work. **Inspect task**
opens the normal task/evidence view. Resolve interrupted work, refresh batch inspection,
choose the entry, acknowledge the tracking-only consequence and choose **Abandon selected
entry**. The server checks scope, journal revision and current task snapshot and refuses
active work. UI eligibility is an aid; server scope and recovery checks are decisive.

Failed or uncertain requests clear actionable inspection state. Load and inspect the
retained batch before retrying; a lost response does not establish that no work ran.
Session expiry, rotation and revocation clear drafts and displayed batch data from the
tab. They do not delete journals or cancel already admitted batch work. Scope creation,
execution and abandonment are separate actions; a draft never grants task approval.

## Authenticated API

All routes use the existing loopback Host/Origin restrictions and operator bearer
session. POST requests use JSON with only the exact documented fields.

| Route | Request |
| --- | --- |
| `GET /batches` | Optional single `after` cursor; fixed 50-item page |
| `POST /batches` | `tasks` list as shown below |
| `GET /batches/BATCH_ID` | No body or query; journal plus current task observations |
| `POST /batches/BATCH_ID/run` | `scope_sha256`, `expected_revision` |
| `POST /batches/BATCH_ID/abandon` | `scope_sha256`, `expected_revision`, `task_id`, `expected_snapshot_sha256` |

Creation returns 201; reads and returned execution/abandonment journals return 200.
An execution response can contain a retained `running` reservation requiring review;
HTTP 200 means a journal was returned, not that every task completed. Invalid/stale
requests return 409; missing/expired sessions return 401. There is no batch approval
endpoint. These routes expose the same bounded serial runner as the CLI.

## CLI

Save a request file using real task IDs and their current task revisions:

```json
{"tasks":[{"task_id":"00000000000000000000000000000000","expected_revision":1}]}
```

The all-zero ID above is a placeholder. Create fixture tasks in the local UI and
read their IDs and revisions before preparing this list. Paused, terminal, duplicate,
stale and recovery-required tasks cannot be included. Creation is all-or-nothing
and does not advance any task.

```sh
python -m orch batch-create --data .runtime/managed --trusted-fixture --intent tasks.json
python -m orch batch-list --data .runtime/managed
python -m orch batch-inspect --data .runtime/managed --batch-id BATCH_ID
python -m orch batch-run --data .runtime/managed --trusted-fixture --batch-id BATCH_ID --expected-sha256 SCOPE_SHA256 --expected-revision JOURNAL_REVISION
```

Review the created scope, task order and snapshots before invoking `batch-run`.
Use the returned **scope SHA-256** and **batch journal revision**, not the individual
task revision, for dispatch. For Docker fixtures replace `--trusted-fixture` with the
same approved `--image DIGEST_PINNED_IMAGE` for creation and execution. If using
`--fixture-provider cli`, keep that setting identical as well. Existing runtime and
approval guards still apply; batch creation does not qualify an image or provider.

Scope is bound to the store path, runtime/provider settings, runtime source digest,
and complete task state/context snapshots. The review window lasts 30 minutes.
Before each workflow step, the batch rechecks its scope and the engine compares the
reviewed snapshot while holding its dispatcher lock. A context-only change also
invalidates the snapshot. A change during one admitted step cannot cancel that step;
the next check stops further advancement. Other direct operator actions retain their
normal locks and can cause a batch to stop on stale state between steps.

Batch execution uses one cross-process batch-dispatch lock per store. It persists a
`running` reservation before a possible effect and a checkpoint after every returned
step. Each task is limited to 40 steps. Normal approval pauses count as `stopped`
entries. Approve each task individually through the existing UI, then create a new
batch for its next segment. Re-running a completed batch does not advance its tasks.

## Interruption and recovery

Listing and inspection do not advance tasks or mutate batch journals. Listing returns
at most 50 summaries; pass its `next` value as `--after-batch` to continue. Inspection
shows current task state/revision and snapshot digest alongside the retained checkpoint.
A matching checkpoint is diagnostic information, never permission to retry.

An interruption, stale task, expiry during execution, exhausted step budget or failed
checkpoint leaves a `running` entry for review. A `FAILED` or `BLOCKED` stopping state
also prevents dispatch of remaining entries. `batch-run` exits 2 when it returns such
a result. A crash can instead leave a reservation without a normal command response.
Restarting the server or CLI never resumes a batch automatically.

Inspect task history and existing recovery diagnostics first. Resolve any active
operation, invocation or container using the existing recovery procedures. To stop
tracking an interrupted/failed entry, or remove an unstarted entry, explicitly run:

```sh
python -m orch batch-abandon --data .runtime/managed --trusted-fixture --batch-id BATCH_ID --expected-sha256 SCOPE_SHA256 --expected-revision JOURNAL_REVISION --task TASK_ID --expected-snapshot-sha256 CURRENT_TASK_SNAPSHOT_SHA256
```

Abandonment checks the current snapshot and refuses active work. It only changes batch
metadata: it does not cancel, undo, recover or retry the task. Successful stopped entries
cannot be rewritten. Remaining pending entries can be dispatched with the new journal
revision if their original scope is still current. Otherwise prepare a new batch from
freshly reviewed tasks. There is no renewal of an old batch's scope or review window.

Journals live under the store's `batches` directory, are bounded to 1,024 batches, and
are never automatically deleted. They are local metadata, not signed execution receipts.
Task events and broker receipts remain the evidence of actual workflow effects. Keep
journals for investigation; capacity exhaustion requires an explicit operator archival
decision. Standard workflow backups do not include batch metadata. Restoring a workflow
to a new path requires fresh batch review; do not copy old journals back as retry authority.

Run the complete synthetic walkthrough with `python scripts/batch_acceptance.py`.
It explicitly represents separate fixture human decisions between three batch rounds.
Use `--destination NEW_DIRECTORY` to retain its two completed tasks and journals.
This scenario is included in the full/offline release-verification lanes.
