"""Role-scoped, expiring claims for agents that call in rather than run in the kernel.

A worker process holds a task with a lock it keeps for as long as it works. An agent in its
own container calls, exits, thinks and calls again, so it holds a lease instead. Both are
holders in one registry, and these tests fix how the two kinds meet.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_workflow as helpers
from test_task_ownership import hold
from orch.__main__ import main
from orch import agent_sessions as sessions
from orch.contracts import Rejected, now

ROOT = Path(__file__).resolve().parents[1]


def later(seconds):
    return (datetime.fromisoformat(now()) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class AgentSessionTests(unittest.TestCase):
    create = helpers.WorkflowTests.create
    approve = helpers.WorkflowTests.approve

    def setUp(self):
        helpers.WorkflowTests.setUp(self)
        self.stoppers = []

    def tearDown(self):
        for stop in self.stoppers:
            stop()
        helpers.WorkflowTests.tearDown(self)

    def worker_holds(self, task):
        """A real worker process holding that task's lock."""
        ready = Path(self.temp.name) / (task + '.ready')
        release = Path(self.temp.name) / (task + '.release')
        child = hold(self.temp.name, task, ready, release)

        def stop():
            release.write_bytes(b'')
            child.wait(timeout=30)

        for _ in range(600):
            if ready.exists() or child.poll() is not None:
                break
            time.sleep(0.05)
        if not ready.exists():
            stop()
            self.fail('the worker never took the lock: ' + str(child.communicate(timeout=10)))
        self.stoppers.append(stop)

    # The role gate ---------------------------------------------------------------------------

    def test_a_claim_must_match_the_role_the_next_step_needs(self):
        task = self.create()
        self.assertEqual(sessions.role_for(self.engine.task(task)["state"]["state"]), "lead_planner")
        for role in ("junior", "reviewer", "guardian", "human"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(Rejected, 'belongs to lead_planner'):
                    sessions.claim(self.engine, task, 'agent-' + role, role)
        with self.assertRaises(Rejected):
            sessions.claim(self.engine, task, 'lead-devops', 'not_a_role')
        granted = sessions.claim(self.engine, task, 'lead-devops', 'lead_planner')
        self.assertEqual((granted['status'], granted['agent'], granted['role']),
                         ('claimed', 'lead-devops', 'lead_planner'))

    def test_the_role_follows_the_task_through_the_workflow(self):
        task = self.create()
        sessions.claim(self.engine, task, 'lead-devops', 'lead_planner')
        sessions.release(self.engine, task, 'lead-devops')
        self.engine.run(task)
        # Waiting on a human approval, so no agent may take it.
        self.assertEqual(sessions.role_for(self.engine.task(task)["state"]["state"]), "human")
        for role in ('lead_planner', 'junior'):
            with self.assertRaisesRegex(Rejected, 'belongs to human'):
                sessions.claim(self.engine, task, 'someone', role)
        self.approve(task)
        self.assertEqual(sessions.role_for(self.engine.task(task)["state"]["state"]), "junior")
        self.assertEqual(sessions.claim(self.engine, task, 'junior-devops', 'junior')['role'], 'junior')

    def test_a_finished_task_needs_nobody(self):
        task = self.create()
        self.engine.cancel(task, self.engine.task(task)["state"]["revision"], "Not needed")
        self.assertIsNone(sessions.role_for(self.engine.task(task)["state"]["state"]))
        with self.assertRaisesRegex(Rejected, 'needs no agent'):
            sessions.claim(self.engine, task, 'lead-devops', 'lead_planner')

    # Where the two kinds of holder meet --------------------------------------------------------

    def test_a_worker_and_an_agent_never_hold_the_same_task(self):
        task = self.create()
        sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 300)
        with self.assertRaisesRegex(Rejected, 'held by agent lead-devops'):
            self.engine.advance(task)
        sessions.release(self.engine, task, 'lead-devops')
        self.assertEqual(self.engine.run(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")
        # And the other way round: a worker's lock refuses an agent.
        other = self.create()
        self.worker_holds(other)
        with self.assertRaisesRegex(Rejected, 'advanced elsewhere'):
            sessions.claim(self.engine, other, 'lead-devops', 'lead_planner')

    def test_an_expired_lease_frees_the_task_without_repeating_work(self):
        task = self.create()
        sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 60)
        with patch('orch.storage.now', return_value=later(3600)):
            self.assertFalse(self.engine.store.live(self.engine.store.owner(task)))
            with self.assertRaisesRegex(Rejected, 'expired'):
                sessions.renew(self.engine, task, 'lead-devops')
            # A worker may take an expired task, and records that it did.
            self.assertEqual(self.engine.run(task)["state"]["state"], "AWAITING_PLAN_APPROVAL")
        taken = [e for e in self.engine.events(task) if e["event_type"] == "task_ownership_taken"]
        self.assertEqual(len(taken), 1)

    def test_a_lease_counts_against_the_admission_bound(self):
        held, waiting = self.create(), self.create()
        sessions.claim(self.engine, held, 'lead-devops', 'lead_planner', 300)
        with patch.object(self.engine, 'admission_bound', return_value=1):
            with self.assertRaisesRegex(Rejected, 'Too many tasks'):
                sessions.claim(self.engine, waiting, 'other-lead', 'lead_planner')
            with self.assertRaisesRegex(Rejected, 'Too many tasks'):
                self.engine.advance(waiting)
        sessions.release(self.engine, held, 'lead-devops')
        with patch.object(self.engine, 'admission_bound', return_value=1):
            self.assertEqual(sessions.claim(self.engine, waiting, 'other-lead', 'lead_planner')['status'],
                             'claimed')

    # Ownership of a session ----------------------------------------------------------------------

    def test_only_the_agent_that_claimed_may_renew_or_release(self):
        task = self.create()
        sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 300)
        for action in (lambda: sessions.renew(self.engine, task, 'junior-devops'),
                       lambda: sessions.release(self.engine, task, 'junior-devops')):
            with self.assertRaisesRegex(Rejected, 'belongs to agent lead-devops'):
                action()
        self.assertEqual(sessions.renew(self.engine, task, 'lead-devops', 600)['status'], 'renewed')
        self.assertEqual(sessions.release(self.engine, task, 'lead-devops')['status'], 'released')
        for action in (lambda: sessions.renew(self.engine, task, 'lead-devops'),
                       lambda: sessions.release(self.engine, task, 'lead-devops')):
            with self.assertRaisesRegex(Rejected, 'No agent session'):
                action()

    def test_renewal_extends_the_lease_it_already_holds(self):
        task = self.create()
        granted = sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 60)
        renewed = sessions.renew(self.engine, task, 'lead-devops', 900)
        self.assertGreater(renewed['expires_at'], granted['expires_at'])
        self.assertEqual((renewed['generation'], renewed['task_revision']),
                         (granted['generation'], granted['task_revision']))

    def test_a_lease_outlives_the_process_that_claimed_it(self):
        task = self.create()
        sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 600)
        code = ("import sys, json\n"
                "from orch.storage import Store\n"
                "from orch import agent_sessions\n"
                "store = Store(sys.argv[1])\n"
                "print(json.dumps(agent_sessions.sessions(store)['sessions']))\n")
        result = subprocess.run([sys.executable, '-c', code, self.temp.name], cwd=ROOT,
                                env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        seen = json.loads(result.stdout)
        self.assertEqual([(s['agent'], s['role'], s['kind'], s['live']) for s in seen],
                         [('lead-devops', 'lead_planner', 'agent', True)])

    def test_sessions_report_both_kinds_of_holder(self):
        agent_task, worker_task = self.create(), self.create()
        sessions.claim(self.engine, agent_task, 'lead-devops', 'lead_planner', 300)
        with self.engine.store.transaction():
            self.engine.store.claim(worker_task, self.engine.owner, 0, 1)
        listed = {row['task_id']: row for row in sessions.sessions(self.engine.store)['sessions']}
        self.assertEqual(listed[agent_task]['kind'], 'agent')
        self.assertEqual(listed[worker_task]['kind'], 'worker')
        self.assertIsNone(listed[worker_task]['expires_at'])

    def test_agent_names_and_lease_durations_are_validated(self):
        task = self.create()
        for agent in ('', 'has space', '-leading', 'x' * 65, 'slash/es', 7):
            with self.subTest(agent=agent):
                with self.assertRaises(Rejected):
                    sessions.claim(self.engine, task, agent, 'lead_planner')
        for seconds in (0, 59, 3601, -1, 'soon', 12.5):
            with self.subTest(seconds=seconds):
                with self.assertRaises(Rejected):
                    sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', seconds)
        self.assertIsNone(self.engine.store.owner(task))

    def test_a_claim_can_be_fenced_on_the_revision_it_read(self):
        task = self.create()
        state = self.engine.task(task)["state"]
        with self.assertRaisesRegex(Rejected, 'revision changed'):
            sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 300, state["revision"] + 1)
        self.assertEqual(sessions.claim(self.engine, task, 'lead-devops', 'lead_planner', 300,
                                        state["revision"])['task_revision'], state["revision"])

    def test_cli_claims_renews_lists_and_releases(self):
        task = self.create()

        def cli(*argv, expect=0):
            output = io.StringIO()
            try:
                with (patch.object(sys, 'argv', ['orch', *argv, '--data', self.temp.name, '--trusted-fixture']),
                      redirect_stdout(output), redirect_stderr(io.StringIO())):
                    main()
            except SystemExit as code:
                self.assertEqual(code.code, expect)
                return None
            self.assertEqual(expect, 0)
            return json.loads(output.getvalue())

        cli('agent-claim', '--task', task, '--agent', 'lead-devops', expect=2)     # A role is required.
        cli('agent-claim', '--task', task, '--agent', 'lead-devops', '--role', 'junior', expect=2)
        claimed = cli('agent-claim', '--task', task, '--agent', 'lead-devops', '--role', 'lead_planner',
                      '--lease', '300')
        self.assertEqual(claimed['status'], 'claimed')
        self.assertEqual(cli('agent-renew', '--task', task, '--agent', 'lead-devops')['status'], 'renewed')
        self.assertEqual(cli('agent-sessions')['held'], 1)
        self.assertEqual(cli('agent-release', '--task', task, '--agent', 'lead-devops')['status'], 'released')
        self.assertEqual(cli('agent-sessions')['held'], 0)


if __name__ == '__main__':
    unittest.main()
