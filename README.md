# orchd.ai

An internal engineering workflow proof with persisted state, explicit approvals,
deterministic mock actors, real fixture tests, and simulated PR receipts.

See the [roadmap progress assessment](docs/progress.md) for implemented scope and
remaining integrations, and [deployment readiness](docs/deployment-readiness.md) for
an executable report of the gates still blocking live providers.
The [offline GitHub request preview](docs/github-preview.md) begins external broker
preparation with explicit destination checks and a digest of the proposed scope.
[Portable evidence bundles](docs/github-evidence-bundles.md) package an explicitly
selected workflow chain for bounded offline verification without the original store.
[Journal-bound bundle review](docs/github-bundle-approval.md) prepares and rechecks an
approval preview against that exact bundle and its intended GitHub operation scope.
[Combined offline preflight](docs/github-preflight.md) adds scope-bound repository/ref
observations, a fixed freshness window and a final journal/expiry recheck.

Phase 1 remains the design baseline below. Phase 2 implements the bounded offline
vertical slice; see [running Phase 2](docs/phase2.md) for APIs, recovery and limitations.
Phase 3 adds [runtime hardening and operator recovery](docs/phase3.md), while keeping
the control plane and broker in Python. Real providers remain gated on containment tests.
Use the [runtime-readiness commands](docs/runtime-readiness.md) to check the intended
Docker host/image and record the remaining pre-provider gate.
The [JSON/CLI provider boundary](docs/provider-adapter.md) can now run the offline
workflow through a fixed subprocess fixture. Live provider execution remains disabled.
The [local management UI](docs/local-management.md) adds task browsing, explicit
approvals, evidence inspection and event replay at the existing loopback server.
[Evidence review](docs/evidence-review-ui.md) now lets operators explicitly select and
assess a review/policy pair, inspect blockers and save a selection for bundle export.
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
4. [Threat model and security acceptance criteria](docs/security.md)
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
