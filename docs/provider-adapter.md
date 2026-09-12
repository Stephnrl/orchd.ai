# JSON/CLI provider boundary

The Python adapter now transports versioned role prompts to the bundled fixture CLI,
parses proposals, and returns AgentInvocationReceipt evidence. The orchestrator still
owns state transitions and both human approvals. This is an offline adapter milestone;
neither Copilot nor the corporate provider executable is launched.

```sh
python -m orch demo --fixture-provider cli --trusted-fixture --data .runtime/cli-demo
python -m orch demo --fixture-provider cli --image python@sha256:APPROVED_DIGEST --data .runtime/cli-docker
```

The first command uses fixed local worker programs and is not a sandbox. The second
uses the existing Docker worker restrictions. In both cases the provider subprocess
is the trusted bundled Python fixture, with an isolated interpreter, temporary working
directory and minimal environment. It has no implemented tool or network operations;
this launcher is not containment for an arbitrary executable or its descendants.

`provider-config-v1.schema.json` defines a closed configuration: canonical provider
name, model, absolute executable and SHA-256, fixed argument array, delivery mode,
timeout and prompt/output budgets. `stdin` is the default. `prompt_argument` substitutes
one whole `{prompt}` argument without invoking a shell; native OS command-line limits
still apply, and arguments may be visible to local process inspection. Credentials do
not belong in either prompt or arguments. Only the fixture transport is implemented.
The configuration is a Python API contract, not an enabled user-supplied executable
configuration route in the management API or CLI.

Role prompts include system instructions, a matching context manifest, verified artifact
references, and explicitly untrusted input data. Planner and reviewer output must match
the Phase 1 JSON schemas, current task, invocation, and source contract references.
Junior output remains limited to the existing fixture edit proposal. Duplicate JSON
keys, non-JSON numbers, unexpected shapes, mismatched references, recognized secrets,
nonzero exits, timeouts and output overflow produce failure receipts with no proposal.
Receipt output artifacts are redacted. Token usage and cost remain unknown (`null`).
Explicit cancellation kills and reaps the fixed fixture process. Cancelling the calling
coroutine also stops it but propagates cancellation rather than returning a receipt.

The engine admits exactly MockProvider and FixtureCliProvider. Both canonical live
names (`github_copilot_cli`, `abc_binary_ai_placeholder`) fail closed before launch,
even if supplied with the fixture transport. No authentication, native provider flags,
credential storage or live provider health claims are implemented.

Before enabling either real provider, independently review its exact executable/version,
tool-disable behavior, credential and SSO boundary, native output format, outbound
access, process-tree termination and supported host containment evidence. Then implement
its native protocol translation and admission policy. The generic JSON fixture protocol
does not imply that a provider accepts these prompts or emits these schemas natively.

Tests cover stdin and argument delivery, a complete revision workflow, malformed output,
task binding, secret redaction, budgets, binary mismatch, fixed-argument admission,
cancellation, live-provider denial and a child that never reads stdin. Docker evidence
is host-specific; Windows Docker Desktop success does not establish WSL/Linux-host
support or authorize live providers. Python remains sufficient for this milestone;
Rust can be considered for a future hostile-process supervisor after its requirements
and measurable constraints are established.

Validation on 2026-09-12: all 60 tests passed with no skips on Windows Docker Desktop,
including both real Linux-container gates. The CLI-provider/Docker-worker demo reached
COMPLETED with 59 events. Phase 1 validation passed 19 examples and 366 negative cases.
Fresh local runtime report `3b539c048e514b37add631d53b2f0d0e` passed with source digest
`c1b6264f3fe9b275424e704d901a0de59fbf7f92726559e60dabd80fbdbdf38d` and explicitly
reported `provider_authorized: false`. Machine-local evidence remains under ignored
`.runtime/`; rerun verification after source/environment changes or report expiration.
