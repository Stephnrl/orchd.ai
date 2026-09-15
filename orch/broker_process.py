"""Trusted broker execution shared by the short-lived subprocess and the loopback service.

Neither entry point receives a database handle, provider output or operator session.
"""
import json
from pathlib import Path
import sys

from .broker import VERSION, atomic_json, file_lock, safe_id, source_digest, validate_wire
from .contracts import Rejected, digest
from .fixtures import EDIT_CODE, TEST_CODE

MAX_REQUEST = 16384
FIELDS = {"schema_version", "operation_id", "task_id", "generation", "nonce", "workspace", "recipe", "args", "mode", "image", "source_digest"}


def check_request(request, workspaces, profile=None):
    """Closed request grammar. The broker, not the caller, decides the workspace parent and execution profile."""
    validate_wire(request, "Request")
    if set(request) != FIELDS or request["schema_version"] != VERSION or type(request["generation"]) is not int or request["generation"] < 1:
        raise Rejected("Invalid broker request")
    operation = safe_id(request["operation_id"])
    safe_id(request["nonce"])
    safe_id(request["task_id"])
    workspace = Path(request["workspace"])
    if workspace.is_symlink() or workspace.resolve() != Path(workspaces).resolve() / operation:
        raise Rejected("Unassigned workspace")
    if request["recipe"] not in ("edit", "test") or (request["recipe"] == "edit" and request["args"] not in (["hello world\n"], ["wrong\n"])) or (request["recipe"] == "test" and request["args"] != []):
        raise Rejected("Untrusted recipe or arguments")
    if request["source_digest"] != source_digest():
        raise Rejected("Runtime digest mismatch")
    if profile is not None and (request["mode"], request["image"]) != (profile["mode"], profile["image"]):
        raise Rejected("Broker execution profile mismatch")
    return operation


def journal(root, operation, request):
    """The retained envelope for exactly this request, or None. Another request's journal is a conflict."""
    path = root / (operation + ".json")
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise Rejected("Broker result unavailable")
    try:
        envelope = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise Rejected("Unreadable broker journal") from exc
    validate_wire(envelope, "Envelope")
    if envelope["request_sha256"] != digest(request):
        raise Rejected("Operation journal belongs to a different request")
    return envelope


def perform(root, request, executor=None):
    """Execute at most once per operation. Repeating an identical request returns its retained envelope."""
    operation = request["operation_id"]
    with file_lock(root / (operation + ".lock")):
        if (root / (operation + ".retired")).exists():
            raise Rejected("Retired execution generation")
        existing = journal(root, operation, request)
        if existing is not None:
            return existing
        if executor is None:
            from .execution import Executor
            executor = Executor(request["mode"], request["image"])
        result = executor.run_direct(Path(request["workspace"]), EDIT_CODE if request["recipe"] == "edit" else TEST_CODE, request["args"], operation)
        envelope = {k: request[k] for k in ("schema_version", "operation_id", "task_id", "generation", "nonce", "source_digest")}
        envelope.update(request_sha256=digest(request), result=result)
        validate_wire(envelope, "Envelope")
        atomic_json(root / (operation + ".json"), envelope)
        return envelope


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise Rejected("Broker request too large")
    request = json.loads(raw)
    root = Path(sys.argv[1]).resolve()
    check_request(request, root.parent / "workspaces")
    perform(root, request)


if __name__ == "__main__":
    main()
