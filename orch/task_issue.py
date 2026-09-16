"""The GitHub issue a confirmed specification implies, derived rather than written.

An agent drafts a specification and a person confirms it by hash. Tracking that work as an
issue then needs an intent — and the obvious way to get one is to let the agent write it.
That would undo what the confirmation is for: a human would have confirmed one set of words
and an agent would have put different ones in the repository, with nothing relating them.

So nothing writes the issue. It is **derived** from the confirmed specification, by a pure
function of what a human signed. The issue says what was confirmed, because it cannot say
anything else.

Two consequences follow, and the second is the useful one.

An agent cannot put arbitrary text in a repository even indirectly. The only route from an
agent's keyboard to a GitHub issue runs through a specification a person read and confirmed
by its hash.

And the operation identifier is derived too, from the task and the confirmed specification's
digest. So deriving twice gives the same operation, which means an interrupted dispatch
**resumes** rather than starting a new one: the marker the first attempt would have written is
the marker the second one searches for. Reconciliation finds the issue that exists instead of
filing a second.
"""
from .contracts import Rejected, digest
from .github_issues import MAX_INTENT_BYTES, validate_intent, validate_profile

# The issue body is the request, then the criteria, then the constraints — always in that
# order, always with these headings, so the same specification always renders the same bytes.
SECTIONS = (("acceptance_criteria", "Acceptance criteria"), ("constraints", "Constraints"))
MAX_BODY = 16384


def rendered(spec, request_text):
    """The body a confirmed specification implies, in one fixed order."""
    parts = [request_text.strip()]
    for field, heading in SECTIONS:
        entries = spec.get(field) or []
        if entries:
            parts.append("## " + heading + "\n" + "\n".join("- " + entry for entry in entries))
    body = "\n\n".join(parts)
    if len(body.encode()) > MAX_BODY:
        # Trimming would mean the issue no longer says what was confirmed, which is the one
        # thing this derivation exists to guarantee.
        raise Rejected("This specification renders an issue body over the "
                       + str(MAX_BODY) + " byte budget; shorten the specification")
    return body


def operation_for(task, spec_sha256):
    """The operation this specification implies, so deriving twice resumes rather than repeats."""
    return digest({"task_id": task, "spec_sha256": spec_sha256, "purpose": "github-issue"})[:32]


def derive(engine, task, profile):
    """The issue intent a confirmed specification implies. Nothing here is written by anyone."""
    validate_profile(profile)
    state, context = engine.store.task(task)
    if not state["spec"]:
        raise Rejected("That task has no specification")
    spec = engine.store.get(state["spec"], task, "TaskSpec")
    if not spec["confirmed_by"] or not context.get("spec_confirmed"):
        # An unconfirmed draft is a proposal, not a decision, and an issue is an effect.
        raise Rejected("Only a confirmed specification implies an issue; confirm it first")
    intent = {"schema_version": "1.0.0", "task_id": task,
              "operation_id": operation_for(task, state["spec"]["sha256"]),
              "repository": profile["repository"], "title": spec["title"],
              "body": rendered(spec, engine.store.read_artifact(spec["request"], task)),
              "labels": []}
    validate_intent(intent, profile)
    if len(str(intent).encode()) > MAX_INTENT_BYTES:
        raise Rejected("Derived issue intent exceeds its budget")
    return {"kind": "DerivedIssueIntent", "intent": intent, "task_id": task,
            "spec_sha256": state["spec"]["sha256"], "confirmed_by": spec["confirmed_by"],
            "live_authorized": False, "retry_allowed": False}
