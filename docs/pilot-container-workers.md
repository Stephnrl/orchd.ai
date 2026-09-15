# Restricted Docker workers for the Git data pilot

The Git JSON pilot can now apply its approved replacements in an unprivileged
Docker container and check the frozen bytes in a second, fresh container. The
source repository, Git metadata, pilot journal and credentials are not mounted.
The supported workload remains the [bounded JSON data policy](repository-pilot.md).
These are fixed Python recipes, not repository programs or model-selected commands.

## Prepare, review and run

Start the intended local Linux Docker daemon and preload an operator-approved,
digest-pinned Python image. Runtime commands never pull or build images. The image
and its Python interpreter are trusted inputs; digest pinning identifies the image
but does not independently establish its safety.

Use the existing repository intent and a separate pilot journal:

```text
python -m orch pilot-prepare --data .runtime/pilot --intent intent.json --pilot-executor docker --image IMAGE@sha256:DIGEST
python -m orch pilot-inspect --data .runtime/pilot --pilot-id ID
python -m orch pilot-run --data .runtime/pilot --pilot-id ID --expected-sha256 REVIEWED_SCOPE_SHA256 --expected-revision 0 --pilot-executor docker --image IMAGE@sha256:DIGEST
```

Review `scope.executor` as well as the baseline, replacement diff and checks. It
binds the image reference, verified local image ID, local host/daemon identity and
fixed recipe hash. The executable container uses the verified image ID and an
explicit Python entrypoint. The run must repeat the Docker profile and image;
omitting them cannot silently downgrade approved Docker work to local execution.
Preparation/admission and each dispatch check the runtime. Inspection is read-only
and needs no Docker daemon. Existing `local-data` runs remain explicitly local
data manipulation without a sandbox.

## Worker restrictions and evidence

Both containers use no network, read-only root filesystems, UID/GID 65534, dropped
capabilities and `no-new-privileges`. Each is limited to one CPU, 256 MiB memory,
no extra swap, 64 processes and a 16 MiB noexec/nosuid temporary filesystem. Process
capture limits each output stream to 64 KiB and each worker invocation to 30 seconds.
Images cannot be pulled during execution. The test workspace mount is read-only.

The host creates only empty files at the approved paths, makes them writable for
the unprivileged edit UID, and sends bounded replacements over stdin. After edit
completion it checks the exact file inventory and bytes, rejects links, hardlinks,
special files, unexpected content and oversized output, and freezes the file modes.
The fresh test container receives the expected file hashes and JSON assertions via
stdin. The host independently checks the result against the reviewed assertions,
requires exact protocol types and verifies workspace bytes again before committing
evidence. Passing proves those JSON assertions only.

The `workers` journal retains distinct edit/test container identities, phase states,
command/input/output hashes, process status, timestamps and absence observations.
The final receipt binds that journal. Runtime errors, timeout, truncation, malformed
output or uncertain cleanup cannot produce a passing receipt. Raw process output
is not stored in the worker journal.

## Interruption and explicit cleanup

A process lock serializes execution and reconciliation for the journal. SQLite
commits the pilot reservation and worker identities before workspace effects, then
commits each `dispatched` phase before launching Docker. A crash can leave a
`reserved` pilot with a live or already-removed container. It never authorizes retry.

Inspect first, then use the current revision and retained scope hash:

```text
python -m orch pilot-reconcile --data .runtime/pilot --pilot-id ID --expected-sha256 REVIEWED_SCOPE_SHA256 --expected-revision CURRENT_REVISION --pilot-executor docker --image IMAGE@sha256:DIGEST
```

Reconciliation requires the same host, daemon and image. It refuses to act while
a runner holds the process lock. Removal requires an exact container name and all
three labels: pilot ID, scope hash and phase. It removes the inspected container
by immutable container ID and checks absence again. Name collisions, label mismatch
or Docker errors remain uncertain. Normal worker completion also checks cleanup.

Every explicit cleanup attempt is retained and increments the revision. If both
containers are confirmed absent, the pilot becomes `interrupted`, with no successful
receipt and no retry authority. Otherwise it remains `reserved`; another cleanup
attempt must use its new revision. Cleanup can handle a recipe code update without
readmitting execution. Changes to the required runtime identity still block it.
Workspaces and evidence remain retained. No automatic workspace deletion, journal
archive, resume or integration write is enabled. Standard workflow backups do not
include this separate pilot journal.

## Acceptance and retained evidence

The full/offline release lane exercises the new admission/dispatch/recovery unit
tests and the local-data CLI walkthrough. The Docker CI lane also runs:

```text
python scripts/pilot_worker_acceptance.py --image IMAGE@sha256:DIGEST --destination .runtime/pilot-worker-report.json
python scripts/pilot_worker_acceptance.py --image IMAGE@sha256:DIGEST --destination .runtime/pilot-worker-report.json --check --expected-sha256 RETAINED_REPORT_SHA256
```

This opt-in script runs the real Docker CLI lifecycle, isolation/limit probes,
timeout/output cleanup and restart/reconciliation of a deliberately interrupted
live container. Probe script substitution is test instrumentation, not an admitted
application API. Reports bind the source inventory, host, daemon and image; checking
also requires the separately retained report digest and a 24-hour freshness window.
Reports are unsigned local evidence, not production approval. Run the existing
`verify-runtime` command as well for the five broader fixture Docker stress gates.
Preflight availability alone is not host qualification.

## Remaining architecture boundary

The pilot is still a CLI-only workflow with its own journal, not an `Engine` task,
UI batch entry, provider adapter or external integration. Its worker supervisor
runs under the local operator's identity and can access Docker; a separate OS broker
identity and independent security review remain deployment gates. A compromised
host administrator or trusted image is outside the claim of this bounded proof.

Keep the fixed recipes and control plane in Python. A Rust supervisor becomes worth
considering when custom images or broader process lifecycle requirements establish
a concrete benefit. This milestone introduces no arbitrary custom-image recipes,
Rust binary, concurrent workers or live AI/GitHub/Jira operations.
