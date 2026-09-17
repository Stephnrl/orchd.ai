---
name: orchd-protocol
description: How to hold, renew and release work through the orchd control plane, and what to do when it refuses you. Read this before claiming a task, whenever a claim or renewal is refused, and before any action that would have an effect outside your own container.
---

# Working through orchd

orchd is not your runtime. It does not start you, prompt you, or run your tools. It decides
whether you may hold a piece of work right now, and it records what actually happened. Other
containers may be running beside you; orchd is what keeps you from colliding with them.

This file is the whole protocol you need in order to work. The wire format, the full state
table and every refusal are in `reference.md` beside this file. Read that when you need it,
not before.

## Four rules

**You never open the store.** Your container is given an endpoint and a secret file, and
nothing else. It is not given a store path. If you find one anyway — mounted, inherited, or
guessed — you still do not use it. Every answer you need comes from the endpoint.

**You never state which agent you are.** No request has a field for it. The secret that
signs your request is your identity, decided by the service and not by you. There is no way
to act as another agent, so do not build one.

**A refusal is an answer, not an obstacle.** orchd fails closed: when it refuses, the thing
you asked for did not happen and will not happen by asking differently. Do not reword it,
split it, or route around it. **A refusal carries `retry_allowed`**: when it is false, stop
and report the refusal to whoever gave you the work. When it is true, the state that refused
you may change on its own — the task frees, the bound empties — so wait, then ask again
unchanged. On a successful reply the same field is always false and means nothing; it is the
refusal that carries the answer.

**You hold work only while your lease is live.** A claim grants a lease measured in seconds,
not a lock held by your process. If you stop renewing it, it expires and the task becomes
someone else's. Release it deliberately when you finish or when your container is stopping.

## The loop

```sh
orchd() { python -m orch "$@" --agent-endpoint "$ORCHD_ENDPOINT" --agent-secret "$ORCHD_SECRET"; }

orchd agent-available                                   # what you could take
orchd agent-sessions                                    # what is held right now
orchd agent-claim --task TASK --role ROLE --lease 900   # ask to hold one task
orchd agent-spec --task TASK                            # read where the work has got to
orchd agent-renew --task TASK --lease 900               # while you are still working
orchd agent-release --task TASK                         # when you are done, or stopping
orchd agent-consultations                              # questions for your role
orchd agent-consult-answer --consultation ID --answer T # an opinion, not a decision
```

Claim before you do anything to a task, renew well before the lease ends, and release even
when the work failed. A claim you never release is not a safe default; it is a task nobody
can pick up until it expires.

Start from `agent-available`. It lists only work your roles are next for, and only what
nobody holds, so anything it offers is genuinely yours to claim. When it reports `at_bound`,
the control plane is already advancing as many tasks as it allows: wait, rather than claiming
into a refusal.

**Holding the task is what lets you work on it.** The routes that write — drafting a
specification, raising a question — all require a live lease held by you, on that task. You
cannot write to a task you have not claimed, one someone else holds, or one whose lease ran
out while you were thinking. If a write is refused, read `agent-spec` before assuming why.

## Two gates, and neither is yours

A claim passes only when **both** are true: your agent is enrolled for the role you asked
for, and that role is the one the task's next step belongs to. You cannot see or change
either. A refusal on the first means you asked for work that is not yours; on the second it
means the task is not at a step you do. Both are ordinary, and neither is an error in your
reasoning.

The roles orchd knows are `project_manager`, `lead_clarifier`, `lead_planner`, `junior`,
`reviewer`, `guardian`, `test_runner`, `orchestrator`, `action_broker` and `human`. You work
as the one your container was enrolled for. `human` steps are not yours to take under any
role.

## What orchd will not do for you

Holding a task is permission to work on it, not permission to act. Anything with an effect
outside your container — opening a pull request, writing to a tracker, running a live
command — passes an approval that you do not grant and cannot reach. If your work is ready
for one, say so and stop. Waiting for a human is a finished turn, not a blocked one.
