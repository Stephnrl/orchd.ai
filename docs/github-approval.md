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

This command checks the selected saved preview, verifies task-owned artifact bytes only
if the preview is current, then rechecks expiry and journal scope/state after artifact I/O.
Both databases are opened read-only. The report binds the complete fresh approval and
artifact assessments; it does not accept previously saved assessments as proof.

`current` exits 0. A blocked preview exits 2 with its blockers; when blocked before file
checks, `artifact_evidence` is null and `artifact_bytes_verified` is false. If expiry or
reservation changes during file checks, the result is blocked even though verified byte
evidence is included. Invalid inputs or artifact failures exit 2 without a partial report.

The recheck detects changes during the artifact stage, but it is not an atomic snapshot
across databases and files or a lock against changes afterward. A current result still
has `evidence_verified`, `live_authorized` and `retry_allowed` false. Semantic evidence,
policy trust and authenticated approval admission remain separate requirements; no
requests or decisions are persisted or consumed.

## Assessing workflow evidence claims

```sh
python -m orch github-check-evidence-claims --data .runtime/phase2 --evidence evidence.json
```

This stricter mode performs the byte checks above and parses those same verified bytes
as canonical `PatchReceipt`, `TestReceipt`, `ReviewDecision` and `ToolDecision` contracts.
Each must exactly match a schema-valid stored record for the same task. Linked
`ReviewRequest` and `ToolRequest` records are resolved by task and document hash with
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

This is consistency checking of local claims, not authentication of execution, reviewer
identity or policy authority. It does not validate every transitive dependency, referenced
diff/log contents, real GitHub commits, or the tool payload against a journal proposal.
The current engine does not automatically export these records as evidence artifacts.
The existing combined approval command still checks bytes only; it does not consume
this stricter report. `evidence_verified`, `live_authorized` and `retry_allowed` stay false.

Next steps require verifying evidence provenance and remaining dependencies, durable request/decision
storage, authenticated approver identity, policy verification, expiry and single-use
admission, plus reviewed provider/credential/remote-branch boundaries. A future broker
must recheck journal state before admission because it can change after this read snapshot.
The fixture approval and this preview remain separate and cannot authorize live GitHub use.
