# Docker resource-containment gates

The mandatory runtime checks now exercise memory exhaustion, PID exhaustion, tmpfs
capacity, oversized stdout and timeout cleanup on the selected Docker host/image.
These extend the existing filesystem/network isolation and full-workflow tests.

Worker containers explicitly use `--memory=256m --memory-swap=256m`, which prevents
additional swap use under Docker's documented [memory/swap semantics](https://docs.docker.com/engine/containers/resource_constraints/).
They also select `--log-driver=none`: bounded stdout/stderr still flow to the broker's
redacted receipt artifacts, while Docker does not retain an additional container-log
copy. Consequently `docker logs` is not an evidence source for these workers. This
is a per-container setting; the daemon's global logging configuration is not changed.
See [Docker logging drivers](https://docs.docker.com/engine/logging/configure/).

| Mandatory test | Evidence |
| --- | --- |
| `test_linux_isolation_and_cleanup` | Non-root, zero effective capabilities, no-new-privileges, read-only root/test workspace, no socket and denied egress |
| `test_full_docker_workflow` | Real edit/test workflow completes through approvals and removes its workspaces |
| `test_resource_limits` | Effective cgroup memory/swap/PID/CPU settings; tmpfs write reaches ENOSPC; bounded child creation reaches EAGAIN |
| `test_memory_limit_and_cleanup` | Allocation is killed with exit 137 and Docker reports OOMKilled; exact labelled container is removed |
| `test_timeout_and_output_cleanup` | Production executor returns timeout/output-limit failures and proves the corresponding containers absent |

Memory/PID stress starts only after checking effective cgroup ceilings. Allocations
are capped at a requested 384 MiB against the verified 256 MiB container limit; child
creation is bounded at 70 attempts against a verified limit of 64 PIDs. The child
program is the image's `sleep` utility with a finite lifetime. The tmpfs probe checks
its size before attempting at most 20 MiB of writes against the 16 MiB mount.
Flood output is finite (4 MiB) and receipt capture stays bounded at 64 KiB per stream.
These programs are test-only substitutions; the production executor still accepts
only its two fixed recipes.

The cgroup v2 layout and a conventional cgroup v1 layout are recognized. Unsupported
layouts, missing controllers/utilities or unenforced limits fail the gate rather
than being skipped as success. CPU validation reads the effective quota; it is not
a scheduler-throughput or sustained CPU-throttling benchmark. This is not kernel
escape certification, full disk-I/O isolation or proof for an untested host.

The flood probe exposed an auto-removal race: a container can disappear between
list and inspect, or between inspect and remove. Reconciliation now requires a fresh,
successful exact-name listing showing absence before accepting either race as clean.
Daemon errors and still-present containers remain uncertain. Label mismatches never
authorize removal. A truncated-output marker is preserved even if cleanup becomes
uncertain, so the receipt does not misrepresent partial output as complete.

Run the expanded verification with a new report path:

```sh
python -m orch verify-runtime --image python@sha256:APPROVED_DIGEST --destination .runtime/resource-report.json
```

No image is pulled automatically. Every one of the five named tests must pass with
zero skips/errors; earlier two-test reports are rejected. Reports remain host/image/
source-bound and expire after 24 hours. Restart running services to load code changes.
Passing these worker gates does not authorize a live provider or external action.
The Copilot/corporate deployment requirements remain in [the provider guide](copilot-protocol.md).

Validation on 2026-09-12: all 76 tests passed with zero skips on Windows 11, Python
3.12.14 and Docker Desktop's Linux engine 29.7.2. The image was
`python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`.
Fresh verification `baf2342b58aa4e88babc00ec88c2d873` passed all five gates with source
digest `16f8b71ffe3cf90e23feee8d53624f23aa61d3ddc4b496d9be825b4f9ddc6a20`.
Contract validation passed 19 examples and 366 negative cases; compilation and
whitespace checks passed. WSL-host and standalone Linux-host runs remain unverified.
The machine-local JSON report is under ignored `.runtime/` and continues to report
`provider_authorized: false`.
