"""What is happening right now, for the person who turned the containers on.

The signals existed and were scattered: task states in the store, leases in the ownership
registry, capacity in each journal, quotas in the artifact table, gates in the deployment
check. Answering "is everything healthy, what are my agents doing, what is waiting on me"
meant running six commands and holding the answers in your head.

This reports all of it at once, and two decisions shape it.

**It never takes the store-wide lock and never asks for quiescence.** `storage_usage` does
both, correctly, because it is maintenance. A status view that refused to answer while work
was happening would be useless exactly when it was wanted, so this reads without excluding
anything. The cost is that it describes a moment rather than a transaction, which is what a
status view should describe anyway.

**It separates what is running from what is waiting on a person.** Those are different
questions with different answers: one tells you the machine is busy, the other tells you the
machine is stopped and you are the reason. The second is the one worth looking at in the
morning.

It changes nothing, decides nothing and authorizes nothing.
"""
import json
import sqlite3

from .agent_queue import available
from .agent_sessions import AGENT_ROLES, holder, role_for
from datetime import datetime

from . import consultations
from .contracts import Rejected, now
from .engine import TERMINAL
from .github_journal import GitHubJournal
from .jira_journal import JiraJournal

MAX_LISTED = 50
# How long after its last call an agent stops counting as present. The loop asks for work
# every fifteen seconds while idle, so four missed rounds is a container that has stopped
# rather than one that is merely between questions.
UNSEEN_SECONDS = 60


def roster(directory):
    """The agents an operator declared, or nothing when the registry cannot be read.

    Reported rather than raised, for the same reason an unopenable journal is: a status view
    that dies because one of the things it describes is broken is the opposite of useful.
    """
    if not directory:
        return None
    from .agent_service import registry
    try:
        return {name: entry["roles"] for name, entry in registry(directory).items()}
    except (Rejected, OSError):
        return {}


def parse(moment):
    return datetime.fromisoformat(str(moment).replace("Z", "+00:00"))


def tasks(store):
    """Every task by state, and the ones a person is holding up."""
    counts, waiting, active = {}, [], 0
    for row in store.db.execute("SELECT id, state FROM tasks ORDER BY id"):
        state = json.loads(row[1])
        name = state["state"]
        counts[name] = counts.get(name, 0) + 1
        if name in TERMINAL:
            continue
        active += 1
        # A state whose next step is a person's is the machine stopped, not the machine busy.
        if role_for(name) == "human" and len(waiting) < MAX_LISTED:
            waiting.append({"task_id": row[0], "state": name, "revision": state["revision"],
                            "since": state.get("created_at")})
    return {"by_state": dict(sorted(counts.items())), "total": sum(counts.values()),
            "active": active, "waiting_on_a_person": waiting}


def enrolled(store, registry):
    """Every agent an operator declared, and whether it is working, idle, or gone.

    Holding a task was the only way an agent was visible, which made a container that is
    running and correctly waiting — because there is nothing for its role to do —
    indistinguishable from one that crashed. Presence separates them.
    """
    if not registry:
        return []
    held, waiting = {}, set()
    for row in store.owners():
        if store.live(row):
            held[holder(row).get("agent")] = row["task_id"]
    for row in store.db.execute("SELECT id, state FROM tasks"):
        state = json.loads(row[1])
        if role_for(state["state"]) == "human":
            waiting.add(row[0])
    seen, moment = store.presence(), now()
    result = []
    for name in sorted(registry):
        last = (seen.get(name) or {}).get("last_seen")
        fresh = bool(last) and (parse(moment) - parse(last)).total_seconds() <= UNSEEN_SECONDS
        task = held.get(name)
        result.append({"agent": name, "roles": sorted(registry[name]), "last_seen": last,
                       "holding": task,
                       # Three states an operator acts on differently: doing something,
                       # available for something, or not there at all.
                       "state": "working" if task and fresh else "idle" if fresh else "not seen",
                       # The one that should catch the eye: this agent's task needs a person.
                       "needs_you": bool(task and task in waiting)})
    return result


def agents(store):
    """Who holds what, and what is merely recorded as having held it."""
    live, expired = [], []
    for row in store.owners():
        who = holder(row)
        entry = {"task_id": row["task_id"], "agent": who.get("agent"), "role": who.get("role"),
                 "kind": "agent" if row["expires_at"] else "worker",
                 "claimed_at": row["claimed_at"], "expires_at": row["expires_at"]}
        (live if store.live(row) else expired).append(entry)
    return {"holding": live, "held": len(live),
            # A lease that lapsed is evidence that a container stopped without releasing, and
            # the task is free again. Worth seeing; not worth alarming about.
            "lapsed": expired, "lapsed_count": len(expired)}


def work(engine):
    """What could be claimed, by role, and whether the bound would allow it.

    One pass over every claimable role rather than one pass each: the listing already reports
    the role of every task it offers, so counting them is a tally rather than a second query.
    """
    offered = available(engine, AGENT_ROLES, limit=10 ** 6)
    counts = {}
    for row in offered["available"]:
        counts[row["role"]] = counts.get(row["role"], 0) + 1
    return {"available_by_role": dict(sorted(counts.items())), "available": offered["count"],
            "admitted": offered["admitted"], "admission_bound": offered["admission_bound"],
            "at_bound": offered["at_bound"]}


def artifacts(store):
    """Quota accounting, read without excluding the work that is producing it."""
    row = store.db.execute(
        "SELECT count(*) AS items, coalesce(sum(json_extract(payload,'$.size_bytes')),0) AS bytes FROM artifacts"
    ).fetchone()
    return {"count": row["items"], "bytes": row["bytes"]}


def journal(path, family):
    """One journal's capacity, opened read-only and closed again, or why it could not be.

    A journal that cannot be opened is reported rather than raised: a status view that dies
    because one of the things it describes is broken is the opposite of useful.
    """
    opener = GitHubJournal if family == "GitHub" else JiraJournal
    try:
        book = opener(path, read_only=True)
    except (Rejected, OSError, sqlite3.Error) as exc:
        return {"family": family, "status": "unavailable", "reason": type(exc).__name__}
    try:
        usage = book.usage()
        return {"family": family, "status": "available",
                "records": usage.get("records"), "remaining_records": usage.get("remaining_records"),
                "succession": (usage.get("succession") or {}).get("status")}
    finally:
        book.close()


def report(engine, github_journal=None, jira_journal=None, registry=None):
    """Everything at once, for the operator who wants one answer rather than six."""
    result = {"kind": "OrchdStatus", "checked_at": now(), "tasks": tasks(engine.store),
              "agents": agents(engine.store), "enrolled": enrolled(engine.store, registry),
              "work": work(engine), "consultations": consultations.summary(engine.store),
              "artifacts": artifacts(engine.store), "journals": [],
              "live_authorized": False, "retry_allowed": False}
    for path, family in ((github_journal, "GitHub"), (jira_journal, "Jira")):
        if path:
            result["journals"].append(journal(path, family))
    # One line an operator can read without reading the rest of it.
    waiting = len(result["tasks"]["waiting_on_a_person"])
    result["summary"] = {
        "tasks_active": result["tasks"]["active"],
        "waiting_on_you": waiting,
        "agents_holding": result["agents"]["held"],
        "work_available": result["work"]["available"],
        "at_bound": result["work"]["at_bound"],
        # An answer nobody has read is waiting for a person as surely as an approval is,
        # and unlike an approval it is finished work sitting unlooked-at.
        "answers_to_read": result["consultations"]["answered"],
        "questions_open": result["consultations"]["open"],
        "attention": bool(waiting or result["work"]["at_bound"] or result["agents"]["lapsed_count"]
                          or result["consultations"]["answered"]),
        "agents_enrolled": len(result["enrolled"]),
        "agents_present": sum(1 for row in result["enrolled"] if row["state"] != "not seen"),
    }
    return result
