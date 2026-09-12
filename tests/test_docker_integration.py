"""Opt-in real Linux-container gates. Never downloads an image or installs Docker."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import uid
from orch.engine import Engine
from orch.execution import Executor
from orch.fixtures import TEST_CODE
from orch.process import capture

IMAGE = os.environ.get("ORCH_DOCKER_TEST_IMAGE")


@unittest.skipUnless(IMAGE, "Set ORCH_DOCKER_TEST_IMAGE to a preloaded digest-pinned Python image")
class DockerIsolationTests(unittest.TestCase):
    def test_linux_isolation_and_cleanup(self):
        executor = Executor("docker", IMAGE)
        operation = uid()
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace).chmod(0o755)
            (Path(workspace) / "greeting.txt").write_text("hello world\n")
            empty = {"code": 0, "stdout": "", "stderr": "", "truncated": False, "failure": None}
            with patch("orch.execution.capture", return_value=empty) as intercepted:
                executor.run_direct(workspace, TEST_CODE, [], operation)
                command = list(intercepted.call_args.args[0])
            probe = '''import os, pathlib, socket
assert os.getuid() != 0
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
assert 'CapEff:\\t0000000000000000' in status
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not os.path.exists('/root/.ssh')
for target in ['/forbidden-write', '/workspace/greeting.txt']:
    try:
        pathlib.Path(target).write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('write permitted: ' + target)
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('192.0.2.1', 80))
except OSError:
    pass
else:
    raise AssertionError('egress permitted')
finally:
    s.close()
print('isolation checks passed')
'''
            command[command.index(TEST_CODE)] = probe
            try:
                result = capture(command, timeout=30)
                self.assertEqual(result["code"], 0, result["stderr"])
                self.assertIsNone(result["failure"])
                remaining = capture(["docker", "ps", "-aq", "--filter", "name=^/orch-" + operation + "$"], timeout=10)
                self.assertEqual(remaining["code"], 0)
                self.assertEqual(remaining["stdout"].strip(), "")
            finally:
                self.assertTrue(executor.reconcile(operation))

    def test_full_docker_workflow(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(root, Executor("docker", IMAGE))
            try:
                task = engine.create_task()
                engine.run(task)
                for _ in range(2):
                    state = engine.task(task)["state"]
                    engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
                    engine.run(task)
                self.assertEqual(engine.task(task)["state"]["state"], "COMPLETED")
                self.assertEqual(list((Path(root) / "workspaces").iterdir()), [])
            finally:
                engine.close()
