"""The first thing that has ever had to obey the protocol rather than be scripted through it.\n\nEvery earlier test knew which call came next. A loop does not: it wakes with no instruction,\nfinds work or does not, holds a lease it must keep alive, and has to tell a refusal from an\noutage. These tests run it against a real agent service over a socket, and the interesting\nones are the failures — what it does when the bound is full, when a claim is lost to another\nagent, when a renewal fails, and when it is asked to stop mid-task.\n"""
import os
from pathlib import Path
import secrets as randomness
import tempfile
import threading
import unittest
from unittest.mock import patch

from orch import agent_sessions, task_drafts
from orch.agent_client import AgentClient
from orch.agent_runner import BACKOFF_SECONDS, IDLE_SECONDS, OUTAGE_SECONDS, Decider, Runner
from orch.agent_main import Idle, configured, load, readable
from orch.agent_service import AgentService
from orch.contracts import Rejected, Transient
from orch.engine import Engine
from orch.execution import Executor

SPEC = {"title": "Add a rate limit", "request": "Limit requests from one client.",
        "acceptance_criteria": ["A client over the limit receives 429"],
        "allowed_paths": ["api/limits.py"]}


class Scripted(Decider):
    """A stand-in for the thinking, so the loop can be tested without an intelligence."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def consider(self, draft):
        self.seen.append(draft)
        if not self.answers:
            return ("release", None)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.agents = self.root / "store", self.root / "agents"
        self.engine = Engine(self.store, Executor("trusted-fixture"))
        self.addCleanup(self.engine.close)
        self.task = self.engine.create_task("Rate limiting", draft=True)
        self.enrol("project-manager", "project_manager")
        self.enrol("rival", "project_manager")
        self.service = AgentService(self.store, self.agents)
        thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.service.server.shutdown(), thread.join(30)))
        self.endpoint = "127.0.0.1:" + str(self.service.port)
        self.slept = []

    def enrol(self, name, *roles):
        folder = self.agents / name
        folder.mkdir(parents=True)
        (folder / "role").write_text("\n".join(roles) + "\n", encoding="ascii")
        secret = folder / "secret"
        secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(secret, 0o600)

    def client(self, name="project-manager"):
        return AgentClient(self.endpoint, str(self.agents / name / "secret"))

    def runner(self, *answers, name="project-manager", **kwargs):
        decider = Scripted(*answers)
        run = Runner(self.client(name), decider, "project_manager", lease=60,
                     sleeper=self.slept.append, **kwargs)
        run.decider = decider
        return run

    def state(self, task=None):
        return self.engine.store.task(task or self.task)[0]["state"]

    # Doing the work

    def test_a_runner_finds_work_claims_it_and_drafts(self):
        run = self.runner(("draft", SPEC))
        run.run(rounds=2)
        self.assertEqual(run.worked, 1)
        self.assertEqual(task_drafts.show(self.engine, self.task)["specification"]["title"],
                         SPEC["title"])

    def test_it_reads_before_it_writes(self):
        # The decider is given the current draft, so it can revise rather than overwrite.
        run = self.runner(("draft", SPEC), ("draft", {**SPEC, "title": "Second thoughts"}))
        run.run(rounds=3)
        self.assertEqual([draft["spec_revision"] for draft in run.decider.seen], [None, 0, 1])

    def test_asking_a_question_hands_the_task_back(self):
        run = self.runner(("draft", SPEC), ("ask", [{"question_id": "w", "question": "Which?"}]))
        run.run(rounds=3)
        self.assertEqual(self.state(), "AWAITING_CLARIFICATION")
        # Having asked, it is a person's turn, so the runner must not still be holding it.
        self.assertIsNone(run.held)
        self.assertEqual(agent_sessions.sessions(self.engine.store)["held"], 0)

    def test_a_runner_always_hands_the_task_back_when_it_finishes(self):
        run = self.runner(("draft", SPEC))
        run.run(rounds=2)
        self.assertEqual(agent_sessions.sessions(self.engine.store)["held"], 0)

    # Stopping

    def test_stopping_releases_the_task_rather_than_leaving_it_stuck(self):
        run = self.runner(("draft", SPEC))
        run.take()
        self.assertEqual(run.held, self.task)
        run.stop()
        run.run()
        self.assertIsNone(run.held)
        self.assertEqual(agent_sessions.sessions(self.engine.store)["held"], 0)

    def test_stopping_is_noticed_between_steps_not_only_between_tasks(self):
        run = self.runner(("draft", SPEC))
        run.take()
        run.stop()
        run.step()
        self.assertEqual(run.worked, 0, "a stopped runner does no further work")

    def test_an_exception_on_the_way_out_still_returns_the_task(self):
        run = self.runner()
        run.take()
        with patch.object(run, "round", side_effect=RuntimeError("thrown out")):
            with self.assertRaises(RuntimeError):
                run.run(rounds=1)
        self.assertEqual(agent_sessions.sessions(self.engine.store)["held"], 0)

    # Waiting rather than spinning

    def test_nothing_to_do_waits_rather_than_asking_again_immediately(self):
        task_drafts.draft(self.engine, self.task, SPEC)
        task_drafts.confirm(self.engine, self.task,
                            task_drafts.show(self.engine, self.task)["spec_sha256"], "steph")
        run = self.runner()
        self.assertFalse(run.take())
        self.assertEqual(self.slept, [IDLE_SECONDS])

    def test_a_full_bound_waits_instead_of_claiming_into_a_refusal(self):
        run = self.runner()
        with patch.object(run.client, "available",
                          return_value={"available": [], "count": 0, "at_bound": True,
                                        "admitted": 4, "admission_bound": 4}):
            self.assertFalse(run.take())
        self.assertEqual(self.slept, [BACKOFF_SECONDS])

    def test_an_unreachable_service_is_waited_out_not_given_up_on(self):
        # Being told no and not knowing are different answers, and only one is final.
        run = self.runner()
        with patch.object(run.client, "available", side_effect=Rejected("unreachable")):
            self.assertFalse(run.take())
        self.assertEqual(self.slept, [OUTAGE_SECONDS])

    # Losing a race, and losing a lease

    def test_a_task_taken_by_another_agent_is_skipped_not_retried(self):
        rival = self.client("rival")
        run = self.runner(("draft", SPEC))
        original = run.client.claim

        def stolen(task, role, lease=None, expected_revision=None):
            rival.claim(task, role, lease)      # somebody else got there first
            return original(task, role, lease, expected_revision)

        with patch.object(run.client, "claim", side_effect=stolen):
            self.assertFalse(run.take())
        self.assertIsNone(run.held)
        self.assertEqual(self.slept, [IDLE_SECONDS], "it moved on rather than retrying")

    def test_a_lost_lease_stops_the_work_it_was_holding(self):
        run = self.runner(("draft", SPEC))
        run.take()
        run.renewed_at = -10 ** 6                 # force a renewal on the next step
        with patch.object(run.client, "renew", side_effect=Rejected("lease gone")):
            run.step()
        self.assertIsNone(run.held, "a runner that lost its lease is not holding anything")
        self.assertEqual(run.worked, 0)

    def test_it_renews_before_working_rather_than_after(self):
        # Work that runs long must not be the reason a lease lapses mid-write.
        run = self.runner(("draft", SPEC))
        run.take()
        run.renewed_at = -10 ** 6
        with patch.object(run.client, "renew", wraps=run.client.renew) as renew:
            run.step()
        renew.assert_called_once()
        self.assertEqual(run.worked, 1)

    def test_it_does_not_renew_while_the_lease_is_young(self):
        run = self.runner(("draft", SPEC))
        run.take()
        with patch.object(run.client, "renew") as renew:
            run.step()
        renew.assert_not_called()

    # The decider's own refusals

    def test_a_decider_that_cannot_do_the_task_hands_it_back(self):
        run = self.runner(Rejected("not for me"))
        run.take()
        run.step()
        self.assertIsNone(run.held)
        self.assertEqual(agent_sessions.sessions(self.engine.store)["held"], 0)

    def test_a_refused_write_is_not_attempted_again(self):
        run = self.runner(("draft", {"title": "", "request": "", "acceptance_criteria": []}))
        run.take()
        run.step()
        self.assertIsNone(run.held, "a refusal is final; the task goes back")
        self.assertEqual(run.worked, 0)

    # Telling "no" from "not yet"

    def test_a_refusal_that_may_change_arrives_as_one(self):
        """The gap writing this loop found: a refusal carried no guidance about retrying.\n\nThe published protocol said `retry_allowed` was the authority on whether to ask\nagain, and it appeared only on successful replies, where it is always false. A\nrefusal came back as a bare 409, so the one moment the field was for was the one\nmoment it was absent.\n"""
        rival = self.client("rival")
        rival.claim(self.task, "project_manager", 900)
        with self.assertRaises(Transient):
            self.client().claim(self.task, "project_manager", 900)

    def test_a_refusal_that_will_not_change_stays_final(self):
        # Asking for a role this agent is not enrolled for will never start working.
        with self.assertRaises(Rejected) as caught:
            self.client().claim(self.task, "junior", 900)
        self.assertNotIsInstance(caught.exception, Transient)

    def test_a_transient_write_keeps_the_task_instead_of_handing_it_back(self):
        # The work is still this agent's to do; only the moment was wrong.
        run = self.runner(("draft", SPEC))
        run.take()
        with patch.object(run.client, "draft", side_effect=Transient("not now")):
            run.step()
        self.assertEqual(run.held, self.task, "a transient refusal does not lose the task")
        self.assertEqual(self.slept, [OUTAGE_SECONDS])

    def test_a_final_write_refusal_hands_the_task_back(self):
        run = self.runner(("draft", SPEC))
        run.take()
        with patch.object(run.client, "draft", side_effect=Rejected("never")):
            run.step()
        self.assertIsNone(run.held)

    def test_a_decider_answering_nonsense_is_a_bug_not_a_release(self):
        run = self.runner(("reticulate", None))
        run.take()
        with self.assertRaisesRegex(Rejected, "draft, ask or release"):
            run.step()

    def test_a_lease_below_the_floor_is_refused_at_construction(self):
        with self.assertRaisesRegex(Rejected, "at least"):
            Runner(self.client(), Scripted(), "project_manager", lease=1)


class EntryPointTests(unittest.TestCase):
    """What a container is given, and what it refuses to start without."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secret = Path(self.temp.name) / "secret"
        self.secret.write_text(randomness.token_hex(32) + "\n", encoding="ascii")
        os.chmod(self.secret, 0o600)

    def environment(self, **changes):
        env = {"ORCHD_ENDPOINT": "127.0.0.1:1", "ORCHD_SECRET_FILE": str(self.secret),
               "ORCHD_ROLE": "project_manager"}
        env.update(changes)
        return {key: value for key, value in env.items() if value is not None}

    def test_a_container_is_given_an_endpoint_a_secret_and_a_role(self):
        runner = configured(self.environment())
        self.assertEqual(runner.role, "project_manager")
        self.assertIsInstance(runner.decider, Idle)

    def test_it_refuses_to_start_without_each_of_them(self):
        for missing in ("ORCHD_ENDPOINT", "ORCHD_SECRET_FILE", "ORCHD_ROLE"):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(Rejected, "ORCHD_ENDPOINT, ORCHD_SECRET_FILE and ORCHD_ROLE"):
                    configured(self.environment(**{missing: None}))

    def test_a_misspelled_decider_is_reported_as_one(self):
        # Not as a missing secret: the decider is checked before any file is opened, so the
        # error names the setting that is actually wrong.
        with self.assertRaisesRegex(Rejected, "ORCHD_DECIDER is module:attribute"):
            configured(self.environment(ORCHD_DECIDER="nonsense"))
        with self.assertRaisesRegex(Rejected, "Could not load the decider"):
            configured(self.environment(ORCHD_DECIDER="orch.agent_main:NoSuchThing"))

    def test_something_that_is_not_a_decider_is_refused(self):
        with self.assertRaisesRegex(Rejected, "is not a Decider"):
            configured(self.environment(ORCHD_DECIDER="orch.agent_main:load"))

    def test_an_unusable_secret_names_the_setting_not_the_broker(self):
        missing = str(Path(self.temp.name) / "absent")
        with self.assertRaisesRegex(Rejected, "ORCHD_SECRET_FILE is unusable"):
            configured(self.environment(ORCHD_SECRET_FILE=missing))

    def test_the_lease_must_be_a_number_of_seconds(self):
        with self.assertRaisesRegex(Rejected, "whole number of seconds"):
            configured(self.environment(ORCHD_LEASE="soon"))
        self.assertEqual(configured(self.environment(ORCHD_LEASE="120")).lease, 120)

    def test_the_idle_decider_takes_work_and_hands_it_straight_back(self):
        # Deliberately useless and deliberately harmless: it exercises the loop without
        # writing a specification nobody meant.
        self.assertEqual(Idle().consider({"spec": None}), ("release", None))

    def test_a_decider_can_be_an_instance_as_well_as_a_class(self):
        self.assertIsInstance(load("orch.agent_main:Idle"), Idle)

    # The host that cannot protect a file

    def test_a_private_secret_is_used_where_it_lies(self):
        self.assertEqual(readable(str(self.secret), False), str(self.secret))
        self.assertEqual(readable(str(self.secret), True), str(self.secret),
                         "a file that is already private needs no copy")

    def test_an_exposed_secret_is_refused_unless_the_host_is_declared_unable(self):
        os.chmod(self.secret, 0o644)
        if os.name == "nt":
            # Windows has no modes to expose, so the question does not arise there; the rule
            # is asserted on the platform that has one rather than skipped on this.
            self.assertEqual(readable(str(self.secret), False), str(self.secret))
            return
        self.assertEqual(readable(str(self.secret), False), str(self.secret),
                         "readable does not refuse; the reader does")
        with self.assertRaisesRegex(Rejected, "ORCHD_SECRET_FILE is unusable"):
            configured(self.environment())

    def test_a_declared_unprotected_host_gets_a_private_copy_of_the_same_secret(self):
        if os.name == "nt":
            return
        os.chmod(self.secret, 0o644)
        copied = readable(str(self.secret), True)
        self.assertNotEqual(copied, str(self.secret))
        self.assertEqual(Path(copied).read_text(encoding="ascii"),
                         self.secret.read_text(encoding="ascii"))
        self.assertEqual(os.stat(copied).st_mode & 0o777, 0o600, "the copy is private")
        self.assertEqual(os.stat(Path(copied).parent).st_mode & 0o777, 0o700,
                         "and so is the directory holding it")
        # And the copy is what makes the container usable: the reader accepts it.
        runner = configured(self.environment(ORCHD_HOST_CANNOT_PROTECT_SECRETS="1"))
        self.assertEqual(runner.role, "project_manager")

    def test_the_copy_does_not_pretend_the_host_is_safe(self):
        # It makes the file private inside the container and nothing else, which is why it
        # is opt-in and why it says so. A silent copy would be worse than the refusal.
        if os.name == "nt":
            return
        os.chmod(self.secret, 0o644)
        with patch("sys.stderr") as printed:
            readable(str(self.secret), True)
        said = "".join(str(call) for call in printed.write.call_args_list)
        self.assertIn("still exposed", said)
        self.assertEqual(os.stat(self.secret).st_mode & 0o777, 0o644,
                         "the host file is left exactly as it was")


if __name__ == "__main__":
    unittest.main()
