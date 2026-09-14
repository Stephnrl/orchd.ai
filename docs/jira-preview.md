# Offline Jira Data Center issue review and comment preview

This starts the Jira Data Center adapter with a bounded, offline workflow: review a
saved issue response against an explicit target, then prepare an explicit comment
request bound to that snapshot. It does not contact Jira, use credentials, create tasks,
generate comments from issue text, grant approval or mutate workflow/journal storage.

## Explicit target and issue response

Save a UTF-8 target file such as `jira-target.json`:

```json
{
  "base_url": "https://jira.example.test/jira",
  "project_id": "100",
  "project_key": "ENG",
  "issue_id": "200",
  "issue_key": "ENG-7"
}
```

The target is an operator-supplied allowlist, not values discovered from an issue body.
It requires an exact HTTPS base URL, optional explicit port and simple context path,
positive decimal IDs as strings, uppercase project/issue keys and matching key prefix.
Userinfo, query strings, fragments, encoded or traversal path segments, trailing slashes
and unknown fields are rejected. The accepted hostname/path grammar is deliberately
conservative; it is not validation of an actual deployed Jira instance.

The saved observation envelope contains exactly `url`, `redirected`, `status` and `body`.
The expected URL is the base URL plus
`/rest/api/2/issue/200?fields=project,summary,description,updated`. `redirected` must be
false for a matching result; `status` must be integer 200. `body` is the saved Jira issue
JSON with `id`, `key`, `self` and `fields`. Required fields are `project` (with `id`, `key`,
`self`), `summary`, `description` and `updated`. Issue and project self URLs must use
the expected base URL and numeric IDs. Unknown response fields are ignored in the
projection but still count toward the observation digest and input budget.

```sh
python -m orch jira-review-issue --jira-target jira-target.json --observations jira-response.json
```

This emits `JiraIssueSnapshot`. A matching result contains the derived GET request,
explicit target, observation digest, bounded summary/description/update time and an
`untrusted_external_issue` provenance label. The snapshot's `sha256` hashes its binding.
Recognized secret patterns in summary/description are replaced using the existing
redactor, with an explicit flag; this is not comprehensive removal of sensitive data.
HTML, wiki text and instruction-like prose remain literal untrusted strings. Assignees,
attachments, rendered fields and other unrelated fields are not copied into the result.

The API shapes follow Atlassian's documented
[Get issue contract](https://docs.atlassian.com/software/jira/docs/api/REST/8.5.1/#api/2/issue-getIssue).
In particular, a moved issue can return its current key without a redirect. This adapter
checks that key and the numeric project identity instead of accepting an old-key alias.
The historical API reference establishes the codec shape; compatibility with a specific
corporate deployment remains untested.

Missing/redirected/unavailable responses and identity mismatches produce `blocked`,
exit 2 and no issue-text projection. Malformed inputs reject with exit 2 and no partial
stdout. A matching snapshot exits 0 but has `remote_issue_verified: false` and
`live_authorized: false`. It does not authenticate the response or check its freshness.

## Prepare an explicit comment

Save `jira-comment.json` with exactly these fields:

```json
{
  "schema_version": "1.0.0",
  "task_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "target": {
    "base_url": "https://jira.example.test/jira",
    "project_id": "100", "project_key": "ENG",
    "issue_id": "200", "issue_key": "ENG-7"
  },
  "snapshot_sha256": "REPLACE_WITH_SNAPSHOT_DIGEST",
  "body": "Prepared a change for human review.",
  "visibility": null
}
```

```sh
python -m orch jira-comment-preview --jira-target jira-target.json --observations jira-response.json --intent jira-comment.json
```

The adapter rechecks the same saved response, requires a matching snapshot and exact
digest, and requires the intent target to equal the independent target file. It emits
a `JiraCommentPreview` with a POST request to the numeric issue's `/comment` resource,
task/operation identifiers, target, snapshot digest and observed issue update time.
It does not send the request. The body comes only from the explicit intent, never from
the imported issue text. Changed observations require a new matching intent digest.

Visibility is mandatory in the intent: null explicitly means the default Jira comment
audience, or an object selects `type: role` or `type: group` and a nonblank `value`.
The chosen visibility is included in the request digest. These are the shapes in
Atlassian's [comment examples](https://developer.atlassian.com/server/jira/platform/jira-rest-api-example-add-comment-8946422/).
No role/group existence, posting permission, notification behavior or issue security
is validated. Rendered comment semantics also depend on the target deployment.

Each input file is limited to 64 KiB and rejects duplicate JSON keys/non-JSON numbers.
The target and intent use a [dedicated versioned JSON schema](../contracts/jira-comment-intent-v1.schema.json). Summary is limited to
1 KiB, description to 16 KiB, comment body to 4 KiB and visibility name to 128 bytes.
Comment intents are capped at 16 KiB. Unsupported control characters, whitespace-only
comments and recognized secret patterns in outgoing text are rejected. Description may
be null (projected as empty text); rich structured descriptions are not supported.

All comment previews retain `remote_issue_verified: false`, `live_authorized: false`
and `retry_allowed: false`. Identifiers do not prove task existence or authority; the
update timestamp is a captured value, not an optimistic-concurrency guard. Before live
use, this still needs deployment-specific conformance, credentials, authenticated human
approval, fresh issue/permission checks, durable intent and uncertain-result recovery.
Epic creation, transitions, linking and automatic task intake remain unimplemented.

Tests cover the complete CLI flow, explicit target binding, issue moves, snapshot drift,
redaction/provenance, visibility, malformed URLs, encoding/byte limits and no network or
process execution. They use synthetic issue responses rather than a corporate server.
