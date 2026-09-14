# MVP milestones and verification

The [original-roadmap progress assessment](progress.md) separates this bounded MVP
from the remaining production integrations. [Deployment checks](deployment-readiness.md)
now combine provider inventory and optional worker evidence without authorizing execution.
The [offline GitHub preview](github-preview.md) supplies a separate destination-bound
request preparation contract; live transport and approval admission remain unimplemented.
An [offline GitHub ref assessment](github-ref-assessment.md) now checks supplied
repository identity and branch-tip observations against that prepared scope.
An [offline reconciliation classifier](github-reconciliation.md) now distinguishes a
single matching draft PR from unresolved observations without enabling dispatch or retry.
A separate [GitHub operation journal](github-journal.md) now durably stages immutable
scope and permits one conservative attempt reservation per task/operation.
Read-only inspection validates retained scope and reservation evidence without creating
or initializing a journal, reconciling an outcome or granting dispatch authority.
Journal-bound offline reconciliation now derives scope from a retained uncertain
reservation and binds the complete assessment to it without resetting the attempt.
Journal admission now has record and scope-byte ceilings, with read-only usage reporting
and no eviction of retained attempt evidence.
A whole-journal audit validates every bounded retained record in one consistent read
snapshot and reports a digest of scope/reservation metadata without proposal text.
Verified journal backup bundles now preserve a consistent SQLite snapshot and bind its
bytes and audit report in a completion manifest; restore/adoption remains disabled.
Read-only backup comparison now identifies missing operations and changed scope or
reservation evidence against a verified, explicitly selected bundle.
Disposable recovery drills exercise one-shot reservation behavior and reopening on a
verified backup copy, then remove scratch storage without replacing an active journal.
Audits and journal mutations now gate on the supported schema and retention-trigger
definitions; backup verification and drills reject altered protections as well.
A [GitHub-specific approval preview](github-approval.md) binds prepared journal scope,
evidence/policy digest claims and expiry without issuing a decision or authorizing dispatch.
Saved approval checks now enforce the preview structure and validity window and detect
changes in journal scope/state or evidence claims, while keeping authorization disabled.
Task-owned artifact checks now validate the claimed evidence bytes against canonical
workflow metadata and content hashes; semantic outcomes and policy trust remain unverified.
Combined local approval assessment now checks artifacts between initial and final preview
validation, detecting expiry or reservation changes during file checks without authorizing use.
Stricter evidence-claim checks now validate canonical stored workflow contracts and their
patch/test/review/policy relationships; independent provenance and live approval remain required.
Combined approval assessment now requires those consistent claims, combines their blockers
with approval blockers, and checks policy expiry against the final assessment time.
It also binds the policy's tool operation, request checksum and task-owned canonical
payload to the exact retained GitHub scope, rejecting simulated or mismatched requests.
Strict evidence assessments now verify task-owned patch diffs and test/patch-command
output artifacts under reference and byte budgets, without claiming execution provenance.
Evidence chronology now checks execution windows and patch/test/review/tool/policy
ordering, rejects future records and contains reported commands within patch execution.
Artifact evidence reads now reject Windows junctions as well as symbolic links, with
a real directory-link regression and canonical-path mocking for Windows runner aliases.

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
It also includes a real Chromium operator workflow check against an isolated local
fixture server, alongside the targeted DOM-stub regressions.
Browser restart checks now cover completed and cancelled workflows: old sessions are
rejected, evidence and event history remain readable, and reconnecting preserves the
task state/context and disabled terminal controls.
[Verified backup publication](verified-backup.md) prevents handled copy/audit failures
from leaving a partially populated backup at the requested destination.
[On-demand integrity audits](integrity-audit.md) expose the existing backup consistency
checks without copying data or attempting repairs.
[GC dry-run previews](gc-dry-run.md) expose exact eligible orphan candidates before
the separate deletion command is invoked.
Integrity audits, backups and restores also cross-check persisted broker requests and
execution-provenance bindings, including historical generations after recovery.
An explicit CLI/API cleanup retry lets operators resolve deferred recovery cleanup
after inspection, guarded by the recovered revision and completed operation.
The local management UI exposes that cleanup retry with an inspection acknowledgement,
revision checks and a refreshed outcome after the request.
GC completes path validation before deletion and opens its database read-only,
preventing scan failures from deleting earlier candidates or migrating storage.
The backup CLI likewise requires existing current-version storage and opens its source
read-only, preventing source typos or backup requests from creating or migrating data.
[Saved-backup verification](verify-backup.md) checks manifest, content and durable
evidence consistency without restoring a snapshot or modifying its files.
The local UI now exposes manual store-wide storage and integrity reports with explicit
verdicts, timestamps and no automatic maintenance actions.
Task refreshes now fence overlapping responses and clear actionable state on pending
or failed reads so older responses cannot restore stale operator controls.
An explicit task-refresh button and status message let operators retry failed reads
while clearly marking retained details as potentially stale.
Evidence reads likewise fence overlapping selections and clear stale contents during
loading or failures, preventing older artifacts from replacing the selected evidence.
Task and record lists fence stale responses and serialize next-page requests to avoid
duplicate entries, with retryable failed pages and cleared record links on task switches.
Task navigation now shows verified specification titles alongside shortened IDs and
state labels, with literal text rendering and identity-based selection for duplicate names.
Event replay now marks update failures explicitly, preserves its cursor for retries,
and ignores responses/errors from earlier task selections.
The evidence viewer supports explicit local review-copy downloads of its current
record or artifact, with fixed filenames and stale-selection guards.

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
