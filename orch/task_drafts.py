"""The project manager's step, which until now the kernel performed on their behalf.

`DRAFT_SPEC` and `AWAITING_CLARIFICATION` have been states since the first design, and
`ClarificationRequest` and `ClarificationResponse` have been contracts for just as long, but
nothing ever entered those states or wrote those records. `create_task` invented a
specification, wrote `confirmed_by`, set `spec_confirmed` and moved to `SPEC_READY` inside
one transaction — so the evidence said a human had confirmed a specification in the same
breath as inventing it. For a fixture that is harmless. As the foundation for an agent that
actually drafts specifications it is a fail-open in the one place that matters most, because
every later gate takes the specification as given.

This module makes the stage real. A task can be opened with no specification at all; a
project manager drafts one and revises it; questions it cannot answer itself move the task
to `AWAITING_CLARIFICATION` until a human answers them; and reaching `SPEC_READY` requires a
human to confirm the exact specification they read, named by its hash. Revising after a
human has read it invalidates nothing and refuses nothing — it simply means the hash they
hold is no longer the specification, and their confirmation will not apply to it.

Drafting is ordinary work that any holder of the task may do. Confirming is not: it is the
human boundary, and nothing an agent can reach through [the agent API](../docs/agent-api.md)
performs it.
"""
import re

from .contracts import Rejected, make, redact
from .fixtures import repository

MAX_TITLE = 200
MAX_REQUEST = 4000
MAX_LINE = 500
MAX_QUESTION = 500
MAX_ANSWER = 2000
MAX_QUESTIONS = 10
# One specification, not a programme of work. A draft that needs more than this is two tasks.
LIMITS = {"acceptance_criteria": 20, "constraints": 20, "allowed_paths": 50, "external_references": 20}
WRITTEN = ("title", "request") + tuple(LIMITS)
PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def principal(value):
    """Who a human boundary crossing is attributed to."""
    if not isinstance(value, str) or not PRINCIPAL.fullmatch(value):
        raise Rejected("A principal is 1 to 128 characters of letters, digits, dashes or underscores")
    return value


def text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected("A specification's " + field + " is 1 to " + str(limit) + " characters")
    if redact(value) != value:
        raise Rejected("A specification must not contain a secret")
    return value


def lines(value, field):
    limit = LIMITS[field]
    if not isinstance(value, list) or len(value) > limit:
        raise Rejected("A specification's " + field + " is a list of at most " + str(limit) + " entries")
    result = [text(entry, field, MAX_LINE) for entry in value]
    if len(set(result)) != len(result):
        raise Rejected("A specification's " + field + " repeats an entry")
    return result


def written(value):
    """The parts of a specification a project manager writes, checked before anything is stored.

    Acceptance criteria are required because a specification nobody can test is not a
    specification; the other lists may be empty, since a task genuinely may have no
    constraint or no external reference.
    """
    if not isinstance(value, dict):
        raise Rejected("A draft is an object of specification fields")
    unknown = sorted(set(value) - set(WRITTEN))
    if unknown:
        raise Rejected("A draft has no field " + unknown[0])
    result = {"title": text(value.get("title"), "title", MAX_TITLE),
              "request": text(value.get("request"), "request", MAX_REQUEST)}
    for field in LIMITS:
        result[field] = lines(value.get(field, []), field)
    if not result["acceptance_criteria"]:
        raise Rejected("A specification states at least one acceptance criterion")
    return result


def questions(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise Rejected("Ask 1 to " + str(MAX_QUESTIONS) + " questions at a time")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or sorted(entry) != ["question", "question_id"]:
            raise Rejected("A question is an object of question_id and question")
        if not IDENTIFIER.fullmatch(str(entry["question_id"])):
            raise Rejected("A question_id is 1 to 128 characters of letters, digits, dashes or underscores")
        result.append({"question_id": entry["question_id"],
                       "question": text(entry["question"], "question", MAX_QUESTION)})
    if len({entry["question_id"] for entry in result}) != len(result):
        raise Rejected("Two questions share an identifier")
    return result


def answers(value, asked):
    """Answers to exactly the questions that were asked, so none is silently dropped."""
    if not isinstance(value, list):
        raise Rejected("Answers are a list of question_id and answer")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or sorted(entry) != ["answer", "question_id"]:
            raise Rejected("An answer is an object of question_id and answer")
        result.append({"question_id": entry["question_id"],
                       "answer": text(entry["answer"], "answer", MAX_ANSWER)})
    given = [entry["question_id"] for entry in result]
    if sorted(given) != sorted(asked):
        raise Rejected("Answer every question that was asked, and only those")
    return result


def drafting(state, expected_revision):
    """A task is draftable while it is in DRAFT_SPEC and nowhere else."""
    if expected_revision is not None and state["revision"] != expected_revision:
        raise Rejected("Task revision changed; read the task again")
    if state["state"] != "DRAFT_SPEC":
        raise Rejected("That task is in " + state["state"] + ", not DRAFT_SPEC")


def summary(engine, task, state, context):
    """What the caller needs in order to decide what to do next, including the fence."""
    spec = engine.store.get(state["spec"], task, "TaskSpec") if state["spec"] else None
    return {"kind": "TaskDraft", "task_id": task, "state": state["state"],
            "revision": state["revision"], "title": spec["title"] if spec else context.get("draft_title"),
            "spec": state["spec"], "spec_revision": spec["revision"] if spec else None,
            "spec_sha256": state["spec"]["sha256"] if state["spec"] else None,
            "confirmed": bool(context.get("spec_confirmed")),
            "pending_questions": context.get("clarification_questions") or [],
            "live_authorized": False}


def draft(engine, task, value, expected_revision=None):
    """Write the next revision of a specification, leaving every earlier one in place."""
    content = written(value)
    with engine.deciding(task), engine.store.transaction():
        state, context = engine.store.task(task)
        drafting(state, expected_revision)
        previous = engine.store.get(state["spec"], task, "TaskSpec") if state["spec"] else None
        spec = make("TaskSpec", task, revision=(previous["revision"] + 1) if previous else 0,
                    title=content["title"],
                    request=engine.store.artifact(task, content["request"], "text/plain"),
                    repository=repository(task),
                    acceptance_criteria=content["acceptance_criteria"],
                    constraints=content["constraints"],
                    allowed_paths=content["allowed_paths"],
                    external_references=content["external_references"],
                    # Nobody has confirmed a draft. The contract has always allowed this.
                    confirmed_by=None)
        state["spec"] = engine.store.put(spec)
        state["revision"] += 1
        engine._event(state, context, "spec_drafted",
                      {"spec": state["spec"], "spec_revision": spec["revision"]}, role="project_manager")
        return summary(engine, task, state, context)


def ask(engine, task, value, expected_revision=None):
    """Raise questions about the current draft and stop until a human answers them.

    The questions are bound to the draft they are about, because an answer to a question
    about one specification is not evidence about a later one.
    """
    asked = questions(value)
    with engine.deciding(task), engine.store.transaction():
        state, context = engine.store.task(task)
        drafting(state, expected_revision)
        if not state["spec"]:
            raise Rejected("Draft a specification before asking about it")
        request = make("ClarificationRequest", task, spec=state["spec"],
                       # No provider invocation produced these, so the operation that asked
                       # names itself. The field has always been the identifier to trace back to.
                       invocation_id=engine.store.get(state["spec"], task, "TaskSpec")["id"],
                       questions=asked)
        context["clarification_request"] = engine.store.put(request)
        context["clarification_questions"] = asked
        engine._event(state, context, "clarification_requested",
                      {"request": context["clarification_request"],
                       "questions": [entry["question_id"] for entry in asked]}, role="project_manager")
        engine._transition(state, context, "AWAITING_CLARIFICATION")
        return summary(engine, task, state, context)


def answer(engine, task, value, who, expected_revision=None):
    """A human answers, and the task goes back to being drafted. Human boundary only."""
    who = principal(who)
    with engine.deciding(task), engine.store.transaction():
        state, context = engine.store.task(task)
        if expected_revision is not None and state["revision"] != expected_revision:
            raise Rejected("Task revision changed; read the task again")
        if state["state"] != "AWAITING_CLARIFICATION":
            raise Rejected("That task is not waiting for an answer")
        pending = context.get("clarification_request")
        if not pending:
            raise Rejected("That task records no question to answer")
        request = engine.store.get(pending, task, "ClarificationRequest")
        given = answers(value, [entry["question_id"] for entry in request["questions"]])
        response = make("ClarificationResponse", task, request=pending,
                        actor={"principal_id": who, "role": "human"}, answers=given)
        context["clarification_response"] = engine.store.put(response)
        context.pop("clarification_questions", None)
        context.pop("clarification_request", None)
        # Recorded before the transition it causes, so the history reads in the order the
        # events actually happened: the answer arrives, and then the task goes back to draft.
        engine._event(state, context, "clarification_answered",
                      {"response": context["clarification_response"], "principal": who}, role="human")
        engine._transition(state, context, "DRAFT_SPEC")
        return summary(engine, task, state, context)


def confirm(engine, task, sha256, who, expected_revision=None):
    """Confirm the exact specification that was read, and let planning begin.

    The hash is the whole point. A human reads a specification, and confirms *that* one; if
    it was revised in between, the hash they hold names a specification that is no longer
    current and the confirmation is refused rather than silently applied to different words.
    """
    who = principal(who)
    if not isinstance(sha256, str) or not re.fullmatch("[0-9a-f]{64}", sha256):
        raise Rejected("Confirm a specification by the sha256 of the one you read")
    with engine.deciding(task), engine.store.transaction():
        state, context = engine.store.task(task)
        drafting(state, expected_revision)
        if not state["spec"]:
            raise Rejected("There is no specification to confirm")
        if state["spec"]["sha256"] != sha256:
            raise Rejected("That specification has been revised since you read it; read it again")
        spec = engine.store.get(state["spec"], task, "TaskSpec")
        # Confirmation is recorded as a new revision naming who confirmed it, so the
        # confirmed specification and the draft that preceded it are both kept.
        confirmed = make("TaskSpec", task, revision=spec["revision"] + 1, title=spec["title"],
                         request=spec["request"], repository=spec["repository"],
                         acceptance_criteria=spec["acceptance_criteria"], constraints=spec["constraints"],
                         allowed_paths=spec["allowed_paths"], external_references=spec["external_references"],
                         confirmed_by=who)
        state["spec"] = engine.store.put(confirmed)
        context["spec_confirmed"] = True
        context["confirmed_sha256"] = sha256
        state["revision"] += 1
        engine._event(state, context, "spec_confirmed",
                      {"principal": who, "spec": state["spec"], "confirmed_draft": sha256}, role="human")
        engine._transition(state, context, "SPEC_READY")
        return summary(engine, task, state, context)


def show(engine, task):
    """The current draft and the hash a human would confirm it by."""
    state, context = engine.store.task(task)
    result = summary(engine, task, state, context)
    if state["spec"]:
        spec = engine.store.get(state["spec"], task, "TaskSpec")
        result["specification"] = {field: spec[field] for field in
                                   ("title", "acceptance_criteria", "constraints", "allowed_paths",
                                    "external_references", "revision", "confirmed_by")}
        result["request"] = engine.store.read_artifact(spec["request"], task)
    return result
