# Offline Jira comment recovery

The Jira adapter can now compare saved comment pages against an exact
[comment preview](jira-preview.md) and an explicitly supplied author key. This provides
an investigation path for a future interrupted delivery. It does not post, adopt or
delete comments, record a successful delivery, or authorize a retry.

## Create the read plan

Keep the original target, issue observations and comment intent used to prepare the
preview. Supply the expected preview digest and the expected Jira user's **key**:

```sh
python -m orch jira-comment-read-plan --jira-target jira-target.json --observations jira-response.json --intent jira-comment.json --expected-sha256 COMMENT_PREVIEW_SHA256 --jira-author-key EXPECTED_USER_KEY
```

The command reconstructs the preview from these inputs and rejects a changed digest or
issue snapshot. `JiraCommentReadPlan` binds that preview, task/operation, target, author
key, first GET request and limits. Its `sha256` is the digest of its binding. It does not
infer an author from the captured comments or accept a display name as a fallback.

The GET uses the numeric issue's `/rest/api/2/issue/{id}/comment` endpoint, with
`startAt=0&maxResults=50&orderBy=created`. Subsequent offsets are derived from the number
of records already checked. No rendered HTML expansion is requested. The pagination
shape follows Atlassian's documented [Get comments API](https://docs.atlassian.com/software/jira/docs/api/REST/8.5.1/).
This is a historical codec reference; the target deployment and its author-key support
still require conformance review. The plan is request data, not a network operation.

## Capture and assess pages

Save a UTF-8 JSON capture containing exactly:

| Field | Value |
| --- | --- |
| `kind` | `JiraCommentCapture` |
| `schema_version` | `1.0.0` |
| `plan_sha256` | The read plan binding digest |
| `pages` | One to ten saved response envelopes in lookup order |

Each page envelope contains exactly `url`, `redirected`, `status` and `body`. The URL
must equal the derived request URL, including its offset and query parameters, and
`redirected` must be false. Status is an integer 200–599. A 200 body contains integer
`startAt`, `maxResults`, `total`, and a `comments` array. Extra body fields are ignored
but remain covered by the capture digest and input size cap.

Each comment needs a positive decimal string `id`, an exact issue-bound `self` URL,
an `author.key` string and a string `body`. Optional `visibility` is compared with the
explicit preview choice. Default visibility matches an omitted/null value; a role or
group restriction must match exactly. There is no username/display-name fallback.
Missing identity/content makes the assessment unresolved, including when an older
deployment or deleted user does not supply an author key.

```sh
python -m orch jira-reconcile-comments --jira-target jira-target.json --observations jira-response.json --intent jira-comment.json --expected-sha256 COMMENT_PREVIEW_SHA256 --jira-author-key EXPECTED_USER_KEY --transcript jira-comments.json
```

The command revalidates the target, original issue snapshot, preview and read-plan
binding before inspecting pages. It never opens a workflow database, modifies the
source files or calls Jira. All inputs use the existing 64 KiB strict JSON loader, which
rejects duplicate keys and non-JSON numbers. The capture is capped at 64 KiB in total,
ten pages and fifty comments per page. Reported page size may be smaller than fifty;
the next offset advances by the actual number of returned comments. Reported totals
must be integers from zero to one million, but at most 500 comments can be assessed.

## Completeness and results

Offsets must be contiguous and start at zero. A changing total, overlapping comment
IDs, excessive returned counts, an empty page before the declared end, or stopping
before the declared total leaves the result unresolved. Reading after a declared end,
changed request URL, redirect, malformed page shape or exceeded budget rejects the
capture. Non-200 pages remain unavailable and their error bodies are not echoed.

Valid unrelated comments are ignored as candidates but still count toward pagination.
A candidate must match the exact proposed comment text, visibility and expected author.
Exactly one matching candidate in a complete capture can yield `candidate_observed`;
multiple matching comments or no match yield `unresolved`. No captured body or author
profile is copied into the result. The report includes only the expected target/author,
digests, checked counts, request plan, fixed blockers and a candidate ID/URL if eligible.

Planning and candidate observations exit 0. Unresolved assessments exit 2 with a
`JiraCommentReconciliation` report. Invalid inputs exit 2 without partial stdout. The
report binds the canonical capture digest and expected preview/read-plan digests.
Every result has `remote_effect_confirmed: false`, `live_authorized: false` and
`retry_allowed: false`.

A matching comment may predate the proposed operation or have been posted independently
by the same account. A stable captured total also does not prove a stable remote list.
These are unauthenticated saved observations, without freshness or delivery provenance.
An empty list is not permission to repost. Live use still needs credentials, trusted
collection, durable dispatch intent, authoritative reconciliation and an approved
operator recovery policy. No such admission or journal is created by this milestone.

Tests cover CLI preparation/recovery, unrelated comments, exact author/body/visibility,
duplicate candidates and IDs, changed totals, empty/truncated pages, invalid origins,
preview drift, byte/page limits and no process/network execution.
