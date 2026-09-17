"""A question to a role, and the answer it gets. Deliberately not a task.

Everything else here exists to reach an approved effect: a task is specified, planned,
implemented, reviewed and dispatched, and a person stands at each gate. A question is not
that. "What are your inputs if I deployed this?" changes nothing, so there is nothing to
approve — and an approval gate on a question would be theatre that teaches people to click
through gates.

That only holds while a consultation *cannot* become an effect. If a consultation were a task
with two extra states, someone could later add an edge from answered into the workflow and the
argument would quietly stop being true. So consultations live here, in their own table, with
their own small lifecycle and **no route into the task state machine at all**: nothing in this
module creates a task, a `ToolRequest`, an `ExternalActionReceipt` or an approval, and
`tests/test_consultations.py` asserts that a consultation leaves none of them behind.

An answer is an agent's opinion. It is recorded as what it is — untrusted output, attributed,
timestamped — and it authorizes nothing. Reading one and then deciding to act is a person's
job, and acting is a task, which has its own gates.

A consultation may *reference* a task, because "the junior opened this, what do you think?" is
a real question. The reference is read-only context: the answer still cannot approve, advance
or change the task it discusses.
"""
import json

from .agent_sessions import AGENT_ROLES, identifier
from .contracts import Rejected, now, redact, uid

MAX_QUESTION = 4000
# An answer has to fit in one authenticated request, whatever alphabet it is written in. The
# envelope bound is 16 KiB of canonical JSON and a character outside ASCII costs three bytes
# there, so this is the length that can always be sent rather than the length that usually
# can: an agent whose answer was refused for being long would simply not answer, and nobody
# would be told why. It is an opinion, not a report.
MAX_ANSWER = 5000
MAX_OPEN = 100
MAX_LISTED = 50
# An agent that took a question and stopped existing should not hold it for ever. This is not
# a lease in the ownership registry, because a consultation is not a task and borrowing that
# machinery would be the first step towards it becoming one.
ANSWERING_SECONDS = 900
STATES = ("asked", "answering", "answered", "withdrawn", "closed")


def text(value, field, limit):
    """Accepted, bounded, and never storing something shaped like a credential.

    A specification *refuses* text that looks like a secret. Here that would be the wrong
    trade: the first real use of this is "look at my terraform and tell me what you think",
    and terraform is full of lines like `password = var.db_password`. Refusing would make the
    feature useless for the thing it was asked for, and refusing an answer throws away work
    that has already been done. So the text is redacted instead, and the record says it was,
    because silently altering something a person will read as quoted is its own problem.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected("A consultation's " + field + " is 1 to " + str(limit) + " characters")
    if any(ord(character) < 32 and character not in "\n\t" or ord(character) == 127 for character in value):
        raise Rejected("Unsupported control character")
    return redact(value)


def consultable(role):
    """A role a question may be put to: one an agent can actually hold."""
    if role not in AGENT_ROLES:
        raise Rejected("No agent works as " + repr(role) + "; ask a role an agent may hold")
    return role


def ask(store, role, question, asked_by, about=None):
    """Put a question to a role. Nothing is approved because nothing will happen."""
    from .task_drafts import principal
    role, asked_by = consultable(role), principal(asked_by)
    kept = text(question, "question", MAX_QUESTION)
    with store.transaction():
        if about is not None:
            # Read-only context. Checking it exists here means a question cannot name a task
            # that does not, which would make the answer unreadable later.
            store.task(about)
        open_now = store.db.execute(
            "SELECT count(*) FROM consultations WHERE state IN ('asked','answering')").fetchone()[0]
        if open_now >= MAX_OPEN:
            raise Rejected("There are already %d open consultations; answer or withdraw some" % MAX_OPEN)
        record = {"id": uid(), "role": role, "question": kept, "asked_by": asked_by,
                  "about": about, "asked_at": now(), "state": "asked", "agent": None,
                  "taken_at": None, "answer": None, "answered_at": None,
                  "redacted": kept != question}
        store.db.execute("INSERT INTO consultations VALUES(?,?,?)",
                         (record["id"], record["state"], json.dumps(record)))
    return reported(record)


def get(store, consultation):
    row = store.db.execute("SELECT payload FROM consultations WHERE id=?", (consultation,)).fetchone()
    if not row:
        raise Rejected("Unknown consultation")
    return json.loads(row[0])


def save(store, record):
    """Write one consultation back. Callers hold the transaction that read it."""
    store.db.execute("UPDATE consultations SET state=?, payload=? WHERE id=?",
                     (record["state"], json.dumps(record), record["id"]))
    return reported(record)


def stale(record):
    """An agent took this and never came back."""
    if record["state"] != "answering" or not record["taken_at"]:
        return False
    from datetime import datetime
    started = datetime.fromisoformat(record["taken_at"].replace("Z", "+00:00"))
    return (datetime.fromisoformat(now().replace("Z", "+00:00")) - started).total_seconds() > ANSWERING_SECONDS


def open_for(store, roles, limit=MAX_LISTED):
    """Questions an agent with these roles could answer, and none it could not.

    Scoped the same way [available work](../docs/agent-queue.md) is, and for the same reason: a
    listing of questions an agent can never answer tells it about things it has no business
    knowing were asked.
    """
    wanted = {role for role in roles if role in AGENT_ROLES}
    result = []
    for row in store.db.execute("SELECT payload FROM consultations WHERE state IN ('asked','answering') ORDER BY id"):
        record = json.loads(row[0])
        if record["role"] not in wanted:
            continue
        if record["state"] == "answering" and not stale(record):
            continue
        result.append({"consultation_id": record["id"], "role": record["role"],
                       "question": record["question"], "about": record["about"],
                       "asked_at": record["asked_at"]})
        if len(result) >= limit:
            break
    return result


def take(store, consultation, agent, roles=()):
    """Say you are answering, so two agents of one role do not both write an opinion."""
    agent = identifier(agent)
    with store.transaction():
        record = get(store, consultation)
        answerable(record, agent, roles)
        if record["state"] == "answering" and record["agent"] != agent and not stale(record):
            raise Rejected("Another agent is already answering that")
        record.update(state="answering", agent=agent, taken_at=now())
        return save(store, record)


def answerable(record, agent, roles):
    """Whether this agent may answer at all: the role the question was put to is its own.

    The service already knows an agent's roles from its own configuration rather than from
    the request, so this is the same gate claiming work passes, applied to opinions.
    """
    if roles and record["role"] not in {role for role in roles if role in AGENT_ROLES}:
        raise Rejected("Agent " + agent + " does not work as " + record["role"])
    if record["state"] == "answered":
        raise Rejected("That consultation is already answered")
    if record["state"] == "withdrawn":
        raise Rejected("That consultation was withdrawn")


def answer(store, consultation, agent, reply, roles=()):
    """Record an agent's opinion as an opinion. It approves nothing and advances nothing."""
    agent = identifier(agent)
    kept = text(reply, "answer", MAX_ANSWER)
    with store.transaction():
        record = get(store, consultation)
        answerable(record, agent, roles)
        if record["state"] == "answering" and record["agent"] != agent and not stale(record):
            raise Rejected("Another agent is answering that")
        record.update(state="answered", agent=agent, answer=kept, answered_at=now(),
                      redacted=record.get("redacted", False) or kept != reply)
        return save(store, record)


def withdraw(store, consultation, asked_by):
    """Take a question back. A withdrawn question is never answered."""
    from .task_drafts import principal
    asked_by = principal(asked_by)
    with store.transaction():
        record = get(store, consultation)
        # Every state but the two open ones, so a question that was answered — or read, or
        # already taken back — is not quietly withdrawn out from under its answer.
        if record["state"] not in ("asked", "answering"):
            raise Rejected("Only a question nobody has answered is withdrawn; that one is "
                           + record["state"])
        record.update(state="withdrawn", withdrawn_by=asked_by, withdrawn_at=now())
        return save(store, record)


def close(store, consultation, by):
    """Say you have read an answer, so the window can stop asking you to.

    Without this an answered question needs you for ever, and a signal that is always on is a
    signal nobody reads. It is a person saying "I have seen this" and nothing else: it changes
    no task, and a closed consultation is still there to read.
    """
    from .task_drafts import principal
    by = principal(by)
    with store.transaction():
        record = get(store, consultation)
        if record["state"] != "answered":
            raise Rejected("Only an answered consultation is closed; that one is " + record["state"])
        record.update(state="closed", closed_by=by, closed_at=now())
        return save(store, record)


def reported(record):
    """What a consultation looks like to anybody reading it."""
    return {"kind": "Consultation", "consultation_id": record["id"], "role": record["role"],
            "question": record["question"], "about": record["about"],
            "asked_by": record["asked_by"], "asked_at": record["asked_at"],
            "state": record["state"], "agent": record["agent"],
            "answer": record["answer"], "answered_at": record["answered_at"],
            # Said plainly wherever an answer is read: this is what an agent thinks, and
            # acting on it is a separate decision with its own gates.
            "answer_is_untrusted": True, "approves_nothing": True,
            "redacted": bool(record.get("redacted")),
            "live_authorized": False, "retry_allowed": False}


def listed(store, limit=MAX_LISTED, state=None):
    """Every consultation, newest question last, for an operator reading the room."""
    if state is not None and state not in STATES:
        raise Rejected("Unknown consultation state")
    rows = store.db.execute(
        "SELECT payload FROM consultations" + (" WHERE state=?" if state else "") + " ORDER BY id",
        (state,) if state else ()).fetchall()
    records = [reported(json.loads(row[0])) for row in rows][:limit]
    return {"kind": "Consultations", "consultations": records, "count": len(records),
            "open": sum(1 for row in records if row["state"] in ("asked", "answering")),
            "answered": sum(1 for row in records if row["state"] == "answered"),
            "live_authorized": False, "retry_allowed": False}


def summary(store):
    """Two numbers for a status view: what is waiting for an agent, and what is waiting for you.

    An answered consultation nobody has read is the one worth surfacing, which is what
    closing one is for: until a person says they have read it, it is still waiting for them.
    """
    counts = {row[0]: row[1] for row in store.db.execute(
        "SELECT state, count(*) FROM consultations GROUP BY state")}
    return {"open": counts.get("asked", 0) + counts.get("answering", 0),
            "answered": counts.get("answered", 0), "closed": counts.get("closed", 0),
            "total": sum(counts.values())}
