"""The work an agent could take, which until now it had no way to find.

An agent could claim a task, hold it, work on it and release it — provided somebody told it
which task. `agent-sessions` lists what is *held*; nothing listed what was available. So a
project manager container starting in the morning had to be handed identifiers out of band,
which is most of the way back to an operator driving it by hand.

This lists what an agent could claim, and the shape of that listing is the design.

**Only work this agent could take.** The listing is filtered to the roles the agent is
enrolled for, not to every task in the store. A listing of work an agent can never claim
tells it about tasks it has no business knowing exist, and the whole boundary so far has been
that an agent sees what it may act on and nothing else.

**Identifiers and state, never content.** No title, no request text, no acceptance criteria.
An agent claims first and reads with `agent-spec`. Otherwise this route becomes a way to read
every task in the store without ever holding one, which is exactly the hole that holding is
supposed to close.

**Nothing waiting on a person.** A human approval pause belongs to nobody an agent can be, so
it never appears here for any role.

**The bound, alongside.** When [admission](../docs/worker-admission.md) is full a claim will
be refused, and an agent that knows this waits rather than spinning through a list it cannot
act on.
"""
import json

from .agent_sessions import ROLE_FOR_STATE, role_for

MAX_LISTED = 50


def available(engine, roles, limit=MAX_LISTED):
    """Tasks whose next step belongs to one of these roles and which nobody holds.

    Ordered by task identifier so two agents asking at once see the same list in the same
    order; which of them wins a contested task is settled by the claim, not by the listing.
    """
    wanted = {role for role in roles if role in set(ROLE_FOR_STATE.values()) and role != "human"}
    bound = engine.admission_bound()
    held = {}
    for row in engine.store.owners():
        if engine.store.live(row):
            held[row["task_id"]] = row
    result = []
    for row in engine.store.db.execute("SELECT id, state FROM tasks ORDER BY id"):
        task, state = row[0], json.loads(row[1])
        role = role_for(state["state"])
        # A terminal task needs nobody; a human pause belongs to nobody an agent can be.
        if role is None or role == "human" or role not in wanted:
            continue
        if task in held:
            continue
        result.append({"task_id": task, "state": state["state"], "role": role,
                       "revision": state["revision"]})
        if len(result) >= limit:
            break
    return {"kind": "AgentAvailableWork", "roles": sorted(wanted), "available": result,
            "count": len(result), "admitted": len(held), "admission_bound": bound,
            # An agent that knows the bound is full waits instead of claiming into a refusal.
            "at_bound": len(held) >= bound, "live_authorized": False, "retry_allowed": False}
