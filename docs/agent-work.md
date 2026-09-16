# An agent doing the work

[The agent API](agent-api.md) gave a container an authenticated identity and a role-scoped
lease. [The draft specification stage](draft-spec.md) gave the project manager real work to
do. Nothing connected them: `task_drafts` was reachable only from the command line, so a
project manager container could authenticate, claim a task, renew its lease, release it —
and do nothing with it.

Everything built up to here was operator-driven. This is what makes it an agent's.

## Three routes

| Route | What it does |
| --- | --- |
| `agent-spec` | Read the current draft, its revision, its `spec_sha256`, and any pending questions |
| `agent-draft` | Write the next revision of the specification |
| `agent-ask` | Raise a clarification, which stops the task until a person answers |

`agent-spec` is not a convenience. Without it an agent writes blind — it cannot see what it
already wrote, cannot learn the hash a human will confirm, and cannot tell whether a question
it raised has been answered.

## Holding the task is the condition

Every one of these requires a **live lease, held by this agent, on this task**. That is the
first time a session gates anything but itself, and it is a real gate rather than a formality
because of where leases come from: [a lease is only granted for the role the task's next step
needs](agent-sessions.md).

So requiring one here means an agent cannot write a specification for a task it never
claimed, nor one whose step belongs to another role, nor one another agent is holding, nor
one whose lease expired while it was thinking. A junior container cannot draft a
specification, not because drafting checks for juniors, but because a junior can never hold a
task that is waiting on the project manager.

Two checks compose, and neither is redundant. The service refuses an agent that is not
holding the task; the kernel refuses work that does not belong to the task's current state.
An agent holding an implementation task can read its specification and cannot rewrite it.

## What stays out of reach

**No route an agent can call answers a clarification or confirms a specification.** There is
a test asserting the route table rather than trusting the absence.

Both of those take a `--principal`, are recorded with a `human` actor, and exist only on the
operator's path. An agent drafts and asks; a person answers and decides. Once a specification
is confirmed the task leaves `DRAFT_SPEC`, and the agent that wrote it can no longer change
a word — which is the point of confirming by hash.

## The asymmetry with the operator

An operator drafting through the CLI holds no lease and is not required to. That is
deliberate and matches [the agent API](agent-api.md): enforcement here is about what an
*agent container* can do, not about locking an operator out of their own store. The operator
is the trusted local account; the container is not.

## What this is not

An agent still proposes nothing outward. Creating a GitHub issue from a drafted specification
is [staged, approved and dispatched](github-issues.md) on the operator's path, and connecting
that to what an agent produces is a separate step — as is letting an agent propose the issue
intent at all.

Nothing here changes the admission bound, the guardian, the approval gates, or what a lease
means. It changes only which of them an agent can reach without a person typing the command.
