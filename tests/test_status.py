"""One answer to the morning question, and a map that cannot describe what the code stopped doing.

The signals were all there and all separate. These tests fix what the combined view says, and
the two properties that make it worth having: it answers while work is happening, and it tells
the machine being busy apart from the machine being stopped because it is waiting on you.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_workflow as helpers
from orch.__main__ import main
from orch import agent_sessions, status, task_drafts
from orch.api import Application
from orch.github_journal import GitHubJournal

SPEC = {"title": "Add a rate limit", "request": "Limit requests from one client.",
        "acceptance_criteria": ["A client over the limit receives 429"],
        "allowed_paths": ["api/limits.py"]}


class StatusTests(unittest.TestCase):
    def setUp(self):
        helpers.WorkflowTests.setUp(self)

    def tearDown(self):
        helpers.WorkflowTests.tearDown(self)

    def report(self, **kwargs):
        return status.report(self.engine, **kwargs)

    # The summary an operator actually reads

    def test_an_empty_store_needs_no_attention(self):
        summary = self.report()["summary"]
        self.assertEqual(summary["tasks_active"], 0)
        self.assertEqual(summary["waiting_on_you"], 0)
        self.assertFalse(summary["attention"])

    def test_a_task_waiting_on_a_person_is_what_needs_attention(self):
        task = self.engine.create_task()
        self.engine.run(task)                       # -> AWAITING_PLAN_APPROVAL
        report = self.report()
        self.assertEqual(report["summary"]["waiting_on_you"], 1)
        self.assertTrue(report["summary"]["attention"])
        [waiting] = report["tasks"]["waiting_on_a_person"]
        self.assertEqual((waiting["task_id"], waiting["state"]), (task, "AWAITING_PLAN_APPROVAL"))
        self.assertIsNotNone(waiting["since"])

    def test_work_being_done_is_not_work_needing_attention(self):
        # A busy machine and a stopped one look nothing alike, and the whole point of the
        # summary is telling them apart.
        drafting = self.engine.create_task("Rate limiting", draft=True)
        agent_sessions.claim(self.engine, drafting, "project-manager", "project_manager", 900)
        summary = self.report()["summary"]
        self.assertEqual(summary["agents_holding"], 1)
        self.assertEqual(summary["waiting_on_you"], 0)
        self.assertFalse(summary["attention"])

    def test_a_clarification_is_counted_as_waiting_on_a_person(self):
        task = self.engine.create_task("Rate limiting", draft=True)
        task_drafts.draft(self.engine, task, SPEC)
        task_drafts.ask(self.engine, task, [{"question_id": "q", "question": "Which window?"}])
        report = self.report()
        self.assertEqual([w["state"] for w in report["tasks"]["waiting_on_a_person"]],
                         ["AWAITING_CLARIFICATION"])
        self.assertTrue(report["summary"]["attention"])

    # What is being worked on

    def test_holders_are_listed_with_what_they_hold(self):
        task = self.engine.create_task("Rate limiting", draft=True)
        agent_sessions.claim(self.engine, task, "project-manager", "project_manager", 900)
        [holding] = self.report()["agents"]["holding"]
        self.assertEqual((holding["agent"], holding["role"], holding["task_id"], holding["kind"]),
                         ("project-manager", "project_manager", task, "agent"))

    def test_a_lapsed_lease_is_reported_rather_than_hidden(self):
        # Evidence that a container stopped without releasing. Worth seeing, not an alarm.
        from datetime import datetime, timedelta
        from orch.contracts import now
        task = self.engine.create_task("Rate limiting", draft=True)
        agent_sessions.claim(self.engine, task, "project-manager", "project_manager", 900)
        later = (datetime.fromisoformat(now()) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        with patch("orch.storage.now", return_value=later):
            report = self.report()
        self.assertEqual(report["agents"]["held"], 0)
        self.assertEqual(report["agents"]["lapsed_count"], 1)
        self.assertTrue(report["summary"]["attention"])

    def test_available_work_is_counted_by_role(self):
        self.engine.create_task("Rate limiting", draft=True)
        self.engine.create_task("Also drafting", draft=True)
        self.engine.create_task("Greeting fixture")          # SPEC_READY -> lead_planner
        work = self.report()["work"]
        self.assertEqual(work["available_by_role"], {"lead_planner": 1, "project_manager": 2})
        self.assertEqual(work["available"], 3)
        self.assertFalse(work["at_bound"])

    def test_the_bound_is_reported_as_reached(self):
        task = self.engine.create_task("Rate limiting", draft=True)
        agent_sessions.claim(self.engine, task, "project-manager", "project_manager", 900)
        with patch.object(self.engine, "admission_bound", return_value=1):
            report = status.report(self.engine)
        self.assertTrue(report["work"]["at_bound"])
        self.assertTrue(report["summary"]["attention"])

    # It answers while work is happening

    def test_status_does_not_wait_for_the_store_to_go_quiet(self):
        # `storage_usage` asks for quiescence, correctly, because it is maintenance. A status
        # view that did the same would refuse exactly when it was wanted.
        task = self.engine.create_task("Rate limiting", draft=True)
        with self.engine.advancing(task):
            report = self.report()
        self.assertEqual(report["kind"], "OrchdStatus")
        self.assertEqual(report["agents"]["held"], 1)

    def test_it_changes_nothing_and_authorizes_nothing(self):
        self.engine.create_task("Rate limiting", draft=True)
        before = self.engine.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        report = self.report()
        after = self.engine.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)
        self.assertIs(report["live_authorized"], False)

    # Journals

    def test_a_journal_that_will_not_open_is_reported_rather_than_raised(self):
        broken = Path(self.temp.name) / "not-a-journal.sqlite"
        broken.write_text("certainly not a database", encoding="utf-8")
        [entry] = self.report(github_journal=str(broken))["journals"]
        self.assertEqual((entry["family"], entry["status"]), ("GitHub", "unavailable"))

    def test_a_journal_that_opens_reports_its_capacity(self):
        path = Path(self.temp.name) / "github.sqlite"
        GitHubJournal(path).close()
        [entry] = self.report(github_journal=str(path))["journals"]
        self.assertEqual((entry["family"], entry["status"]), ("GitHub", "available"))
        self.assertEqual(entry["records"], 0)
        self.assertGreater(entry["remaining_records"], 0)

    # Through the command line and the API

    def test_the_command_line_prints_the_report(self):
        self.engine.create_task("Rate limiting", draft=True)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["orch", "status", "--data", self.temp.name, "--trusted-fixture"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        self.assertEqual(code, 0, err.getvalue())
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["kind"], "OrchdStatus")
        self.assertEqual(printed["summary"]["work_available"], 1)


class StatusEndpointTests(unittest.TestCase):
    """The same report through the authenticated API, for the operator's own window."""

    def setUp(self):
        helpers.WorkflowTests.setUp(self)
        self.app = Application(self.engine)

    def tearDown(self):
        helpers.WorkflowTests.tearDown(self)

    def test_the_endpoint_answers_with_the_same_report(self):
        self.engine.create_task("Rate limiting", draft=True)
        code, body = self.app.dispatch("GET", "/status", {}, self.app.session)
        self.assertEqual(code, 200)
        self.assertEqual(body["kind"], "OrchdStatus")
        self.assertEqual(body["summary"], status.report(self.engine)["summary"])

    def test_the_endpoint_needs_a_session_like_every_other(self):
        code, _ = self.app.dispatch("GET", "/status", {}, "not-a-session")
        self.assertEqual(code, 401)

    def test_the_endpoint_is_read_only(self):
        for method in ("POST", "DELETE", "PUT"):
            code, _ = self.app.dispatch(method, "/status", {}, self.app.session)
            self.assertNotEqual(code, 200, method + " must not be served")


class ModuleMapTests(unittest.TestCase):
    """The map is generated, so the only thing worth testing is that it still matches."""

    def test_the_map_describes_exactly_the_modules_that_exist(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import module_map
        current = (Path(__file__).resolve().parents[1] / "docs" / "modules.md").read_text(encoding="utf-8")
        self.assertEqual(current, module_map.rendered(),
                         "docs/modules.md is stale; run python scripts/module_map.py")

    def test_every_module_leads_with_a_sentence_saying_what_it_is_for(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import module_map
        for path in module_map.modules():
            with self.subTest(module=path.name):
                self.assertTrue(module_map.summary(path).strip(), path.name + " leads with nothing")


if __name__ == "__main__":
    unittest.main()
