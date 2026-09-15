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
Strict assessments now resolve task-owned test requests and bind their operation,
patch/work-order references, snapshot, complete command, runner image and creation time.
The referenced work order is now resolved and checked for repository/review bindings,
exact allowed paths and tests, reported change-byte limits and execution deadlines.
Plan and historical plan-approval records now resolve within the evidence snapshot,
with scope/attempt limits, exact subject/evidence digests and approval chronology checks.
Task specifications and their original request artifacts now anchor that chain, with
recorded confirmation, repository/path containment and spec-before-plan chronology checks.
Plan approval evidence now requires the workflow registry to bind the exact request
and decision IDs in the same read snapshot; unregistered decisions cannot pass.
Patch/test evidence now requires completed task-owned operations at the expected stage
and attempt, with bounded canonical results naming the exact successful receipts.
Completed operations now require canonical retained broker requests for their exact
generation, task, operation and fixture recipe, with request digests in the assessment.
Retained broker envelopes now require task-owned canonical artifacts, matching request
identity/fences/digests and successful untruncated outcomes under UTF-8 output budgets.
Broker results now match test and single-command patch receipts by executed argv,
timing, exit/truncation claims and redacted output artifact hashes and byte counts.
Runtime claims now require matching patch/test source, mode and image, digest-pinned
Docker images with matching Linux test metadata, and exact requested workspace strings.
An explicit read-only stored-record CLI now runs the same claims checks before receipt
artifact export, exposing blockers without creating artifacts or changing approval admission.
Real trusted-fixture records now pass that diagnostic at action approval: assessment
accepts the executor's exact fixed-recipe wrapper and optional success failure field,
with end-to-end coverage and rejection of modified wrappers.
A bounded read-only task catalog now exposes candidate receipt/policy references and
creation times, retaining multiple candidates for explicit record-assessment selection.
An explicit review/policy anchor resolver now produces assessment-ready selections by
following retained review links, rejecting ambiguity and conflicting patch references.
Implementation snapshot artifacts now require task ownership, bounded byte verification
and agreement with patch receipt digests, closing a gap beyond receipt-only hash claims.
The fixed edit fixture now checks its diff, before/after hashes, changed-byte count and
broker argument against those snapshot bytes; arbitrary repository diffs remain outside
this diagnostic boundary.
Patch command claims now require the executor's exact fixed edit wrapper, rejecting
matching receipt/envelope claims for altered scripts, arguments or Docker isolation
options without executing commands or resolving historical workspace paths.
[Portable evidence bundles](github-evidence-bundles.md) now provide a complete offline
handoff: explicit selection, read-only dependency collection, bounded canonical export,
exclusive publication and standalone verification in disposable storage. Verification
rechecks current claims and expiry and rejects missing, extra or altered dependencies;
the format neither restores a workflow nor grants execution authority.
[Journal-bound bundle review](github-bundle-approval.md) now derives approval evidence
directly from verified bundles, checks their tool payloads against prepared scope and
binds the exact bundle file in a saved preview. Reassessment checks expiry and journal
state again after bundle/tool inspection; no approval decision or dispatch is enabled.
[Combined offline preflight](github-preflight.md) now adds scope-bound repository and
ref observation snapshots to that review, enforces a five-minute freshness window and
rechecks journal state and policy/approval expiry after observation assessment. Supplied
responses and timestamps remain unauthenticated; no remote fetch or dispatch is enabled.
The [local evidence review workspace](evidence-review-ui.md) now exposes explicit
review/policy selection, inspection, diagnostics, selection downloads and portable
bundle downloads through authenticated API routes and the UI. Bundle downloads recheck
the selected chain, verify bounded bytes in the browser and cancel obsolete requests;
browser coverage includes standalone verification of the downloaded file.
[Received bundle review](received-bundle-review.md) completes the receiving workflow:
an empty local workspace can verify uploaded bytes against an explicit expected digest,
reassess them in disposable storage and save the diagnostic without adopting a task.
[GitHub read plans and transcripts](github-read-transcripts.md) now cover the offline
response-ingestion boundary, including exact GET identities, bounded decoding, failure
normalization and a final prepared-scope check before producing preflight snapshots.
[Recovery transcripts](github-recovery-transcripts.md) extend this contract to uncertain
operations, exact paginated PR lookup, validated continuation links and a fresh
reservation check before read-only reconciliation. Incomplete captures cannot authorize retry.

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

The [offline Jira foundation](jira-preview.md) now implements explicit instance/project/
issue validation, a bounded untrusted-text projection and comment previews bound to that
snapshot. It does not enable Jira calls or task adoption.
[Jira comment recovery](jira-comment-recovery.md) adds preview-bound read plans and
paginated candidate assessment, including author/visibility matching. Incomplete or
ambiguous captures remain unresolved. It does not confirm delivery or authorize another attempt.
[Retained Jira operations](jira-journal.md) add immutable staging, one-use reservations,
capacity enforcement, integrity audits and recovery against saved scope. Live dispatch,
credential admission and operator retry policy remain unimplemented.
[Jira recovery bundles](jira-backups.md) now provide verified snapshots, comparison with
active reservation history and disposable recovery drills. They do not restore active storage.
[The consolidated Jira package](jira-integration.md) now adds metadata-bound Epic/Story/Task
creation, transitions, Epic relationships, issue links, GitHub associations, journal support
for those actions, expiring review previews and identity/permission/freshness preflight.
A synthetic acceptance scenario exercises the complete offline lifecycle. Live transport,
credential/approval admission and authoritative evidence/recovery remain explicit deployment gates.

## Local repository data pilot — September 15, 2026

The [Git JSON pilot](repository-pilot.md) provides exact commit intake, explicit
replacement/check review, one-use scope/revision approval, isolated data workspace
creation and durable interruption inspection in one CLI milestone. Its disposable
Git acceptance runs in release verification. The fixture kernel, UI, provider and
integration authority remain unchanged; this is a bounded data workload with fixed
checks, not arbitrary code execution. Rust is deferred until a worker requirement
justifies it.

## Decisions to resolve before enabling real execution

The [repository-task kernel milestone](repository-tasks.md) connects JSON pilots to
TaskSpec, dedicated plan/result contracts, execution approval, local snapshot sign-off,
shared evidence/replay/backup and explicit recovery across the pilot/kernel commit
boundary. It adds authenticated task controls and browser acceptance while rejecting
fixture-batch adoption and remote-action claims for this workload.

The [security acceptance milestone](security-acceptance.md) binds each mandatory
criterion to the tests that exercise it, runs them as a release-lane stage and reports
coverage, Docker-gated evidence, other stages and deferred limits per criterion.

The broker secret rotation follow-up in [the broker service](broker-service.md) adds a
two-secret overlap window, live re-read without restart, replies signed with the
authenticating secret, `broker-rotate-secret`, and reported freshness that keeps the
`broker_identity` deployment gate blocked while a rotation is unfinished or a secret
is past its documented maximum age.

The [operator visibility follow-up](pilot-operator-visibility.md) surfaces pilot usage
in the authenticated UI/API, pilot state in repository recovery diagnostics and an
audited reason on live reclamation, without adding any UI mutation.

The [pilot retention milestone](pilot-retention.md) adds `pilot-usage` and reviewed
`pilot-reclaim` with dry-run, a durable `reclaiming`/`reclaimed` record, tamper-retaining
eligibility checks, bound-task terminal gating and release-lane acceptance. It releases
live journal capacity without deleting evidence, archive or automation.

The [pilot container-worker milestone](pilot-container-workers.md) extends the
Git JSON pilot with two restricted containers, runtime-bound approval, per-phase
durable dispatch and ownership-checked cleanup after interruption. It adds real
Docker acceptance for the pilot while preserving the existing five fixture gates.
Custom executable recipes, generalized kernel tasks and separate broker identity
are subsequent boundaries; fixed Python recipes remain sufficient here.

The [broker service milestone](broker-service.md) adds the separate-identity boundary
for the fixture broker: `broker-serve` under its own OS account, HMAC-authenticated
loopback routes for execute/result/retire/reconcile/identity, a broker-owned journal,
`broker_identities` evidence, `broker-check` and a `broker_identity` deployment gate,
with threaded, subprocess and release-lane acceptance. The follow-up routes the pilot's
Docker workers through the same service (`pilot-profile`, `pilot-execute`,
`pilot-reconcile`, `--pilot-root`) with real-Docker acceptance in the CI Docker lane.
Host-side account provisioning is not included.

Confirm corporate source-sharing rules and CLI tool-disable/SSO behavior; select approved
image digests and dependency sources; set artifact retention/quotas; choose operator
identity provisioning; validate Docker Desktop isolation against corporate policy.
These decisions do not prevent review of the architecture/contracts.
