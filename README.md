# orchd.ai

Design baseline for an internal, container-isolated engineering orchestration platform.
This revision defines contracts and implementation gates; it is not a working agent runtime.

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

The first proposed slice ends with a **simulated** PR. Actual GitHub/Jira brokers,
production actions, scheduling, retrieval, memory, and concurrent workers are deferred.
Opening the design PR itself is repository collaboration, not an implemented platform feature.
