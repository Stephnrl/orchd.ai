# Runtime readiness: the remaining pre-provider gate

Phase 3 is merged. Before a real provider is introduced, the Docker gates need to pass
on the host and pinned image that will execute its work. This increment packages that
check into reproducible commands and diagnostic evidence. It does not claim completion
of real-container validation on a workstation without Docker.

## Operator commands

From the repository root, with the existing Python dependencies installed:

```sh
python -m orch doctor
python -m orch doctor --image python@sha256:YOUR_APPROVED_DIGEST
python -m orch verify-runtime --image python@sha256:YOUR_APPROVED_DIGEST --destination .runtime/runtime-report.json
```

Use an approved **preloaded** Python 3.12+ image. The commands never install Docker,
enable virtualization, pull images, contact an AI provider or use corporate credentials.
Run them directly on each intended host: Windows with Docker Desktop Linux containers,
inside the intended WSL environment, or Linux Docker. The current default Docker context
must target a daemon able to mount that host's workspace. Ambient DOCKER_HOST overrides
are excluded to match the execution broker's environment; remote-host orchestration is
not supported by this increment.

`doctor` is read-only. It checks CLI availability, daemon identity, Linux container mode,
and local image identity. A reachable daemon without the required image is not ready.
`verify-runtime` runs both existing real-Docker tests in a bounded subprocess and records
their outcomes. Exit 0 means ready/passed for the respective command; exit 2 means
unavailable/failed. Missing arguments are usage errors. Existing report paths are not
overwritten; choose a new path for each verification run.

The preflight uses Docker's documented [system information](https://docs.docker.com/reference/cli/docker/system/info/)
and [image inspection](https://docs.docker.com/reference/cli/docker/image/inspect/)
commands with selected JSON fields. It avoids dumping full daemon configuration or
environment values into evidence.

## Evidence and fail-closed rules

The versioned report includes correlation ID, timestamps, hashed hostname and host
runtime metadata, daemon identity/version/architecture, requested image digest and local
image ID, source/contract/test digest, and exact test IDs/outcomes. Tests must both run
and pass. A skipped test, zero-test run, timeout, malformed result or environment change
cannot become a pass. Preflight is repeated after the tests to detect drift.

Reports expire after 24 hours. `require_current_report` rejects stale reports, changed
source/contracts, mismatched hosts/daemons/images and incomplete test results. It is a
trusted-local diagnostic helper for future provider admission; it is not an authentication
service, signed attestation or a new workflow approval. Report files must remain under
trusted local control. This hash does not attest installed third-party dependencies or
the operating system, and a local administrator can rewrite both code and evidence.

All reports explicitly carry `provider_authorized=false`. The Engine now rejects a
non-mock provider factory before opening storage or dispatching anything. A future
provider-adapter PR must deliberately replace that gate with provider-specific tool
containment, source-sharing policy and authentication checks in addition to current
runtime evidence. A passed Docker test alone will not authorize a provider.

## Current status and next action

Docker Desktop was installed and repaired after this readiness increment was merged.
On 2026-09-12, both real gates passed on Windows 11 using Docker Desktop Linux
containers. See [the validation record](docker-validation.md) for the exact image,
runtime and scope. Earlier unavailable/skipped results below describe the initial
implementation run, not the current host.

For another execution host, run `verify-runtime` there and retain its JSON report.
The next implementation step is one proposal-only provider adapter, with offline
conformance tests first and a separate review before any live provider invocation.
Python remains the control-plane/broker implementation; the JSON boundary stays suitable
for a future Rust broker if there is a concrete need.

Validation on Windows/Python 3.12, 2026-09-12: 52 tests discovered, 50 passed and two
real-Docker tests skipped. The eight readiness tests use explicit fixtures for daemon
and successful-check responses; they do not claim real Docker validation. The actual
doctor and verify-runtime CLI paths returned unavailable/exit 2 on this host, and the
actual machine runner returned exit 2 for skipped gates. Phase 1 validation passed
19 examples and 366 negative cases; Python compilation and whitespace checks passed.
