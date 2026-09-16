# Roadmap progress — September 16, 2026

The [outbound dispatch milestone](outbound-dispatch.md) also performs the read plans this
project has been preparing since its first integration. Nothing could perform one before, so
every preview needed a person to fetch its URLs by hand; a fetch now does it with the same
credential, needs no approval because it changes nothing, and refuses anything that is not a
GET to that credential's origin. Plan, fetch, preview, dispatch is now one sequence, and the
tests walk it against a real origin. It adds the first path that sends
anything, and puts it on the broker. Mounting a token into a project manager's container was
the obvious shape and the wrong one: the container could use it for anything at any time
while the evidence named a single approved call, which leaves every approval above it
decorative. The broker holds the credential under its own OS account, the credential names
the origin it may go to, the request must hash to what was approved, redirects are never
followed, and refused, failed and uncertain stay three distinct answers with uncertain
returned as a receipt rather than raised. It is not yet wired into the task workflow: reaching
`PR_CREATED` still requires a simulated receipt, and admitting a real one there is a separate
deliberate step.

The [GitHub issue milestone](github-issues.md) adds the intent a project manager needs
to track work where the work lives. Jira already modelled Epics, Stories, Tasks and a link to
a GitHub issue by number; nothing modelled the issue, so the number had no origin. Issue
intents now have a read plan, a repository pinned by ID, labels that must already exist, an
operation marker that makes creation idempotent and an uncertain dispatch reconcilable, and a
duplicate search that produces the request to make rather than making it. It is a codec:
staging it in a journal with an approval, and assembling an epic with its stories and issue
as one reviewed bundle, are the next steps, and reaching a real GitHub still needs the
least-privilege token and destination-bound broker that remain gated.

The [draft specification milestone](draft-spec.md) closes a fail-open at the top of the
chain. `create_task` invented a specification, wrote `confirmed_by` and set `spec_confirmed`
in one transaction, so the evidence recorded a human confirmation nobody performed — and
every later gate takes the specification as given. A task can now be opened with no
specification at all, drafted and revised as append-only evidence, stopped on questions a
human must answer, and confirmed only by a person naming the sha256 of the words they read.
The fixture path is unchanged and still opt-out; what follows `SPEC_READY` is still the
greeting fixture, and routing drafting through the authenticated agent API is a separate step.

The [agent protocol context milestone](agent-protocol-context.md) publishes how to work
through this control plane as plain Markdown an agent container can be given. The tools that
run agents disagree about where such text lives, not about what it says, so there is one
protocol in two files — a short always-loaded core and a reference read on demand — and
`skill-export` places them for a Claude-style loader, for Copilot CLI or as a vendored copy,
with `--check` for an image that carries one. Tests compare every state, role, route, field
and lease bound in the text against the code it describes. The text explains the boundary and
is no part of it: approvals, roles and refusals are unchanged.

The [agent API milestone](agent-api.md) turns role-scoped claiming from a convention into a
boundary. An agent container held a lease only because it had the control plane installed
and the store mounted, so it could have claimed any role or written the evidence directly.
Agents now reach one loopback service through five authenticated fixed routes: the agent a
request comes from is the secret that signed it rather than a field in the body, and the
roles it may claim come from the service's registry rather than the caller. Secrets rotate
live as the broker's do, replies are signed with the secret that authenticated the request
and name the route and nonce they answer, and each request opens the store and closes it
again. The service decides which agent and which role; approvals, the guardian and the
broker still decide what that agent may do.

The [agent sessions milestone](agent-sessions.md) adds the holder an agent needs. Ownership
claims a task with a lock held by the process doing the work, which an agent calling in from
its own container never has, so a session is a role-scoped lease instead: granted to a named
agent, live until it expires, renewed while it works and released when its container stops.
A task may only be claimed by the role its next step belongs to, so no agent takes work that
is waiting on a human, and leases and worker locks exclude each other in one registry and
count against the same admission bound. Sessions are cooperative scheduling between local
agents, not an authorization boundary; approvals, the guardian and the broker still decide
what an agent may do.

The [worker admission milestone](worker-admission.md) bounds the concurrency ownership
made possible: at most four tasks may be advancing at once in one store, counted on live
claims under the store-wide lock, so two workers never both see the last free slot. Work is
refused before anything is claimed, a claim left by a crash frees its slot immediately, and
a worker never competes with itself. It also fixes a flaw in ownership that only a second
worker could reveal: registering a claim now waits briefly for the shared lock instead of
failing a millisecond race, so two workers starting together both proceed.

The [task ownership milestone](task-ownership.md) removes the single obstacle to more
than one worker: a store-wide dispatcher lock that let exactly one process advance anything
at a time. Task work now holds that task's own lock, so two workers can advance two
different tasks at once and never the same one, while a durable claim records who holds a
task and survives a crash that a kernel lock cannot. Whole-store maintenance still excludes
every task mutation, and takeover never repeats interrupted work: an unresolved operation
still forces explicit reconciliation. Scheduling, admission slots and conflict handling
remain unimplemented, and nothing here starts a second worker.

The [intent revision milestone](journal-revision.md) closes the other half of the same
contract: a task whose prepared scope went stale, because the base branch moved or the plan
changed, could never be prepared again. A prepared operation is now withdrawn against its
retained scope digest and an audited reason, freeing its task for a new operation ID, while
a consumed attempt is refused and must be reconciled. A unique index restricted to live
operations enforces one live operation per task in storage rather than in code, and the
rebuilt table is reached by a verified, atomic upgrade that reproduces every record.

The [operation journal succession milestone](journal-succession.md) closes the same dead
end for the GitHub and Jira operation journals, where the hazard is sharper: their records
hold one-use attempt reservations, so a hand-made replacement journal would hand a task a
second attempt for work whose first attempt may already have reached the remote. A full
journal now retires against a verified snapshot, deletes no record and produces the same
audit digest afterwards; only new admission stops, and the successor refuses any task or
operation identifier its chain already retains, failing closed when a predecessor cannot
be read.

The [journal succession milestone](pilot-journal-succession.md) closes the last documented
dead end in pilot retention: a full journal now retires against a verified snapshot and
continues in a recorded successor, with the link checked in both directions and the chain
walkable read-only. It deletes no row, changes no pilot and keeps the retired journal
inspectable, reconcilable and reclaimable; only preparation stops.

The [pilot journal snapshot milestone](pilot-journal-backups.md) gives the pilot journal
the audit and verified backup the GitHub and Jira journals already had: a
workspace-independent integrity audit, a two-file bundle bound by manifest digest,
verification that proves a snapshot refuses its own scopes, and a comparison that names
per-pilot drift. Workspaces are never copied and there is no restore.

The [security acceptance milestone](security-acceptance.md) makes the ten mandatory
criteria executable: each one names the tests that exercise it, the release lane runs
them, and the report states per criterion what local evidence establishes, what the
opt-in Docker gates add, which other stages cover the rest and what stays deferred to a
deployment host or an independent review. A renamed or deleted test now fails the lane.

The [broker secret rotation follow-up](broker-service.md) gives the broker's shared
secret the rotation the operator session already had: a two-secret overlap window, a
live re-read that needs no restart and drops no operation, replies signed with the
secret that authenticated them, `broker-rotate-secret` for both stages, and reported
age and rotation state that gate `deployment-check`. Age is reported, never enforced.

The [operator visibility follow-up](pilot-operator-visibility.md) completes the
retention milestone's deferred operator surface: `GET /pilot-usage` and a **Check pilot
usage** button in the store maintenance panel, the retained pilot's state and
reclamation eligibility inside repository-task recovery diagnostics, and a required,
audited `--reason` on live `pilot-reclaim`. Reclamation stays a reviewed CLI action.

The [broker service milestone](broker-service.md) adds `broker-serve`, a loopback HTTP
broker the operator can start under a separate OS account, an HMAC-authenticated
five-route transport for fixture edits/tests, broker-owned journals and Docker
reconciliation, per-execution identity evidence and a `broker_identity` deployment
gate. The [pilot workers now dispatch through that service](broker-service.md) as well,
with the broker account bound into the approved executor profile and every retained
observation. The subprocess and direct transports stay the defaults; the actual
second-account provisioning remains deployment work.

The [pilot retention milestone](pilot-retention.md) adds a read-only usage report and
explicitly reviewed, two-phase workspace reclamation for terminal pilots, including
those left `prepared` by kernel rejection or cancellation. Journal rows, receipts and
kernel snapshots are never deleted; a live cap of 128 unreclaimed pilots sits under an
absolute journal cap of 1024. No automatic reclamation, archive or restore is added.

The [repository-task kernel milestone](repository-tasks.md) connects the bounded
JSON pilot to shared task records/events, explicit execution and local-result
approvals, retained snapshots, authenticated controls and no-retry recovery.
It adds two distinct contracts (21 total). Fixture batches and external-action
evidence remain isolated; general code execution and live providers remain deferred.

The [pilot container-worker milestone](pilot-container-workers.md) adds separate
restricted Docker edit/test processes, image/daemon-bound approval, retained
dispatch phases and explicit interruption cleanup. The Docker CI lane includes
real pilot isolation/recovery acceptance. The task-kernel milestone above now
integrates this JSON workload; separate broker identity and live-provider admission remain.

The [local Git repository pilot](repository-pilot.md) adds a separate CLI path for
reviewed JSON replacements in a disposable workspace, exact baseline/scope fencing,
trusted JSON checks and durable no-retry recovery inspection. This goes beyond the
greeting fixture for a narrow data workload. The subsequent milestones add Docker
qualification and kernel admission without arbitrary repository tests or live providers.

Engineering estimate: **40–50% of the original 12-phase roadmap**, or **85–90% of
the bounded offline MVP**. These ranges describe implemented scope, not elapsed effort,
remaining calendar time, or production readiness. The phases differ greatly in size;
counting merged PRs or averaging phase counts would overstate progress. Advanced
orchestration needs a bounded definition before an overall completion date is useful.
The offline MVP's remaining acceptance work includes deployment-host verification,
independent security review and broader failure-path acceptance checks, rather than
an assumption that more UI features alone will make the system production-ready.

| Original phase | Current position |
| --- | --- |
| 1. Design/contracts/threat model | Implemented design baseline, 21 contracts and adversarial acceptance criteria |
| 2. Kernel and mocks | Implemented durable fixture workflow, approvals, replay and recovery |
| 3. Real AI adapters | Partial: fixture CLI transport and offline Copilot codec; live launch disabled; corporate protocol unknown |
| 4. Isolated Docker worker | Implemented for bounded fixture edits/tests, with five host-specific isolation gates |
| 5. Guardian/tool broker and policies | Implemented for the fixture workflow, with an optional separate-identity broker service transport; broader tools and external brokers remain |
| 6. Local management UI | Substantially implemented, including restart/replay, evidence, operator controls and reviewed serial batches |
| 7. GitHub integration | Offline preview, ref comparison, reconciliation classifier and durable intent/reservation journal; platform actions still simulated; no live PR creation |
| 8. Jira Data Center integration | Consolidated offline comments, Epic/Story/Task preparation, relationships, transitions, GitHub associations, durable journal, review/preflight and recovery; live deployment/admission remains blocked |
| 9. Credential broker/auth hardening | Local session expiry, rotation/revocation, stale-client clearing, strict HTTP parsing and broker shared-secret rotation implemented; production provider credentials/SSO remain |
| 10. OWASP/ACS hardening and adversarial tests | Substantial boundary tests, consolidated source-bound offline release verification and an executable criterion-to-test mapping; independent deployment review and broader live-system testing remain |
| 11. Concurrent workers | Implemented for bounded parallel work on one host: one owner per task, worker locks and agent leases in one registry, role-scoped claiming and an admission bound of four; scheduling, resource models and same-repository conflicts remain |
| 12. Advanced orchestration | Bounded serial fixture batches implemented; broader scheduling and orchestration remain to be scoped |

PRs opened while developing this repository are collaboration through development
tools, not evidence that orchd.ai itself has a GitHub integration. Deterministic
fixture success also does not establish compatibility with live model providers.

The operator-session milestone's full offline run completed 578 Python tests: 573 passed and five expected
Docker skips. This includes the consolidated Jira package and local operator session/
HTTP hardening tests. All 11 Chromium tests and 11 targeted UI scripts passed after the
final session response guard, including rotation, revocation, stale authentication,
bundle review and restart/replay. Desktop and mobile layouts were inspected without
horizontal overflow. The preceding Jira milestone also passed its synthetic end-to-end
acceptance scenario and 76 focused tests.
Those are dated local results, not a claim that every current host or hosted CI job has
passed. Docker evidence expires and is tied to source, host, daemon and image.

The release-verification milestone adds a single command for contract, Python,
synthetic Jira, discovered UI-script and Chromium acceptance, with fixed CI lanes.
Its retained report records the actual test counts and exact skips for each run;
read-only verification rejects changed source, altered reports, partial lanes and
expired results. It supplies unsigned local evidence, not production authorization.

## Next delivery sequence

Prioritize extending the bounded repository pilot into the kernel/broker with an
explicit workload and qualified test image before live-provider admission. The
pilot acceptance now runs in the consolidated offline/full release lane.

1. Make deployment gaps explicit with [deployment-check](deployment-readiness.md).
2. Obtain exact deployed provider version/protocol and independent containment/credential
   evidence. Implement and review the admitted live-provider path and conformance tests.
3. Implement a destination-bound GitHub broker with least-privilege credentials and
   uncertain-result reconciliation; then add Jira Data Center with its own review.
   [Operation journal succession](journal-succession.md) now removes the capacity dead end
   in both journals without weakening the one-attempt rule, so a live slice inherits a
   journal that can be continued rather than replaced.
   [Offline PR preparation](github-preview.md) now validates and hashes proposed scope;
   authenticated approval admission, remote verification and dispatch remain.
   [Portable evidence bundles](github-evidence-bundles.md) now support complete offline
   review handoffs with standalone verification of selected records and artifacts.
   [Journal-bound bundle review](github-bundle-approval.md) now prepares and reassesses
   previews directly from verified bundles, including exact file and tool-scope binding.
   [Combined offline preflight](github-preflight.md) adds scope-bound ref observations,
   freshness checks and a final journal/expiry recheck without authenticating remote state.
   [Read plans and transcripts](github-read-transcripts.md) now validate the offline
   request/response boundary before creating snapshots. A trusted live fetcher remains.
   [Recovery transcripts](github-recovery-transcripts.md) now cover bounded pagination
   and retained-reservation reconciliation, without authorizing retry or PR adoption.
   [Jira issue/comment preparation](jira-preview.md) starts the next integration's offline
   contract while live GitHub and Jira admission remain gated on separate reviews.
   [Jira comment recovery](jira-comment-recovery.md) now checks paginated captures against
   the exact preview and expected author, without confirming delivery or permitting retry.
   [Retained Jira operations](jira-journal.md) add immutable scope, one-use reservations,
   cross-process capacity enforcement and read-only integrity/recovery diagnostics.
   [Jira recovery bundles](jira-backups.md) now verify snapshots, compare reservation history
   and exercise recovery on disposable copies; actual restore admission remains deferred.
   [The consolidated Jira package](jira-integration.md) now covers the remaining bounded
   offline action set, legacy journal compatibility and expiring review/preflight checks.
   The executable deployment report keeps version conformance, credentials, trusted transport,
   independent containment, authenticated approval, verified workflow evidence and authoritative
   reconciliation/restore policy blocked. Completing the offline package does not close Phase 8.
4. Complete deployment/auth hardening and adversarial release checks before concurrency.
   The [broker service](broker-service.md) now supports a separate broker OS account with
   authenticated loopback routes and identity evidence; proving separation on a deployment
   host and moving pilot Docker workers behind it remain open.
   [Release verification](release-verification.md) now consolidates offline acceptance
   and source/lane/expiry checking, while retaining all external deployment gates.
   [Local operator sessions](operator-sessions.md) now have idle/absolute expiry,
   rotation/revocation, safe UI reconnection and strict HTTP parsing. Corporate identity,
   credential brokering and independent deployment review remain separate gates.
5. Define and implement bounded concurrent-worker and advanced-orchestration milestones.
   [Task ownership](task-ownership.md) and [worker admission](worker-admission.md) are the
   first two and are implemented: the dispatcher no longer serialises the whole store, and
   a bound decides how many tasks may advance at once. What remains is same-repository
   conflicts, budgets and scheduling. Both are single-host mechanisms; multi-host operation
   needs a different primitive and its own review.
   [Serial workflow batches](workflow-batches.md) now provide operator-reviewed task
   lists, snapshot fencing, durable reservations/checkpoints and explicit interruption
   review. They run existing fixture tasks serially and preserve every approval pause;
   this does not admit concurrent workers or live integrations. Their two-task walkthrough
   is part of the consolidated release package.
   The local UI supports draft assembly, explicit batch review/run, paginated inspection
   and snapshot-bound abandonment through authenticated endpoints, with stale-response
   and session-clearing checks. Individual task approvals remain separate.

Missing corporate documentation does not prevent offline contract and broker development,
but it does prevent a claim that the corporate provider is ready for use.
