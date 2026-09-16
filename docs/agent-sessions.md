# Agent sessions

[Task ownership](task-ownership.md) claims a task with a file lock held by the process doing
the work. That is right for a worker process and wrong for an agent. An agent running in its
own container calls in, exits, thinks, and calls again; between calls it has no process at
all, so a lock-backed claim would hold nothing.

An agent session is the other kind of holder in the same registry: granted to a named agent
in a declared role, live until it expires, renewed while the agent works, released when its
container stops.

```text
python -m orch agent-claim  --data STORE --task TASK --agent lead-devops --role lead_planner --lease 900
python -m orch agent-renew  --data STORE --task TASK --agent lead-devops
python -m orch agent-release --data STORE --task TASK --agent lead-devops
python -m orch agent-sessions --data STORE
```

## The role gate

A task may only be claimed by the role its **next step** belongs to, taken from the roles
the engine actually invokes:

| Task state | Role that may claim it |
| --- | --- |
| `DRAFT_SPEC`, `AWAITING_CLARIFICATION` | `project_manager` |
| `SPEC_READY`, `PLANNING` | `lead_planner` |
| `READY_FOR_IMPLEMENTATION`, `CHANGES_REQUESTED`, `IMPLEMENTING` | `junior` |
| `TESTING` | `test_runner` |
| `REVIEWING` | `reviewer` |
| `AWAITING_PLAN_APPROVAL`, `AWAITING_ACTION_APPROVAL`, `BLOCKED` | `human` |
| `PR_CREATED` | `action_broker` |
| terminal states | nobody |

So a junior agent cannot take work that is waiting on the lead planner, and **no agent can
take a task waiting on a human**: approval pauses belong to a person, and a session cannot
be used to step around one. A finished task needs nobody, and says so.

The guardian is deliberately absent from that table. It does not advance tasks; it decides
tool requests, which is [the broker's boundary](broker-service.md), not this one.

## Where the two kinds of holder meet

One registry holds both, so they exclude each other. A worker refuses to advance a task an
agent holds and says who holds it and until when; an agent's claim is refused while a worker
holds the task's lock. Both count against the [admission bound](worker-admission.md), so
four agents and four workers cannot quietly become eight.

An expired lease frees the task. A worker that takes it records a `task_ownership_taken`
event exactly as it would for a crashed worker, and repeats nothing: an operation left
unresolved still requires explicit reconciliation. Expiry gives the task back; it never
decides that interrupted work may be redone.

Renewal extends only a lease that is still live and still belongs to that agent. An expired
session cannot be renewed back to life — it has to be claimed again, which re-checks the
role, the bound and the current state.

## Running agents in containers

The lifecycle a container follows is claim, renew while working, release on the way down:

```sh
trap 'python -m orch agent-release --data "$STORE" --task "$TASK" --agent "$AGENT"' TERM INT
python -m orch agent-claim --data "$STORE" --task "$TASK" --agent "$AGENT" --role "$ROLE" --lease 900
```

Releasing on `SIGTERM` is what makes powering a container off different from losing one: a
released task is immediately free, while a lost container leaves a lease that simply expires.
Both are safe; only one is instant.

Leases run from 60 seconds to an hour, defaulting to fifteen minutes. Pick a lease a little
longer than the agent's step, not its whole session: it is a statement about how long the
task should stay held if the agent disappears, not how long the agent intends to work.

## What this is not

A session is **cooperative scheduling between agents that already run on this host under the
operator's account**. It is not an authorization boundary: nothing here authenticates the
caller, and any local process could claim any role. What an agent may actually *do* is
unchanged and is enforced elsewhere — approvals gate effects, the guardian decides tool
requests, the broker records what really ran, and every fence on scope, revision and
receipts still applies. Adding authenticated agent identity is a separate decision about
credentials, and it deserves its own review rather than arriving quietly inside this.

orchd also does not start agents, supervise them, restart them, schedule them or route work
between them. It has no polling loop and never will as part of this milestone: the agent
decides when to call, and a runtime outside this repository decides when the agent runs.
Sessions are per store on one host, like the ownership they extend.
