# GitHub issues as intents

A project manager's work is tracking work: an issue on the repository, a ticket beside it,
and a link between them. [Jira actions](jira-integration.md) already modelled the ticket
half — `create_issue` for an Epic, Story or Task, `epic_link`, `issue_link`, and a
`github_link` that names a GitHub issue by number.

Nothing modelled the issue itself. The only GitHub intent this control plane had was a pull
request, so the number a `github_link` pointed at had to come from somewhere else entirely.

This is the missing half, in the same shape as everything else here: an intent checked
against a contract, a **read plan** naming exactly what must be observed first, a capture of
those reads checked against the intent, and only then the exact write request. Nothing in
`orch/github_issues.py` opens a socket or holds a credential. Every record it returns says
`live_authorized: false`, and it says so because no dispatch path exists — not as a flag
someone forgot to flip.

## Is this already an issue?

```sh
python -m orch github-issue-duplicate-plan --github-profile profile.json \
    --terms "rate limiting in:title"
python -m orch github-issue-duplicates --github-profile profile.json \
    --terms "rate limiting in:title" --transcript capture.json
```

The first command answers nothing. It hands you the precise requests to make — the
repository, and one bounded issue search — and whoever can reach GitHub makes them and
brings the answer back. The second checks that answer against the plan it claims to answer
and reports the issues it found.

That split is deliberate and is the same one [the GitHub reader](github-read-transcripts.md)
uses: *"Offline contract for a future GitHub reader. No sockets, credentials or retries."*
Giving this control plane a network read is a separate decision with its own gate, not a
convenience to add along the way.

A search whose `total_count` exceeds the results it returned is **refused**. Acting on a
truncated view of what already exists is how duplicates get made.

## Creating one

```sh
python -m orch github-issue-read-plan  --github-profile profile.json --intent intent.json
python -m orch github-issue-preview    --github-profile profile.json --intent intent.json \
    --transcript capture.json
```

The profile is the destination, supplied by an operator and never inferred from an intent:

```json
{"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
 "repository": "owner/name", "repository_id": 12345}
```

**The numeric id is not redundant.** A repository can be renamed or transferred, and the name
that still resolves afterwards may belong to someone else. The preview refuses unless the
observed repository matches both, and refuses again if it is archived, disabled, or has
issues turned off.

The read plan asks for three things and nothing more: the repository, its labels, and a
search for this operation's marker. A label the repository does not have is **named rather
than created** — creating an issue with an unknown label creates the label too, which is an
effect nobody reviewed.

## The operation marker

Every issue this would create carries one line in its body:

```text
<!-- orchd-operation: b7c1... -->
```

An HTML comment: invisible where the issue is read, exact where it is searched for. A label
would have been simpler and would have silently created a label that did not exist.

It does two jobs. Before creating, the read plan searches for it, so an operation that has
already run **refuses to create a second issue and names the number of the first**. After an
uncertain dispatch, `github-issue-reconcile` searches for it again to identify what was
actually created:

```sh
python -m orch github-issue-reconcile --github-profile profile.json --intent intent.json \
    --transcript capture.json
```

Two issues with the same title are ordinary; two carrying the same operation marker are not,
so that case is a blocker rather than a choice. An intent may not write a marker itself —
otherwise one operation could make another's issue look like its own.

## What this is not

This is a codec, not a client. It produces request data that a human or a broker could act
on; it performs nothing, authorizes nothing, and reaching a real GitHub still requires a
least-privilege token and a destination-bound broker, which remain their own gate.

It is also not yet wired into the workflow. [The GitHub journal](github-journal.md) is
pull-request shaped — its destination check matches `/pulls` and it holds one operation per
task — so staging an issue intent with a reservation and an approval is a separate step, as
is connecting it to [the draft specification stage](draft-spec.md) so that a project manager
can produce an epic, its stories and the issue that tracks them as one reviewed bundle.

Creating the issue and linking a ticket to it are two actions with two approvals. Nothing
here makes them one transaction, and on two different services nothing could.
