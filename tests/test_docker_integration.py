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

# Bound the probes before stressing anything. Unknown layouts fail, never skip.
LIMIT_CHECK = r'''
from pathlib import Path
cg = Path('/sys/fs/cgroup')
if (cg / 'cgroup.controllers').exists():
    memory = int((cg / 'memory.max').read_text())
    swap = int((cg / 'memory.swap.max').read_text())
    pids = int((cg / 'pids.max').read_text())
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
else:
    memory = int((cg / 'memory/memory.limit_in_bytes').read_text())
    swap = int((cg / 'memory/memory.memsw.limit_in_bytes').read_text()) - memory
    pids = int((cg / 'pids/pids.max').read_text())
    quota = int((cg / 'cpu/cpu.cfs_quota_us').read_text())
    period = int((cg / 'cpu/cpu.cfs_period_us').read_text())
assert 0 < memory <= 256 * 1024 * 1024, memory
assert swap == 0, swap
assert 0 < pids <= 64, pids
assert 0 < quota <= period, (quota, period)
'''


@unittest.skipUnless(IMAGE, "Set ORCH_DOCKER_TEST_IMAGE to a preloaded digest-pinned Python image")
class DockerIsolationTests(unittest.TestCase):
    def probe_command(self, workspace, operation, script, retain=False):
        executor = Executor("docker", IMAGE)
        empty = {"code": 0, "stdout": "", "stderr": "", "truncated": False, "failure": None}
        with patch("orch.execution.capture", return_value=empty) as intercepted:
            executor.run_direct(workspace, TEST_CODE, [], operation)
        command = list(intercepted.call_args.args[0])
        command[command.index(TEST_CODE)] = script
        if retain:
            command.remove("--rm")
        return executor, command

    def assert_absent(self, operation):
        result = capture(["docker", "ps", "-aq", "--filter", "name=^/orch-" + operation + "$"], timeout=10)
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIsNone(result["failure"])
        self.assertEqual(result["stdout"].strip(), "")

    def test_resource_limits(self):
        script = LIMIT_CHECK + r'''
import errno, os, subprocess, sys
space = os.statvfs('/tmp')
assert space.f_blocks * space.f_frsize <= 16 * 1024 * 1024
try:
    with open('/tmp/bounded-fill', 'wb', buffering=0) as stream:
        for _ in range(20):
            stream.write(b'x' * 1024 * 1024)
except OSError as exc:
    assert exc.errno == errno.ENOSPC, exc
else:
    raise AssertionError('tmpfs limit not enforced')
finally:
    Path('/tmp/bounded-fill').unlink(missing_ok=True)
children = []
denied = False
before = (cg / 'pids.events').read_text() if (cg / 'pids.events').exists() else None
try:
    for _ in range(70):
        try:
            # sleep is a tiny image-provided executable; cap children and lifetime.
            children.append(subprocess.Popen(['sleep', '15']))
        except OSError as exc:
            assert exc.errno == errno.EAGAIN, exc
            denied = True
            break
    assert denied, 'PID limit not enforced'
    assert len(children) < 64
    if before is not None:
        after = (cg / 'pids.events').read_text()
        assert int(after.split()[1]) > int(before.split()[1]), (before, after)
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait(timeout=3)
print('resource limits enforced')
'''
        operation = uid()
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace).chmod(0o755)
            executor, command = self.probe_command(workspace, operation, script)
            try:
                result = capture(command, timeout=30)
                self.assertEqual(result["code"], 0, result["stderr"])
                self.assertIsNone(result["failure"])
                self.assertIn("resource limits enforced", result["stdout"])
                self.assert_absent(operation)
            finally:
                self.assertTrue(executor.reconcile(operation))

    def test_memory_limit_and_cleanup(self):
        script = LIMIT_CHECK + "\nblocks = [bytearray(1024 * 1024) for _ in range(384)]\nraise AssertionError('memory limit not enforced')\n"
        operation = uid()
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace).chmod(0o755)
            executor, command = self.probe_command(workspace, operation, script, retain=True)
            try:
                result = capture(command, timeout=30)
                self.assertIsNone(result["failure"])
                self.assertEqual(result["code"], 137, result["stderr"])
                inspected = capture(["docker", "inspect", "orch-" + operation], timeout=10)
                self.assertEqual(inspected["code"], 0, inspected["stderr"])
                info = json.loads(inspected["stdout"])[0]
                self.assertTrue(info["State"]["OOMKilled"])
                self.assertFalse(info["State"]["Running"])
                self.assertEqual(info["HostConfig"]["LogConfig"]["Type"], "none")
                self.assertEqual(info["HostConfig"]["MemorySwap"], info["HostConfig"]["Memory"])
            finally:
                self.assertTrue(executor.reconcile(operation))
                self.assert_absent(operation)

    def test_timeout_and_output_cleanup(self):
        for script, expected in (("import time; time.sleep(60)", "timeout"),
                                 ("import sys; sys.stdout.write('x' * 4 * 1024 * 1024); sys.stdout.flush()", "output_limit")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as workspace:
                Path(workspace).chmod(0o755)
                operation = uid()
                executor = Executor("docker", IMAGE)
                def substitute(argv, **kwargs):
                    command = list(argv)
                    if command[:2] == ["docker", "run"]:
                        command[command.index(TEST_CODE)] = script
                    return capture(command, **kwargs)
                try:
                    with patch("orch.execution.capture", side_effect=substitute):
                        result = executor.run_direct(workspace, TEST_CODE, [], operation)
                    self.assertEqual(result["failure"], expected, result["stderr"])
                    self.assertIsNone(result["code"])
                    self.assertLessEqual(len(result["stdout"].encode()), 65536)
                    self.assertEqual(result["truncated"], expected == "output_limit")
                    self.assert_absent(operation)
                finally:
                    self.assertTrue(executor.reconcile(operation))

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
