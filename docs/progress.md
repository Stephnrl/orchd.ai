# Roadmap progress — September 14, 2026

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
| 1. Design/contracts/threat model | Implemented design baseline, 19 contracts and adversarial acceptance criteria |
| 2. Kernel and mocks | Implemented durable fixture workflow, approvals, replay and recovery |
| 3. Real AI adapters | Partial: fixture CLI transport and offline Copilot codec; live launch disabled; corporate protocol unknown |
| 4. Isolated Docker worker | Implemented for bounded fixture edits/tests, with five host-specific isolation gates |
| 5. Guardian/tool broker and policies | Implemented for the fixture workflow; broader tools and external brokers remain |
| 6. Local management UI | Substantially implemented, including restart/replay, evidence and operator controls |
| 7. GitHub integration | Offline preview, ref comparison, reconciliation classifier and durable intent/reservation journal; platform actions still simulated; no live PR creation |
| 8. Jira Data Center integration | Consolidated offline comments, Epic/Story/Task preparation, relationships, transitions, GitHub associations, durable journal, review/preflight and recovery; live deployment/admission remains blocked |
| 9. Credential broker/auth hardening | Local session expiry, rotation/revocation, stale-client clearing and strict HTTP parsing implemented; production provider credentials/SSO remain |
| 10. OWASP/ACS hardening and adversarial tests | Substantial boundary tests; independent deployment review and broader live-system testing remain |
| 11. Concurrent workers | Deferred; current execution is deliberately bounded |
| 12. Advanced orchestration | Deferred; needs explicit scope |

PRs opened while developing this repository are collaboration through development
tools, not evidence that orchd.ai itself has a GitHub integration. Deterministic
fixture success also does not establish compatibility with live model providers.

The latest full offline run completed 578 Python tests: 573 passed and five expected
Docker skips. This includes the consolidated Jira package and local operator session/
HTTP hardening tests. All 11 Chromium tests and 11 targeted UI scripts passed after the
final session response guard, including rotation, revocation, stale authentication,
bundle review and restart/replay. Desktop and mobile layouts were inspected without
horizontal overflow. The preceding Jira milestone also passed its synthetic end-to-end
acceptance scenario and 76 focused tests.
Those are dated local results, not a claim that every current host or hosted CI job has
passed. Docker evidence expires and is tied to source, host, daemon and image.

## Next delivery sequence

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
   [Local operator sessions](operator-sessions.md) now have idle/absolute expiry,
   rotation/revocation, safe UI reconnection and strict HTTP parsing. Corporate identity,
   credential brokering and independent deployment review remain separate gates.
5. Define and implement bounded concurrent-worker and advanced-orchestration milestones.

Missing corporate documentation does not prevent offline contract and broker development,
but it does prevent a claim that the corporate provider is ready for use.
