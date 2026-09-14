"""Fixed-recipe process execution. Local mode is ONLY for trusted test fixtures."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from .contracts import Rejected, actor, error, make, now, ref
from .fixtures import EDIT_CODE, TEST_CODE, recipe
from .process import capture


class Executor:
    def __init__(self, mode="docker", image=None):
        if mode not in ("docker", "trusted-fixture"):
            raise Rejected("Unknown executor")
        if mode == "docker" and (not image or not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", image)):
            raise Rejected("Docker requires an operator-selected digest-pinned Python image")
        self.mode, self.image = mode, image
        self.broker = None

    @property
    def image_digest(self):
        return self.image.split("@sha256:")[1] if self.image else hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()

    def run(self, workspace, script, args, operation_id):
        if self.broker:
            if script not in (TEST_CODE, EDIT_CODE):
                raise Rejected("Untrusted script")
            return self.broker.run(workspace, "test" if script == TEST_CODE else "edit", args, operation_id)
        return self.run_direct(workspace, script, args, operation_id)

    def run_direct(self, workspace, script, args, operation_id):
        if script not in (TEST_CODE, EDIT_CODE):
            raise Rejected("Untrusted script")
        workspace = Path(workspace).resolve()
        command = self.command_for(workspace, script, args, operation_id)
        started = now()
        truncated = False
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        try:
            result = capture(command, cwd=workspace, env=env, timeout=10 if script == TEST_CODE else 20)
            truncated = result["truncated"]
            if self.mode == "docker" and result["failure"]:
                if not self.reconcile(operation_id):
                    result["failure"] = "cleanup_uncertain"
            code, stdout, stderr, failure = result["code"], result["stdout"], result["stderr"], result["failure"]
            if failure:
                code = None
        except OSError:
            code, stdout, stderr, failure = None, "", "executor unavailable", "executor_unavailable"
        return dict(command=command, started=started, ended=now(), code=code, stdout=stdout, stderr=stderr, truncated=truncated, failure=failure)

    def command_for(self, workspace, script, args, operation_id, python_executable=None):
        """Render fixed-recipe argv without resolving paths or executing a process."""
        if self.mode == "docker":
            return ["docker", "run", "--rm", "--pull=never", "--name", "orch-" + operation_id,
                       "--label", "ai.orchd.operation=" + operation_id,
                       "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                       "--user=65534:65534", "--pids-limit=64", "--cpus=1", "--memory=256m", "--memory-swap=256m", "--log-driver=none",
                       "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--mount", f"type=bind,source={workspace},target=/workspace" + (",readonly" if script == TEST_CODE else ""),
                       "--workdir=/workspace", self.image, "python", "-I", "-c", script, "/workspace/greeting.txt", *args]
        else:
            return [python_executable or sys.executable, "-I", "-c", script, str(workspace / "greeting.txt"), *args]

    def reconcile(self, operation_id):
        """Remove only the exact labelled task container; daemon errors are not absence."""
        from .broker import safe_id
        safe_id(operation_id)
        if self.mode != "docker":
            return False  # Cannot prove an orphaned local fixture process has exited.
        try:
            listing = capture(["docker", "ps", "-aq", "--filter", "name=^/orch-" + operation_id + "$"], timeout=10)
            if listing["code"] != 0 or listing["failure"]:
                return False
            if not listing["stdout"].strip():
                return True
            inspected = capture(["docker", "inspect", "orch-" + operation_id], timeout=10)
            if inspected["code"] != 0 or inspected["failure"]:
                # --rm may finish between list and inspect. Only a successful fresh
                # query proving absence can resolve this race; errors cannot.
                check = capture(["docker", "ps", "-aq", "--filter", "name=^/orch-" + operation_id + "$"], timeout=10)
                return check["code"] == 0 and not check["failure"] and not check["stdout"].strip()
            info = json.loads(inspected["stdout"])[0]
            if info["Config"].get("Labels", {}).get("ai.orchd.operation") != operation_id:
                return False
            removed = capture(["docker", "rm", "-f", info["Id"]], timeout=10)
            # Auto-removal may also win the race with rm. Verify absence either way.
            check = capture(["docker", "ps", "-aq", "--filter", "name=^/orch-" + operation_id + "$"], timeout=10)
            return check["code"] == 0 and not check["failure"] and not check["stdout"].strip()
        except (OSError, ValueError, KeyError, IndexError):
            return False


class FixtureWorker:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def workspace(self, operation_id):
        from .broker import safe_id
        safe_id(operation_id)
        parent = self.store.root / "workspaces"
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise Rejected("Workspace root link")
        path = self.store.root / "workspaces" / operation_id
        if path.is_symlink():
            raise Rejected("Workspace link")
        path.mkdir(parents=True, exist_ok=True)
        if path.resolve().parent != (self.store.root / "workspaces").resolve():
            raise Rejected("Workspace escape")
        return path

    def cleanup(self, operation_id):
        from .broker import safe_id
        safe_id(operation_id)
        path = self.store.root / "workspaces" / operation_id
        if not path.exists():
            return
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or path.resolve() != self.store.root / "workspaces" / operation_id:
            raise Rejected("Unsafe cleanup path")
        files = list(path.iterdir())
        if any(p.name != "greeting.txt" or not p.is_file() or p.is_symlink() for p in files):
            raise Rejected("Unexpected workspace content; retain for investigation")
        for file in files:
            file.chmod(0o600)
            file.unlink()
        path.rmdir()

    def implement(self, operation_id, work, proposal):
        if set(proposal) != {"content"} or proposal["content"] not in ("hello world\n", "wrong\n"):
            raise Rejected("Invalid fixture proposal")
        if work["allowed_paths"] != ["greeting.txt"] or work["permitted_tests"] != [recipe()]:
            raise Rejected("Unapproved fixture scope")
        workspace = self.workspace(operation_id)
        path = workspace / "greeting.txt"
        if path.is_symlink():
            raise Rejected("Fixture link")
        path.write_text("hello\n", newline="")
        path.chmod(0o666)
        result = self.executor.run(workspace, EDIT_CODE, [proposal["content"]], operation_id)
        if result["code"] != 0:
            return {"failure": result["failure"] or "implementation_process_failed", "execution": result}
        raw = path.read_bytes()
        if raw != proposal["content"].encode() or len(raw) > work["max_changed_bytes"]:
            raise Rejected("Patch outside scope")
        snapshot = self.store.artifact(work["task_id"], raw.decode(), "text/plain", "action_broker", "trusted_receipt")
        patch = make("PatchReceipt", work["task_id"], operation_id=operation_id, work_order=ref(work), producer=actor("action_broker"), repository=work["repository"],
                     started_at=result["started"], ended_at=result["ended"], requested_commands=[],
                     executed_commands=[dict(argv=result["command"], working_directory=str(workspace), started_at=result["started"], ended_at=result["ended"], exit_code=result["code"],
                                             stdout=self.store.artifact(work["task_id"], result["stdout"], "text/plain"), stderr=self.store.artifact(work["task_id"], result["stderr"], "text/plain"))],
                     snapshot_sha256=snapshot["sha256"], diff=self.store.artifact(work["task_id"], "--- greeting.txt\n+++ greeting.txt\n@@ -1 +1 @@\n-hello\n+" + raw.decode(), "text/x-diff"),
                     changed_files=[dict(path="greeting.txt", change="modify", before_sha256=hashlib.sha256(b"hello\n").hexdigest(), after_sha256=snapshot["sha256"])], changed_bytes=len(raw))
        return {"patch": patch, "snapshot": snapshot}

    def test(self, operation_id, request, patch, snapshot):
        if request["command"] != recipe() or request["snapshot_sha256"] != snapshot["sha256"] or patch["snapshot_sha256"] != snapshot["sha256"]:
            raise Rejected("Test evidence mismatch")
        workspace = self.workspace(operation_id)
        path = workspace / "greeting.txt"
        if path.is_symlink():
            raise Rejected("Fixture link")
        path.write_text(self.store.read_artifact(snapshot, request["task_id"]), newline="")
        path.chmod(0o444)
        result = self.executor.run(workspace, TEST_CODE, [], operation_id)
        outcome = "timeout" if result["failure"] == "timeout" else "error" if result["failure"] else "passed" if result["code"] == 0 else "failed"
        return make("TestReceipt", request["task_id"], operation_id=operation_id, request=ref(request), patch=ref(patch), snapshot_sha256=snapshot["sha256"], producer=actor("test_runner"),
                    requested_command=recipe(), executed_argv=result["command"], working_directory=str(workspace),
                    environment=dict(os="linux" if self.executor.mode == "docker" else platform.system(), architecture=platform.machine(), tool_version="python-fixture-v1", image_digest=self.executor.image_digest),
                    started_at=result["started"], ended_at=result["ended"], exit_code=result["code"], outcome=outcome,
                    stdout=self.store.artifact(request["task_id"], result["stdout"], "text/plain", "test_runner", "trusted_receipt"),
                    stderr=self.store.artifact(request["task_id"], result["stderr"], "text/plain", "test_runner", "trusted_receipt"),
                    output_truncated=result["truncated"], error=error(result["failure"]) if result["failure"] else None)
