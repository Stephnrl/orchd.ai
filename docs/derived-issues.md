# The issue a specification implies

An agent [drafts a specification](agent-work.md) and a person [confirms it by
hash](draft-spec.md). Tracking that work as [a GitHub issue](github-issues.md) then needs an
intent, and the obvious way to get one is to let the agent write it.

That would quietly undo what the confirmation is for. A human would have confirmed one set of
words and an agent would have put different ones in the repository, with nothing relating the
two. The hash a person signed would describe something nobody filed.

So nothing writes the issue. It is **derived** — a pure function of the specification that was
confirmed:

```sh
python -m orch task-issue-intent --task TASK --github-profile profile.json > intent.json
```

```text
<the request text>

## Acceptance criteria
- ...

## Constraints
- ...
```

Always that order, always those headings, empty sections omitted. The same specification
renders the same bytes, so anyone can re-derive the intent and compare it with what was filed.

## What follows from that

**An agent cannot put arbitrary text in a repository, even indirectly.** The only route from
an agent's keyboard to a GitHub issue runs through a specification a person read and confirmed
by its hash. There is no step at which free text enters.

**An interrupted dispatch resumes instead of repeating.** The operation identifier is derived
too, from the task and the confirmed specification's digest — so deriving twice gives the same
operation, which means the [marker](github-issues.md#the-operation-marker) the first attempt
would have written is exactly the one the second searches for. Reconciliation finds the issue
that exists rather than filing a second.

That second property is why the identifier is derived rather than random. A fresh identifier
on every attempt would make every retry a new operation with a new marker, and the idempotency
built into the codec would have nothing to recognise.

**A specification too large to render is refused, not trimmed.** Trimming would mean the issue
no longer says what was confirmed, which is the one thing this exists to guarantee.

## Only a confirmed specification implies anything

An unconfirmed draft is a proposal, not a decision, and an issue is an effect. Deriving from
one is refused, and so is deriving from a task with no specification at all.

The derivation reports who confirmed it and the `spec_sha256` it came from, so the issue, the
approval and the specification are all reachable from one another.

## Where it sits

```text
agent drafts ──> human confirms ──> intent derived ──> read plan ──> preview
                                                                        │
                            dispatched <── approved <── staged <────────┘
```

The derived intent is exactly what `github-issue-read-plan` takes, so it drops into
[the runbook's](live-integration-runbook.md) create sequence at step 1 with nothing retyped.

## What this is not

It does not file anything. Everything from the read plan onward is unchanged: the repository
is still checked by id, the labels must still exist, the approval still happens on evidence a
person supplies, and the dispatch still only sends a request matching the hash that was
approved.

It also does not decide *whether* a task should be tracked as an issue. Someone still chooses
to derive one; this only fixes what it says when they do.
