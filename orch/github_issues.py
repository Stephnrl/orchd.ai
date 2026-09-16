"""Offline GitHub issue codec: request data only, never live dispatch.

A project manager's work is tracking work — an issue on the repository, a ticket beside it,
and a link between them. [Jira actions](jira_actions.py) already model the ticket half,
including a `github_link` that names an issue by number. Nothing modelled the issue itself:
the only GitHub intent this control plane had was a pull request.

This closes that half, in the same shape as the rest: an intent is validated against a
contract, a **read plan** says exactly what would have to be observed before acting, a
capture of those reads is checked against the intent, and only then is the exact write
request produced. Nothing here opens a socket, holds a credential, or authorizes anything.
`live_authorized` is false in every record this module returns, and it is false because no
dispatch path exists, not as a flag someone forgot to flip.

Two properties are worth naming because they are the reason this is a codec and not a
client. The repository is pinned by **numeric id** as well as name, because a repository can
be renamed or transferred and a name that still resolves may no longer be the same
repository. And every issue this module would create carries an operation marker in its
body, so that after an uncertain dispatch [reconciliation](#reconcile_action) can find the
one issue that operation created rather than guessing from a title.
"""
import json
from pathlib import Path
import re
from urllib.parse import quote

from jsonschema import Draft202012Validator, ValidationError

from .contracts import Rejected, canonical, digest, redact

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/github-issue-intent-v1.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
FLAGS = {"live_authorized": False, "retry_allowed": False, "remote_effect_confirmed": False}
API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
MAX_INTENT_BYTES = 65536
# The same bound the CLI's loader enforces, so nothing is acceptable here that
# cannot be handed in through the command line.
MAX_CAPTURE_BYTES = 65536
MAX_LABELS = 200
MAX_SEARCH_RESULTS = 50
# An HTML comment: invisible where the issue is read, exact where it is searched for. A
# label would have been simpler and would silently create a label that did not exist.
MARKER = "<!-- orchd-operation: %s -->"
MARKER_PATTERN = re.compile(r"<!-- orchd-operation: ([a-f0-9]{32}) -->")


def request(url, method="GET", body=None):
    result = {"method": method, "url": url, "headers": dict(HEADERS)}
    if body is not None:
        result["body"] = body
    return result


def validate_profile(profile):
    """The destination, supplied by an operator and never inferred from an intent."""
    if not isinstance(profile, dict) or set(profile) != {"schema_version", "codec", "repository", "repository_id"}:
        raise Rejected("Unsupported GitHub issue profile shape")
    if profile["schema_version"] != "1.0.0" or profile["codec"] != "github-rest-2026-03-10":
        raise Rejected("Unsupported GitHub issue codec profile")
    if not isinstance(profile["repository"], str) or not re.fullmatch(
            SCHEMA["properties"]["repository"]["pattern"], profile["repository"]):
        raise Rejected("Invalid GitHub repository")
    if profile["repository"].lower().endswith(".git"):
        raise Rejected("Invalid GitHub repository")
    # An id, not only a name: a repository can be renamed or transferred, and the name that
    # still resolves afterwards may belong to someone else entirely.
    if type(profile["repository_id"]) is not int or not 0 < profile["repository_id"] <= 9007199254740991:
        raise Rejected("An explicit repository ID is required")
    return profile


def validate_intent(intent, profile):
    validate_profile(profile)
    try:
        VALIDATOR.validate(intent)
        encoded = canonical(intent).decode()
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise Rejected("Invalid GitHub issue intent") from exc
    if len(encoded.encode()) > MAX_INTENT_BYTES or redact(encoded) != encoded:
        raise Rejected("Intent over budget or contains recognized secrets")
    if intent["repository"] != profile["repository"]:
        raise Rejected("GitHub destination is not allowed")
    for key, text in (("title", intent["title"]), ("body", intent["body"])):
        if any(ord(char) < 32 and (key == "title" or char not in "\n\t") or ord(char) == 127 for char in text):
            raise Rejected("Unsupported control character")
    if not intent["title"].strip():
        raise Rejected("An issue needs a title")
    if MARKER_PATTERN.search(intent["body"]):
        raise Rejected("An intent may not carry its own operation marker")
    if len(set(intent["labels"])) != len(intent["labels"]):
        raise Rejected("An issue repeats a label")
    return intent


def marked(intent):
    """The body as it would be created: what was written, plus the operation that wrote it."""
    marker = MARKER % intent["operation_id"]
    return (intent["body"] + "\n\n" + marker) if intent["body"].strip() else marker


def repository_request(profile):
    return request(API + "/repos/" + profile["repository"])


def search_request(query):
    return request(API + "/search/issues?q=" + quote(query, safe="") + "&per_page=" + str(MAX_SEARCH_RESULTS))


def read_plan(intent, profile):
    """What must be observed before this issue could be created, and nothing more."""
    validate_intent(intent, profile)
    reads = {"repository": repository_request(profile),
             "labels": request(API + "/repos/" + profile["repository"] + "/labels?per_page=" + str(MAX_LABELS)),
             # The operation marker is unique, so this read is what makes creation idempotent:
             # an issue already carrying it means this operation has already run.
             "existing": search_request("repo:" + profile["repository"] + " is:issue "
                                        + (MARKER % intent["operation_id"]))}
    binding = {"schema_version": "1.0.0", "intent_sha256": digest(intent),
               "profile_sha256": digest(profile), "requests": reads}
    return {"kind": "GitHubIssueReadPlan", "binding": binding, "sha256": digest(binding), **FLAGS}


def duplicate_plan(profile, terms):
    """What to read to answer "is this work already an issue here?".

    This control plane makes no request itself. The plan says exactly what to ask; whoever
    can reach GitHub asks it and brings the answer back to `duplicates`.
    """
    validate_profile(profile)
    if not isinstance(terms, str) or not terms.strip() or len(terms) > 200:
        raise Rejected("Search terms are 1 to 200 characters")
    if redact(terms) != terms:
        raise Rejected("Search terms must not contain a secret")
    if any(ord(char) < 32 or ord(char) == 127 for char in terms):
        raise Rejected("Unsupported control character")
    query = "repo:" + profile["repository"] + " is:issue " + terms
    binding = {"schema_version": "1.0.0", "profile_sha256": digest(profile), "terms": terms,
               "requests": {"repository": repository_request(profile), "search": search_request(query)}}
    return {"kind": "GitHubIssueDuplicatePlan", "binding": binding, "sha256": digest(binding), **FLAGS}


def responses(plan, capture):
    """A capture bound to the plan it answers, checked before a single body is read."""
    if not isinstance(capture, dict) or set(capture) != {"schema_version", "plan_sha256", "responses"}:
        raise Rejected("Unsupported GitHub capture shape")
    if capture["schema_version"] != "1.0.0" or capture["plan_sha256"] != plan["sha256"]:
        raise Rejected("GitHub capture binding mismatch")
    if len(canonical(capture)) > MAX_CAPTURE_BYTES:
        raise Rejected("GitHub capture exceeds budget")
    wanted = plan["binding"]["requests"]
    if not isinstance(capture["responses"], dict) or set(capture["responses"]) != set(wanted):
        raise Rejected("GitHub capture answers a different plan")
    bodies = {}
    for key, read in wanted.items():
        response = capture["responses"][key]
        if not isinstance(response, dict) or set(response) != {"url", "redirected", "status", "body"}:
            raise Rejected("Unsupported GitHub response shape")
        if response["url"] != read["url"] or response["redirected"] is not False:
            raise Rejected("GitHub captured read came from a different origin")
        if type(response["status"]) is not int or response["status"] != 200:
            raise Rejected("GitHub captured read unavailable")
        bodies[key] = response["body"]
    return bodies


def checked_repository(profile, body):
    """The observed repository is this repository, by id and by name."""
    if not isinstance(body, dict):
        raise Rejected("GitHub repository body missing")
    if body.get("id") != profile["repository_id"] or body.get("full_name") != profile["repository"]:
        raise Rejected("GitHub repository identity mismatch")
    if body.get("archived") is True or body.get("disabled") is True:
        raise Rejected("GitHub repository is archived or disabled")
    if body.get("has_issues") is not True:
        raise Rejected("GitHub repository has issues disabled")
    return body


def checked_labels(body):
    if not isinstance(body, list) or len(body) > MAX_LABELS:
        raise Rejected("Invalid GitHub label list")
    names = set()
    for row in body:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise Rejected("Invalid GitHub label")
        names.add(row["name"])
    return names


def matches(body, expected_query):
    """The issues a search returned, checked for shape and bounded."""
    if not isinstance(body, dict) or type(body.get("total_count")) is not int:
        raise Rejected("Invalid GitHub search body")
    items = body.get("items")
    if not isinstance(items, list) or len(items) > MAX_SEARCH_RESULTS:
        raise Rejected("Invalid GitHub search results")
    if body["total_count"] > len(items):
        # Acting on a truncated view of what already exists is how duplicates get made.
        raise Rejected("GitHub search results are incomplete; narrow the query")
    result = []
    for row in items:
        if not isinstance(row, dict) or type(row.get("number")) is not int or not 0 < row["number"] <= 2147483647:
            raise Rejected("Invalid GitHub search result")
        if not isinstance(row.get("title"), str) or not isinstance(row.get("html_url"), str):
            raise Rejected("Invalid GitHub search result")
        if row.get("pull_request") is not None:
            raise Rejected("GitHub search returned a pull request")
        if expected_query is not None and not row["html_url"].startswith(API.replace("api.", "") + "/"):
            raise Rejected("GitHub result came from another host")
        result.append({"number": row["number"], "title": row["title"], "html_url": row["html_url"],
                       "state": row.get("state"), "body": row.get("body") or ""})
    return result


def duplicates(plan, capture, profile):
    """What the search found, as a reviewable answer rather than a decision."""
    if plan["kind"] != "GitHubIssueDuplicatePlan":
        raise Rejected("That is not a duplicate plan")
    bodies = responses(plan, capture)
    checked_repository(profile, bodies["repository"])
    found = matches(bodies["search"], plan["binding"]["terms"])
    binding = {"schema_version": "1.0.0", "plan_sha256": plan["sha256"], "capture_sha256": digest(capture),
               "terms": plan["binding"]["terms"],
               "issues": [{key: issue[key] for key in ("number", "title", "state", "html_url")} for issue in found]}
    return {"kind": "GitHubIssueDuplicateReport", "binding": binding, "sha256": digest(binding),
            "found": len(found), **FLAGS}


def preview_issue(intent, profile, capture):
    """The exact request that would create this issue, checked against what was observed."""
    plan = read_plan(intent, profile)
    bodies = responses(plan, capture)
    checked_repository(profile, bodies["repository"])
    known = checked_labels(bodies["labels"])
    unknown = sorted(set(intent["labels"]) - known)
    if unknown:
        # Creating an issue with an unknown label creates the label too, which is an effect
        # nobody reviewed. Naming it is more useful than quietly making it.
        raise Rejected("This repository has no label " + unknown[0])
    already = matches(bodies["existing"], None)
    if already:
        raise Rejected("This operation already created issue #" + str(already[0]["number"]))
    write = request(API + "/repos/" + intent["repository"] + "/issues", "POST",
                    {"title": intent["title"], "body": marked(intent), "labels": list(intent["labels"])})
    binding = {"schema_version": "1.0.0", "task_id": intent["task_id"], "operation_id": intent["operation_id"],
               "repository_id": profile["repository_id"], "plan_sha256": plan["sha256"],
               "capture_sha256": digest(capture), "request": write}
    return {"kind": "GitHubIssuePreview", "binding": binding, "sha256": digest(binding), **FLAGS}


def reconcile_issue(intent, profile, capture):
    """After an uncertain dispatch, find the one issue this operation created.

    The operation marker is what makes this an observation rather than a guess: two issues
    with the same title are ordinary, two carrying the same operation marker are not.
    """
    plan = read_plan(intent, profile)
    bodies = responses(plan, capture)
    checked_repository(profile, bodies["repository"])
    marker = MARKER % intent["operation_id"]
    blockers, candidates = [], []
    for issue in matches(bodies["existing"], None):
        if marker in issue["body"]:
            candidates.append({"number": issue["number"], "html_url": issue["html_url"]})
    if len(candidates) > 1:
        blockers.append("duplicate_operation_marker")
    if not candidates:
        blockers.append("no_candidate_observed")
    binding = {"schema_version": "1.0.0", "task_id": intent["task_id"], "operation_id": intent["operation_id"],
               "plan_sha256": plan["sha256"], "capture_sha256": digest(capture),
               "blockers": blockers, "candidate": None if blockers else candidates[0]}
    return {"kind": "GitHubIssueReconciliation", "binding": binding, "sha256": digest(binding),
            "status": "unresolved" if blockers else "candidate_observed", **FLAGS}
