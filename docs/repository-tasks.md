# Repository JSON tasks in the durable kernel

The bounded Git data pilot is now a task workload in the common workflow store.
It has a TaskSpec, a retained execution plan, two human approval boundaries,
durable operation/event history and immutable result snapshots. It appears in
the existing task list and uses the authenticated approval, run, cancellation,
recovery and evidence controls.

The supported change is still the [JSON data pilot](repository-pilot.md): exact
baseline, bounded replacements and fixed assertions. Execution can use local data
mode or the [restricted Docker workers](pilot-container-workers.md). This does
not admit arbitrary repository programs, model proposals or remote writes.

## Create and operate

Use the existing pilot intent format. Create with the server's intended runtime:

```text
python -m orch repository-task-create --data .runtime/managed --intent intent.json --title "Validate repository settings" --image IMAGE@sha256:DIGEST
python -m orch repository-task-approve --data .runtime/managed --task TASK_ID --request-id REQUEST_ID --expected-revision REVISION --decision approve --image IMAGE@sha256:DIGEST
python -m orch repository-task-run --data .runtime/managed --task TASK_ID --image IMAGE@sha256:DIGEST
```

Use `--trusted-fixture` instead of `--image` for explicit local-data execution.
Despite that legacy flag name, repository tasks retain their JSON workload;
they do not become greeting fixtures. Start `serve` against the same store and
runtime to review the task in the existing UI. Creation is available through the
CLI and authenticated `POST /repository-tasks` with exactly `title` and `intent`.
The existing UI create button continues to create a greeting fixture. HTTP requests
retain the existing 4 KiB limit; use the CLI for larger admitted intents.

1. Creation reads the exact Git baseline and retains a `RepositoryTaskPlan`, then
   pauses at `AWAITING_PLAN_APPROVAL`. No worker has run. The plan links to the
   full scope artifact: paths, replacement bytes, assertions, runtime and expiry.
2. Human approval moves the task to `READY_FOR_IMPLEMENTATION`. Approval alone
   does not execute it. Run admits one durable `repository_pilot` operation.
3. The pilot reads the authoritative task store and requires a matching current
   plan approval and live kernel reservation. Calling the standalone pilot CLI
   cannot bypass that boundary. Its own worker reservations remain one-use.
4. Successful checks produce a `RepositoryTaskResult` and retained file artifacts,
   then pause at `AWAITING_ACTION_APPROVAL`. This request means **accept the local
   validated snapshot**, and the UI labels it accordingly. Failed checks end in
   `FAILED` without offering result acceptance.
5. Human acceptance ends in `COMPLETED`, displayed as **LOCAL SNAPSHOT ACCEPTED**.
   Rejection at either boundary ends in `CANCELLED`. There is no simulated PR,
   GitHub/Jira effect, provider invocation or fabricated independent AI review.

The RepositoryRef branch names a planned disposable scope; no Git branch is
created by the task. Final acceptance binds retained snapshot artifacts, not
mutable workspace files. Completion does not certify more than the approved
JSON assertions and human result sign-off.

## Expiry and workload isolation

Execution approval expires with the pilot's 30-minute scope. An expired execution
scope requires a newly prepared task; it cannot be renewed to evade a changed
baseline/runtime. Result approval expires after one hour and can be explicitly
renewed after verifying the retained evidence. Renewal neither executes work nor
grants a decision. Expiry does not retroactively cancel an admitted operation.

Fixture transitions and serial fixture batches reject repository tasks. The UI
disables adding them to a batch. Their plan/result contracts are distinct from
fixture ImplementationPlan/PatchReceipt/TestReceipt and cannot serve as fixture
GitHub evidence. Existing fixtures retain their original lifecycle and policies.

## Recovery without duplicate effects

The kernel operation commits before invoking the pilot. Pilot dispatches and
container identity remain durable in the separate journal. A process interruption
can occur between those commits, so a surviving `IMPLEMENTING` task becomes
`BLOCKED` when advanced again. Nothing automatically retries or adopts a result.

Use the existing task recovery control or:

```text
python -m orch recover --data .runtime/managed --task TASK_ID --expected-revision REVISION --image IMAGE@sha256:DIGEST
```

Recovery requires the current blocked task, its exact operation and its retained
pilot scope. An unused pilot reservation is abandoned. An interrupted Docker
pilot goes through ownership-checked container reconciliation. Local-data mode
creates no child process, so acquiring its writer lock permits marking a partial
attempt interrupted. Neither path deletes the workspace or retries effects.

A completed pilot with a missing kernel checkpoint is retained as interruption
evidence; it is not silently promoted into a successful task. After reconciliation,
the task ends in `FAILED` with a `RepositoryTaskResult` whose outcome is
`interrupted`. A new attempt requires a newly prepared and approved task. If the
kernel already committed its result checkpoint before the crash, restart preserves
the result approval pause and never invokes the pilot again.

Missing journals, mismatched runtime or uncertain cleanup leave the task blocked.
The generic fixture cleanup-retry command is not admitted for repository tasks.

## Storage and restore

Task plans, approvals, result snapshots and history use the existing append-only
records/events and content-addressed artifacts. Standard workflow backup now
includes that evidence, and replay/integrity checks cover it. The live pilot journal
and disposable workspaces remain under `repository-pilots` and are **not** included
in that backup. Restoring the common database elsewhere preserves inspection but
does not restore execution authority: the scope is bound to the original store path
and live journal. Create a fresh task to execute in another store.

A crash during creation can leave a pilot whose task transaction never committed.
It cannot pass kernel admission and remains retained for inspection. Rejection and
cancellation leave the bound pilot `prepared` in its journal; it cannot run, and
[reviewed reclamation](pilot-retention.md) abandons and reclaims it once the task is
terminal. Retention limits and the lack of automatic archive/deletion remain as
documented for pilots.

## Verification and remaining scope

`python scripts/repository_task_acceptance.py` exercises CLI approvals, snapshot
retention, replay/backup and the pilot/kernel crash boundary in a disposable Git
repository. Add `--image IMAGE@sha256:DIGEST` to run the same lifecycle with Docker.
The offline/full release lane includes local acceptance; the Docker CI lane includes
the real-worker variant. Browser acceptance checks both approval labels and restart.

This connects the bounded workload to the task kernel, not a general engineering
agent. Independent review, real proposal-only providers, custom executable recipes,
concurrent scheduling and live integrations remain future milestones. Both the fixture
broker and the pilot's Docker workers can now run under a [separate OS identity](broker-service.md)
when the server is started with `--broker-endpoint`/`--broker-secret`; the kernel passes
that configuration to every pilot it prepares. Python remains the control plane
and fixed worker language.
