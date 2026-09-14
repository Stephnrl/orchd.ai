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

Next steps require retrieving and validating workflow evidence, durable request/decision
storage, authenticated approver identity, policy verification, expiry and single-use
admission, plus reviewed provider/credential/remote-branch boundaries. A future broker
must recheck journal state before admission because it can change after this read snapshot.
The fixture approval and this preview remain separate and cannot authorize live GitHub use.
