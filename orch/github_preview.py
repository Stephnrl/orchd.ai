"""Pure GitHub draft-PR preparation. No transport, credentials or approval authority."""
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError

from .contracts import Rejected, canonical, digest, redact

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/github-pr-intent-v1.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def branch(value):
    return (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value) is not None
            and value != "HEAD" and not value.startswith("refs/") and ".." not in value
            and all(part and not part.startswith(".") and not part.endswith((".", ".lock")) for part in value.split("/")))


def prepare_pull_request(intent, allowed_repository):
    try:
        VALIDATOR.validate(intent)
        encoded = canonical(intent).decode()
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise Rejected("Invalid GitHub intent") from exc
    if len(encoded.encode()) > 65536 or redact(encoded) != encoded:
        raise Rejected("Intent over budget or contains recognized secrets")
    # Exact operator-supplied destination; no host override, forks or inferred repository.
    if (intent["repository"] != allowed_repository or intent["repository"].lower().endswith(".git")
            or not re.fullmatch(SCHEMA["properties"]["repository"]["pattern"], intent["repository"])):
        raise Rejected("GitHub destination is not allowed")
    for key, length in (("task_id", 32), ("operation_id", 32), ("expected_base_sha", 40), ("expected_head_sha", 40)):
        if not re.fullmatch(r"[a-f0-9]{" + str(length) + "}", intent[key]):
            raise Rejected("Invalid identity or commit hash")
    if not branch(intent["base"]) or not branch(intent["head"]) or intent["base"] == intent["head"]:
        raise Rejected("Unsupported GitHub branches")
    if intent["head"] != f"orchd/{intent['task_id']}/{intent['operation_id']}":
        raise Rejected("Head must be the task and operation branch")
    if intent["expected_base_sha"] == intent["expected_head_sha"]:
        raise Rejected("Expected head must differ from base")
    for key in ("title", "body"):
        text = intent[key]
        if any(ord(char) < 32 and (key == "title" or char not in "\n\t") or ord(char) == 127 for char in text):
            raise Rejected("Unsupported control character")
    if not intent["title"].strip() or len(intent["body"].encode()) > 16384:
        raise Rejected("Invalid title or body budget")
    request = {"method": "POST", "url": f"https://api.github.com/repos/{intent['repository']}/pulls",
               "headers": {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
               "body": {key: intent[key] for key in ("title", "body", "base", "head")}}
    request["body"].update(draft=True, maintainer_can_modify=False)
    binding = {"schema_version": "1.0.0", "task_id": intent["task_id"], "operation_id": intent["operation_id"],
               "expected_base_sha": intent["expected_base_sha"], "expected_head_sha": intent["expected_head_sha"], "request": request}
    return {"kind": "GitHubPullRequestPreview", "binding": binding, "sha256": digest(binding),
            "live_authorized": False, "remote_refs_verified": False}


def load_intent(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise Rejected("Duplicate intent key")
            value[key] = item
        return value

    def invalid_number(_):
        raise Rejected("Non-JSON number")

    try:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise Rejected("Intent must be a regular file")
        with source.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise Rejected("Intent file exceeds budget")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_number)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("Unreadable or invalid intent JSON") from exc
