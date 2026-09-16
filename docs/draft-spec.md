# The draft specification stage

`DRAFT_SPEC` and `AWAITING_CLARIFICATION` have been states since the first design, and
`ClarificationRequest` and `ClarificationResponse` have been contracts for just as long. No
code ever entered those states or wrote those records. `create_task` invented a
specification, wrote `confirmed_by`, set `spec_confirmed` and moved to `SPEC_READY` — all in
one transaction.

So the evidence said a human had confirmed a specification in the same breath as inventing
it. For a fixture that is harmless. As the foundation for [an agent that drafts
specifications](agent-protocol-context.md) it is a fail-open in the place that matters most,
because every later gate in this control plane takes the specification as given: the plan is
checked against it, the review is checked against the plan, and the action is checked against
the review. A specification nobody confirmed makes all of that a chain fastened to nothing.

## The stage

```text
task-open ──> DRAFT_SPEC ──spec-draft──> DRAFT_SPEC ──spec-confirm──> SPEC_READY
                  │  ▲
            spec-ask  spec-answer
                  ▼  │
          AWAITING_CLARIFICATION
```

```sh
python -m orch task-open    --title "Rate limiting"
python -m orch spec-draft   --task TASK --from draft.json
python -m orch spec-ask     --task TASK --from questions.json
python -m orch spec-answer  --task TASK --from answers.json --principal steph
python -m orch spec-show    --task TASK
python -m orch spec-confirm --task TASK --spec-sha256 HEX --principal steph
```

A task opened this way has **no specification at all** — `spec` is null and
`spec_confirmed` is false. That is the honest starting point for work whose shape nobody has
decided yet, and it is what a project manager is for.

`draft.json` holds the parts a project manager writes: `title`, `request`,
`acceptance_criteria`, `constraints`, `allowed_paths` and `external_references`. Acceptance
criteria are required, because a specification nobody can test is not a specification. The
lists are bounded — one specification, not a programme of work — no entry may repeat, and
nothing may contain a recognised secret.

## Revising, and what that keeps

Each draft is stored as a new `TaskSpec` revision. Nothing is edited in place and nothing is
deleted, so the history shows what was proposed, what changed, and what was eventually
confirmed. A draft carries `confirmed_by: null`, which the contract has always permitted and
which nothing previously wrote.

`--expected-revision` fences a draft against a task that moved underneath it, the same way
every other mutation in this control plane is fenced.

## Questions are a stop, not a hint

`spec-ask` records a `ClarificationRequest` bound to the exact draft it is about, and moves
the task to `AWAITING_CLARIFICATION`. While it sits there nothing can be drafted and nothing
can be confirmed: the work has genuinely stopped, and it is waiting on a person.

`spec-answer` records a `ClarificationResponse` with a human actor and returns the task to
`DRAFT_SPEC`. Every question must be answered and no others, so an answer cannot quietly drop
one. Binding the request to a specific draft matters for the same reason: an answer about one
specification is not evidence about a later one.

## Confirmation names the words it confirms

`spec-confirm` takes the **sha256 of the specification that was read**, which `spec-show`
prints. If the draft was revised in between, the hash names a specification that is no longer
current, and the confirmation is refused rather than silently applied to different words.

This is the fence that makes the rest of the chain mean something. A project manager can
revise freely right up to the moment of confirmation — revising breaks nothing and refuses
nothing. It simply means the human is confirming words, not a task.

Confirmation is recorded as one further revision carrying `confirmed_by`, so the confirmed
specification and the draft it came from are both kept, and the event names the draft hash
that was confirmed.

A confirmation does not survive a return to `DRAFT_SPEC`. `CHANGES_REQUESTED` may send a task
back to be redrafted, and without dropping the confirmation the task could reach `SPEC_READY`
again on words that had since been rewritten.

## The human boundary

Answering and confirming are human acts. They take a `--principal`, they are recorded with a
`human` actor, and **nothing an agent can reach through [the agent API](agent-api.md)
performs either of them** — those five routes claim, renew, release and list sessions, and
none of them writes a specification or a confirmation. An agent drafts; a person decides.

Drafting itself is ordinary work for whoever holds the task, and a project manager container
holds it the way any agent does, through [a role-scoped lease](agent-sessions.md).

## What this is not

Routing drafting through the authenticated agent API is a separate step: today a project
manager drafts through the CLI on the host, as an operator does. The [local UI](phase2.md)
does not expose the stage either — it lists a task being drafted by the title it was opened
with, and nothing more.

A drafted specification also cannot yet run the rest of the workflow. Everything downstream
of `SPEC_READY` is still the offline greeting fixture, which accepts one fixed set of allowed
paths and one test recipe, so a specification drafted about anything else is refused at
planning. The drafting stage is real; what follows it is still a fixture, and admitting a
live provider remains its own gate.

Selecting a repository is not part of drafting. A drafted specification carries the same
fixture repository reference every task does, because pointing a task at a real repository is
a separate boundary with its own review.
