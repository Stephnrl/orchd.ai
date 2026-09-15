# Initial threat model and release gates

Assets: repository integrity, corporate credentials, source confidentiality, approved
scope, test/review evidence, audit history and host availability. Adversaries include
malicious request authors, repository/dependency maintainers, compromised providers and
malfunctioning workers. Trust boundaries are human/API, orchestrator/provider, broker/
worker, artifact ingestion and external services. Host administrators remain trusted.

The controls below are this project's proposed design, informed by the
[OWASP GenAI Security Project](https://genai.owasp.org/) and
[OWASP Agentic Applications Top 10](https://genai.owasp.org/download/52117/).
They are acceptance requirements, not claims of implemented compliance.

| Threat | Enforced mitigation | Required negative test |
| --- | --- | --- |
| Direct prompt injection / excessive agency | Role and capability intersection from trusted state; proposal-only providers | User text requests host command and approval bypass: no execution |
| Indirect injection in repo, Issue, Jira, docs, tool output | Provenance-labelled bounded artifacts; no conversion to system instructions | Malicious README asks for secret upload: denied and recorded |
| Arbitrary execution / tool misuse | Fixed recipes, argv grammar, shell=false, no general shell/interpreter escape; OS isolation | Shell metacharacters, option injection, git hooks and executable override denied |
| Privilege escalation / workspace escape | Non-root, fixed mounts, no socket, no-new-privileges, canonical paths and race-resistant access | Traversal, symlink swap, Windows junction, UNC/device path rejected |
| Secret exposure / exfiltration | Broker-only secrets, no worker egress, redaction before storage/UI | Canary token in stderr/artifact never appears in stored logs or UI |
| Context/cross-agent poisoning | Orchestrator-only transport; task ACLs and hash-bound context manifests | Another task's artifact and implementer transcript rejected for reviewer |
| Memory poisoning | No long-term model memory; immutable validated task records | Fake historical approval cannot become authoritative evidence |
| Rogue/malfunctioning agents | Deadlines, budgets, retries, strict structured output, fenced attempts | Infinite loop, output flood, invalid result and duplicate request terminate safely |
| Supply chain attack | Digest-pinned worker/tool images, dependency lockfiles, scanning and trusted test recipes | Changed image digest or test recipe blocks dispatch |
| Test/review fabrication | Runner-only receipts; immutable snapshot; separate reviewer identity | Model-authored passing TestReceipt and stale ACCEPT rejected |
| Human approval fatigue / trust exploitation | Specific diff/scope/expiry, no blanket grant, rejection reasons | Hidden destination change and replayed approval fail |
| Untrusted external content / UI injection | Escaped text, sanitized links, artifact authorization | Script in test output remains inert; cross-task URL inaccessible |
| Resource exhaustion / cascading failure | CPU/memory/PID/disk/output quotas; one active task; bounded retry | Fork bomb, huge patch and disk exhaustion produce bounded failure receipt |
| Audit tampering / duplicate actions | Append-only API, transactional events/outbox, idempotency/reconciliation | Crash after action before acknowledgement does not create duplicate PR |
| Local web attack | Session auth, CSRF/Origin/Host validation, loopback binding | Cross-origin approval and unauthenticated localhost request fail |

Recognized secret redaction covers configured secret values plus token/PAT/private-key/
authorization patterns, including common encoded forms. Redaction is defense in depth;
it cannot recognize every transformed secret. Prevent secrets reaching workers or model
context in the first place. Restrict raw artifacts, enforce retention, and block provider
dispatch when source classification disallows that destination. Never log full environment
variables. Allow only benign OS/architecture/tool/image metadata in receipts.

## Guardian and ACS boundary

Pre-action evaluation returns allow, deny, modify, or require_human_approval. Failure,
timeout, unknown tool/policy/version, missing evidence or malformed request is deny.
Post-action ingestion records actual execution/result, verifies constraints and blocks
progress on violation. A post-action hook cannot undo a side effect.

Internal ToolRequest/ToolDecision and receipt interfaces are independent of wire format.
An optional `ActionHookAdapter` maps pre-action requests to ACS-style `toolCallRequest`
and actual receipts to `toolCallResult`, with operation/task correlation and minimized
metadata. Consult and pin the exact [ACS specification revision](https://github.com/GenAI-Security-Project/agent-control-standard)
before implementing wire compatibility; unsupported semantics fail closed. No ACS
compliance claim or runtime dependency is made in this baseline.

## Mandatory security acceptance criteria

- SEC-01: All workflow transitions and approval consumption are server-enforced; stale,
  forged, expired, cross-task and replayed approvals fail without execution.
- SEC-02: Models never author trusted command/test receipts; commands and actual argv,
  exit status, timing and bounded output artifacts are independently recorded.
- SEC-03: Workers cannot access host files beyond assigned mounts, Docker, credentials,
  orchestrator database, other tasks or network; adversarial container tests prove it.
- SEC-04: Every write is within approved spec/plan/base/path/budget scope; changed evidence
  invalidates downstream tests/review/action approval.
- SEC-05: Plan approval precedes write-capable execution. Destructive, privilege, secret,
  infrastructure, production, external communication and PR actions require specific human
  approval; unsupported operations remain denied even if a human approves them.
- SEC-06: Redaction, UI escaping, task artifact ACLs and output/resource limits pass
  adversarial tests. No credentials in prompts, images, repository or workflow values.
- SEC-07: Audit replay and restore reconstruct complete lifecycle; crash injection across
  every dispatch/receipt boundary cannot silently repeat or lose external effects.
- SEC-08: Reviewer is a separate invocation with only approved evidence; ACCEPT never
  overrides deterministic failures. Both REJECT and NEEDS_CHANGES exercise retry routes.
- SEC-09: Provider tool-disable capability is verified before host CLI use. Unverified
  corporate SSO/tool behavior blocks real provider enablement; fixtures can validate flow.
- SEC-10: Dependency/image digests and scan results are recorded before execution;
  network-disabled tests run from a trusted recipe against the reviewed snapshot.

[Security acceptance](security-acceptance.md) binds each criterion above to the exact
tests that exercise it, runs them, and reports what that evidence does and does not
establish. It runs in the offline and full release lanes, so a renamed or deleted test
is a failure rather than a silent loss of coverage. It is local evidence about this
source, not an independent review.

Residual risk: containers share a kernel; provider output can still be incorrect; secret
recognition is imperfect; a local privileged user can rewrite the audit database. Stronger
VM isolation, off-host audit signing and corporate identity integration are later decisions.

The implemented [local operator session boundary](operator-sessions.md) now enforces
idle/absolute expiry, immediate rotation and revocation, in-memory token handling,
stale-client UI clearing and strict authenticated HTTP framing/JSON parsing. These
controls are covered by clock-boundary, concurrency, raw HTTP and browser tests.
They do not establish production identity or credential-broker readiness.

The [broker service](broker-service.md) implements the architecture's "separate OS
identity; narrowly authenticated local API" for the fixture broker: HMAC-authenticated
loopback routes verified before parsing, a broker-owned journal and execution profile,
and per-execution identity evidence. It is covered by unauthenticated, tampered,
oversized, cross-origin, profile-mismatch and crash-injection tests. Whether the two
accounts are actually separate is host provisioning that `broker-check` reports; it is
not established by this repository's own acceptance runs.
