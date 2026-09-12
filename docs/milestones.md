# MVP milestones and verification

Phase 1 is the design baseline. M1–M3 are implemented for the bounded fixture workflow;
the local management UI now covers M4 visibility and explicit decisions. M5 has an
offline CLI conformance boundary and [Copilot protocol codec](copilot-protocol.md);
real providers remain disabled pending their
provider-specific reviews. See [local management](local-management.md) and
[provider adapter](provider-adapter.md) for the implemented scope and limitations.
Worker hardening now includes [mandatory resource-containment gates](docker-resource-gates.md).
The human control boundary also supports [durable cancellation](operator-cancellation.md)
between operations.
Backup/restore recovery now includes [verified restore publication](verified-restore.md),
so a failed copy or audit does not expose a partially restored destination.
Operators can [renew expired pending approvals](approval-renewal.md) while retaining
the exact scope and a separate human decision.
[Storage usage reporting](storage-usage.md) now makes quota consumption and conservative
orphan cleanup candidates visible without changing workflow data.
[Recovery diagnostics](recovery-diagnostics.md) explain blocked-task preconditions
in the UI and API; runtime recovery remains an explicit, separately checked action.
The diagnostic panel now offers guarded operator recovery with an evidence-review
acknowledgement, matching revision, and no automatic continuation afterward.
Recovery retries now preserve the committed outcome and separately record completed,
pending or deferred workspace cleanup, avoiding repeated runtime reconciliation.
[Continuous validation](continuous-validation.md) runs offline checks on Windows/Linux
and isolates the five mandatory Docker gates in a Linux CI job.
[Verified backup publication](verified-backup.md) prevents handled copy/audit failures
from leaving a partially populated backup at the requested destination.

| Milestone | Deliverable | Exit gate |
| --- | --- | --- |
| M0: design | Boundaries, threat model, transitions, 19 versioned schemas, interfaces, layout and tests | Contract validator passes; architecture/security decisions reviewed |
| M1: durable core | SQLite migrations, events/outbox, schema validation, CLI/API task creation and human spec/plan approval, fixture provider | Replay and crash tests; every illegal edge/approval attack rejected |
| M2: isolated edit/test | Single host broker, fresh workspace, proposal patch application in Junior container, trusted frozen-snapshot test runner | One allowed edit and predefined test; SEC-02/03/04/10 adversarial gates |
| M3: independent review/action | Separate reviewer invocation, retry/replan routes, action approval and idempotent simulated PR | ACCEPT, REJECT, NEEDS_CHANGES, expiry and crash/reconciliation paths verified |
| M4: local visibility | Authenticated localhost UI with task detail, approvals and replayable SSE | All evidence visible after restart; CSRF/XSS/artifact ACL checks |
| M5: provider integration | Versioned Copilot and corporate CLI adapters, role routing, capability/health checks, optional constrained SSO broker | Both pass same conformance suite; unsupported auth/tools block safely |

M1-M4 use deterministic fixture providers so correctness does not depend on live model
availability. M5 completes the real-provider slice; fixture success alone is not evidence
that either corporate authentication or CLI containment works. Native provider agents
remain disabled; model proposes the edit, the Junior worker applies it.

## Smallest proposed vertical slice

Use a disposable repository containing `greeting.txt` and an immutable test script from
the approved base. Human creates/explicitly confirms a TaskSpec to replace `hello` with
`hello world`. Lead produces a plan allowing exactly that file, one small patch and the
predefined argv `python /trusted-tests/check_greeting.py /workspace/greeting.txt`.
The human approves its hashes; a Junior model proposes the replacement; the broker
validates and applies it in a network-disabled container. Freeze the patch and run the
trusted test in a new container. A separate reviewer receives only spec, plan, diff and
test receipts. ACCEPT leads to human approval of the simulated PR destination/payload;
record a local simulation identifier (no fabricated GitHub URL), then complete the task.
All invocations, decisions, actual commands and transitions appear after UI restart.

Also demonstrate failing test, reviewer rejection and revised plan approval. Any patch
to the trusted test, unexpected file, symlink or out-of-budget change fails closed.

## Testing strategy

Contract checks validate the schema against the Draft 2020-12 metaschema, resolve all
references locally, validate one example per contract and reject unknown/missing fields,
bad versions, invalid enum values, bad paths and invalid timestamps. JSON Schema handles
shape; domain tests must enforce hash bindings, identities, temporal relationships,
reference existence, state guards and policy semantics.

Unit tests exhaust allowed and disallowed transition edges, role context inclusion/
exclusion, policy matching and canonical approval digests. Integration tests exercise
SQLite transactions, outbox deduplication, artifact finalization and authenticated UI.
Property tests vary task revisions, retries and interleavings. Crash injection covers
intent, dispatch, process completion, artifact commit, receipt acknowledgement and state
commit. Restore tests replay the events and compare projections/evidence manifests.

Container tests run on Docker Desktop Linux containers from both PowerShell 7 and WSL,
then a Linux host. Test denial of socket/home mounts, root escalation, symlink traversal,
network egress and resource exhaustion. These are release blockers, not merely unit tests
of command-line flags. Provider conformance fixtures cover valid/invalid JSON, unexpected
tools, timeouts, auth failure, partial output, cancellation and unavailable model/usage.
Live opt-in tests must use approved non-sensitive fixtures and no production credentials.

Real GitHub actions follow only after this slice: branch/commit/push/PR broker with
destination-bound approval, least-privilege identity and uncertain-result reconciliation.
Then Jira Data Center may support PAT/service identity, Epic lookup or explicit creation,
Story/Task links, GitHub Issue/PR associations and status updates through the PM broker.
No Jira credential distribution to workers. Production actions, memory, autonomous
scheduling, concurrent workers and retrieval remain outside MVP.

## Decisions to resolve before enabling real execution

Confirm corporate source-sharing rules and CLI tool-disable/SSO behavior; select approved
image digests and dependency sources; set artifact retention/quotas; choose operator
identity provisioning; validate Docker Desktop isolation against corporate policy.
These decisions do not prevent review of the architecture/contracts.
