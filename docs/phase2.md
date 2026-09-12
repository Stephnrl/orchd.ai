# Phase 2: executable offline workflow

The Phase 1 state names, contracts and approval rules are unchanged. This implementation
is a deliberately narrow proof against a built-in greeting fixture. It cannot run a
user-selected repository command, real provider, or remote action.

## Running and demonstrating

Use Python 3.12+ from the repository root. Install the two pinned dependencies once from
an approved package source; execution/tests require no internet or service credentials.

```sh
python -m pip install -r requirements-dev.txt
python scripts/validate_contracts.py
python -m unittest discover -s tests -v
python -m orch demo --trusted-fixture --data .runtime/demo
```

The demo creates a confirmed TaskSpec, pauses for plan approval, explicitly submits an
operator approval, implements/tests v1, receives NEEDS_CHANGES, implements/tests v2,
receives ACCEPT, pauses for action approval, explicitly submits it, creates a simulated
PR receipt and reaches COMPLETED. It prints the task ID. It is an executable scripted
demonstration of the approval boundary, not an autonomous approval feature.

Inspect the full persisted history, including materialized event payloads:

```sh
python -m orch history --trusted-fixture --data .runtime/demo --task TASK_ID
```

`tests/test_workflow.py::test_needs_changes_full_loop` provides the equivalent automated
assertions. Every fixture test invokes an actual Python child process. Failure scenarios
are configured with `Scenario` through the trusted in-process demonstration/test interface;
the HTTP API does not accept arbitrary provider configuration.

For the Docker path, preload a corporate-approved Python 3.12+ image and supply its
actual digest (the following placeholder must be replaced):

```sh
python -m orch demo --image python@sha256:YOUR_APPROVED_DIGEST --data .runtime/docker-demo
```

Docker never pulls images implicitly. Each edit/test runs in a separate `--rm` container,
non-root, network disabled, root read-only, capabilities dropped, no-new-privileges,
bounded CPU/memory/PIDs and tmpfs. Test mounts are read-only. No Docker socket is mounted.
The trusted host process invokes Docker. This path requires Docker Desktop/Linux
containers or Linux Docker; its OS isolation has not been verified on this workstation.

## Components and storage

- `orch/contracts.py`: Phase 1 JSON Schema validation, RFC 8785 canonical hashes, IDs,
  timestamp checks and recognized-secret redaction. The engine validates every contract
  boundary; the Phase 1 `ValidatedContract` wrapper alone remains non-authoritative.
- `orch/engine.py`: sole task-state writer, explicit transition matrix/guards, role context,
  approval boundary, bounded retries and persisted operation dispatch.
- `orch/storage.py`: SQLite WAL, foreign keys, append-only records/events/approvals/effects,
  receipt uniqueness, task-bound artifact references and immutable content-addressed bytes.
- `orch/provider.py`: deterministic AgentProvider implementing the Phase 1 protocol.
  Planner, Junior and reviewer calls have separate invocation IDs. Reviewer gets only
  spec, plan, patch, diff, test and ReviewRequest, never Junior conversation.
- `orch/execution.py`: worker and real subprocess runner using bundled constant recipes.
- `orch/actors.py`: deterministic Guardian and replaceable simulated external-action port.
- `orch/api.py`: a small loopback JSON controller with an in-memory operator session.

Schema version 1 creates `tasks`, `records`, `events`, `artifacts`, `approvals`,
`operations` and `effects`. Generic immutable `records` store typed contract payloads;
indexes prevent multiple execution receipts for the same operation and multiple
invocation receipts for the same invocation. `operations` is the synchronous durable
dispatch/outbox ledger. No dedicated dispatcher process is needed in Phase 2.

Events have monotonically increasing per-task sequence numbers. Their artifact payloads
contain the TaskState/context projection and operation details, so replay works without
application logs. SQLite triggers reject updates/deletes to immutable tables. Task
projection writes and event appends share a transaction. Artifacts are redacted, hashed,
fsynced and atomically placed before references are recorded. Intermediate orphaned
artifact files may remain after a crash; no automatic garbage collection is implemented.

## Approval and state semantics

Task creation through the operator API is also explicit confirmation of the fixed
specification. The DRAFT_SPEC -> SPEC_READY transition and confirmation event are both
recorded. Planning produces the immutable scope for exactly `greeting.txt` and its trusted
test recipe. Approval binds task, spec/plan/base, evidence and policy with canonical
hashes and expiry. External approval additionally binds patch/test/review and destination.
Only the operator boundary inserts `ApprovalDecision` and its unique approval row.
There is no API for injecting receipts, identity, arbitrary transitions or shell commands.

The state machine checks current persisted approval/evidence before protected transitions.
Repeated identical approvals are idempotent; conflicting decisions and stale revisions
are rejected. Plan rejection replans; action rejection requests changes. Failed
implementation/testing/review returns to CHANGES_REQUESTED and a fresh WorkOrder, with
three attempts maximum. Failed planning and failed simulated action end in FAILED.
Terminal tasks do not dispatch again. Clarification, user-edited replanning, cancellation
and manual BLOCKED recovery are not exposed in this narrow API.

## Restart/idempotency

A kernel file lock permits one dispatcher across processes. An operation intent and
start event commit before execution. Invocation context/start events commit before mock
invocation. Results and completion status persist before the next workflow transition.
Restart reuses a completed operation's saved result. A crash after testing but before
REVIEWING therefore does not rerun the worker or test process.

Simulated effects and ExternalActionReceipt commit in one SQLite transaction, keyed by
the approved ToolRequest operation ID. Reconciliation can recover that effect even if
the outer dispatch acknowledgement was interrupted. Repeated run/advance after completion
does nothing. Exact duplicate receipts are accepted; conflicting IDs/payloads for the
same operation are rejected.

An interrupted operation with no durable completion becomes BLOCKED and is not blindly
repeated. Phase 2 does not guess whether an orphaned Docker process stopped. An operator
must investigate; automated kill/reap, leases/fencing and general reconciliation are
Phase 3 work. The single-dispatcher lock supplies process exclusion in this phase and
must not be treated as distributed fencing or exactly-once remote execution.

## HTTP API

```sh
python -m orch serve --trusted-fixture --data .runtime/server --port 8080
```

Use `http://127.0.0.1:8080`. Startup prints an ephemeral local operator session; send
`Authorization: Bearer SESSION` on each request. It is kept only in memory, expires on
restart and is never given to a provider. This local capability is not corporate
authentication. Host headers must match the loopback endpoint. Browser Origin requests
are rejected; no cookies or permissive CORS are used. Do not expose this diagnostic
stdlib HTTP server beyond loopback. Responses are JSON, not an HTML frontend.

| Method/path | Body / result |
| --- | --- |
| POST `/tasks` | `{ "title": "Greeting fixture" }`; returns task ID and confirms fixed spec |
| GET `/tasks/{id}` | Current TaskState and evidence references |
| GET `/tasks/{id}/events` | Ordered TaskEvents |
| GET `/tasks/{id}/history` | Events with verified materialized payloads |
| POST `/tasks/{id}/advance` | `{}`; one state-machine step |
| POST `/tasks/{id}/run` | `{}`; progress until approval, terminal state or BLOCKED |
| POST `/tasks/{id}/approvals` | `{ "request_id": "...", "decision": "approve", "expected_revision": 3 }` (or `reject`) |

Read `state.pending_approval.id` and `state.revision` before approving. The principal is
server-assigned; extra approval fields are rejected. Invalid requests return 409; missing
sessions return 401. The local OS user is trusted; this is not a multi-user authorization
system. Direct Python service access is trusted in-process code, like direct database
access, and is not exposed to mock agents.

## Phase 1 implementation decisions and limitations

1. Phase 1 forbids simulated real GitHub URLs, whereas Phase 2 lists a fake PR URL as
   an example. Preserve Phase 1: `external_url=null`; `external_id=sim-pr-OPERATION_ID`,
   with deterministic fake number/branch/repository in the response artifact.
2. Phase 1 requires containers for worker isolation. Preserve the Docker executor and
   require a digest-pinned image by default. Offline tests opt into a **trusted fixture
   executor**, which runs only two bundled constant scripts. It does not claim isolation
   and cannot be extended to untrusted code without the Phase 1 isolation gates.
3. Phase 1 proposes FastAPI, but does not require it. This small diagnostic HTTP adapter
   uses the standard library; contracts/state logic are transport-independent.
4. Phase 1 does not define the implementer's proposal payload. A private closed
   `FixtureEdit-v1` shape permits one known content field/two fixture strings; the mock
   returns a valid AgentInvocationReceipt referencing that proposal. The broker alone
   computes the valid PatchReceipt. No Phase 1 schema was weakened.

Workspaces are retained under the data directory for diagnosis; immutable snapshot/diff/
output evidence is stored independently. No arbitrary repository cloning, image builder,
dependency retrieval, file upload, general tool broker, SSE/frontend, background scheduler,
or production action exists. Resource/output handling is designed for bounded fixed fixture
programs; general untrusted execution needs streaming quotas and stronger recovery first.
Recognized-secret redaction is defense in depth, not a general secret detector. Never
submit sensitive material to the fixture API. Local host administrators remain trusted.
In trusted-fixture receipts, `environment.image_digest` and `runner_image_digest` are
the local Python executable's SHA-256 fingerprint, explicitly a test-mode substitute;
Docker mode records the operator-selected container image digest. No local fixture
receipt is evidence that a container ran. The fixture base commit is a stable synthetic
identifier, not a commit in the application's checkout.

## Validation performed on 2026-09-12

- Windows/Python 3.12: `python -m unittest discover -s tests -v` passed all 29 tests,
  including fixed subprocess execution and a real loopback HTTP service.
- `python -m orch demo --trusted-fixture --data .runtime/phase2-verified-demo` completed
  the two-implementation review loop with 59 persisted events and both approval pauses.
- Phase 1 schema validation passed 19 examples and 366 negative cases.
- Python compilation and `git diff --check` passed.
- Docker command construction was unit-tested with a mocked Docker process. Docker
  was unavailable on this workstation; no Docker, WSL or Linux isolation result is claimed.

## Phase 3 recommendations

Verify Docker isolation and crash cleanup on Windows/WSL/Linux, implement fenced broker
reconciliation and operator BLOCKED recovery, strengthen receipt provenance for separate
process identities, add immutable snapshot lifecycle/quotas/GC and backup-restore drills,
then add a real provider only after its tools and authentication can be contained.
Keep GitHub/Jira integration and corporate authentication separately reviewed and brokered.
