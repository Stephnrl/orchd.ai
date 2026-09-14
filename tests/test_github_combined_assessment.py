from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest, ref
from orch.github_approval import prepare_approval
from orch.github_evidence import assess_approval_evidence, check_evidence_contents
from orch.github_journal import GitHubJournal
from orch.storage import Store
from test_github_preview import intent
from github_evidence_support import register_support, link_plan_evidence


class CombinedAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = GitHubJournal(self.root / 'journal.sqlite')
        self.saved = self.journal.stage(intent(), 'example/project', 123)
        self.store = Store(self.root / 'workflow')
        task = intent()['task_id']
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (task, '{}', '{}'))
        examples = json.loads((Path(__file__).resolve().parents[1] / 'contracts/v1/examples.json').read_text())
        self.docs = {role: examples[kind] for role, kind in (
            ('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'),
            ('policy', 'ToolDecision'), ('review_request', 'ReviewRequest'), ('tool', 'ToolRequest'), ('test_request', 'TestRequest'), ('work_order', 'WorkOrder'), ('plan', 'ImplementationPlan'), ('plan_request', 'ApprovalRequest'), ('plan_decision', 'ApprovalDecision'))}
        for role, document in self.docs.items():
            document.update(id=role, task_id=task, created_at='2026-01-01T00:00:00Z')
            for field in ('started_at', 'ended_at'):
                if field in document:
                    document[field] = document['created_at']
        register_support(self.store, task, self.docs)
        self.docs['work_order']['deadline'] = '2026-01-01T00:00:00Z'
        self.docs['plan_request']['expires_at'] = '2026-01-01T00:15:00Z'
        self.docs['plan_decision']['decided_at'] = '2026-01-01T00:00:00Z'
        link_plan_evidence(self.docs)
        self.docs['patch']['work_order'] = ref(self.docs['work_order'])
        self.docs['test_request']['work_order'] = ref(self.docs['work_order'])
        self.docs['test']['patch'] = ref(self.docs['patch'])
        self.docs['test_request']['patch'] = ref(self.docs['patch'])
        self.docs['test']['request'] = ref(self.docs['test_request'])
        request = self.docs['review_request']
        request.update(patch=ref(self.docs['patch']), test_receipts=[ref(self.docs['test'])], implementer_invocation_id='implementer', reviewer_invocation_id='reviewer')
        self.docs['review'].update(request=ref(request), reviewer_invocation_id='reviewer')
        self.docs['tool']['tool'] = 'github'
        self.docs['tool']['operation_id'] = self.saved['operation_id']
        self.docs['tool']['payload'] = self.store.artifact(task, {'adapter': 'github-draft-preview', 'scope': self.saved['scope']})
        self.docs['tool']['request_sha256'] = digest({key: value for key, value in self.docs['tool'].items() if key != 'request_sha256'})
        self.docs['policy'].update(operation_id=self.saved['operation_id'], request=ref(self.docs['tool']), decision='require_human_approval', approval_request={'id': 'approval', 'sha256': 'a' * 64}, expires_at='2026-01-01T00:10:00Z')
        self.evidence = {'task_id': task}
        for role, document in self.docs.items():
            self.store.put(document)
            if role in ('patch', 'test', 'review', 'policy'):
                self.evidence[role + '_sha256'] = self.store.artifact(task, document)['sha256']
        clock = patch('orch.github_evidence.now', return_value='2026-01-01T00:01:00Z')
        clock.start()
        self.addCleanup(clock.stop)
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)

    def tearDown(self):
        self.journal.close()
        self.store.close()
        self.temp.cleanup()

    def assess(self):
        return assess_approval_evidence(self.journal, self.store, self.preview, self.preview['sha256'], self.evidence)

    def test_combines_fresh_checks_and_binds_complete_results(self):
        before = (self.root / 'journal.sqlite').read_bytes()
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
            report = self.assess()
        self.assertEqual(report['status'], 'current')
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['binding']['tool_scope']['status'], 'matches')
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['artifact_evidence']['binding']['evidence_sha256'], digest(self.evidence))
        self.assertEqual(report['binding']['approval']['binding']['preview_sha256'], self.preview['sha256'])
        for key in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[key], False)
        self.assertEqual((self.root / 'journal.sqlite').read_bytes(), before)

    def test_initially_blocked_preview_skips_artifact_reads(self):
        with patch('orch.github_approval.now', return_value='2026-01-01T00:15:00Z'), patch('orch.github_evidence.check_evidence_contents', side_effect=AssertionError('No artifact reads')):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertIsNone(report['binding']['artifact_evidence'])
        self.assertFalse(report['artifact_bytes_verified'])

    def test_expiry_during_artifact_check_is_caught(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:14:59Z', '2026-01-01T00:15:00Z']):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['binding']['approval']['binding']['blockers'], ['expired'])

    def test_reservation_during_artifact_check_is_caught(self):
        def check_and_reserve(store, evidence):
            result = check_evidence_contents(store, evidence)
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return result

        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), patch('orch.github_evidence.check_evidence_contents', side_effect=check_and_reserve):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['binding']['approval']['binding']['blockers'], ['journal_not_prepared'])

    def test_missing_artifact_rejects_without_partial_assessment(self):
        (self.store.root / 'artifacts' / self.evidence['test_sha256']).unlink()
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(Rejected):
            self.assess()
        self.assertFalse(self.store.db.in_transaction)
        self.assertFalse(self.journal.db.in_transaction)

    def test_policy_expiry_at_final_check_blocks_unexpired_preview(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:09:59Z', '2026-01-01T00:10:00Z']):
            report = self.assess()
        self.assertEqual(report['binding']['approval']['status'], 'current')
        self.assertEqual(report['binding']['evidence_claims']['status'], 'claims_consistent')
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['binding']['blockers'], ['evidence:policy_not_current'])

    def test_declined_review_blocks_even_when_bytes_and_preview_match(self):
        review = {**self.docs['review'], 'id': 'declined-review', 'decision': 'REJECT'}
        self.store.put(review)
        self.evidence['review_sha256'] = self.store.artifact(self.evidence['task_id'], review)['sha256']
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
            report = self.assess()
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['binding']['approval']['status'], 'current')
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('evidence:review_not_clean_accept', report['binding']['blockers'])

    def test_arbitrary_json_no_longer_satisfies_combined_assessment(self):
        self.evidence['review_sha256'] = self.store.artifact(self.evidence['task_id'], {'role': 'review'})['sha256']
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(Rejected):
            self.assess()

    def replace_tool(self, tool, tag, recompute=True):
        tool = {**tool, 'id': 'tool-' + tag}
        if recompute:
            tool['request_sha256'] = digest({key: value for key, value in tool.items() if key != 'request_sha256'})
        self.store.put(tool)
        policy = {**self.docs['policy'], 'id': 'policy-' + tag, 'request': ref(tool), 'operation_id': tool['operation_id']}
        self.store.put(policy)
        self.evidence['policy_sha256'] = self.store.artifact(self.evidence['task_id'], policy)['sha256']
        with patch('orch.github_approval.now', return_value='2026-01-01T00:00:00Z'):
            self.preview = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], self.evidence)

    def test_simulated_or_changed_repository_payload_is_blocked(self):
        for tag in ('simulated', 'repository'):
            payload = {'adapter': 'github-draft-preview', 'scope': copy.deepcopy(self.saved['scope'])}
            if tag == 'simulated':
                payload['adapter'] = 'simulated-github'
            else:
                payload['scope']['repository_id'] = 999
            item = self.store.artifact(self.evidence['task_id'], payload)
            self.replace_tool({**self.docs['tool'], 'payload': item}, tag)
            with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
                report = self.assess()
            self.assertEqual(report['status'], 'blocked')
            self.assertIn('tool:payload_scope_mismatch', report['binding']['blockers'])

    def test_policy_before_tool_request_blocks_with_matching_scope(self):
        self.replace_tool({**self.docs['tool'], 'created_at': '2026-01-01T00:00:01Z'}, 'late-request')
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
            report = self.assess()
        self.assertEqual(report['binding']['approval']['status'], 'current')
        self.assertEqual(report['binding']['tool_scope']['status'], 'matches')
        self.assertTrue(report['artifact_bytes_verified'])
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('evidence:policy_timeline_invalid', report['binding']['blockers'])
        self.assertFalse(report['live_authorized'])

    def test_operation_checksum_and_missing_payload_block(self):
        for tag, updates, reason in (
            ('operation', {'operation_id': 'c' * 32}, 'operation_mismatch'),
            ('checksum', {'request_sha256': 'f' * 64}, 'request_digest_mismatch'),
            ('paths', {'target_paths': ['greeting.txt']}, 'unsupported_tool_scope'),
            ('payload', {'payload': None}, 'missing_payload')):
            self.replace_tool({**self.docs['tool'], **updates}, tag, recompute=tag != 'checksum')
            with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'):
                report = self.assess()
            self.assertEqual(report['status'], 'blocked')
            self.assertIn('tool:' + reason, report['binding']['blockers'])

    def test_tool_payload_reference_must_belong_to_same_task(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        item = self.store.artifact(other, {'adapter': 'github-draft-preview', 'scope': self.saved['scope']})
        self.replace_tool({**self.docs['tool'], 'payload': item}, 'foreign')
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(Rejected):
            self.assess()

    def test_tool_payload_corruption_rejects(self):
        path = self.store.root / 'artifacts' / self.docs['tool']['payload']['sha256']
        path.write_bytes(b'corrupt')
        with patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(Rejected):
            self.assess()
        self.assertFalse(self.store.db.in_transaction)

    def test_cli_uses_read_only_stores_and_reports_failure_without_partial_output(self):
        from orch.__main__ import main
        preview_path, evidence_path = self.root / 'preview.json', self.root / 'evidence.json'
        preview_path.write_text(json.dumps(self.preview))
        evidence_path.write_text(json.dumps(self.evidence))
        argv = ['orch', 'github-assess-approval', '--data', str(self.store.root), '--journal', str(self.root / 'journal.sqlite'), '--approval-preview', str(preview_path), '--evidence', str(evidence_path), '--expected-sha256', self.preview['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'current')
        (self.store.root / 'artifacts' / self.evidence['test_sha256']).unlink()
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z'), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
