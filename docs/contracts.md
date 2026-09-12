# Contract semantics and interfaces

Canonical machine format: [Draft 2020-12 JSON Schema](../contracts/v1/contracts.schema.json).
All 19 top-level contracts require `schema_version`, `contract_type`, `id`, `task_id`
and UTC `created_at`. Closed objects reject extra fields. Embedded definitions inherit
the enclosing contract version. Producers validate before emission; consumers validate
before use. Unknown versions fail closed. Version changes are explicit migrations;
immutable historical records retain their original schema/version. No remote schema
resolution is permitted. The URN identifies this schema, not a network endpoint.

The compact [schema builder](../scripts/build_contracts.py) is the editable source;
commit regenerated schema/examples together. Examples exercise structural validation
only: placeholder hashes, IDs and artifacts are not an executable lifecycle or genuine
approval evidence. [Validation](../scripts/validate_contracts.py) checks their structure
and rejects representative invalid contracts. It does not implement domain guards.

| Contract | Producer | Consumer / semantic binding |
| --- | --- | --- |
| TaskSpec | Human / PM proposal | Lead; human confirmation event binds exact revision |
| ClarificationRequest | Lead clarifier | Human; spec revision and distinct invocation |
| ClarificationResponse | Authenticated human | Spec editor; all question IDs must match request |
| ImplementationPlan | Lead planner | Human approval and WorkOrder builder; spec/base/path/test scope |
| WorkOrder | Orchestrator | Junior/broker; exact approved plan, retry budget, workspace and deadline |
| AgentInvocation | Orchestrator | Provider; selected role, versioned instructions and artifact manifest |
| AgentInvocationReceipt | Provider broker | Orchestrator; timing/exit/output/result/usage/error |
| ToolRequest | Agent proposal or orchestrator | Guardian; actor derived from authenticated dispatch |
| ToolDecision | Guardian | Broker; request digest, policy, expiry and any replacement/approval request |
| PatchReceipt | Action broker | Test/reviewer; independent diff/files/snapshot evidence |
| TestRequest | Orchestrator | Runner; approved recipe and exact frozen patch/image |
| TestReceipt | Deterministic runner | Review/gates; exact requested and executed argv, output and exit |
| ReviewRequest | Orchestrator | Reviewer; spec/plan/patch/test references, separate invocations |
| ReviewDecision | Reviewer proposal | Orchestrator; explicit decision and findings, validated identity |
| ApprovalRequest | Orchestrator | Human UI; subject/evidence/destination/policy digest and expiry |
| ApprovalDecision | Authenticated human API | Engine; server stamps identity/time; cannot come from provider |
| ExternalActionReceipt | Action broker | Engine; operation/approval/destination, simulation and actual outcome |
| TaskEvent | Orchestrator | Audit/replay/UI; unique monotonic task sequence and transition evidence |
| TaskState | Orchestrator projection | UI/dispatcher; revision, current work and blocked resume state |

`DocumentRef` binds a record ID and canonical payload SHA-256. `ArtifactRef` binds immutable
retained bytes, size, producer and trust label. Labels are assigned by the artifact service,
not accepted from models. References must resolve within task ACLs and permitted role
context. `SecretRef` contains only broker/reference identifiers; it never resolves inside
a model or worker. No object may inline secret values or an arbitrary environment map.

`Command` names a trusted recipe, its digest, exact argv and bounded timeout. Its `cwd`
is deliberately limited in v1; actual resolved directory is recorded by the runner.
Recipe policy fixes executable, argument grammar and permitted files. JSON Schema path
checks are lexical only: broker must reject reserved/device paths, canonicalize within
workspace, reject escaping links and enforce OS isolation. Strings cannot authorize tools.

The engine must additionally enforce temporal order, hash equality, record existence,
same-task identity, monotonic sequence, valid transition, retries and approval consumption.
For example, `confirmed_by` is descriptive only; a human confirmation event is required.
Likewise a schema-valid `actor.role=human` is not authentication. Test `passed` requires
zero exit, no error, complete required receipts and runner provenance; exit zero alone
does not authorize acceptance. Null exit means no observed process exit (timeout/spawn
error); output references may point to explicit empty artifacts. Failed operations still
emit error receipts. Provider results refer to separately validated structured artifacts.

Patch receipts preserve requested versus actually executed commands and their outputs;
an in-process patch application uses empty command arrays and records the patch operation
itself in the event trail. A denied operation has a ToolDecision and no execution receipt.
Future tool families must add typed execution receipts before being enabled; they cannot
silently use provider prose as completion evidence.

Interface proposals are in [interfaces.py](../contracts/interfaces.py). The wrapper is
intentionally design-only; M1 must add a validating codec and strongly typed domain
models with conformance tests. No implementation should mistake the wrapper for enforced
validation. Broker execution accepts only a persisted operation ID/fencing token, never
an arbitrary incoming request accompanied by a model-produced allow decision.
