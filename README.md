# orchd.ai

An internal engineering workflow proof with persisted state, explicit approvals,
deterministic mock actors, real fixture tests, and simulated PR receipts.

Phase 1 remains the design baseline below. Phase 2 implements the bounded offline
vertical slice; see [running Phase 2](docs/phase2.md) for APIs, recovery and limitations.
Phase 3 adds [runtime hardening and operator recovery](docs/phase3.md), while keeping
the control plane and broker in Python. Real providers remain gated on containment tests.
Use the [runtime-readiness commands](docs/runtime-readiness.md) to check the intended
Docker host/image and record the remaining pre-provider gate.

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
