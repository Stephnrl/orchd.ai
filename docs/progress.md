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
| 8. Jira Data Center integration | Design only |
| 9. Credential broker/auth hardening | Local session boundary implemented; production provider credentials/SSO remain |
| 10. OWASP/ACS hardening and adversarial tests | Substantial boundary tests; independent deployment review and broader live-system testing remain |
| 11. Concurrent workers | Deferred; current execution is deliberately bounded |
| 12. Advanced orchestration | Deferred; needs explicit scope |

PRs opened while developing this repository are collaboration through development
tools, not evidence that orchd.ai itself has a GitHub integration. Deterministic
fixture success also does not establish compatibility with live model providers.

The latest full offline run completed 232 Python tests: 227 passed and five expected
Docker skips. PR #37 also passed seven Chromium tests and seven targeted UI scripts.
Those are dated local results, not a claim that every current host or hosted CI job has
passed. Docker evidence expires and is tied to source, host, daemon and image.

## Next delivery sequence

1. Make deployment gaps explicit with [deployment-check](deployment-readiness.md).
2. Obtain exact deployed provider version/protocol and independent containment/credential
   evidence. Implement and review the admitted live-provider path and conformance tests.
3. Implement a destination-bound GitHub broker with least-privilege credentials and
   uncertain-result reconciliation; then add Jira Data Center with its own review.
   [Offline PR preparation](github-preview.md) now validates and hashes proposed scope;
   workflow approval binding, remote verification and dispatch remain.
4. Complete deployment/auth hardening and adversarial release checks before concurrency.
5. Define and implement bounded concurrent-worker and advanced-orchestration milestones.

Missing corporate documentation does not prevent offline contract and broker development,
but it does prevent a claim that the corporate provider is ready for use.
