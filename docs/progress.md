# Roadmap progress — September 15, 2026

The [broker service milestone](broker-service.md) adds `broker-serve`, a loopback HTTP
broker the operator can start under a separate OS account, an HMAC-authenticated
five-route transport for fixture edits/tests, broker-owned journals and Docker
reconciliation, per-execution identity evidence and a `broker_identity` deployment
gate. The subprocess transport stays the default; the pilot's Docker workers and the
actual second-account provisioning remain deployment work.

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
| 9. Credential broker/auth hardening | Local session expiry, rotation/revocation, stale-client clearing and strict HTTP parsing implemented; production provider credentials/SSO remain |
| 10. OWASP/ACS hardening and adversarial tests | Substantial boundary tests and consolidated source-bound offline release verification; independent deployment review and broader live-system testing remain |
| 11. Concurrent workers | Deferred; current execution is deliberately bounded |
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
