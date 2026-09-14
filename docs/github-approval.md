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
authenticate the actor or policy, consult the approval registry for supersession, verify
the task revision against historical state, or admit/consume a live approval.

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
