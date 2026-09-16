# Finding the work

An agent could claim a task, hold it, [work on it](agent-work.md) and release it — provided
somebody told it which task. [`agent-sessions`](agent-api.md) lists what is *held*; nothing
listed what was available. So a project manager container starting in the morning had to be
handed identifiers out of band, which is most of the way back to an operator driving it by
hand.

`agent-available` lists what an agent could claim. The shape of that listing is the design.

## Only work this agent could take

The listing is filtered to the roles the agent is **enrolled for**, not to every task in the
store. A listing of work an agent can never claim tells it about tasks it has no business
knowing exist, and the boundary throughout has been that an agent sees what it may act on and
nothing else.

## Identifiers and state, never content

Each entry is a `task_id`, a `state`, the `role` whose turn it is, and the task `revision`.
No title, no request text, no acceptance criteria.

An agent claims first and reads with `agent-spec`. Without that restriction this route would
become a way to read every task in the store without ever holding one — which is exactly the
hole that holding exists to close.

## Nothing waiting on a person

A human pause belongs to nobody an agent can be, so it never appears here for any role. This
now includes `AWAITING_CLARIFICATION`; see below.

## The bound, alongside

`admitted`, `admission_bound` and `at_bound` come back with the listing. When
[admission](worker-admission.md) is full a claim will be refused, and an agent that knows this
waits rather than working through a list it cannot act on.

## Two bugs this found

Building the listing exposed two faults in the role model, both of which predate it.

**An agent could be enrolled as `human`.** `ROLES` contains `human` because the workflow has
human steps, and enrolment checked against `ROLES` — so an operator could enrol an agent as
one, and that agent could then claim a task waiting at an approval pause. Both
[agent sessions](agent-sessions.md) and [the agent API](agent-api.md) said this could not
happen; it could. Enrolment now checks `AGENT_ROLES`, which excludes it, **and** claiming
refuses `human` regardless of the task's state, so neither layer alone is load-bearing.

The existing test looked like it covered this. It tried claiming as `human` on a task whose
step belonged to `lead_planner`, so the claim was refused for the wrong reason and the gap
stayed invisible.

**`AWAITING_CLARIFICATION` belonged to `project_manager`**, while every other state waiting on
a person maps to `human`. That is not merely untidy. A project manager agent would have been
offered such a task as available work, claimed it, been unable to act on it — drafting and
asking both require `DRAFT_SPEC` — and held an admission slot until its lease expired. Agents
would have starved the bound on tasks they could never advance.

Having asked a question, the next move is not the asker's. The state now belongs to a human,
which is what it always meant.

## What this is not

It does not assign work, and it does not decide which task an agent should take. Two agents
of the same role see the same list in the same order; which of them gets a contested task is
settled by [the claim](agent-sessions.md), not by the listing. Nothing here queues, prioritises
or schedules — it reports what could be claimed at the moment it was asked.
