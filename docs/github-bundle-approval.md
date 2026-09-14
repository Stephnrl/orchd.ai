# Journal-bound portable evidence review

Portable evidence can now be reviewed against a prepared GitHub operation without the
original workflow store or a manually assembled evidence-hash file. Two commands prepare
a bundle-bound approval preview and reassess it against the current journal. They do
not issue an approval decision, reserve an attempt, call GitHub or authorize execution.

## Prepare a preview

Start with an explicitly selected, [exported evidence bundle](github-evidence-bundles.md)
and a [prepared journal operation](github-journal.md). Retain the bundle file's digest
from its export report and the operation's scope digest from journal inspection.

```sh
python -m orch github-bundle-approval-preview --journal .runtime/github.sqlite --operation-id OPERATION_ID --expected-sha256 JOURNAL_SCOPE_SHA256 --bundle .runtime/evidence.json --expected-bundle-sha256 BUNDLE_FILE_SHA256
```

The command opens the journal read-only and independently verifies the bundle in
disposable storage. The bundle's current claims must be consistent. Its tool request
must match the journal operation, checksum and supported GitHub request shape, and its
task-owned payload must contain exactly the retained scope under `github-draft-preview`.
Missing payloads, simulated adapters, changed destinations and unrelated operations
cannot produce this preview.

After checking the tool payload, preparation rechecks the expected prepared journal
scope through the existing approval-preview function. The policy must still be current
at the new preview's issue time. An observed reservation or expiry prevents output.
No request or decision is persisted; preparation is a read-only review operation.

Save successful JSON stdout as UTF-8 `bundle-preview.json`. It is a
`GitHubBundleApprovalPreview` containing the ordinary approval preview inside a closed,
versioned binding alongside the exact whole-file bundle digest. The outer `sha256`
binds both. The inner preview derives its task and four evidence hashes directly from
the verified selection. It retains the existing fifteen-minute preview lifetime;
the evidence policy can expire earlier.

## Reassess the saved preview

```sh
python -m orch github-assess-bundle-approval --journal .runtime/github.sqlite --approval-preview bundle-preview.json --expected-sha256 BUNDLE_PREVIEW_SHA256 --bundle .runtime/evidence.json --expected-bundle-sha256 BUNDLE_FILE_SHA256
```

For preparation, `--expected-sha256` identifies the journal scope. For reassessment,
it identifies the saved **outer bundle preview**. In both commands,
`--expected-bundle-sha256` identifies the complete bundle file. No `--data` or
`--evidence` file is needed. Ordinary approval previews and bundle previews are distinct
formats and cannot be substituted for one another.

Reassessment performs the following sequence:

1. Validate the outer preview, expected preview digest and expected bundle binding.
2. Validate the nested approval preview against the current journal and its lifetime.
   If it is already blocked, return that result without reading the bundle.
3. Verify the exact bundle, including its dependency set, artifacts and current claims,
   and compare its tool request and payload with the saved operation scope.
4. Recheck the approval against the journal using evidence hashes derived from the
   verified bundle. Check policy validity at this final assessment time as well.

A different bundle cannot be substituted under the same saved preview. Rehashed but
inconsistent evidence, mismatched tool scope, changed evidence hashes, preview expiry,
policy expiry and journal reservation changes fail or block as appropriate. The journal
can still change after this report; this is not an atomic admission transaction.

## Results and boundaries

Preparation exits 0 with a preview or 2 with no JSON output. Assessment returns a
`GitHubBundleApprovalAssessment` with `current`/exit 0 or `blocked`/exit 2. Malformed,
missing or corrupt inputs fail with exit 2 and no partial JSON report.

The assessment binds the outer preview digest, bundle digest, final approval result,
bundle assessment, tool-scope assessment and fixed blockers. Blockers use `approval:`,
`evidence:` and `tool:` prefixes. `bundle_bytes_verified` is true only if the bundle was
actually verified during this run; it is false when an initially blocked preview skips
bundle I/O. Reports contain metadata, not retained artifact contents. The preview does
contain the intended journal scope and should be handled as project data.

`evidence_verified`, `live_authorized` and `retry_allowed` remain false in every result.
This does not authenticate evidence producers, operators or approvers; grant a decision;
consume approval; prove remote branch state; or dispatch a GitHub request. Verification
reuses query-only disposable storage and deletes it after success or handled failure.
The bundle's existing size, publication, confidentiality and crash-cleanup limits apply.

The current engine's simulated GitHub payload deliberately does not qualify. A real
fixture workflow may pass standalone bundle verification while this journal-bound
preparation rejects its simulated action. Tests use explicitly scope-bound offline
records to prove the positive path; they do not turn simulation into live integration.

Tests cover preparation and reassessment without source artifacts, read-only journals,
query-only scratch storage, exact digest binding, nested-preview validation, altered
evidence/tool scope, simulated payloads, expiry boundaries, reservations during both
flows, failure cleanup and CLI exit/output behavior.
