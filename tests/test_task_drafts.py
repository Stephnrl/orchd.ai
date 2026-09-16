"""The project manager's step, and the human boundary at the end of it.

Until now `create_task` invented a specification, wrote `confirmed_by` and set
`spec_confirmed` in one transaction, so the evidence recorded a human confirmation nobody
performed. These tests fix the replacement: a task can exist with no specification, drafting
is revisable and append-only, questions stop the work until a human answers them, and
reaching `SPEC_READY` takes a human naming the exact words they read.
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
from orch import task_drafts as drafts
from orch.contracts import Rejected

DRAFT = {"title": "Add a rate limit to the public API",
         "request": "Requests from one client should be limited.",
         "acceptance_criteria": ["A client over the limit receives 429"],
         "constraints": ["No new runtime dependency"],
         "allowed_paths": ["api/limits.py"]}
QUESTIONS = [{"question_id": "window", "question": "What time window should the limit apply over?"}]
ANSWERS = [{"question_id": "window", "answer": "A rolling sixty seconds."}]


class DraftSpecTests(unittest.TestCase):
    create = helpers.WorkflowTests.create

    def setUp(self):
        helpers.WorkflowTests.setUp(self)

    def tearDown(self):
        helpers.WorkflowTests.tearDown(self)

    def opened(self, title="Rate limiting"):
        return self.engine.create_task(title, draft=True)

    def drafted(self, task=None, **changes):
        task = task or self.opened()
        drafts.draft(self.engine, task, {**DRAFT, **changes})
        return task

    def state(self, task):
        return self.engine.store.task(task)[0]["state"]

    def events(self, task):
        return [event["event_type"] for event in self.engine.store.events(task)]

    def specs(self, task):
        return [json.loads(row[0]) for row in self.engine.store.db.execute(
            "SELECT payload FROM records WHERE task_id=? AND kind='TaskSpec'", (task,))]

    # A task that has not been specified yet

    def test_a_task_can_be_opened_with_no_specification_at_all(self):
        task = self.opened()
        state, context = self.engine.store.task(task)
        self.assertEqual(state["state"], "DRAFT_SPEC")
        self.assertIsNone(state["spec"])
        self.assertFalse(context["spec_confirmed"])
        self.assertEqual(self.events(task), ["task_created"])

    def test_the_fixture_path_still_produces_a_ready_task(self):
        # Every existing test creates tasks this way; the drafted path is opt-in.
        task = self.engine.create_task("Greeting fixture")
        self.assertEqual(self.state(task), "SPEC_READY")

    def test_an_unspecified_task_cannot_be_confirmed_or_asked_about(self):
        task = self.opened()
        with self.assertRaisesRegex(Rejected, "no specification to confirm"):
            drafts.confirm(self.engine, task, "a" * 64, "steph")
        with self.assertRaisesRegex(Rejected, "before asking about it"):
            drafts.ask(self.engine, task, QUESTIONS)

    # Drafting

    def test_drafting_stores_a_specification_nobody_has_confirmed(self):
        task = self.drafted()
        [spec] = self.specs(task)
        self.assertEqual(spec["revision"], 0)
        self.assertIsNone(spec["confirmed_by"])
        self.assertEqual(self.state(task), "DRAFT_SPEC")
        self.assertIn("spec_drafted", self.events(task))

    def test_revising_keeps_every_earlier_draft(self):
        task = self.drafted()
        drafts.draft(self.engine, task, {**DRAFT, "title": "Rate limit the public API"})
        revisions = sorted(spec["revision"] for spec in self.specs(task))
        self.assertEqual(revisions, [0, 1])
        current = self.engine.store.get(self.engine.store.task(task)[0]["spec"], task, "TaskSpec")
        self.assertEqual(current["title"], "Rate limit the public API")

    def test_a_draft_moves_the_task_revision_so_a_stale_writer_is_refused(self):
        task = self.opened()
        drafts.draft(self.engine, task, DRAFT, expected_revision=0)
        with self.assertRaisesRegex(Rejected, "revision changed"):
            drafts.draft(self.engine, task, DRAFT, expected_revision=0)

    def test_a_specification_needs_an_acceptance_criterion(self):
        task = self.opened()
        with self.assertRaisesRegex(Rejected, "acceptance criterion"):
            drafts.draft(self.engine, task, {**DRAFT, "acceptance_criteria": []})

    def test_a_draft_refuses_unknown_fields_rather_than_dropping_them(self):
        task = self.opened()
        with self.assertRaisesRegex(Rejected, "no field owner"):
            drafts.draft(self.engine, task, {**DRAFT, "owner": "steph"})

    def test_a_draft_refuses_a_secret_and_bounds_every_list(self):
        task = self.opened()
        with self.assertRaisesRegex(Rejected, "must not contain a secret"):
            drafts.draft(self.engine, task, {**DRAFT, "request": "use token: ghp_aaaaaaaaaaaaaaaaaaaa"})
        with self.assertRaisesRegex(Rejected, "at most"):
            drafts.draft(self.engine, task, {**DRAFT, "allowed_paths": ["p%d" % n for n in range(51)]})
        with self.assertRaisesRegex(Rejected, "repeats an entry"):
            drafts.draft(self.engine, task, {**DRAFT, "constraints": ["one", "one"]})

    # Clarification

    def test_asking_stops_the_work_until_a_human_answers(self):
        task = self.drafted()
        report = drafts.ask(self.engine, task, QUESTIONS)
        self.assertEqual(report["state"], "AWAITING_CLARIFICATION")
        self.assertEqual([entry["question_id"] for entry in report["pending_questions"]], ["window"])
        with self.assertRaisesRegex(Rejected, "not DRAFT_SPEC"):
            drafts.draft(self.engine, task, DRAFT)

    def test_a_question_is_bound_to_the_draft_it_is_about(self):
        task = self.drafted()
        spec = self.engine.store.task(task)[0]["spec"]
        drafts.ask(self.engine, task, QUESTIONS)
        request = self.engine.store.get(self.engine.store.task(task)[1]["clarification_request"],
                                        task, "ClarificationRequest")
        self.assertEqual(request["spec"], spec)

    def test_answering_returns_the_task_to_draft_and_records_who_answered(self):
        task = self.drafted()
        drafts.ask(self.engine, task, QUESTIONS)
        report = drafts.answer(self.engine, task, ANSWERS, "steph")
        self.assertEqual(report["state"], "DRAFT_SPEC")
        self.assertEqual(report["pending_questions"], [])
        response = self.engine.store.get(self.engine.store.task(task)[1]["clarification_response"],
                                         task, "ClarificationResponse")
        self.assertEqual(response["actor"], {"principal_id": "steph", "role": "human"})
        self.assertEqual(self.events(task).count("clarification_answered"), 1)

    def test_every_question_must_be_answered_and_no_others(self):
        task = self.drafted()
        drafts.ask(self.engine, task, QUESTIONS + [{"question_id": "scope", "question": "Which endpoints?"}])
        with self.assertRaisesRegex(Rejected, "every question"):
            drafts.answer(self.engine, task, ANSWERS, "steph")
        with self.assertRaisesRegex(Rejected, "every question"):
            drafts.answer(self.engine, task, ANSWERS + [{"question_id": "other", "answer": "no"}], "steph")

    def test_answering_a_task_that_asked_nothing_is_refused(self):
        task = self.drafted()
        with self.assertRaisesRegex(Rejected, "not waiting for an answer"):
            drafts.answer(self.engine, task, ANSWERS, "steph")

    def test_two_questions_may_not_share_an_identifier(self):
        task = self.drafted()
        with self.assertRaisesRegex(Rejected, "share an identifier"):
            drafts.ask(self.engine, task, QUESTIONS + [{"question_id": "window", "question": "Again?"}])

    # The human boundary

    def test_confirming_the_specification_that_was_read_reaches_spec_ready(self):
        task = self.drafted()
        report = drafts.show(self.engine, task)
        confirmed = drafts.confirm(self.engine, task, report["spec_sha256"], "steph")
        self.assertEqual(confirmed["state"], "SPEC_READY")
        self.assertTrue(confirmed["confirmed"])
        current = self.engine.store.get(self.engine.store.task(task)[0]["spec"], task, "TaskSpec")
        self.assertEqual(current["confirmed_by"], "steph")

    def test_a_specification_revised_after_it_was_read_cannot_be_confirmed(self):
        task = self.drafted()
        read = drafts.show(self.engine, task)["spec_sha256"]
        drafts.draft(self.engine, task, {**DRAFT, "request": "Something materially different."})
        with self.assertRaisesRegex(Rejected, "revised since you read it"):
            drafts.confirm(self.engine, task, read, "steph")
        self.assertEqual(self.state(task), "DRAFT_SPEC")

    def test_confirmation_keeps_the_draft_it_confirmed(self):
        task = self.drafted()
        drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        by = {spec["revision"]: spec["confirmed_by"] for spec in self.specs(task)}
        self.assertEqual(by, {0: None, 1: "steph"})

    def test_confirming_is_refused_while_a_question_is_outstanding(self):
        task = self.drafted()
        read = drafts.show(self.engine, task)["spec_sha256"]
        drafts.ask(self.engine, task, QUESTIONS)
        with self.assertRaisesRegex(Rejected, "not DRAFT_SPEC"):
            drafts.confirm(self.engine, task, read, "steph")

    def test_a_confirmation_needs_a_hash_and_a_principal_that_look_like_ones(self):
        task = self.drafted()
        read = drafts.show(self.engine, task)["spec_sha256"]
        with self.assertRaisesRegex(Rejected, "sha256"):
            drafts.confirm(self.engine, task, "not-a-hash", "steph")
        with self.assertRaisesRegex(Rejected, "principal"):
            drafts.confirm(self.engine, task, read, "who is this?")

    def test_nothing_may_be_drafted_once_the_specification_is_confirmed(self):
        task = self.drafted()
        drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        for call in (lambda: drafts.draft(self.engine, task, DRAFT),
                     lambda: drafts.ask(self.engine, task, QUESTIONS)):
            with self.assertRaisesRegex(Rejected, "not DRAFT_SPEC"):
                call()

    def returned(self, task):
        """Put a task where CHANGES_REQUESTED leaves it, which is the only route back to draft.

        Reaching that state for real needs the whole fixture workflow, and the fixture
        workflow only accepts the greeting specification — so the state is set here directly
        rather than drafting something the downstream fixture would refuse.
        """
        state, context = self.engine.store.task(task)
        state["state"] = "CHANGES_REQUESTED"
        self.engine.store.db.execute("UPDATE tasks SET state=? WHERE id=?",
                                     (json.dumps(state), task))
        self.engine.store.db.commit()

    def test_a_confirmation_does_not_survive_a_return_to_draft(self):
        # CHANGES_REQUESTED may send a task back to DRAFT_SPEC. Without dropping the old
        # confirmation, it could reach SPEC_READY again on words that had been rewritten.
        task = self.drafted()
        drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        self.returned(task)
        state, context = self.engine.store.task(task)
        with self.engine.store.transaction():
            self.engine._transition(state, context, "DRAFT_SPEC")
        self.assertFalse(self.engine.store.task(task)[1]["spec_confirmed"])
        state, context = self.engine.store.task(task)
        with self.assertRaisesRegex(Rejected, "confirmation required"):
            with self.engine.store.transaction():
                self.engine._transition(state, context, "SPEC_READY")

    def test_a_task_sent_back_can_be_redrafted_and_confirmed_again(self):
        task = self.drafted()
        drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        self.returned(task)
        state, context = self.engine.store.task(task)
        with self.engine.store.transaction():
            self.engine._transition(state, context, "DRAFT_SPEC")
        drafts.draft(self.engine, task, {**DRAFT, "request": "Rewritten after review."})
        again = drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        self.assertEqual(again["state"], "SPEC_READY")

    # The history it leaves

    def test_the_evidence_reads_in_the_order_things_happened(self):
        task = self.drafted()
        drafts.ask(self.engine, task, QUESTIONS)
        drafts.answer(self.engine, task, ANSWERS, "steph")
        drafts.draft(self.engine, task, {**DRAFT, "request": "Limited over a rolling sixty seconds."})
        drafts.confirm(self.engine, task, drafts.show(self.engine, task)["spec_sha256"], "steph")
        self.assertEqual(self.events(task),
                         ["task_created", "spec_drafted", "clarification_requested", "state_transition",
                          "clarification_answered", "state_transition", "spec_drafted",
                          "spec_confirmed", "state_transition"])

    def test_show_returns_the_request_text_and_the_hash_to_confirm_by(self):
        task = self.drafted()
        report = drafts.show(self.engine, task)
        self.assertEqual(report["request"], DRAFT["request"])
        self.assertEqual(report["specification"]["acceptance_criteria"], DRAFT["acceptance_criteria"])
        self.assertEqual(report["spec_sha256"], self.engine.store.task(task)[0]["spec"]["sha256"])

    # Through the command line

    def cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['orch', *argv, '--data', self.temp.name, '--trusted-fixture']), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = 0
                main()
            except SystemExit as exit:
                code = exit.code
        self.assertEqual(code, expect, err.getvalue())
        return json.loads(out.getvalue()) if code == 0 and out.getvalue() else err.getvalue()

    def write(self, name, value):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return str(path)

    def test_the_whole_stage_works_from_the_command_line(self):
        opened = self.cli('task-open', '--title', 'Rate limiting')
        task = opened['task_id']
        self.assertIsNone(opened['spec'])
        self.cli('spec-draft', '--task', task, '--from', self.write('draft.json', DRAFT))
        self.cli('spec-ask', '--task', task, '--from', self.write('ask.json', QUESTIONS))
        answered = self.cli('spec-answer', '--task', task, '--from', self.write('answer.json', ANSWERS),
                            '--principal', 'steph')
        self.assertEqual(answered['state'], 'DRAFT_SPEC')
        shown = self.cli('spec-show', '--task', task)
        confirmed = self.cli('spec-confirm', '--task', task, '--spec-sha256', shown['spec_sha256'],
                             '--principal', 'steph')
        self.assertEqual(confirmed['state'], 'SPEC_READY')

    def test_the_command_line_refuses_a_stale_hash_and_a_missing_principal(self):
        task = self.cli('task-open', '--title', 'Rate limiting')['task_id']
        self.cli('spec-draft', '--task', task, '--from', self.write('draft.json', DRAFT))
        stale = self.cli('spec-show', '--task', task)['spec_sha256']
        self.cli('spec-draft', '--task', task, '--from', self.write('again.json', {**DRAFT, "title": "Other"}))
        refusal = self.cli('spec-confirm', '--task', task, '--spec-sha256', stale, '--principal', 'steph', expect=2)
        self.assertIn('revised since you read it', refusal)
        missing = self.cli('spec-confirm', '--task', task, '--spec-sha256', stale, expect=2)
        self.assertIn('--principal', missing)


if __name__ == "__main__":
    unittest.main()
