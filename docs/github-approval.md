# GitHub-specific approval scope preparation

The fixture workflow approval does not bind the standalone GitHub request. This offline
preview prepares a separate scope for future human approval without issuing a decision,
persisting a request, reserving an attempt or enabling dispatch.

```sh
python -m orch github-approval-preview --journal .runtime/github-journal.sqlite --operation-id bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --expected-sha256 <scope-digest> --evidence evidence.json
```

Use the immutable scope digest returned by journal staging or inspection. The journal
must have its supported schema, and the selected operation must still be `prepared`.
Those checks and the record read share one read transaction. The CLI opens the journal
read-only and does not create a missing journal.

The evidence file is a closed object containing `task_id`, `patch_sha256`, `test_sha256`,
`review_sha256` and `policy_sha256`. Task identity must match the journal. Every digest
must be exactly 64 lowercase hexadecimal characters. The file loader rejects duplicate
keys, non-JSON numbers and inputs above 64 KiB. These are caller-supplied digest claims:
the command does not retrieve artifacts, validate test/review outcomes or authenticate policy.

The `GitHubApprovalPreview` binding includes a new request ID, task/operation IDs, the
complete retained GitHub scope and its digest, the evidence envelope, issue time and a
15-minute expiry. Its canonical digest covers every binding field, including repository
identity, request content and commit expectations through the retained scope. Repeated
previews have distinct request IDs and do not supersede or consume anything.

Exit 0 means a preview was prepared; invalid inputs exit 2 without a preview. Results
always carry `evidence_verified: false`, `live_authorized: false` and `retry_allowed: false`.
The expiry is part of the proposed scope, not an enforced authorization: no component
accepts this preview as a decision or capability. The local clock is not trusted attestation.

## Checking a saved preview

```sh
python -m orch github-check-approval --journal .runtime/github-journal.sqlite --approval-preview approval.json --expected-sha256 <preview-digest> --evidence evidence.json
```

Supply the saved preview's outer digest, not its journal scope digest. Both files use
the bounded strict JSON loader. The checker reconstructs the closed preview structure,
including scope, IDs, evidence, false authority flags and exactly 15 minutes of validity.
It then compares the saved scope and supplied current evidence claims to a read-only
journal snapshot. The expected digest identifies the selected preview; it is not a signature.

The assessment is `current` (exit 0) only when the local clock is at or after issue time
and strictly before expiry, the journal remains prepared with the same scope, and the
evidence envelope matches. Fixed blockers `not_yet_valid`, `expired`,
`journal_not_prepared`, `scope_mismatch` and `evidence_mismatch` produce `blocked`
(exit 2). Malformed previews, unknown operations and invalid journals also exit 2 without
an assessment. The report binds the preview digest, current scope/state, supplied evidence
digest, check time and blockers without echoing proposal text.

This enforces the preview window for this diagnostic check only. A `current` result
still has unverified evidence and no approval, retry or live authority; the journal may
change after the snapshot. The check neither persists nor consumes requests or decisions.

## Checking local artifact bytes

```sh
python -m orch github-check-evidence --data .runtime/phase2 --evidence evidence.json
```

This opens an existing version-2 workflow store read-only. Each patch/test/review/policy
digest must identify an artifact registered to the envelope's task. The checker validates
canonical artifact metadata against `ArtifactRef`, checks its row identity, and reads the
content-addressed file with a 1 MiB limit per role. Recorded size and SHA-256 must match
the bytes. Artifact-directory and file symlinks are rejected. Metadata queries share a
read transaction; artifact files are checked sequentially and could change afterward.

The result is `bytes_match` (exit 0) with `artifact_bytes_verified: true`; invalid or missing
evidence exits 2 without a report. It binds the evidence-envelope digest and each role's
artifact ID, digest and size, without returning contents or paths. If identical content has
multiple registrations for the same task, the lowest artifact ID is selected deterministically.
No missing directories, artifacts or workflow records are created.

These digests refer to stored artifact bytes, not automatically to contract-record hashes.
The policy artifact must also be registered to the task; the command does not invent one
from `fixture-v1`. Matching bytes do not establish that a file is a patch, tests passed,
a reviewer accepted the change, or a policy is trusted. Task ownership relies on the local
store's records, not independent provenance. `evidence_verified`, `live_authorized` and
`retry_allowed` remain false, and saved approval checks do not consume this report yet.

## Combined local assessment

```sh
python -m orch github-assess-approval --data .runtime/phase2 --journal .runtime/github-journal.sqlite --approval-preview approval.json --expected-sha256 <preview-digest> --evidence evidence.json
```

This command checks the selected saved preview, verifies task-owned artifact bytes and
the stored workflow claims below only if the preview is current, then rechecks expiry
and journal scope/state after artifact I/O. Policy validity is also compared with the
final approval-check time, so expiry during verification blocks the result.
Both databases are opened read-only. The report binds the complete fresh approval and
artifact assessments; it does not accept previously saved assessments as proof.

`current` exits 0. A blocked preview exits 2 with its blockers; when blocked before file
checks, `artifact_evidence` and `evidence_claims` are null and `artifact_bytes_verified`
is false. If expiry or
reservation changes during file checks, the result is blocked even though verified byte
evidence is included. Inconsistent claims also block even when the preview is current.
The binding includes the complete claims assessment and a sorted, deduplicated blocker
list prefixed with `approval:` or `evidence:`. Invalid inputs or artifact failures exit 2
without a partial report. Existing callers must now supply canonical registered contracts;
matching arbitrary JSON is no longer sufficient for this combined command.

The recheck detects changes during the artifact stage, but it is not an atomic snapshot
across databases and files or a lock against changes afterward. A current result still
has `evidence_verified`, `live_authorized` and `retry_allowed` false. Semantic evidence,
policy trust and authenticated approval admission remain separate requirements; no
requests or decisions are persisted or consumed.

## Binding the policy tool request to the journal proposal

The combined assessment additionally resolves the policy's stored `ToolRequest` and
checks its operation against the saved preview. `request_sha256` must equal the canonical
digest of the request with that field omitted. The tool must be `github` with no command,
target paths, secret references, work order or workspace. The payload must be a task-owned
artifact whose metadata, size and bytes pass the bounded artifact checks.

Payload bytes must exactly equal canonical JSON with these two fields:

```json
{"adapter":"github-draft-preview","scope":"<the complete retained scope object>"}
```

The scope value above denotes the actual object, not a string. It includes the numeric
repository identity and complete prepared preview. Extra fields, formatting changes,
the `simulated-github` adapter or any changed request scope cannot match. The existing
fixture engine does not generate this new payload or authorize live use.

The report includes a `tool_scope` assessment, payload digest and fixed `tool:` blockers.
Invalid or unowned payload artifacts fail without an assessment. The final approval and
policy-time checks run after payload verification. This verifies a local request binding;
it does not authenticate the policy or approver, bind a human decision, or grant credentials.

## Assessing workflow evidence claims

To inspect stored records before receipt artifacts have been exported, use:

```sh
python -m orch github-list-evidence-records --data .runtime/phase2 --task TASK_ID
```

This read-only catalog lists patch, test, review and policy candidates for the exact
task, ordered by contract kind and record ID. Each entry contains its role, canonical
document reference and creation time; record contents are omitted. Multiple candidates
are retained rather than selecting the latest. Unknown tasks, malformed/noncanonical
records, records over 1 MiB or more than 200 candidates fail without a partial catalog.
The catalog hash binds the complete metadata list. It does not inspect dependencies or
artifacts, assess outcomes, export records or authorize execution. Use the intended
record references in the selection file below.

You can resolve the patch/test pair from an explicitly chosen review instead of copying
all four references manually. Create a UTF-8 `anchors.json` containing only `task_id`,
`review` and `policy`, using exact `{id, sha256}` references from the catalog, then run:

```sh
python -m orch github-resolve-evidence-selection --data .runtime/phase2 --records anchors.json
```

Save the JSON stdout as UTF-8 `records.json`. The resolver validates the chosen records,
follows the review request's patch and single test reference, and requires that test to
name the same patch. It never chooses a newer review or a policy automatically. Missing,
corrupt, foreign-task or conflicting records and reviews with multiple test references
fail without partial output. The resolver is read-only and deliberately does not assess
outcomes, policy compatibility or artifacts; a rejected review can still be selected.
Run the following assessment to evaluate the selected evidence.

```sh
python -m orch github-check-record-claims --data .runtime/phase2 --records records.json
```

The selection file is a closed object with `task_id` (32 lowercase hex characters)
and `patch`, `test`, `review`, `policy` document references. Each reference contains
the exact stored record `id` and canonical record `sha256`, as used by the workflow's
document references. Select the intended attempt explicitly; this command never picks
the latest record. It resolves the four records by task, kind and hash in one read
transaction and runs the same chain, supporting-artifact and broker checks described
below. It does not require copies of the four primary records as artifact files.

The `GitHubRecordClaimsAssessment` binds the selection and claims, returning
`claims_consistent`/exit 0 or `blocked`/exit 2. Invalid selections or missing/corrupt
dependencies exit 2 without a report. No artifacts are exported and no workflow or
network action runs. This report is not an artifact-evidence envelope and cannot replace
`--evidence` in combined approval assessment. The current fixture workflow may still
report consistency blockers; this command exposes them without authorizing execution.

```sh
python -m orch github-check-evidence-claims --data .runtime/phase2 --evidence evidence.json
```

This stricter mode performs the byte checks above and parses those same verified bytes
as canonical `PatchReceipt`, `TestReceipt`, `ReviewDecision` and `ToolDecision` contracts.
Each must exactly match a schema-valid stored record for the same task. Linked
`WorkOrder`, `TestRequest`, `ReviewRequest` and `ToolRequest` records are resolved by task and document hash with
bounded reads, within the artifact metadata read transaction. Ordinary log files and
arbitrary JSON cannot satisfy these role requirements.

The assessment checks the test's patch reference and snapshot, a passed/untruncated
result, requested versus executed argv, successful reported patch commands, a clean
ACCEPT review, and the review request's exact patch and single test-receipt reference.
Reviewer invocation must match and differ from the implementer. Policy must currently
require human approval, with no effective-request override, and match the GitHub tool
request's operation and policy version. Fixed blockers describe mismatches without
returning document contents. `claims_consistent` exits 0, `blocked` exits 2; invalid,
missing or corrupt records exit 2 without a report.

Both the standalone claims check and combined assessment also verify the patch diff,
test stdout/stderr and each reported patch command's stdout/stderr. Every reference
must exactly match task-owned artifact metadata and bounded file bytes. The report
binds role, artifact ID, digest and byte count without returning supporting contents.
Supporting references are limited to 32 entries, 1 MiB each and 8 MiB total; repeated
references count separately. Count and declared byte budgets are checked before reading
supporting files. Missing, altered or foreign-task support fails without a report.
Artifact directories and individual artifact paths must not be symbolic links or
Windows junctions. This gate applies to primary evidence, supporting files and the
combined assessment's tool payload. It is a path check, not an atomic filesystem
snapshot: concurrent replacement of local evidence files remains outside this guarantee.

Receipt chronology is also checked. Patch and test execution must start no later than
they end, and end no later than their receipt creation. The patch receipt must exist
before test execution starts. Test receipt, review request, review decision, GitHub tool
request and policy decision must then occur in that order. Equal instants are permitted
for coarse clocks. Every reported patch command must fit within the patch execution
window, and none of the six records may claim creation after the assessment time.
Timeline violations produce fixed blockers and exit 2, including when artifact bytes,
preview scope and policy expiry otherwise pass. These checks use claimed timestamps;
they do not authenticate clocks or producers, impose an evidence age limit, or establish
the chronology of unresolved dependencies such as planner invocations.

The test receipt must resolve to a canonical stored `TestRequest` for the same task.
Its operation must match the receipt, its patch must match the assessed patch, its
snapshot must match the receipt, and its work-order reference must match the patch's.
The entire command contract (including recipe, working-directory and timeout fields)
must match the receipt's requested command; the requested runner image must match the
receipt's environment image digest. Request creation must fall between patch receipt
creation and test execution start and cannot be in the future. Mismatches block both
strict assessment commands; missing, corrupt or foreign-task requests fail without a
report. The resolved request reference is included in the assessment binding. This
does not verify an image's provenance or the real runner's behavior.

The patch's `WorkOrder` must also resolve to a canonical stored record for the task.
Its repository object must equal the patch's, and its spec/plan references must match
the review request's. The requested test must exactly match a permitted command contract.
Every reported changed path must exactly match an allowed path (no prefix or glob
expansion), and the reported changed-byte count must not exceed the work-order limit.
Work-order creation must precede or equal patch execution start and cannot be in the
future; its deadline cannot predate creation, and patch/test completion cannot exceed it.
Equality at the byte limit or deadline is accepted. The report binds the work-order
reference. These checks do not recompute changes from the diff, authenticate the work
order or prove that execution enforced its capabilities.

The work order's `ImplementationPlan`, plan `ApprovalDecision` and linked
`ApprovalRequest` must resolve to canonical records owned by the task. Spec, repository,
allowed-path list and permitted-test list must match the work order exactly; the work
order's byte limit and attempt cannot exceed the plan's limits. The request must be for
that plan, with exactly the spec and plan references as evidence. Its canonical subject
digest binds those references, task, revision and declared policy version; the decision
must approve that same digest. Plan creation, request creation, decision time, decision
record creation and work-order creation must occur in order. The work order must be
created before approval expiry and its deadline cannot extend that expiry.

The report includes those three references and fixed blockers in `plan_approval`.
This checks historical approval claims, not current approval validity: expiry after
work-order creation does not by itself invalidate completed evidence. It does not
authenticate the actor or policy, establish supersession from historical task state, verify
the task revision against historical state, or admit/consume a live approval.

The workflow's `approvals` registry must map the exact plan request ID to the assessed
decision ID. A missing entry, or an entry for another request or decision, produces
`plan_approval_not_registered`; the combined command prefixes that blocker with
`evidence:`. The report binds a `registered` boolean without exposing a different
registered decision's ID. This lookup uses the existing evidence read transaction and
never registers or repairs a decision. Registration is local workflow evidence, not
authentication or proof that a request has not been superseded.

The plan's task-owned `TaskSpec` is resolved from its canonical stored record. It must
have a non-null recorded confirmation, use the same repository as the plan and be
created no later than the plan. Every plan path must exactly match an entry in the
specification's allowed paths; the plan may narrow that list. The specification's
original request artifact must pass task ownership, exact metadata, regular-file and
hash/size checks, with a separate 1 MiB maximum read. Missing or corrupt specification
evidence fails without a report. The `plan_approval` binding includes the specification
reference and request artifact ID, hash and byte count, without request text or confirmer
identity. This does not authenticate confirmation or evaluate whether the plan satisfies
the specification's acceptance criteria and constraints.

Patch and test receipts must also resolve to task-owned `operations` rows in the same
evidence snapshot. Their stages must be `implementation` and `tests`, their slots must
equal the work-order attempt, status must be `done`, and generation must be a positive
integer. A canonical JSON result, read with a 1 MiB cap, must name the exact receipt
under `patch` or `test` and have no non-null `failure` value (successful implementation
results omit that optional field). Missing operations fail without a
report; wrong state/binding and absent or oversized results block; malformed or
noncanonical results fail without a report. The `execution` binding reports operation
IDs, result hashes and fixed blockers without result contents. This establishes local
completion records only: broker request/envelope provenance, generation fencing and
the actual execution environment remain independently unverified.

For operations whose stage, attempt, status and generation pass, the assessment now
also reads the retained `broker_requests` row for that exact operation/generation.
The canonical request must pass the broker wire schema and match task, operation and
generation. Patch requests must use `edit` with one allowed fixture argument; test
requests must use `test` with no arguments. Missing, malformed, noncanonical or
oversized requests fail without a report; mismatched bindings block. Reads are capped
at 1 MiB and only the request digest is included in the operation report. Incomplete
operations remain blocked without requiring broker records. This binds a historical
request to the retained generation; it does not approve the source digest or image,
or establish that execution enforced the request.

The same completed operation must now also resolve an `execution_provenance` entry for
its exact generation. Its canonical artifact reference must belong to the task, pass
the regular-file and 1 MiB byte checks, and contain a canonical broker `Envelope` whose
digest matches the provenance row. Envelope version, task, operation, generation,
nonce and source digest must match the retained request, as must its request digest.
Broker failure, nonzero/missing exit status or truncated output blocks assessment.
Each stdout/stderr value has a 256 KiB UTF-8 byte cap in addition to schema limits.
Missing/corrupt provenance fails without a report, while binding or outcome mismatches
produce blockers. Only the envelope digest is added to the operation report. This
does not approve the declared runtime/image, authenticate the producer or authorize
live execution.

The broker result must also agree with the receipt's executed argv, exit status and
start/end instants. Test truncation flags must agree. For patch receipts, exactly one
reported command is required because the broker format retains one execution result
per operation. Both output streams are redacted using the storage redactor, encoded as
UTF-8 and compared by hash and byte count with the receipt's output artifact references;
those artifacts independently pass the supporting-file checks. Contradictory commands,
timing or output produce fixed `broker_receipt_*` blockers. These comparisons do not
reconstruct workspace contents, evaluate the patch diff or prove producer authenticity.

The test's logical command may differ from executed argv only when it exactly matches
the built-in fixture recipe and argv exactly matches the executor's fixed wrapper for
the recorded mode, image, workspace and operation. Execution and assessment share a
pure command renderer. Windows and POSIX workspace spellings are handled without
resolving historical paths. The recorded host interpreter path is compared as part of
the wrapper but is not authenticated. Extra arguments, changed inline scripts and
altered logical recipe limits cannot qualify through this path. An end-to-end trusted
fixture run now passes record-claims assessment at `AWAITING_ACTION_APPROVAL`; that
diagnostic result neither approves nor dispatches its external action.

Patch and test broker requests must agree exactly on source digest, execution mode and
image reference. Docker claims require a digest-pinned image in the executor's accepted
format; the test receipt must report Linux and that image digest. Trusted-fixture
requests must not claim an image. Receipt working-directory strings must exactly match
their broker request workspace, without resolving paths on the assessment host. These
checks can reject inconsistent historical records without launching Docker or touching
the recorded workspace. They do not approve an image/source digest, prove containment,
verify the host interpreter for fixture execution or authenticate runtime claims.

This is consistency checking of local claims, not authentication of execution, reviewer
identity or policy authority. It does not validate every transitive dependency,
the meaning of diff/log contents, real GitHub commits, or the tool payload against a journal proposal.
The current engine does not automatically export these records as evidence artifacts.
The standalone claims check does not compare the tool payload to a journal proposal;
the combined assessment adds the binding described above.

The combined approval command runs this stricter assessment fresh rather than trusting
a saved report. `evidence_verified`, `live_authorized` and `retry_allowed` stay false.

Next steps require verifying evidence provenance and remaining dependencies, durable request/decision
storage, authenticated approver identity, policy verification, expiry and single-use
admission, plus reviewed provider/credential/remote-branch boundaries. A future broker
must recheck journal state before admission because it can change after this read snapshot.
The fixture approval and this preview remain separate and cannot authorize live GitHub use.
