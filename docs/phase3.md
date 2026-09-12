# Phase 3: runtime hardening before real providers

Decision: retain Python for both control plane and the small execution broker now.
Keep the versioned JSON boundary language-neutral. Consider replacing the broker with
Rust only after its interface stabilizes and measured packaging, concurrency or assurance
needs justify another implementation. A language change does not itself isolate a
worker, authenticate a receipt, enforce approvals or make crash recovery correct.

The roadmap now reads: Phase 1 design; Phase 2 offline kernel; Phase 3 runtime/recovery
hardening; next, pass real Docker gates on supported hosts, then add one constrained
provider adapter. GitHub/Jira, corporate authentication, concurrent workers and advanced
orchestration remain separate later phases. The alternative twelve-phase roadmap is
useful as a capability list, but isolation and broker checks should precede real agents.

## Added boundaries

Every worker edit/test is dispatched to `orch.broker_process`, a short-lived Python
subprocess. Its stdin accepts a closed versioned request selecting only the bundled
edit/test recipes. It gets an assigned workspace, not a database handle, provider output,
operator session, home mount, Docker socket mount or arbitrary shell string. The host
broker still has trusted host OS authority; Docker is invoked only from that broker.

The new [broker wire schema](../contracts/broker-v1.schema.json) is separate from the
unchanged 19 Phase 1 contracts. A durable request binds task, operation, generation,
nonce, workspace, recipe, arguments, executor mode/image and source/schema digest.
The broker writes its result to an atomic, fsynced journal while holding an OS file lock.
The engine checks exact request hash, generation, nonce, source digest and result schema,
then persists an immutable execution-provenance artifact. No HTTP endpoint accepts
worker-produced envelopes or workflow receipts from arbitrary clients.

This is **provenance within a trusted local OS identity**, not a cryptographic signature
or separate-user security sandbox. A host administrator or same-user attacker can edit
the database/journal/code. Separate broker service identities, authenticated transport
and signed/off-host evidence are still required before hostile multi-user deployments.
Source digests detect changed broker code/schema during recovery; they do not attest
the entire Python dependency supply chain.

## Fencing and recovery

SQLite schema v2 migrates v1 without discarding tasks. Operations gain a monotonic
generation; immutable `broker_requests` and `execution_provenance` tables bind execution
to that generation. Completion uses a conditional update on live operation/generation.
Unknown newer database versions are rejected instead of being overwritten.

Existing completed-operation replay and simulated-effect reconciliation still apply.
If broker acknowledgement is uncertain, the task enters BLOCKED with its resume state
and operation ID. An authenticated operator may request:

```text
POST /tasks/{task_id}/recover
{"expected_revision": CURRENT_BLOCKED_REVISION}
```

Or from the trusted local CLI:

```sh
python -m orch recover --trusted-fixture --data .runtime/demo --task TASK_ID --expected-revision REVISION
```

Recovery requires the exact current revision and unexpired plan approval. It accepts
only a matching durable broker result once the broker lock is free. A fully committed
workflow result resumes through the existing replay logic. If only raw execution
evidence exists, recovery preserves it, retires that incomplete attempt, advances the
generation, records the operator action, and follows the allowed prior-state ->
CHANGES_REQUESTED path. The next dispatch gets a fresh bounded WorkOrder. It never
fabricates a successful PatchReceipt from partially persisted context.

Without a valid journal, Docker recovery holds the broker lock, writes a durable retired
marker (preventing a late broker launch), and inspects/removes only the exact labelled
operation container. A daemon error is not evidence of absence. Local fixture mode
cannot independently prove an unknown orphan process exited, so it remains BLOCKED.
Invalid existing journals are retained for investigation. Expired approvals are never
bypassed. Later milestones add [pending approval renewal](approval-renewal.md) and
[quiescent cancellation](operator-cancellation.md); general remediation remains future work.

The single-host dispatcher lock remains the scheduling boundary. Generations reject
stale results but do not provide distributed consensus or exactly-once remote execution.

## Bounded execution and cleanup

Process output is drained concurrently with a 64 KiB per-stream retained-byte limit.
Overflow or deadline terminates the direct child and is recorded as failure; a test
uses its trusted recipe's ten-second deadline. The broker client also bounds its
envelope and execution time. These fixed programs do not spawn children. This is not
a general hostile process-tree supervisor; arbitrary local execution is still forbidden.

Docker execution retains non-root identity, network none, read-only root, dropped
capabilities, no-new-privileges, resource limits and read-only test workspaces. Error
cleanup checks the operation label before removing a container and verifies absence.
The controls use Docker's documented [run options](https://docs.docker.com/reference/cli/docker/container/run)
and [none network](https://docs.docker.com/engine/network/drivers/none/); flags alone are
not evidence that the host/container boundary has been tested.

After durable workflow receipt completion, only the expected `greeting.txt` is removed
and the workspace directory is removed nonrecursively. Links, junctions, unexpected
files and out-of-scope paths fail closed and are retained with a cleanup-deferred event.
Snapshots, diffs, test output and provenance remain in immutable artifact storage.

## Artifact lifecycle and backups

Defaults: 1 MiB per artifact, 64 MiB cumulative referenced bytes per task and 256 MiB
physical artifact storage. A 4 MiB trusted audit reserve permits recording failure.
Duplicate references count toward the task limit intentionally. These bounds govern
artifact ingestion, not the total SQLite database, broker journal or host disk usage.
More comprehensive disk/account quotas are required before broad workloads.

```sh
python -m orch gc --data .runtime/demo
python -m orch backup --data .runtime/demo --destination .runtime/demo-backup
python -m orch restore --data .runtime/demo-backup --destination .runtime/demo-restored
```

GC deletes only unreferenced hash/staging files older than a day, never rows or live
audit evidence. It takes the dispatcher lock and refuses links/junctions. Referenced
artifact retention is unchanged; policy-based expiry of task evidence is not implemented.

Backup takes the dispatcher lock, verifies SQLite integrity/foreign keys, validates
records, verifies artifact bytes and compares every task projection with event replay.
It uses Python's [SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup)
and copies referenced artifacts into a new destination, with a digest manifest. Restore
requires a new directory, validates hashes before copying and repeats the integrity/
replay audit. A corrupted backup cannot overwrite a live data directory.

Backups do not copy live broker locks/journals or workspaces. Committed receipt provenance
is already an artifact; unresolved restored operations remain blocked rather than
assuming ownership of original-host processes. Manifests detect accidental corruption,
not malicious replacement by someone who can rewrite both backup and manifest. Backups
must be stored under trusted access controls. Interrupted backup directories remain
for inspection. Restore now uses [verified staging publication](verified-restore.md),
with handled-failure cleanup and separate instructions for abandoned staging directories.

## Validation and Docker gates

Offline suite (no LLM or external services):

```sh
python -m unittest discover -s tests -v
python scripts/validate_contracts.py
python -m orch demo --trusted-fixture --data .runtime/phase3-demo
```

Added tests exercise real broker processes, stale generations, bad nonces, unknown
outcomes, active broker locks, output flood/deadline limits, conservative cleanup,
quotas/GC, v1 migration, future-version rejection, backup corruption and restore/continue.

Real container gates are opt-in and never download an image. On each approved Windows
Docker Desktop, WSL/Linux and Linux Docker host, preload a digest-pinned Python image:

```powershell
$env:ORCH_DOCKER_TEST_IMAGE = 'python@sha256:YOUR_APPROVED_DIGEST'
python -m unittest discover -s tests -p test_docker_integration.py -v
```

```sh
ORCH_DOCKER_TEST_IMAGE=python@sha256:YOUR_APPROVED_DIGEST python -m unittest discover -s tests -p test_docker_integration.py -v
```

These gates run the full container workflow and probe UID, effective capabilities,
no-new-privileges, read-only root/workspace, socket absence, egress denial and cleanup.
Without the image/runtime they are explicitly skipped. They are a first containment
gate, not a complete adversarial resource-exhaustion or kernel escape assessment.

This workstation has neither Docker Desktop nor an installed WSL environment. No
Windows/WSL/Linux container-isolation result is claimed here. Real provider enablement
remains gated on those results and a separate review of tool-disable/authentication
behavior. Rust is an available future broker implementation, not a prerequisite for
finishing these concrete security checks.

Executed on Windows/Python 3.12 on 2026-09-12: 44 tests discovered, 42 passed and the
two real-Docker tests skipped. This includes real subprocess and loopback HTTP tests,
operator recovery and a backup/restore/continue drill. The opt-in test results must be
recorded separately on each supported container host before enabling a real provider.
The CLI demo also reached COMPLETED with 59 events; CLI backup/restore verified and
restored 89 unique artifact blobs. Phase 1 validation passed all 19 examples and 366
negative cases. Python compilation and whitespace checks passed.
