# Docker Desktop validation — 2026-09-12

The real readiness command passed on Windows 11 AMD64 with Python 3.12.14 and Docker
Desktop's Linux engine, Docker Server 29.7.2, x86_64.

The official `python:3.12-slim` image was downloaded explicitly during workstation
setup. Verification then used its resolved immutable reference:

```text
python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
```

```sh
python -m orch verify-runtime --image python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea --destination .runtime/docker-verification-1.json
```

Result: `status=passed`, exit 0, both required tests passed, no failures, errors or
skips. The gate exercised non-root execution, effective capabilities, no-new-privileges,
read-only root and test workspace, socket absence, denied egress, container cleanup,
and the full approval-driven fixture workflow in real containers.

The complete regression suite was then executed with `ORCH_DOCKER_TEST_IMAGE` set to
the same pinned reference: **52 tests passed, zero skipped**. A subsequent Docker
listing filtered by `ai.orchd.operation` returned no leftover task containers.

The local machine-readable report has verification ID
`4b3a891492b24c85a29a178b1d75ee4f` and source digest
`b67660d30f244de3a3644fd50194ceb9a8a8641fc9f23e2a3d161b06f4c50c4d`.
It was completed at `2026-09-12T14:44:03.946056Z` and expires 24 hours later. This
document is a historical validation record, not reusable authorization after the
report expires or the runtime changes. Re-run verification before relying on it.

This result covers Windows with Docker Desktop Linux containers. It does not claim
independent WSL-shell or Linux-server validation, comprehensive kernel-escape testing,
or provider authentication/tool containment. `provider_authorized` remains false.

Next: implement one constrained proposal-only provider adapter with offline conformance
tests, then separately review its tool-disable behavior, source-sharing policy and
authentication before any live invocation. Keep the engine's real-provider gate closed
until those conditions and a current runtime report are satisfied.
