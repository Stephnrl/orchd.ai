"""A role-scoped, expiring claim that an agent holds across separate invocations.

[Task ownership](../docs/task-ownership.md) claims a task with a file lock held by the
process doing the work, which is exactly right for a worker process and wrong for an agent.
An agent running in its own container calls this CLI, exits, thinks, and calls again; there
is no resident process, so between calls it would hold nothing at all.

An agent session is the other kind of holder in the same registry: granted to a named agent
in a declared role, live until it expires, renewed while the agent is working and released
when its container stops. Both kinds count against the [admission
bound](../docs/worker-admission.md), so four agents and four workers cannot quietly become
eight.

A session is cooperative scheduling between agents that already run on this host under the
operator's own account. It is not an authorization boundary: it does not authenticate the
caller, and any local process could claim any role. What an agent may actually *do* stays
where it already is — approvals gate effects, the guardian decides tool requests, and the
broker records what really ran.
"""
import re

from .contracts import Rejected, canonical, now, uid

# The role each state needs next, from the roles the engine actually invokes: planning is
# the lead planner's, implementation the junior's, review the reviewer's, and every
# approval pause is a human's. Terminal states need nobody.
ROLE_FOR_STATE = {
    "DRAFT_SPEC": "project_manager",
    "AWAITING_CLARIFICATION": "project_manager",
    "SPEC_READY": "lead_planner",
    "PLANNING": "lead_planner",
    "AWAITING_PLAN_APPROVAL": "human",
    "READY_FOR_IMPLEMENTATION": "junior",
    "CHANGES_REQUESTED": "junior",
    "IMPLEMENTING": "junior",
    "TESTING": "test_runner",
    "REVIEWING": "reviewer",
    "AWAITING_ACTION_APPROVAL": "human",
    "PR_CREATED": "action_broker",
    "BLOCKED": "human",
}
ROLES = frozenset(ROLE_FOR_STATE.values()) | {"guardian", "orchestrator", "lead_clarifier"}
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MIN_LEASE, MAX_LEASE, DEFAULT_LEASE = 60, 3600, 900


def identifier(agent):
    if not isinstance(agent, str) or not AGENT.fullmatch(agent):
        raise Rejected("An agent name is 1-64 characters of letters, digits, dashes or underscores")
    return agent


def session(agent, role):
    """The owner string a session records, distinct from a worker process's."""
    if role not in ROLES:
        raise Rejected("Unknown agent role")
    return canonical({"agent": identifier(agent), "role": role, "session": uid()}).decode()


def duration(seconds):
    if seconds is None:
        return DEFAULT_LEASE
    if type(seconds) is not int or not MIN_LEASE <= seconds <= MAX_LEASE:
        raise Rejected("A lease runs for %d to %d seconds" % (MIN_LEASE, MAX_LEASE))
    return seconds


def role_for(state):
    """Which role the task's next step belongs to, or None when it needs nobody."""
    return ROLE_FOR_STATE.get(state)


def claim(engine, task, agent, role, seconds=None, expected_revision=None):
    """Take a lease on a task whose next step belongs to this agent's role."""
    seconds = duration(seconds)
    owner = session(agent, role)
    with engine.deciding(task):
        state, _ = engine.store.task(task)
        if expected_revision is not None and state["revision"] != expected_revision:
            raise Rejected("Task revision changed; read it again before claiming")
        wanted = role_for(state["state"])
        if wanted is None:
            raise Rejected("That task is finished and needs no agent")
        if wanted != role:
            raise Rejected("That task's next step belongs to " + wanted + ", not " + role)
        held = engine.store.owner(task)
        if held and engine.store.live(held) and held["owner"] != owner:
            raise Rejected("That task is already held by " + describe(held))
        active = engine.store.admitted(task)
        if len(active) >= engine.admission_bound():
            raise Rejected("Too many tasks are being advanced at once (%d); wait for one to finish"
                           % len(active))
        with engine.store.transaction():
            engine.store.claim(task, owner, state["revision"],
                               (held["generation"] + 1) if held else 1, expires_in=seconds)
        return report(engine, task, "claimed")


def renew(engine, task, agent, seconds=None):
    """Extend a lease this agent still holds. An expired one must be claimed again."""
    seconds = duration(seconds)
    with engine.deciding(task):
        held = engine.store.owner(task)
        if not held or not held["expires_at"]:
            raise Rejected("No agent session holds that task")
        if holder(held)["agent"] != identifier(agent):
            raise Rejected("That session belongs to " + describe(held))
        if not engine.store.live(held):
            raise Rejected("That session expired; claim the task again")
        with engine.store.transaction():
            engine.store.claim(task, held["owner"], held["revision"], held["generation"],
                               expires_in=seconds)
        return report(engine, task, "renewed")


def release(engine, task, agent):
    """Give a task back, which is what a container does on its way down."""
    with engine.deciding(task):
        held = engine.store.owner(task)
        if not held or not held["expires_at"]:
            raise Rejected("No agent session holds that task")
        if holder(held)["agent"] != identifier(agent):
            raise Rejected("That session belongs to " + describe(held))
        with engine.store.transaction():
            engine.store.disclaim(task, held["owner"])
        return {"kind": "AgentSession", "status": "released", "task_id": task,
                "agent": identifier(agent), "released_at": now(),
                "retry_allowed": False, "live_authorized": False}


def sessions(store):
    """Every current holder, so an operator can see who has what."""
    result = []
    for row in store.owners():
        who = holder(row)
        result.append({"task_id": row["task_id"], "agent": who.get("agent"), "role": who.get("role"),
                       "kind": "agent" if row["expires_at"] else "worker",
                       "claimed_at": row["claimed_at"], "expires_at": row["expires_at"],
                       "revision": row["revision"], "generation": row["generation"],
                       "live": store.live(row)})
    return {"kind": "AgentSessions", "generated_at": now(), "sessions": result,
            "held": sum(1 for row in result if row["live"]),
            "retry_allowed": False, "live_authorized": False}


def holder(row):
    try:
        import json
        value = json.loads(row["owner"])
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def describe(row):
    who = holder(row)
    if who.get("agent"):
        return "agent " + who["agent"] + " (" + str(who.get("role")) + ")"
    return "a worker process"


def report(engine, task, status):
    row = engine.store.owner(task)
    who = holder(row)
    return {"kind": "AgentSession", "status": status, "task_id": task, "agent": who.get("agent"),
            "role": who.get("role"), "claimed_at": row["claimed_at"], "expires_at": row["expires_at"],
            "task_revision": row["revision"], "generation": row["generation"],
            "retry_allowed": False, "live_authorized": False}
