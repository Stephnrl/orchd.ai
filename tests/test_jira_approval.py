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
from orch.jira_actions import preview_action
from orch.jira_approval import prepare_approval, check_approval, preflight_plan, assess_preflight
from orch.jira_journal import JiraJournal
from jira_action_support import profile, intent, capture, preflight_capture

NOW = '2026-09-14T12:00:00Z'


class JiraApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'jira.sqlite'
        self.journal = JiraJournal(self.path)
        self.addCleanup(self.journal.close)
        self.profile, self.intent = profile(), intent('transition')
        observed = capture(self.intent, self.profile)
        preview = preview_action(self.intent, self.profile, observed)
        self.saved = self.journal.stage_action(self.intent, self.profile, observed, preview['sha256'])
        self.operation, self.sha = self.intent['operation_id'], self.saved['binding']['sha256']
        self.evidence = {'task_id': self.intent['task_id'], 'review_sha256': 'a'*64, 'policy_sha256': 'b'*64}
        self.clock = patch('orch.jira_approval.now', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.preview = prepare_approval(self.journal, self.operation, self.sha, self.evidence, 'author-key')

    def observations(self):
        plan = preflight_plan(self.journal, self.preview, self.preview['sha256'])
        return preflight_capture(plan, self.journal.get(self.operation)['scope'])

    def assess(self, observed=None, observed_at=NOW, evidence=None):
        return assess_preflight(self.journal, self.preview, self.preview['sha256'], evidence or self.evidence,
                                observed or self.observations(), observed_at)

    def test_current_review_preflight_is_read_only_and_never_admits(self):
        before = self.path.read_bytes()
        with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            review = check_approval(self.journal, self.preview, self.preview['sha256'], self.evidence)
            result = self.assess()
        self.assertEqual(review['status'], 'current')
        self.assertEqual(result['status'], 'current')
        for report in (review, result):
            for key in ('evidence_verified', 'live_authorized', 'retry_allowed', 'remote_effect_confirmed'):
                self.assertFalse(report[key])
            self.assertNotIn('Synthetic Jira notes.', canonical(report).decode())
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_evidence_and_consumed_reservation_block(self):
        evidence = {**self.evidence, 'policy_sha256': 'c'*64}
        result = check_approval(self.journal, self.preview, self.preview['sha256'], evidence)
        self.assertIn('evidence_mismatch', result['binding']['blockers'])
        self.journal.reserve(self.operation, self.sha)
        result = check_approval(self.journal, self.preview, self.preview['sha256'], self.evidence)
        self.assertIn('journal_not_prepared', result['binding']['blockers'])
        with self.assertRaises(Rejected): prepare_approval(self.journal, self.operation, self.sha, self.evidence, 'author-key')

    def test_expired_future_and_tampered_approval_reject_or_block(self):
        for checked, reason in (('2026-09-14T12:15:00Z', 'expired'), ('2026-09-14T11:59:59Z', 'not_yet_valid')):
            with patch('orch.jira_approval.now', return_value=checked):
                result = check_approval(self.journal, self.preview, self.preview['sha256'], self.evidence)
            self.assertIn(reason, result['binding']['blockers'])
        for key, value in (('live_authorized', True), ('extra', 1), ('evidence_verified', True), ('sha256', 'f'*64)):
            with self.assertRaises(Rejected): check_approval(self.journal, {**self.preview, key: value}, self.preview['sha256'], self.evidence)

    def test_freshness_checks_include_boundary_and_future_observations(self):
        observed = self.observations()
        with patch('orch.jira_approval.now', return_value='2026-09-14T12:05:00Z'):
            self.assertIn('observation_stale', self.assess(observed)['binding']['blockers'])
        self.assertIn('observation_from_future', self.assess(observed, '2026-09-14T12:00:01Z')['binding']['blockers'])
        self.assertIn('observation_predates_review', self.assess(observed, '2026-09-14T11:59:59Z')['binding']['blockers'])
        with self.assertRaises(Rejected): self.assess(observed, '2026-09-14T12:00:00+00:00')

    def test_identity_permissions_and_remote_context_drift_block(self):
        for mode in ('identity', 'inactive', 'permission', 'boolean', 'context', 'status'):
            observed = self.observations()
            bodies = {key: value['body'] for key, value in observed['responses'].items()}
            if mode == 'identity': bodies['identity']['key'] = 'other-author'
            if mode == 'inactive': bodies['identity']['active'] = False
            if mode == 'permission': bodies['permissions']['permissions'].pop('TRANSITION_ISSUES')
            if mode == 'boolean': bodies['permissions']['permissions']['TRANSITION_ISSUES']['havePermission'] = 1
            if mode == 'context': bodies['issue']['fields']['updated'] = '2026-09-14T12:01:00Z'
            if mode == 'status': bodies['issue']['fields']['status']['id'] = '9'
            result = self.assess(observed)
            self.assertEqual(result['status'], 'blocked', mode)

    def test_final_check_sees_concurrent_reservation_after_refresh_snapshot(self):
        observed = self.observations()
        original = self.journal.get
        calls = 0
        def read(operation):
            nonlocal calls
            calls += 1
            # preflight_plan + refresh read occur within the snapshot; final read is outside.
            if calls == 3:
                writer = JiraJournal(self.path)
                try: writer.reserve(self.operation, self.sha)
                finally: writer.close()
            return original(operation)
        with patch.object(self.journal, 'get', side_effect=read):
            result = self.assess(observed)
        self.assertIn('journal_not_prepared', result['binding']['blockers'])

    def test_final_check_detects_expiry_during_assessment(self):
        observed = self.observations()
        with patch('orch.jira_approval.now', side_effect=[NOW, NOW, '2026-09-14T12:15:00Z']):
            result = self.assess(observed)
        self.assertIn('expired', result['binding']['blockers'])

    def test_comment_scope_uses_same_review_and_author_boundary(self):
        from test_jira_preview import target, observation, intent as comment_intent
        from orch.jira_preview import review_issue, prepare_comment
        proposal = comment_intent(review_issue(target(), observation()))
        prepared = prepare_comment(proposal, target(), observation())
        saved = self.journal.stage(proposal, target(), observation(), prepared['sha256'], 'author-key')
        evidence = {**self.evidence, 'task_id': proposal['task_id']}
        with self.assertRaises(Rejected): prepare_approval(self.journal, proposal['operation_id'], saved['binding']['sha256'], evidence, 'other')
        preview = prepare_approval(self.journal, proposal['operation_id'], saved['binding']['sha256'], evidence, 'author-key')
        plan = preflight_plan(self.journal, preview, preview['sha256'])
        self.assertIn('ADD_COMMENTS', plan['binding']['required_permissions'])
        observed = preflight_capture(plan, self.journal.get(proposal['operation_id'])['scope'])
        self.assertEqual(assess_preflight(self.journal, preview, preview['sha256'], evidence, observed, NOW)['status'], 'current')
        changed = copy.deepcopy(preview)
        changed['binding']['author_key'] = 'other-author'
        changed['sha256'] = digest(changed['binding'])
        self.assertIn('author_scope_mismatch', check_approval(self.journal, changed, changed['sha256'], evidence)['binding']['blockers'])

    def test_review_cli_roundtrip_and_errors(self):
        from orch.__main__ import main
        (self.root/'evidence.json').write_bytes(canonical(self.evidence))
        (self.root/'approval.json').write_bytes(canonical(self.preview))
        (self.root/'capture.json').write_bytes(canonical(self.observations()))
        common = ['--journal', str(self.path), '--expected-sha256', self.preview['sha256'], '--approval-preview', str(self.root/'approval.json')]
        for command, extra in (('jira-check-approval', ['--evidence', str(self.root/'evidence.json')]),
                               ('jira-preflight-read-plan', []),
                               ('jira-preflight', ['--evidence', str(self.root/'evidence.json'), '--transcript', str(self.root/'capture.json'), '--observed-at', NOW])):
            output = io.StringIO()
            with patch.object(sys, 'argv', ['orch', command, *common, *extra]), redirect_stdout(output): main()
            self.assertFalse(json.loads(output.getvalue())['live_authorized'])
        with patch.object(sys, 'argv', ['orch', 'jira-preflight-read-plan', *common, '--intent', 'override.json']), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised: main()
        self.assertEqual(raised.exception.code, 2)
