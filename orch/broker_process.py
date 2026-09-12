"""Trusted short-lived broker subprocess. It has no database handle or provider input."""
import json
from pathlib import Path
import sys

from .broker import VERSION, atomic_json, file_lock, safe_id, source_digest, validate_wire
from .contracts import Rejected, digest
from .fixtures import EDIT_CODE, TEST_CODE


def main():
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise Rejected("Broker request too large")
    request = json.loads(raw)
    validate_wire(request, "Request")
    expected = {"schema_version", "operation_id", "task_id", "generation", "nonce", "workspace", "recipe", "args", "mode", "image", "source_digest"}
    if set(request) != expected or request["schema_version"] != VERSION or type(request["generation"]) is not int or request["generation"] < 1:
        raise Rejected("Invalid broker request")
    operation = safe_id(request["operation_id"])
    safe_id(request["nonce"])
    safe_id(request["task_id"])
    root = Path(sys.argv[1]).resolve()
    workspace = Path(request["workspace"])
    if workspace.is_symlink() or workspace.resolve() != root.parent / "workspaces" / operation:
        raise Rejected("Unassigned workspace")
    if request["recipe"] not in ("edit", "test") or (request["recipe"] == "edit" and request["args"] not in (["hello world\n"], ["wrong\n"])) or (request["recipe"] == "test" and request["args"] != []):
        raise Rejected("Untrusted recipe or arguments")
    if request["source_digest"] != source_digest():
        raise Rejected("Runtime digest mismatch")
    with file_lock(root / (operation + ".lock")):
        if (root / (operation + ".retired")).exists():
            raise Rejected("Retired execution generation")
        path = root / (operation + ".json")
        if path.exists():
            raise Rejected("Operation already has a journal")
        from .execution import Executor
        executor = Executor(request["mode"], request["image"])
        result = executor.run_direct(workspace, EDIT_CODE if request["recipe"] == "edit" else TEST_CODE, request["args"], operation)
        envelope = {k: request[k] for k in ("schema_version", "operation_id", "task_id", "generation", "nonce", "source_digest")}
        envelope.update(request_sha256=digest(request), result=result)
        validate_wire(envelope, "Envelope")
        atomic_json(path, envelope)


if __name__ == "__main__":
    main()
