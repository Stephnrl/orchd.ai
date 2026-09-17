"""Starting the containers an operator declared, and never anything a request described.

This is the milestone that lets a window start a process, so the tests that matter most are
the ones about what it cannot start. A request contributes one thing — which enrolled agent —
and everything else comes from a file on the host whose every field has a grammar. The argv
is asserted whole, because a flag that quietly appears in it is exactly the kind of change
that would not fail anything else.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch import agent_processes as lifecycle
from orch.agent_service import enrol
from orch.api import Application
from orch.contracts import Rejected
from orch.engine import Engine
from orch.execution import Executor

IMAGE = "orchd-agent@sha256:" + "a" * 64
IDENTITY = "sha256:" + "b" * 64
ENDPOINT = "127.0.0.1:8721"


class FakeDocker:
    """A daemon that answers, so the argv and the decisions can be tested without one."""

    def __init__(self, running=(), run_code=0, stderr="", ps_code=0):
        self.calls, self.state = [], {name: "running" for name in running}
        self.run_code, self.stderr, self.ps_code = run_code, stderr, ps_code

    def __call__(self, argv, timeout=None, limit=None, env=None):
        self.calls.append(argv)
        command = argv[1]
        if command == "ps":
            rows = [json.dumps({"Names": lifecycle.PREFIX + name, "ID": "c" * 64, "State": state,
                                "Status": "Up 3 minutes", "Image": IMAGE, "RunningFor": "3 minutes"})
                    for name, state in self.state.items()]
            return {"code": self.ps_code, "stdout": "\n".join(rows), "stderr": "", "failure": None}
        if command == "run":
            if self.run_code == 0:
                self.state[argv[argv.index("--name") + 1][len(lifecycle.PREFIX):]] = "running"
            return {"code": self.run_code, "stdout": "c" * 64, "stderr": self.stderr, "failure": None}
        if command == "stop":
            self.state[argv[-1][len(lifecycle.PREFIX):]] = "exited"
            return {"code": 0, "stdout": "", "stderr": "", "failure": None}
        if command == "rm":
            self.state.pop(argv[-1][len(lifecycle.PREFIX):], None)
            return {"code": 0, "stdout": "", "stderr": "", "failure": None}
        if command == "image":
            return {"code": 0, "stdout": json.dumps(IDENTITY), "stderr": "", "failure": None}
        raise AssertionError("unexpected docker command " + command)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agents = Path(self.temp.name) / "agents"
        enrol(self.agents, "lead-devops", ["lead_planner", "reviewer"])
        self.path = self.agents / "lead-devops" / lifecycle.DECLARATION

    def declare(self, **fields):
        return lifecycle.declare(self.agents, "lead-devops", fields.pop("image", IMAGE),
                                 fields.pop("service", ENDPOINT), fields.pop("role", "lead_planner"),
                                 **fields)

    def rewrite(self, **fields):
        """Put a declaration on disk that `declare` would not have written."""
        current = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.path.write_text(json.dumps({**current, **fields}), encoding="utf-8")

    def docker(self, fake):
        return patch.multiple(lifecycle, capture=fake, shutil=FakeWhich())

    # The declaration is the only thing that says what may run -------------------------------

    def test_a_declaration_is_written_and_read_back_field_for_field(self):
        written = self.declare()
        self.assertEqual(written["image"], IMAGE)
        self.assertEqual(written["role"], "lead_planner")
        self.assertEqual(written["lease"], lifecycle.DEFAULT_LEASE)
        self.assertEqual(written["network"], "host")
        self.assertIs(written["live_authorized"], False)
        self.assertEqual(lifecycle.declaration(self.agents, "lead-devops")["image"], IMAGE)

    def test_a_field_this_does_not_know_about_is_refused_rather_than_ignored(self):
        # A setting nobody reads is how a container quietly stops running what it should.
        self.declare()
        for extra in ({"command": ["sh", "-c", "curl evil"]}, {"secret": "/etc/shadow"},
                      {"volumes": ["/:/host"]}, {"privileged": True}, {"entrypoint": "sh"},
                      {"user": "0:0"}, {"env": {"AWS_SECRET_ACCESS_KEY": "x"}}):
            with self.subTest(extra=extra):
                self.rewrite(**extra)
                with self.assertRaisesRegex(Rejected, "only known fields"):
                    lifecycle.declaration(self.agents, "lead-devops")

    def test_every_value_has_a_grammar(self):
        self.declare()
        for field, value in (("image", "orchd-agent:latest"), ("image", "sha256:nothex"),
                             ("endpoint", "10.0.0.1:8721"), ("endpoint", "127.0.0.1:0"),
                             ("role", "junior"), ("role", "human"), ("role", "not_a_role"),
                             ("lease", 10), ("lease", 10 ** 6), ("lease", "900"),
                             ("network", "bridge"), ("network", "host --privileged"),
                             ("network", "container:../x"), ("memory", "all of it"),
                             ("decider", "rm -rf /"), ("decider", "no_colon"),
                             ("host_cannot_protect_secrets", "yes"),
                             ("resolved_from", "a tag with spaces")):
            with self.subTest(field=field, value=value):
                self.rewrite(**{field: value})
                with self.assertRaises(Rejected):
                    lifecycle.declaration(self.agents, "lead-devops")

    def test_a_container_runs_as_a_role_its_agent_is_enrolled_for(self):
        # The same gate a claim passes, applied to what the container will ask to claim as.
        self.declare(role="reviewer")
        self.rewrite(role="junior")
        with self.assertRaisesRegex(Rejected, "enrolled for"):
            lifecycle.declaration(self.agents, "lead-devops")

    def test_declaring_twice_refuses_rather_than_replacing(self):
        self.declare()
        with self.assertRaisesRegex(Rejected, "already declared"):
            self.declare()

    def test_a_declaration_that_would_not_start_is_not_left_on_disk(self):
        # Worse than no declaration: one that looks right in the window and fails later.
        with self.assertRaises(Rejected):
            self.declare(lease=5)
        self.assertFalse(self.path.exists())

    def test_an_agent_that_is_not_enrolled_has_nothing_to_declare_or_start(self):
        for act in (lambda: lifecycle.declaration(self.agents, "stranger"),
                    lambda: self.declare and lifecycle.declare(self.agents, "stranger", IMAGE, ENDPOINT, "junior")):
            with self.subTest(act=act):
                with self.assertRaisesRegex(Rejected, "not enrolled"):
                    act()

    def test_a_tag_is_pinned_when_it_is_declared_and_the_tag_is_remembered(self):
        fake = FakeDocker()
        with self.docker(fake):
            written = lifecycle.declare(self.agents, "lead-devops", "orchd-agent:latest", ENDPOINT, "lead_planner")
        self.assertEqual(written["image"], IDENTITY, "the declaration records bytes, not a moving tag")
        self.assertEqual(json.loads(self.path.read_text())["resolved_from"], "orchd-agent:latest")
        self.assertIn(["docker", "image", "inspect", "orchd-agent:latest", "--format", "{{json .Id}}"],
                      [call for call in fake.calls])

    # The command is a template, not a composition ---------------------------------------

    def test_the_argv_is_exactly_what_it_should_be(self):
        self.declare()
        argv = lifecycle.command_for(self.agents, "lead-devops",
                                     lifecycle.declaration(self.agents, "lead-devops"))
        secret = str((self.agents / "lead-devops" / "secret").resolve())
        self.assertEqual(argv[:3], ["docker", "run", "--detach"])
        self.assertIn("--pull=never", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("--security-opt=no-new-privileges", argv)
        self.assertIn("--restart=no", argv)
        self.assertIn("--network=host", argv)
        self.assertIn("--name", argv)
        self.assertEqual(argv[argv.index("--name") + 1], "orchd-agent-lead-devops")
        self.assertIn("type=bind,source=" + secret + ",target=/run/secrets/agent,readonly", argv)
        self.assertEqual(argv[-1], IMAGE, "no command follows the image")
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("-v", argv)

    def test_no_credential_and_no_store_path_reach_the_container(self):
        self.declare()
        argv = lifecycle.command_for(self.agents, "lead-devops",
                                     lifecycle.declaration(self.agents, "lead-devops"))
        passed = [argv[index + 1] for index, item in enumerate(argv) if item == "--env"]
        self.assertEqual(sorted(name.split("=")[0] for name in passed),
                         ["ORCHD_DECIDER", "ORCHD_ENDPOINT", "ORCHD_LEASE", "ORCHD_ROLE", "ORCHD_SECRET_FILE"])
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertEqual(len(mounts), 1, "one mount, and it is the agent's own secret")

    def test_the_unprotected_host_flag_is_the_only_thing_that_adds_an_environment_entry(self):
        self.declare(host_cannot_protect_secrets=True)
        argv = lifecycle.command_for(self.agents, "lead-devops",
                                     lifecycle.declaration(self.agents, "lead-devops"))
        self.assertIn("ORCHD_HOST_CANNOT_PROTECT_SECRETS=1", argv)

    def test_joining_a_service_container_is_the_other_deployment_that_works(self):
        self.declare(network_mode="container:orchd-service")
        argv = lifecycle.command_for(self.agents, "lead-devops",
                                     lifecycle.declaration(self.agents, "lead-devops"))
        self.assertIn("--network=container:orchd-service", argv)

    # Starting and stopping ----------------------------------------------------------------

    def test_starting_runs_the_declared_container_and_reports_it(self):
        self.declare()
        fake = FakeDocker()
        with self.docker(fake):
            started = lifecycle.start(self.agents, "lead-devops")
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["container"], "c" * 12)
        self.assertIs(started["live_authorized"], False)
        self.assertEqual([call[1] for call in fake.calls], ["ps", "run"])

    def test_a_second_container_for_one_agent_is_refused(self):
        # Two would both hold leases as the same agent, and the second would look like the
        # first coming back.
        self.declare()
        with self.docker(FakeDocker(running=["lead-devops"])):
            refused = lifecycle.start(self.agents, "lead-devops")
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "already_running"))

    def test_a_previous_stopped_container_is_removed_before_the_next_start(self):
        # Not `--rm`, so the last run's log survives until there is a next one.
        self.declare()
        fake = FakeDocker()
        fake.state["lead-devops"] = "exited"
        with self.docker(fake):
            self.assertEqual(lifecycle.start(self.agents, "lead-devops")["status"], "started")
        self.assertEqual([call[1] for call in fake.calls], ["ps", "rm", "run"])

    def test_an_image_that_is_not_here_is_named_as_the_reason(self):
        self.declare()
        with self.docker(FakeDocker(run_code=125, stderr="Error: No such image: sha256:...")):
            refused = lifecycle.start(self.agents, "lead-devops")
        self.assertEqual(refused["reason"], "image_not_present")

    def test_nothing_is_pulled_for_you(self):
        self.declare()
        fake = FakeDocker()
        with self.docker(fake):
            lifecycle.start(self.agents, "lead-devops")
        run = [call for call in fake.calls if call[1] == "run"][0]
        self.assertIn("--pull=never", run)
        self.assertNotIn("pull", [item for item in run if item == "pull"])

    def test_stopping_gives_the_loop_time_to_hand_back_what_it_holds(self):
        self.declare()
        fake = FakeDocker(running=["lead-devops"])
        with self.docker(fake):
            stopped = lifecycle.stop(self.agents, "lead-devops")
        self.assertEqual(stopped["status"], "stopped")
        stop = [call for call in fake.calls if call[1] == "stop"][0]
        self.assertEqual(stop[2:4], ["--time", str(lifecycle.STOP_SECONDS)])

    def test_stopping_what_is_not_running_is_refused_rather_than_pretended(self):
        self.declare()
        with self.docker(FakeDocker()):
            refused = lifecycle.stop(self.agents, "lead-devops")
        self.assertEqual(refused["reason"], "not_running")

    def test_an_agent_that_is_not_enrolled_cannot_be_started_or_stopped(self):
        with self.docker(FakeDocker()) as _:
            for act in (lifecycle.start, lifecycle.stop):
                with self.subTest(act=act.__name__):
                    self.assertEqual(act(self.agents, "stranger")["reason"], "not_enrolled")

    def test_a_name_that_is_not_an_agent_name_never_reaches_docker(self):
        fake = FakeDocker()
        with self.docker(fake):
            for name in ("../escape", "lead-devops; rm -rf /", "-v/:/host", "", "a" * 200):
                with self.subTest(name=name):
                    self.assertEqual(lifecycle.start(self.agents, name)["reason"], "not_enrolled")
        self.assertEqual(fake.calls, [], "a refused name is refused before the daemon is asked")

    def test_missing_docker_is_a_reason_and_not_a_crash(self):
        self.declare()
        with patch.multiple(lifecycle, shutil=FakeWhich(found=None)):
            self.assertEqual(lifecycle.start(self.agents, "lead-devops")["reason"], "docker_cli_missing")
            self.assertEqual(lifecycle.processes(self.agents)["docker"], "unavailable")

    def test_a_daemon_that_does_not_answer_is_a_reason_and_not_a_crash(self):
        self.declare()
        hung = lambda argv, **kwargs: {"code": 1, "stdout": "", "stderr": "", "failure": "timeout"}
        with patch.multiple(lifecycle, capture=hung, shutil=FakeWhich()):
            self.assertEqual(lifecycle.start(self.agents, "lead-devops")["reason"], "daemon_unavailable")
            self.assertEqual(lifecycle.processes(self.agents)["reason"], "daemon_unavailable")

    # What the window is shown --------------------------------------------------------------

    def test_the_listing_pairs_every_enrolled_agent_with_its_container(self):
        enrol(self.agents, "junior-devops", ["junior"])
        self.declare()
        with self.docker(FakeDocker(running=["lead-devops"])):
            report = lifecycle.processes(self.agents)
        rows = {row["agent"]: row for row in report["agents"]}
        self.assertEqual(sorted(rows), ["junior-devops", "lead-devops"])
        self.assertEqual((rows["lead-devops"]["declared"], rows["lead-devops"]["state"]), (True, "running"))
        self.assertEqual((rows["junior-devops"]["declared"], rows["junior-devops"]["state"]), (False, "no container"))
        self.assertIsNotNone(rows["junior-devops"]["reason"], "an agent with no declaration says so")

    def test_containers_that_are_not_orchd_agents_are_not_listed(self):
        self.declare()
        fake = FakeDocker()

        def other(argv, **kwargs):
            if argv[1] == "ps":
                return {"code": 0, "failure": None, "stderr": "",
                        "stdout": json.dumps({"Names": "someone-elses-container", "ID": "d" * 64,
                                              "State": "running", "Status": "Up", "Image": "x",
                                              "RunningFor": "1 minute"})}
            return fake(argv, **kwargs)

        with patch.multiple(lifecycle, capture=other, shutil=FakeWhich()):
            report = lifecycle.processes(self.agents)
        self.assertEqual(report["agents"][0]["state"], "no container")

    def test_the_listing_asks_docker_only_about_labelled_containers(self):
        self.declare()
        fake = FakeDocker()
        with self.docker(fake):
            lifecycle.processes(self.agents)
        listing = [call for call in fake.calls if call[1] == "ps"][0]
        self.assertIn("--filter", listing)
        self.assertEqual(listing[listing.index("--filter") + 1], "label=" + lifecycle.LABEL)


class FakeWhich:
    """`shutil` with only the one function this module uses."""

    def __init__(self, found="docker"):
        self.found = found

    def which(self, _name):
        return self.found


class LifecycleApiTests(unittest.TestCase):
    """The window's side: one route per verb, and the agent name is all it carries."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.agents = root / "agents"
        enrol(self.agents, "lead-devops", ["lead_planner"])
        lifecycle.declare(self.agents, "lead-devops", IMAGE, ENDPOINT, "lead_planner")
        self.engine = Engine(root / "store", Executor("trusted-fixture"))
        self.addCleanup(self.engine.close)
        self.app = Application(self.engine, agents=self.agents)

    def request(self, method, path, payload=None, token=None):
        return self.app.dispatch(method, path, payload or {},
                                 self.app.session if token is None else token)

    def test_the_window_starts_and_stops_a_declared_agent(self):
        fake = FakeDocker()
        with patch.multiple(lifecycle, capture=fake, shutil=FakeWhich()):
            code, started = self.request("POST", "/agents/lead-devops/start")
            self.assertEqual((code, started["status"]), (200, "started"))
            code, listed = self.request("GET", "/agents")
            self.assertEqual((code, listed["agents"][0]["state"]), (200, "running"))
            code, stopped = self.request("POST", "/agents/lead-devops/stop")
            self.assertEqual((code, stopped["status"]), (200, "stopped"))

    def test_a_refusal_comes_back_as_a_reason_the_operator_can_read(self):
        # Not a generic 409: "it did not start" is the thing they need to see.
        with patch.multiple(lifecycle, capture=FakeDocker(), shutil=FakeWhich()):
            code, refused = self.request("POST", "/agents/junior-devops/start")
        self.assertEqual((code, refused["status"], refused["reason"]), (200, "refused", "not_enrolled"))

    def test_no_route_takes_an_image_a_command_or_a_path(self):
        fake = FakeDocker()
        with patch.multiple(lifecycle, capture=fake, shutil=FakeWhich()):
            for payload in ({"image": "evil@sha256:" + "c" * 64}, {"command": ["sh"]},
                            {"network": "host"}, {"argv": ["--privileged"]}, {"agent": "other"}):
                with self.subTest(payload=payload):
                    code, _ = self.request("POST", "/agents/lead-devops/start", payload)
                    self.assertEqual(code, 409, "a lifecycle route accepts no fields at all")
        self.assertEqual(fake.calls, [], "nothing with a payload ever reached the daemon")

    def test_unsupported_verbs_are_refused(self):
        with patch.multiple(lifecycle, capture=FakeDocker(), shutil=FakeWhich()):
            for path in ("/agents/lead-devops/exec", "/agents/lead-devops/declare",
                         "/agents/lead-devops/run", "/agents/lead-devops"):
                with self.subTest(path=path):
                    code, _ = self.request("POST", path)
                    self.assertIn(code, (404, 405, 409))

    def test_lifecycle_needs_the_operator_session(self):
        code, _ = self.request("POST", "/agents/lead-devops/start", token="not-the-session")
        self.assertEqual(code, 401)
        code, _ = self.request("GET", "/agents", token="")
        self.assertEqual(code, 401)

    def test_a_server_given_no_registry_offers_nothing_to_start(self):
        bare = Application(self.engine)
        with patch.multiple(lifecycle, capture=FakeDocker(), shutil=FakeWhich()):
            for method, path in (("GET", "/agents"), ("POST", "/agents/lead-devops/start")):
                with self.subTest(path=path):
                    self.assertEqual(bare.dispatch(method, path, {}, bare.session)[0], 409)


class LifecycleCommandTests(unittest.TestCase):
    """The same thing from a terminal, which is where declaring happens."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agents = Path(self.temp.name) / "agents"
        enrol(self.agents, "lead-devops", ["lead_planner"])

    def run_command(self, *argv):
        import io
        import sys
        from contextlib import redirect_stderr, redirect_stdout
        from orch.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", *argv]), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_declaring_then_starting_from_the_command_line(self):
        code, out, err = self.run_command("agent-declare", "--agents", str(self.agents),
                                          "--agent", "lead-devops", "--image", IMAGE,
                                          "--agent-endpoint", ENDPOINT, "--role", "lead_planner")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["kind"], "AgentDeclared")
        with patch.multiple(lifecycle, capture=FakeDocker(), shutil=FakeWhich()):
            code, out, err = self.run_command("agent-start", "--agents", str(self.agents),
                                              "--agent", "lead-devops")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "started")

    def test_a_refusal_exits_non_zero_so_a_script_can_tell(self):
        with patch.multiple(lifecycle, capture=FakeDocker(), shutil=FakeWhich()):
            code, out, _ = self.run_command("agent-start", "--agents", str(self.agents),
                                            "--agent", "lead-devops")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["reason"], "not_declared")

    def test_the_commands_name_what_they_need(self):
        for argv in (("agent-start", "--agent", "lead-devops"),
                     ("agent-declare", "--agents", str(self.agents), "--agent", "lead-devops"),
                     ("agent-stop", "--agents", str(self.agents))):
            with self.subTest(argv=argv):
                code, _, err = self.run_command(*argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
