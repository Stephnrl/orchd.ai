"""A question to a role, and the answer it gets — which is not a task and cannot become one.

Every other path through this control plane exists to reach an approved effect, and a person
stands at each gate. A question reaches no effect, so it has no gate; that argument is only
sound while a consultation cannot turn into something that changes anything. The first test
here is the one that keeps it sound, and the rest hold the small lifecycle to its promises:
the role the question was put to is the only role that may answer it, an answer is recorded as
an opinion, and nothing anywhere records that it authorized something.
"""
import ast
import json
import os
from pathlib import Path
import secrets as randomness
import tempfile
import threading
import unittest

from orch import consultations
from orch.agent_client import AgentClient
from orch.agent_runner import Decider, Runner
from orch.agent_service import AgentService
from orch.api import Application
from orch.contracts import Rejected
from orch.engine import Engine
from orch.execution import Executor
from orch.status import report as status_report
from orch.storage import CURRENT_VERSION

ROOT = Path(__file__).resolve().parents[1]
QUESTION = 'I have a repo with terraform here. What are your inputs if I deployed this?'


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root / 'store', Executor('trusted-fixture'))
        self.addCleanup(self.engine.close)
        self.store = self.engine.store

    def ask(self, role='lead_planner', question=QUESTION, asked_by='steph', about=None):
        return consultations.ask(self.store, role, question, asked_by, about)

    def answer(self, consultation, agent='lead-devops', reply='Two inputs and a region-locked AMI.',
               roles=('lead_planner',)):
        consultations.take(self.store, consultation, agent, roles)
        return consultations.answer(self.store, consultation, agent, reply, roles)

    # The promise the whole design rests on ---------------------------------------------------

    def test_a_consultation_leaves_no_task_no_approval_and_no_request_behind(self):
        """If asking could produce any of these, asking without a gate would be a hole."""
        asked = self.ask()
        self.answer(asked['consultation_id'])
        consultations.close(self.store, asked['consultation_id'], 'steph')
        for table in ('tasks', 'records', 'events', 'artifacts', 'approvals', 'operations',
                      'effects', 'task_owners', 'broker_requests'):
            with self.subTest(table=table):
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_nothing_in_this_module_reaches_the_state_machine(self):
        # Read from the syntax rather than the prose: an import of the engine, or a call that
        # advances or approves anything, is how a later edit would quietly turn an answer into
        # an effect. The module may read a task; it may not do anything to one.
        tree = ast.parse((ROOT / 'orch/consultations.py').read_text(encoding='utf-8'))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or '')
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ('engine', '.engine', 'orch.engine', 'execution', 'dispatch', 'broker'):
            with self.subTest(imported=forbidden):
                self.assertNotIn(forbidden, imported)
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        for forbidden in ('advance', 'approve', 'run', 'create_task', 'create_repository_task',
                          'claim', 'release', 'put_record', 'write_artifact'):
            with self.subTest(called=forbidden):
                self.assertNotIn(forbidden, called)

    def test_an_answer_says_it_is_an_opinion_wherever_it_is_read(self):
        answered = self.answer(self.ask()['consultation_id'])
        self.assertIs(answered['answer_is_untrusted'], True)
        self.assertIs(answered['approves_nothing'], True)
        self.assertIs(answered['live_authorized'], False)
        listed = consultations.listed(self.store)['consultations'][0]
        self.assertIs(listed['answer_is_untrusted'], True)
        self.assertIs(listed['approves_nothing'], True)

    # Who may ask, and who may answer ---------------------------------------------------------

    def test_a_question_goes_to_a_role_an_agent_may_actually_hold(self):
        for role in ('human', 'nobody', '', None):
            with self.subTest(role=role):
                with self.assertRaises(Rejected):
                    self.ask(role=role)

    def test_only_the_role_the_question_was_put_to_may_answer_it(self):
        asked = self.ask(role='lead_planner')['consultation_id']
        with self.assertRaisesRegex(Rejected, 'does not work as lead_planner'):
            consultations.take(self.store, asked, 'junior-devops', ('junior',))
        with self.assertRaisesRegex(Rejected, 'does not work as lead_planner'):
            consultations.answer(self.store, asked, 'junior-devops', 'Mine now', ('junior',))
        self.assertEqual(consultations.get(self.store, asked)['state'], 'asked')

    def test_an_agent_only_sees_questions_its_roles_could_answer(self):
        self.ask(role='lead_planner')
        self.ask(role='junior', question='Is this module ours?')
        for roles, expected in ((('junior',), ['Is this module ours?']),
                                (('lead_planner',), [QUESTION]),
                                (('lead_planner', 'junior'), [QUESTION, 'Is this module ours?']),
                                (('reviewer',), [])):
            with self.subTest(roles=roles):
                offered = consultations.open_for(self.store, roles)
                self.assertEqual(sorted(row['question'] for row in offered), sorted(expected))

    def test_two_agents_of_one_role_do_not_both_answer(self):
        asked = self.ask()['consultation_id']
        consultations.take(self.store, asked, 'lead-one', ('lead_planner',))
        with self.assertRaisesRegex(Rejected, 'already answering'):
            consultations.take(self.store, asked, 'lead-two', ('lead_planner',))
        with self.assertRaisesRegex(Rejected, 'is answering'):
            consultations.answer(self.store, asked, 'lead-two', 'Mine', ('lead_planner',))
        # And it is no longer offered while somebody is writing.
        self.assertEqual(consultations.open_for(self.store, ('lead_planner',)), [])

    def test_an_agent_that_took_a_question_and_vanished_does_not_hold_it_for_ever(self):
        asked = self.ask()['consultation_id']
        consultations.take(self.store, asked, 'lead-gone', ('lead_planner',))
        record = consultations.get(self.store, asked)
        record['taken_at'] = '2020-01-01T00:00:00Z'
        with self.store.transaction():
            consultations.save(self.store, record)
        self.assertEqual([row['consultation_id'] for row in consultations.open_for(self.store, ('lead_planner',))], [asked])
        self.assertEqual(self.answer(asked, agent='lead-here')['agent'], 'lead-here')

    # The lifecycle ---------------------------------------------------------------------------

    def test_an_answered_question_is_not_answered_twice(self):
        asked = self.ask()['consultation_id']
        self.answer(asked)
        with self.assertRaisesRegex(Rejected, 'already answered'):
            consultations.answer(self.store, asked, 'lead-devops', 'And another thing', ('lead_planner',))

    def test_a_withdrawn_question_is_never_answered(self):
        asked = self.ask()['consultation_id']
        self.assertEqual(consultations.withdraw(self.store, asked, 'steph')['state'], 'withdrawn')
        with self.assertRaisesRegex(Rejected, 'withdrawn'):
            consultations.answer(self.store, asked, 'lead-devops', 'Too late', ('lead_planner',))
        with self.assertRaisesRegex(Rejected, 'that one is withdrawn'):
            consultations.withdraw(self.store, asked, 'steph')

    def test_an_answered_question_cannot_be_withdrawn_and_is_closed_instead(self):
        asked = self.ask()['consultation_id']
        self.answer(asked)
        with self.assertRaisesRegex(Rejected, 'that one is answered'):
            consultations.withdraw(self.store, asked, 'steph')
        self.assertEqual(consultations.close(self.store, asked, 'steph')['state'], 'closed')
        # Nor once it has been read: the answer stands, and there is nothing to take back.
        with self.assertRaisesRegex(Rejected, 'that one is closed'):
            consultations.withdraw(self.store, asked, 'steph')

    def test_only_an_answered_question_is_closed(self):
        asked = self.ask()['consultation_id']
        with self.assertRaisesRegex(Rejected, 'Only an answered consultation'):
            consultations.close(self.store, asked, 'steph')

    def test_an_unknown_consultation_is_refused_rather_than_invented(self):
        for act in (lambda: consultations.get(self.store, 'a' * 32),
                    lambda: consultations.take(self.store, 'a' * 32, 'lead-devops'),
                    lambda: consultations.answer(self.store, 'a' * 32, 'lead-devops', 'Hi'),
                    lambda: consultations.withdraw(self.store, 'a' * 32, 'steph')):
            with self.subTest(act=act):
                with self.assertRaisesRegex(Rejected, 'Unknown consultation'):
                    act()

    def test_a_question_may_name_a_task_as_read_only_context(self):
        task = self.engine.create_task('Something to discuss')
        # What the task already is, so the comparison is about what asking added: nothing.
        before = self.store.db.execute('SELECT count(*) FROM records WHERE task_id=?', (task,)).fetchone()[0]
        revision = self.engine.task(task)['state']['revision']
        asked = self.ask(question='The junior opened this. What do you think?', about=task)
        self.assertEqual(asked['about'], task)
        answered = self.answer(asked['consultation_id'], reply='It needs a test for the empty case.')
        # The task it discusses is untouched: same revision, no new evidence, still nobody's.
        self.assertEqual(self.engine.task(task)['state']['revision'], revision)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM records WHERE task_id=?', (task,)).fetchone()[0], before)
        self.assertIsNone(self.store.owner(task))
        self.assertIs(answered['approves_nothing'], True)

    def test_a_question_cannot_name_a_task_that_does_not_exist(self):
        with self.assertRaisesRegex(Rejected, 'Unknown task'):
            self.ask(about='no-such-task')

    # What is accepted ------------------------------------------------------------------------

    def test_a_question_and_an_answer_are_bounded(self):
        with self.assertRaises(Rejected):
            self.ask(question='x' * (consultations.MAX_QUESTION + 1))
        for empty in ('', '   ', None, 7):
            with self.subTest(empty=empty):
                with self.assertRaises(Rejected):
                    self.ask(question=empty)
        asked = self.ask()['consultation_id']
        with self.assertRaises(Rejected):
            consultations.answer(self.store, asked, 'lead-devops', 'y' * (consultations.MAX_ANSWER + 1))

    def test_text_that_looks_like_a_credential_is_redacted_and_said_to_be(self):
        # Refusing would be the wrong trade here: reviewing terraform is the use case, and
        # terraform is full of lines that look like this.
        asked = self.ask(question='Review this: password = hunter2 and token: ghp_' + 'a' * 20)
        self.assertNotIn('hunter2', asked['question'])
        self.assertNotIn('ghp_', asked['question'])
        self.assertIs(asked['redacted'], True)
        answered = self.answer(asked['consultation_id'], reply='Use bearer sk-secretvalue instead')
        self.assertNotIn('sk-secretvalue', answered['answer'])
        self.assertIs(answered['redacted'], True)
        self.assertNotIn('hunter2', json.dumps(consultations.listed(self.store)))

    def test_a_question_that_is_not_redacted_says_so_too(self):
        self.assertIs(self.ask()['redacted'], False)
        self.assertIs(self.answer(self.ask()['consultation_id'])['redacted'], False)

    def test_who_asked_is_a_principal_and_who_answered_is_an_agent(self):
        with self.assertRaises(Rejected):
            self.ask(asked_by='not a principal!')
        asked = self.ask()['consultation_id']
        with self.assertRaises(Rejected):
            consultations.take(self.store, asked, 'not an agent!')

    def test_open_questions_are_bounded_so_a_queue_cannot_be_filled_for_ever(self):
        for index in range(consultations.MAX_OPEN):
            self.ask(question='Question %d' % index)
        with self.assertRaisesRegex(Rejected, 'already %d open' % consultations.MAX_OPEN):
            self.ask()
        # Answering one makes room again; closed and withdrawn questions do not count.
        self.answer(consultations.listed(self.store)['consultations'][0]['consultation_id'])
        self.ask(question='Room again')

    # What an operator sees -------------------------------------------------------------------

    def test_the_status_view_counts_questions_and_answers_nobody_has_read(self):
        quiet = status_report(self.engine)
        self.assertEqual(quiet['consultations'], {'open': 0, 'answered': 0, 'closed': 0, 'total': 0})
        self.assertIs(quiet['summary']['attention'], False)
        asked = self.ask()['consultation_id']
        self.assertEqual(status_report(self.engine)['summary']['questions_open'], 1)
        self.answer(asked)
        busy = status_report(self.engine)
        self.assertEqual(busy['summary']['answers_to_read'], 1)
        self.assertIs(busy['summary']['attention'], True, 'finished work nobody has read is waiting on a person')
        consultations.close(self.store, asked, 'steph')
        self.assertIs(status_report(self.engine)['summary']['attention'], False)

    def test_the_listing_reports_states_and_refuses_a_state_that_is_not_one(self):
        self.ask()
        answered = self.ask(question='Second')['consultation_id']
        self.answer(answered)
        self.assertEqual(consultations.listed(self.store)['count'], 2)
        self.assertEqual(consultations.listed(self.store, state='answered')['count'], 1)
        self.assertEqual(consultations.listed(self.store)['open'], 1)
        with self.assertRaisesRegex(Rejected, 'Unknown consultation state'):
            consultations.listed(self.store, state='pondering')

    def test_the_store_carries_the_schema_version_that_added_this(self):
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], CURRENT_VERSION)
        self.assertEqual(CURRENT_VERSION, 6)


class ConsultationApiTests(unittest.TestCase):
    """The window's side: one local operator session, and no route into the workflow."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'store', Executor('trusted-fixture'))
        self.addCleanup(self.engine.close)
        self.app = Application(self.engine)

    def request(self, method, path, payload=None, token=None):
        return self.app.dispatch(method, path, payload or {}, self.app.session if token is None else token)

    def test_a_question_is_asked_read_and_closed_through_the_window(self):
        code, asked = self.request('POST', '/consultations', {'role': 'lead_planner', 'question': QUESTION})
        self.assertEqual(code, 201)
        self.assertEqual(asked['asked_by'], 'local-operator')
        code, listed = self.request('GET', '/consultations')
        self.assertEqual((code, listed['count'], listed['open']), (200, 1, 1))
        consultations.take(self.engine.store, asked['consultation_id'], 'lead-devops', ('lead_planner',))
        consultations.answer(self.engine.store, asked['consultation_id'], 'lead-devops', 'Two inputs.', ('lead_planner',))
        code, closed = self.request('POST', '/consultations/' + asked['consultation_id'] + '/close')
        self.assertEqual((code, closed['state']), (200, 'closed'))

    def test_a_question_can_be_withdrawn_through_the_window(self):
        _, asked = self.request('POST', '/consultations', {'role': 'junior', 'question': 'Is this ours?'})
        code, withdrawn = self.request('POST', '/consultations/' + asked['consultation_id'] + '/withdraw')
        self.assertEqual((code, withdrawn['state']), (200, 'withdrawn'))

    def test_the_window_cannot_answer_a_question_itself(self):
        # An operator typing the answer would be evidence of an agent's opinion that no agent
        # held. There is no route for it, and the refusal is the same 404 any other has.
        _, asked = self.request('POST', '/consultations', {'role': 'lead_planner', 'question': QUESTION})
        for method, suffix, payload in (('POST', '/answer', {'answer': 'I say yes'}),
                                        ('POST', '/take', {}),
                                        ('POST', '/approve', {})):
            with self.subTest(suffix=suffix):
                code, _ = self.request(method, '/consultations/' + asked['consultation_id'] + suffix, payload)
                self.assertIn(code, (404, 405, 409))
        self.assertEqual(consultations.get(self.engine.store, asked['consultation_id'])['state'], 'asked')

    def test_unknown_fields_and_unknown_roles_are_refused(self):
        for payload in ({'role': 'human', 'question': 'Hello'},
                        {'role': 'lead_planner'},
                        {'question': 'Who is there?'},
                        {'role': 'lead_planner', 'question': 'Hi', 'approved': True},
                        {'role': 'lead_planner', 'question': 'Hi', 'about': 'no-such-task'}):
            with self.subTest(payload=payload):
                code, _ = self.request('POST', '/consultations', payload)
                self.assertEqual(code, 409)

    def test_consultations_need_the_operator_session_like_everything_else(self):
        code, _ = self.request('GET', '/consultations', token='not-the-session')
        self.assertEqual(code, 401)
        code, _ = self.request('POST', '/consultations', {'role': 'junior', 'question': 'Hi'}, token='')
        self.assertEqual(code, 401)


class ConsultationServiceTests(unittest.TestCase):
    """The agent's side: answering is authenticated, role-scoped, and opens no store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.agents = self.root / 'store', self.root / 'agents'
        engine = Engine(self.data, Executor('trusted-fixture'))
        try:
            self.asked = consultations.ask(engine.store, 'lead_planner', QUESTION, 'steph')['consultation_id']
        finally:
            engine.close()
        for name, role in (('lead-devops', 'lead_planner'), ('junior-devops', 'junior')):
            folder = self.agents / name
            folder.mkdir(parents=True)
            (folder / 'role').write_text(role + "\n", encoding='ascii')
            secret = folder / 'secret'
            secret.write_text(randomness.token_hex(32) + "\n", encoding='ascii')
            os.chmod(secret, 0o600)
        self.service = AgentService(self.data, self.agents)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.temp.cleanup)
        self.endpoint = '127.0.0.1:' + str(self.service.port)

    def tearDown(self):
        self.service.server.shutdown()
        self.thread.join(timeout=30)

    def client(self, name):
        return AgentClient(self.endpoint, self.agents / name / 'secret')

    def opened(self):
        engine = Engine(self.data, Executor('trusted-fixture'))
        try:
            return consultations.get(engine.store, self.asked)
        finally:
            engine.close()

    def test_an_agent_is_offered_only_questions_for_its_own_roles(self):
        self.assertEqual([row['question'] for row in self.client('lead-devops').consultations()['consultations']],
                         [QUESTION])
        self.assertEqual(self.client('junior-devops').consultations()['consultations'], [])

    def test_no_request_says_which_agent_is_answering(self):
        wire = json.loads((ROOT / 'contracts/agent-v1.schema.json').read_text())['$defs']
        for kind in ('AgentConsultationsRequest', 'AgentConsultTakeRequest', 'AgentConsultAnswerRequest'):
            with self.subTest(kind=kind):
                self.assertNotIn('agent', wire[kind]['properties'])

    def test_the_longest_permitted_answer_fits_in_one_request_in_any_alphabet(self):
        # The bound exists because an answer travels in one 16 KiB authenticated request and a
        # character outside ASCII costs three bytes there. An agent refused for length would
        # simply not answer, and nobody would be told why, so the limit is the length that can
        # always be sent.
        longest = '中' * consultations.MAX_ANSWER
        reply = self.client('lead-devops').consult_answer(self.asked, longest)
        self.assertEqual(reply['consultation']['answer'], longest)

    def test_an_agent_answers_a_question_put_to_its_role(self):
        self.client('lead-devops').consult_take(self.asked)
        reply = self.client('lead-devops').consult_answer(self.asked, 'The VPC id and the instance type.')
        self.assertEqual(reply['consultation']['state'], 'answered')
        self.assertEqual(reply['consultation']['agent'], 'lead-devops')
        self.assertIs(reply['consultation']['answer_is_untrusted'], True)
        self.assertIs(reply['live_authorized'], False)

    def test_an_agent_cannot_answer_for_a_role_it_was_not_enrolled_for(self):
        with self.assertRaises(Rejected):
            self.client('junior-devops').consult_answer(self.asked, 'I will take this one')
        self.assertEqual(self.opened()['state'], 'asked')

    def test_answering_is_recorded_against_the_secret_that_signed_it(self):
        self.client('lead-devops').consult_answer(self.asked, 'Mine')
        self.assertEqual(self.opened()['agent'], 'lead-devops')

    def test_answering_records_presence_like_every_other_authenticated_call(self):
        self.client('lead-devops').consultations()
        engine = Engine(self.data, Executor('trusted-fixture'))
        try:
            self.assertEqual(engine.store.presence()['lead-devops']['route'], 'agent-consultations')
        finally:
            engine.close()

    def test_the_loop_answers_a_question_before_it_looks_for_work(self):
        class Opinion(Decider):
            def consider(self, draft):
                raise AssertionError('The loop looked for work while a question was waiting')

            def advise(self, question):
                return 'Three inputs, and nothing applies from a laptop.'

        runner = Runner(self.client('lead-devops'), Opinion(), 'lead_planner', sleeper=lambda seconds: None)
        self.assertEqual(runner.run(rounds=1), 1)
        self.assertEqual(runner.answered, 1)
        self.assertIsNone(runner.held, 'answering a question holds nothing')
        self.assertEqual(self.opened()['answer'], 'Three inputs, and nothing applies from a laptop.')

    def test_a_decider_with_nothing_to_say_leaves_the_question_for_someone_else(self):
        # Taking before thinking would strand the question for a quarter of an hour.
        runner = Runner(self.client('lead-devops'), Decider(), 'lead_planner', sleeper=lambda seconds: None)
        runner.run(rounds=1)
        self.assertEqual(runner.answered, 0)
        self.assertEqual(self.opened()['state'], 'asked')

    def test_a_decider_that_refuses_one_question_leaves_it_and_keeps_going(self):
        class Fussy(Decider):
            def consider(self, draft):
                return ('release', None)

            def advise(self, question):
                raise Rejected('Not mine to answer')

        runner = Runner(self.client('lead-devops'), Fussy(), 'lead_planner', sleeper=lambda seconds: None)
        runner.run(rounds=1)
        self.assertEqual(self.opened()['state'], 'asked')


if __name__ == '__main__':
    unittest.main()
