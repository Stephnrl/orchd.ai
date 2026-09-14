# Offline GitHub pull request preview

This is the first separate GitHub broker preparation boundary. It produces reviewable
request data only: no Git commands, network calls, credentials, branch creation, commits,
pushes, PR creation or workflow storage. The engine's existing action remains simulated.

```sh
python -m orch github-preview --intent intent.json --allow-repository example/project
```

Exit 0 means a preview was prepared; it does not mean a remote operation succeeded.
Invalid input exits 2. The explicit allow-repository value must exactly match the intent,
including case. This conservative restriction is stricter than GitHub's case-insensitive
repository lookup. No destination is inferred from the checkout or an agent response.

An example input (the IDs and commits below are placeholders, not verified evidence):

```json
{
  "schema_version": "1.0.0",
  "task_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "repository": "example/project",
  "base": "main",
  "head": "orchd/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "expected_base_sha": "1111111111111111111111111111111111111111",
  "expected_head_sha": "2222222222222222222222222222222222222222",
  "title": "Review greeting",
  "body": "Evidence summary"
}
```

The separate versioned schema is `contracts/github-pr-intent-v1.schema.json`. The
loader limits input to 64 KiB, rejects duplicate keys/non-JSON numbers and accepts no
extra fields. Policy restricts requests to github.com, same-repository draft PRs, an
operation-specific `orchd/<task_id>/<operation_id>` head and conservative branch syntax.
It requires distinct 40-character lowercase commit hashes, a nonblank single-line title
of at most 256 characters and a body of at most 16 KiB UTF-8. Control characters other
than body newline/tab and recognized credential patterns are rejected. Secret pattern
checking is defense in depth, not proof that arbitrary text contains no sensitive data.
Enterprise hosts, forks, issue conversion and non-draft creation are unsupported.

The prepared POST request follows GitHub's [create-PR API](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request),
with fixed API version and media type, `draft: true` and `maintainer_can_modify: false`.
It contains no authorization header. A canonical SHA-256 covers the complete request,
task/operation IDs and expected base/head commits. Editing those values changes the
digest; it is a scope identifier, not a signature or approval token.

## Required before live dispatch

The [offline repository/ref assessment](github-ref-assessment.md) now compares supplied
response observations to this preview. It remains separate from authenticated fetching
and never upgrades the preview to live authorization.
The [offline reconciliation classifier](github-reconciliation.md) can identify one
matching observed draft PR, but never confirms a remote effect or permits a POST retry.

Every preview reports `live_authorized: false` and `remote_refs_verified: false`.
Task IDs and commit hashes are input claims; this function does not prove they match
stored workflow evidence or existing GitHub objects. The current fixture action approval
does not bind this new request and cannot authorize it.
The [GitHub approval preview](github-approval.md) now binds the retained scope to explicit
evidence and policy digest claims, a request ID and expiry; it remains unverified and is
not an approval decision.

A future broker must bind verified patch/test/review evidence and a new human approval
to the preview digest; enforce an independently configured repository/credential policy;
verify remote identity and both branch tips; and durably record intent before dispatch.
GitHub's create-PR endpoint uses branch names, not an atomic expected-SHA condition.
Checking tips before a call alone does not solve concurrent branch changes. Protected
operation branches and response verification require an explicit reviewed design.
Timeouts must trigger remote reconciliation before retry, never an assumed failure or
blind repeated POST. These are remaining work, not guarantees provided by this preview.
