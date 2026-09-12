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


class Executor:
    def __init__(self, mode="docker", image=None):
        if mode not in ("docker", "trusted-fixture"):
            raise Rejected("Unknown executor")
        if mode == "docker" and (not image or not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", image)):
            raise Rejected("Docker requires an operator-selected digest-pinned Python image")
        self.mode, self.image = mode, image

    @property
    def image_digest(self):
        return self.image.split("@sha256:")[1] if self.image else hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()

    def run(self, workspace, script, args, operation_id):
        if script not in (TEST_CODE, EDIT_CODE):
            raise Rejected("Untrusted script")
        workspace = Path(workspace).resolve()
        if self.mode == "docker":
            command = ["docker", "run", "--rm", "--pull=never", "--name", "orch-" + operation_id,
                       "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                       "--user=65534:65534", "--pids-limit=64", "--cpus=1", "--memory=256m",
                       "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--mount", f"type=bind,source={workspace},target=/workspace" + (",readonly" if script == TEST_CODE else ""),
                       "--workdir=/workspace", self.image, "python", "-I", "-c", script, "/workspace/greeting.txt", *args]
        else:
            command = [sys.executable, "-I", "-c", script, str(workspace / "greeting.txt"), *args]
        started = now()
        # Fixed tiny programs have no untrusted code, child processes, network or output loops.
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        try:
            result = subprocess.run(command, cwd=workspace, env=env, shell=False, capture_output=True, timeout=20)
            code, stdout, stderr, failure = result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace"), None
        except subprocess.TimeoutExpired:
            if self.mode == "docker":
                subprocess.run(["docker", "rm", "-f", "orch-" + operation_id], capture_output=True, timeout=10, shell=False)
            code, stdout, stderr, failure = None, "", "deadline exceeded", "timeout"
        except OSError:
            code, stdout, stderr, failure = None, "", "executor unavailable", "executor_unavailable"
        return dict(command=command, started=started, ended=now(), code=code, stdout=stdout[:65536], stderr=stderr[:65536], truncated=len(stdout) > 65536 or len(stderr) > 65536, failure=failure)


class FixtureWorker:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def workspace(self, operation_id):
        path = self.store.root / "workspaces" / operation_id
        if path.is_symlink():
            raise Rejected("Workspace link")
        path.mkdir(parents=True, exist_ok=True)
        if path.resolve().parent != (self.store.root / "workspaces").resolve():
            raise Rejected("Workspace escape")
        return path

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
