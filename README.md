# orchd.ai

An internal engineering workflow proof with persisted state, explicit approvals,
deterministic mock actors, real fixture tests, and simulated PR receipts.

See the [roadmap progress assessment](docs/progress.md) for implemented scope and
remaining integrations, and [deployment readiness](docs/deployment-readiness.md) for
an executable report of the gates still blocking live providers.
[Offline release verification](docs/release-verification.md) runs the complete local
acceptance package and checks retained evidence against its source, lane and expiry.
[Serial workflow batches](docs/workflow-batches.md) advance reviewed fixture task lists
to their next approval boundaries with durable checkpoints and explicit interruption review.
The local Task batches panel provides scope preparation, reviewed execution and recovery
inspection through the same authenticated API and serial runner.
The [offline GitHub request preview](docs/github-preview.md) begins external broker
preparation with explicit destination checks and a digest of the proposed scope.
[Portable evidence bundles](docs/github-evidence-bundles.md) package an explicitly
selected workflow chain for bounded offline verification without the original store.
[Journal-bound bundle review](docs/github-bundle-approval.md) prepares and rechecks an
approval preview against that exact bundle and its intended GitHub operation scope.
[Combined offline preflight](docs/github-preflight.md) adds scope-bound repository/ref
observations, a fixed freshness window and a final journal/expiry recheck.
[GitHub read plans and transcripts](docs/github-read-transcripts.md) define exact GET
requests and validate bounded captured responses before producing preflight snapshots.
[Recovery transcripts](docs/github-recovery-transcripts.md) extend that boundary to
paginated PR lookup and retained-reservation reconciliation without retry authority.
[Jira Data Center preparation](docs/jira-preview.md) adds explicit issue/project review
and snapshot-bound comment previews using saved responses, with live actions disabled.
[Jira comment recovery](docs/jira-comment-recovery.md) checks captured comment pages
against the exact preview and expected author without permitting automatic reposting.
[Retained Jira operations](docs/jira-journal.md) add durable staging, one-use reservation
tracking, integrity audits and recovery from saved scope across restarts.
[Jira recovery bundles](docs/jira-backups.md) add verified snapshots, stale-reservation
comparison and disposable recovery drills without replacing active journals.
[Operation journal succession](docs/journal-succession.md) retires a full GitHub or Jira
journal against such a snapshot and continues in a recorded successor that refuses a second
attempt for any task its chain already retains, deleting no record.
[Intent revision](docs/journal-revision.md) withdraws a prepared operation whose scope went
stale so its task can be prepared again under a new operation ID, while a consumed attempt
stays where it is and must be reconciled.
[Task ownership](docs/task-ownership.md) replaces the store-wide dispatcher lock with one
owner per task, so two workers can advance two different tasks at once and never the same
one, and a worker that dies leaves a recorded claim instead of a silently reusable task.
[Worker admission](docs/worker-admission.md) bounds how many tasks may be advancing at
once, refusing new work before anything is claimed rather than exhausting the host and
leaving an operation to reconcile.
[Agent sessions](docs/agent-sessions.md) let an agent in its own container hold a task by a
role-scoped lease it renews and releases, so agents that call in between thinking are
holders in the same registry as worker processes, and no agent can take work waiting on a
human.
[The loopback agent API](docs/agent-api.md) makes that enforced rather than cooperative: an
agent container holds a secret and a socket instead of the store, the agent it is comes from
the secret that signed the request, and the roles it may claim come from the service's own
registry.
[The live integration runbook](docs/live-integration-runbook.md) is the ordered path from a
token to a created issue on a machine that can reach GitHub and Jira: what to scope, what to
write, what proves each step, what to do when a dispatch comes back uncertain, and the tasks
that are genuinely still open.
[Outbound dispatch](docs/outbound-dispatch.md) also performs the read plans every codec
here prepares, which nothing could do before: a preview needed a person to fetch each URL by
hand and paste the JSON back. A fetch needs no approval because it changes nothing, and its
fence is that only GETs, only to the credential's origin, are performed. It is the one path that actually sends a
prepared request, and it lives on the broker because the credential does: an agent holds a
secret that proves which agent it is and grants nothing, while the broker under its own OS
account holds what can change the world. The credential names the origin it may go to, the
request must hash to what was approved, redirects are never followed, and refused, failed
and uncertain are three different answers.
[GitHub issues as intents](docs/github-issues.md) are now staged, approved and reserved in
the same journal as pull requests, with evidence that suits what is being approved — a
specification and a duplicate search rather than a patch, a test and a review — so the hash
the dispatcher accepts is reachable from the approval rather than asserted beside it. They add
the half of a project manager's
tracking work that was missing: an issue intent with a read plan, a repository pinned by
numeric ID, labels that must already exist, an operation marker that makes creation
idempotent and reconcilable, and a duplicate search that says what to ask rather than asking
it. Jira already modelled the ticket and the link; nothing modelled the issue.
[An agent doing the work](docs/agent-work.md) connects the two: a project manager container
can read, write and revise a specification and raise a clarification through the API it
authenticates to, and every one of those requires a live lease it holds on that task. Nothing
an agent can call answers a clarification or confirms a specification.
[The draft specification stage](docs/draft-spec.md) makes the project manager's step real:
a task can be opened with no specification, drafted and revised as append-only evidence,
stopped on questions only a human can answer, and moved on only when a person confirms the
exact specification they read, named by its hash. `create_task` used to invent a
specification and record its own confirmation of it.
[The agent protocol as plain context](docs/agent-protocol-context.md) gives those containers
the text that tells them how to work: one Markdown protocol published to a Claude-style skill
directory, a Copilot instructions layout or a vendored copy, with `skill-export --check` for
an image that carries one and tests that fail when the text and the code disagree.
[The consolidated Jira package](docs/jira-integration.md) adds Epic/Story/Task preparation,
relationships, transitions, GitHub associations and review/preflight diagnostics, with
synthetic examples and a complete offline acceptance scenario. Live activation remains gated.

Phase 1 remains the design baseline below. Phase 2 implements the bounded offline
vertical slice; see [running Phase 2](docs/phase2.md) for APIs, recovery and limitations.
Phase 3 adds [runtime hardening and operator recovery](docs/phase3.md), while keeping
the control plane and broker in Python. Real providers remain gated on containment tests.
The [broker service](docs/broker-service.md) lets that fixture broker run under a separate
OS account behind five HMAC-authenticated loopback routes, records the broker and
orchestrator identities with every execution, and adds a `broker_identity` deployment gate.
The Git JSON pilot's Docker workers dispatch through the same service when it is started
with `--image` and `--pilot-root`. Its shared secret rotates through a two-secret overlap
window without restarting either process.
Use the [runtime-readiness commands](docs/runtime-readiness.md) to check the intended
Docker host/image and record the remaining pre-provider gate.
The [JSON/CLI provider boundary](docs/provider-adapter.md) can now run the offline
workflow through a fixed subprocess fixture. Live provider execution remains disabled.
The [local management UI](docs/local-management.md) adds task browsing, explicit
approvals, evidence inspection and event replay at the existing loopback server.
[Operator sessions](docs/operator-sessions.md) add idle and absolute expiry, rotation,
revocation, safe reconnection and strict HTTP request validation.
[Evidence review](docs/evidence-review-ui.md) now lets operators explicitly select and
assess a review/policy pair, inspect blockers, save its selection and download a portable
bundle with a fresh server assessment and browser digest verification.
[Received bundle review](docs/received-bundle-review.md) verifies a handoff in an empty
workspace using its expected digest, disposable storage and downloadable diagnostics.
The [Copilot protocol codec and deployment guide](docs/copilot-protocol.md) provide
documented prompt/output preparation and read-only provider inventory without requiring
either provider CLI on this workstation.
Readiness now requires [five real Docker gates](docs/docker-resource-gates.md),
including resource exhaustion and failure cleanup.
Operators can [cancel a task between operations](docs/operator-cancellation.md)
through the UI, API or CLI while retaining its audit evidence.
Restore now [verifies a staging copy before publishing its destination](docs/verified-restore.md).
Expired pending requests support [fresh approval requests](docs/approval-renewal.md)
without granting a decision or starting execution.
Read-only [storage usage reports](docs/storage-usage.md) expose artifact quota headroom
and orphan cleanup candidates through the CLI and authenticated API.
Blocked tasks expose [recovery diagnostics](docs/recovery-diagnostics.md) in the UI
and API, explaining saved preconditions without probing or changing the runtime.
[Continuous validation](docs/continuous-validation.md) adds Windows/Linux offline
checks and a separate mandatory Linux Docker job for pull requests.
Backup creation now [verifies a staged snapshot before publication](docs/verified-backup.md),
matching the existing restore safeguard.
An [on-demand integrity audit](docs/integrity-audit.md) verifies stored artifacts and
event replay through the CLI or authenticated API without creating a backup.
Artifact cleanup supports an explicit [GC dry-run preview](docs/gc-dry-run.md) before
deleting eligible orphan files.

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m orch demo --trusted-fixture --data .runtime/demo
```

The demo explicitly represents both human approval calls and exercises
NEEDS_CHANGES -> reimplementation -> ACCEPT. `run()` itself always pauses for approvals.
`--trusted-fixture` runs only the bundled fixed programs locally and is **not a sandbox**.
The default executor requires Docker and a preloaded digest-pinned Python image.

Read in order:

1. [Architecture and repository layout](docs/architecture.md)
2. [Persisted workflow and recovery](docs/workflow.md)
3. [Contract rules and interfaces](docs/contracts.md)
4. [Threat model and security acceptance criteria](docs/security.md), checked against the
   tests that exercise them by [security acceptance](docs/security-acceptance.md)
5. [MVP milestones and testing strategy](docs/milestones.md)

Machine-readable contracts live in `contracts/v1/contracts.schema.json`; proposed
Python interfaces live in `contracts/interfaces.py`. All 19 requested contracts
are JSON Schema definitions, with explicit version fields and closed objects.
Validate with Python 3.11+:

```sh
python -m pip install -r requirements-dev.txt
python scripts/validate_contracts.py
```

The executable slice ends with a **simulated** PR. Actual GitHub/Jira brokers,
production actions, scheduling, retrieval, memory, and concurrent workers are deferred.
Opening the design PR itself is repository collaboration, not an implemented platform feature.

## Local repository pilot

[Repository JSON tasks](docs/repository-tasks.md) connect the pilot to durable task
approvals, retained result snapshots and common UI/API recovery controls. Completion
means human acceptance of a validated local snapshot.

Use the [bounded Git JSON pilot](docs/repository-pilot.md) to prepare, review and
test a small data change in a disposable workspace with durable recovery inspection.
Run `python scripts/pilot_acceptance.py` for its disposable end-to-end walkthrough.
[Pilot journal snapshots](docs/pilot-journal-backups.md) audit the retained scopes,
receipts and reclamation records and copy them into a verified bundle that cannot execute.
[Journal succession](docs/pilot-journal-succession.md) retires a settled journal against
such a snapshot and continues in a recorded successor, deleting no row.
[Reviewed pilot retention](docs/pilot-retention.md) reports journal and workspace usage
and reclaims a terminal pilot's disposable workspace after explicit review, retaining
every journal row, receipt and kernel snapshot.
[Operator visibility](docs/pilot-operator-visibility.md) adds the pilot usage report to
the local UI, the pilot's state to repository-task recovery diagnostics, and an audited
reason to every live reclamation.
The optional [Docker worker profile](docs/pilot-container-workers.md) uses separate
restricted edit/test containers with runtime-bound approvals and explicit recovery.
