import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.jira_actions import ACTIONS, read_plan, preview_action, action_scope, reconcile_action, validate_profile
from orch.jira_journal import JiraJournal
from orch.jira_backup import backup_journal, drill_journal_backup
from jira_action_support import profile, intent, capture, recovery_capture


class JiraActionTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()

    def test_complete_acceptance_scenario_retains_reviewable_artifacts(self):
        from scripts.jira_acceptance import run
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root)/'acceptance'
            with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                result = run(destination)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual({row['action'] for row in result['records']}, set(ACTIONS))
            self.assertEqual(json.loads((destination/'acceptance.json').read_bytes()), result)
            self.assertEqual(result['drill']['binding']['recovery_plans_checked'], len(ACTIONS))
            self.assertEqual(result['deployment']['status'], 'blocked')
            with self.assertRaises(FileExistsError): run(destination)

    def test_all_actions_preview_stage_reopen_recover_and_backup(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'jira.sqlite'
            journal = JiraJournal(path)
            try:
                for number, action in enumerate(ACTIONS, 1):
                    proposal = intent(action, number)
                    observations = capture(proposal, self.profile)
                    before = copy.deepcopy((proposal, self.profile, observations))
                    with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                        preview = preview_action(proposal, self.profile, observations)
                        staged = journal.stage_action(proposal, self.profile, observations, preview['sha256'])
                    self.assertEqual((proposal, self.profile, observations), before)
                    self.assertEqual(staged, journal.stage_action(proposal, self.profile, observations, preview['sha256']))
                    scope_sha = staged['binding']['sha256']
                    journal.reserve(proposal['operation_id'], scope_sha)
                    saved = journal.get(proposal['operation_id'])
                    recovery = journal.reconcile(proposal['operation_id'], scope_sha, recovery_capture(saved['scope']))
                    self.assertEqual(recovery['status'], 'candidate_observed', action)
                    for key in ('retry_allowed', 'live_authorized', 'remote_effect_confirmed'): self.assertFalse(recovery[key])
                    self.assertNotIn(proposal['parameters'].get('description', 'NO_PRIVATE_TEXT'), canonical(recovery).decode())
                backup = backup_journal(path, Path(root)/'backup')
                drill = drill_journal_backup(Path(root)/'backup', backup['sha256'])
                self.assertEqual(drill['binding']['existing_uncertain_checked'], 5)
                self.assertEqual(drill['binding']['recovery_plans_checked'], 5)
                reader = JiraJournal(path, read_only=True)
                try:
                    self.assertEqual(reader.audit(), journal.audit())
                finally:
                    reader.close()
            finally:
                journal.close()

    def test_epic_story_and_task_creation_payloads_use_configured_ids(self):
        for issue_type in ('epic', 'story', 'task'):
            proposal = intent()
            proposal['parameters'].update(issue_type=issue_type, epic_name='Fixture Epic' if issue_type == 'epic' else None,
                                          epic=None if issue_type == 'epic' else {'id': '300', 'key': 'ENG-9'})
            preview = preview_action(proposal, self.profile, capture(proposal, self.profile))
            fields = preview['binding']['request']['body']['fields']
            self.assertEqual(fields['issuetype'], {'id': self.profile['issue_types'][issue_type]})
            self.assertEqual(fields['project'], {'id': '100'})
            self.assertEqual(fields['labels'], ['orchd-op-'+proposal['operation_id']])
            if issue_type == 'epic': self.assertEqual(fields['customfield_10001'], 'Fixture Epic')
            else: self.assertEqual(fields['customfield_10002'], 'ENG-9')

    def test_profile_validation_rejects_injected_destinations_and_ambiguous_fields(self):
        for changes in ({'base_url': 'http://jira.example.test'}, {'base_url': 'https://user:pass@jira.example.test'},
                        {'base_url': 'https://jira.example.test/%2e%2e'}, {'project_id': '100\n'},
                        {'issue_types': {'epic': '10', 'story': '10', 'task': '12'}},
                        {'epic_link_field': 'customfield_10001'}, {'epic_name_field': 'summary'},
                        {'github_repository': '../evil'}, {'codec': 'cloud'}, {'extra': True}):
            with self.subTest(changes=changes), self.assertRaises(Rejected): validate_profile({**self.profile, **changes})

    def test_closed_intents_and_unsupported_operations(self):
        for changes in ({'extra': 1}, {'task_id': 'x'}, {'action': 'delete_issue'}, {'issue': {'id': '200', 'key': 'ENG-7'}}):
            with self.assertRaises(Rejected): read_plan({**intent(), **changes}, self.profile)
        for body in ('Bearer private', 'password=private', 'x'*4097, 'x\x00'):
            proposal = intent(); proposal['parameters']['description'] = body
            with self.assertRaises(Rejected): read_plan(proposal, self.profile)
        for action in ACTIONS:
            proposal = intent(action); proposal['parameters']['arbitrary_field'] = 'override'
            with self.assertRaises(Rejected): read_plan(proposal, self.profile)

    def test_create_metadata_missing_required_fields_and_wrong_types_reject(self):
        proposal = intent()
        for mode in ('required', 'unsettable', 'wrong_type', 'wrong_custom', 'project', 'subtask', 'duplicate_type'):
            observed = capture(proposal, self.profile)
            project = observed['responses']['metadata']['body']['projects'][0]
            meta = project['issuetypes'][0]
            if mode == 'required': meta['fields']['customfield_99999'] = {'required': True}
            if mode == 'unsettable': meta['fields']['summary']['operations'] = []
            if mode == 'wrong_type': meta['fields']['customfield_10002']['schema']['type'] = 'number'
            if mode == 'wrong_custom': meta['fields']['customfield_10002']['schema']['custom'] = 'some.other:field'
            if mode == 'project': project['id'] = '999'
            if mode == 'subtask': meta['subtask'] = True
            if mode == 'duplicate_type': project['issuetypes'].append(copy.deepcopy(meta))
            with self.subTest(mode=mode), self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)

    def test_epic_identity_type_and_reparenting_are_checked(self):
        proposal = intent('epic_link')
        for mode in ('wrong_type', 'foreign_project', 'existing_epic', 'no_metadata'):
            observed = capture(proposal, self.profile)
            if mode == 'wrong_type': observed['responses']['epic']['body']['fields']['issuetype']['id'] = '11'
            if mode == 'foreign_project': observed['responses']['epic']['body']['fields']['project']['id'] = '999'
            if mode == 'existing_epic': observed['responses']['issue']['body']['fields']['customfield_10002'] = 'ENG-8'
            if mode == 'no_metadata': observed['responses']['edit_metadata']['body']['fields'] = {}
            with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)
        proposal['parameters']['expected_epic_key'] = 'ENG-8'
        with self.assertRaises(Rejected): read_plan(proposal, self.profile)

    def test_transition_unavailable_status_drift_and_required_screen_fields_reject(self):
        proposal = intent('transition')
        for mode in ('status', 'missing', 'required', 'destination', 'duplicate'):
            observed = capture(proposal, self.profile)
            transitions = observed['responses']['transitions']['body']['transitions']
            if mode == 'status': observed['responses']['issue']['body']['fields']['status']['id'] = '5'
            if mode == 'missing': transitions.clear()
            if mode == 'required': transitions[0]['fields'] = {'resolution': {'required': True}}
            if mode == 'destination': transitions[0]['to']['id'] = '9'
            if mode == 'duplicate': transitions.append(copy.deepcopy(transitions[0]))
            with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)

    def test_issue_link_direction_identity_and_duplicates(self):
        for direction in ('inward', 'outward'):
            proposal = intent('issue_link'); proposal['parameters']['direction'] = direction
            observed = capture(proposal, self.profile)
            preview = preview_action(proposal, self.profile, observed)
            body = preview['binding']['request']['body']
            self.assertEqual(body['inwardIssue']['id'], '200' if direction == 'outward' else '400')
            observed['responses']['issue']['body']['fields']['issuelinks'] = [{'id': '88', 'type': {'id': '30'}, 'outwardIssue': {'id': '400', 'key': 'ENG-10'}}]
            with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)

    def test_github_links_reject_existing_associations_and_implicit_updates(self):
        proposal = intent('github_link')
        preview = preview_action(proposal, self.profile, capture(proposal, self.profile))
        expected = preview['binding']['request']['body']
        self.assertEqual(expected['object']['url'], 'https://github.com/example/project/pull/5')
        for row in ({'id': 1, 'globalId': expected['globalId']}, {'id': 2, 'object': expected['object']}):
            observed = capture(proposal, self.profile)
            observed['responses']['remote_links']['body'] = [row]
            with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)
        proposal['parameters']['number'] = True
        with self.assertRaises(Rejected): read_plan(proposal, self.profile)

    def test_capture_origins_redirects_status_and_budget_are_closed(self):
        proposal = intent('transition')
        for changes in ({'url': 'https://evil.example'}, {'redirected': True}, {'redirected': 0}, {'status': True}, {'status': 403}, {'extra': 1}):
            observed = capture(proposal, self.profile)
            observed['responses']['issue'].update(changes)
            with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)
        observed = capture(proposal, self.profile)
        observed['responses']['issue']['body']['extra'] = 'x'*65536
        with self.assertRaises(Rejected): preview_action(proposal, self.profile, observed)

    def test_recovery_missing_ambiguous_and_truncated_create_candidates(self):
        proposal = intent()
        observed = capture(proposal, self.profile)
        scope = action_scope(proposal, self.profile, observed, preview_action(proposal, self.profile, observed)['sha256'])
        for mode in ('empty', 'duplicate', 'truncated', 'wrong_text'):
            recovered = recovery_capture(scope)
            search = recovered['responses']['search']['body']
            if mode == 'empty': search.update(total=0, issues=[])
            if mode == 'duplicate': search['issues'].append(copy.deepcopy(search['issues'][0])); search['total'] = 2
            if mode == 'truncated': search['total'] = 3
            if mode == 'wrong_text': search['issues'][0]['fields']['summary'] = 'Different'
            result = reconcile_action(scope, recovered)
            self.assertEqual(result['status'], 'unresolved')
            self.assertIsNone(result['binding']['candidate'])

    def test_retained_action_tampering_and_legacy_comment_coexistence(self):
        from test_jira_preview import target as old_target, observation as old_observation, intent as old_intent
        from orch.jira_preview import prepare_comment, review_issue
        with tempfile.TemporaryDirectory() as root:
            journal = JiraJournal(Path(root)/'journal.sqlite')
            try:
                proposal = old_intent(review_issue(old_target(), old_observation()))
                preview = prepare_comment(proposal, old_target(), old_observation())
                journal.stage(proposal, old_target(), old_observation(), preview['sha256'], 'author-key')
                new = intent('github_link'); observed = capture(new, self.profile)
                preview = preview_action(new, self.profile, observed)
                saved = journal.stage_action(new, self.profile, observed, preview['sha256'])
                with self.assertRaises(Rejected): journal.stage_action(new, self.profile, observed, 'f'*64)
                scope = journal.get(new['operation_id'])['scope']
                scope['preview']['binding']['request']['url'] = 'https://evil.example'
                with self.assertRaises(Rejected): reconcile_action(scope, recovery_capture(scope))
                self.assertEqual(journal.audit()['binding']['record_count'], 2)
            finally:
                journal.close()

    def test_action_cli_and_deployment_gates(self):
        from orch.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            proposal = intent()
            observed = capture(proposal, self.profile)
            for name, value in (('profile', self.profile), ('intent', proposal), ('capture', observed)):
                (root/(name+'.json')).write_bytes(canonical(value))
            common = ['--jira-profile', str(root/'profile.json'), '--intent', str(root/'intent.json')]
            for command in ('jira-action-read-plan', 'jira-action-preview', 'jira-stage-action'):
                extra = [] if command == 'jira-action-read-plan' else ['--transcript', str(root/'capture.json')]
                if command == 'jira-stage-action': extra += ['--journal', str(root/'journal.sqlite'), '--expected-sha256', preview_action(proposal, self.profile, observed)['sha256']]
                out = io.StringIO()
                with patch.object(sys, 'argv', ['orch', command, *common, *extra]), redirect_stdout(out): main()
                self.assertFalse(json.loads(out.getvalue())['live_authorized'])
            out = io.StringIO()
            with patch.object(sys, 'argv', ['orch', 'jira-deployment-check', '--jira-profile', str(root/'profile.json')]), redirect_stdout(out), self.assertRaises(SystemExit) as raised: main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('credential_broker_and_revocation', json.loads(out.getvalue())['binding']['blocking_gates'])
