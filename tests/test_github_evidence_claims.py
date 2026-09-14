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
from orch.github_evidence import check_evidence_contents
from orch.storage import Store
from github_evidence_support import register_support, link_plan_evidence


class EvidenceClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'workflow')
        self.task = 'a' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (self.task, '{}', '{}'))
        examples = json.loads((Path(__file__).resolve().parents[1] / 'contracts/v1/examples.json').read_text())
        self.docs = {role: copy.deepcopy(examples[kind]) for role, kind in (
            ('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'),
            ('policy', 'ToolDecision'), ('review_request', 'ReviewRequest'), ('tool', 'ToolRequest'), ('test_request', 'TestRequest'), ('work_order', 'WorkOrder'), ('plan', 'ImplementationPlan'), ('plan_request', 'ApprovalRequest'), ('plan_decision', 'ApprovalDecision'))}
        for role, document in self.docs.items():
            document.update(id=role, task_id=self.task)
        register_support(self.store, self.task, self.docs)
        self.docs['plan_request']['expires_at'] = '2026-09-12T14:15:00Z'
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
        self.docs['policy'].update(request=ref(self.docs['tool']), decision='require_human_approval', approval_request={'id': 'approval', 'sha256': 'a' * 64}, expires_at='2026-09-12T14:15:00Z')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def persist(self, omit=None):
        self.evidence = {'task_id': self.task}
        for role, document in self.docs.items():
            if role != omit:
                self.store.put(document)
            if role in ('patch', 'test', 'review', 'policy'):
                self.evidence[role + '_sha256'] = self.store.artifact(self.task, document)['sha256']

    def check(self, at='2026-09-12T14:01:00Z'):
        with patch('orch.github_evidence.now', return_value=at):
            return check_evidence_contents(self.store, self.evidence)

    def test_consistent_records_are_diagnostic_and_do_not_emit_contents(self):
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['claims']['blockers'], [])
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[flag], False)
        self.assertNotIn('summary', json.dumps(report))

    def test_failed_tests_and_declined_review_block(self):
        self.docs['test'].update(outcome='failed', exit_code=1)
        self.docs['test']['executed_argv'] = ['unexpected']
        self.docs['review']['decision'] = 'REJECT'
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('test_not_passed_completely', report['binding']['claims']['blockers'])
        self.assertIn('test_command_mismatch', report['binding']['claims']['blockers'])
        self.assertIn('review_not_clean_accept', report['binding']['claims']['blockers'])

    def relink(self):
        self.docs['patch']['work_order'] = ref(self.docs['work_order'])
        self.docs['test_request']['work_order'] = ref(self.docs['work_order'])
        self.docs['test']['patch'] = ref(self.docs['patch'])
        self.docs['test_request']['patch'] = ref(self.docs['patch'])
        self.docs['test']['request'] = ref(self.docs['test_request'])
        self.docs['review_request'].update(patch=ref(self.docs['patch']), test_receipts=[ref(self.docs['test'])])
        self.docs['review']['request'] = ref(self.docs['review_request'])

    def command(self):
        test = self.docs['test']
        return dict(argv=['fixture'], working_directory='.', started_at=test['started_at'],
                    ended_at=test['ended_at'], exit_code=0, stdout=test['stdout'], stderr=test['stderr'])

    def test_support_metadata_is_bound_without_contents(self):
        self.docs['patch']['executed_commands'] = [self.command()]
        self.relink()
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'claims_consistent')
        support = report['binding']['claims']['supporting_artifacts']
        self.assertEqual([item['role'] for item in support],
                         ['patch.diff', 'test.stdout', 'test.stderr', 'patch.command.0.stdout', 'patch.command.0.stderr'])
        self.assertEqual(support[0]['sha256'], self.docs['patch']['diff']['sha256'])
        self.assertNotIn('fixture diff', json.dumps(report))
        self.assertEqual(report['sha256'], digest(report['binding']))

    def test_missing_and_corrupt_support_rejects_intact_receipts(self):
        self.persist()
        for role, field in [('patch', 'diff'), ('test', 'stdout'), ('test', 'stderr')]:
            path = self.store.root / 'artifacts' / self.docs[role][field]['sha256']
            original = path.read_bytes()
            for corrupt in (False, True):
                with self.subTest(role=role, field=field, corrupt=corrupt):
                    if corrupt:
                        path.write_bytes(b'altered')
                    else:
                        path.unlink()
                    try:
                        with self.assertRaises((Rejected, OSError)):
                            self.check()
                        self.assertFalse(self.store.db.in_transaction)
                    finally:
                        path.write_bytes(original)

    def test_foreign_task_support_is_rejected(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['patch']['diff'] = self.store.artifact(other, 'fixture diff', media_type='text/plain')
        self.relink()
        self.persist()
        with self.assertRaises(Rejected):
            self.check()

    def test_patch_command_output_is_required(self):
        command = self.command()
        command['stderr'] = self.store.artifact(self.task, 'unique patch stderr', media_type='text/plain')
        self.docs['patch']['executed_commands'] = [command]
        self.relink()
        self.persist()
        (self.store.root / 'artifacts' / command['stderr']['sha256']).unlink()
        with self.assertRaises((Rejected, OSError)):
            self.check()

    def test_reference_budget_rejects_before_support_reads(self):
        self.docs['patch']['executed_commands'] = [self.command() for _ in range(15)]
        self.relink()
        self.persist()
        (self.store.root / 'artifacts' / self.docs['patch']['diff']['sha256']).unlink()
        with self.assertRaisesRegex(Rejected, 'reference budget'):
            self.check()

    def test_byte_budget_counts_repeated_references(self):
        large = self.store.artifact(self.task, 'x' * (1024 * 1024), media_type='text/plain')
        self.docs['patch']['diff'] = large
        self.docs['test'].update(stdout=large, stderr=large)
        self.docs['patch']['executed_commands'] = [self.command() for _ in range(3)]
        self.relink()
        self.persist()
        (self.store.root / 'artifacts' / large['sha256']).unlink()
        with self.assertRaisesRegex(Rejected, 'byte budget'):
            self.check()

    def assert_timeline_blocked(self, reason):
        self.relink()
        self.docs['policy']['request'] = ref(self.docs['tool'])
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn(reason, report['binding']['claims']['blockers'])
        self.assertFalse(report['live_authorized'])

    def test_reversed_patch_window_blocks(self):
        self.docs['patch']['started_at'] = '2026-09-12T14:00:01Z'
        self.assert_timeline_blocked('patch_execution_window_invalid')

    def persist_request(self, omit=None):
        self.docs['test']['request'] = ref(self.docs['test_request'])
        self.docs['review_request']['test_receipts'] = [ref(self.docs['test'])]
        self.docs['review']['request'] = ref(self.docs['review_request'])
        self.persist(omit=omit)

    def assert_request_blocked(self, reason):
        self.persist_request()
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn(reason, report['binding']['claims']['blockers'])
        self.assertEqual(report['binding']['claims']['test_request'], ref(self.docs['test_request']))
        self.assertFalse(report['live_authorized'])

    def test_missing_test_request_rejects(self):
        self.persist_request(omit='test_request')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def assert_work_blocked(self, reason):
        self.relink()
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn(reason, report['binding']['claims']['blockers'])
        self.assertEqual(report['binding']['claims']['work_order'], ref(self.docs['work_order']))
        self.assertFalse(report['live_authorized'])

    def test_missing_work_order_rejects(self):
        self.persist(omit='work_order')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_foreign_task_work_order_rejects(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['work_order']['task_id'] = other
        self.relink()
        self.persist()
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_work_order_hash_mismatch_rejects(self):
        self.docs['work_order']['max_changed_bytes'] = 2
        self.persist()
        with self.assertRaises(Rejected):
            self.check()

    def test_work_order_repository_mismatch_blocks(self):
        self.docs['work_order']['repository']['base_commit'] = 'c' * 40
        self.assert_work_blocked('work_order_repository_mismatch')

    def test_work_order_review_plan_mismatch_blocks(self):
        self.docs['review_request']['plan'] = {'id': 'other-plan', 'sha256': 'b' * 64}
        self.assert_work_blocked('work_order_review_mismatch')

    def assert_plan_blocked(self, reason):
        self.relink()
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn(reason, report['binding']['claims']['blockers'])
        self.assertFalse(report['live_authorized'])

    def test_missing_plan_rejects(self):
        self.persist(omit='plan')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_missing_plan_decision_rejects(self):
        self.persist(omit='plan_decision')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_missing_plan_request_rejects(self):
        self.persist(omit='plan_request')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_foreign_task_plan_rejects(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['plan']['task_id'] = other
        link_plan_evidence(self.docs)
        self.relink()
        self.persist()
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_work_order_cannot_expand_plan_paths(self):
        self.docs['work_order']['allowed_paths'].append('extra.txt')
        self.assert_plan_blocked('plan_work_order_mismatch')

    def test_work_order_cannot_exceed_plan_attempts(self):
        self.docs['work_order']['attempt'] = 2
        self.assert_plan_blocked('plan_work_order_mismatch')

    def test_work_order_cannot_exceed_plan_byte_budget(self):
        self.docs['work_order']['max_changed_bytes'] = 2
        self.assert_plan_blocked('plan_work_order_mismatch')

    def test_denied_plan_approval_blocks(self):
        self.docs['plan_decision']['decision'] = 'reject'
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('plan_approval_binding_mismatch')

    def test_plan_approval_digest_mismatch_blocks(self):
        self.docs['plan_decision']['subject_sha256'] = 'b' * 64
        self.docs['work_order']['plan_approval'] = ref(self.docs['plan_decision'])
        self.assert_plan_blocked('plan_approval_binding_mismatch')

    def test_plan_approval_wrong_evidence_blocks(self):
        self.docs['plan_request']['evidence'] = [ref(self.docs['plan'])]
        self.docs['plan_decision']['request'] = ref(self.docs['plan_request'])
        self.docs['work_order']['plan_approval'] = ref(self.docs['plan_decision'])
        self.assert_plan_blocked('plan_approval_binding_mismatch')

    def test_plan_approval_expired_at_work_creation_blocks(self):
        self.docs['plan_request']['expires_at'] = self.docs['work_order']['created_at']
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('plan_approval_timeline_invalid')

    def test_plan_decision_before_request_blocks(self):
        self.docs['plan_decision']['decided_at'] = '2026-09-12T13:59:59Z'
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('plan_approval_timeline_invalid')

    def test_work_deadline_cannot_extend_plan_approval(self):
        self.docs['work_order']['deadline'] = '2026-09-12T14:15:00.001Z'
        self.assert_plan_blocked('plan_approval_timeline_invalid')

    def test_work_order_test_limits_must_match(self):
        self.docs['work_order']['permitted_tests'][0]['timeout_seconds'] = 2
        self.assert_work_blocked('work_order_test_not_permitted')

    def test_work_order_paths_are_exact(self):
        self.docs['patch']['changed_files'][0]['path'] = 'example/child.txt'
        self.assert_work_blocked('work_order_path_not_permitted')

    def test_work_order_byte_limit_blocks(self):
        self.docs['patch']['changed_bytes'] = 2
        self.assert_work_blocked('work_order_byte_limit_exceeded')

    def test_work_order_created_after_patch_start_blocks(self):
        self.docs['work_order']['created_at'] = '2026-09-12T14:00:00.001Z'
        self.assert_work_blocked('work_order_created_after_patch_start')

    def test_work_order_deadline_blocks_late_test(self):
        self.docs['test']['ended_at'] = '2026-09-12T14:00:01Z'
        self.assert_work_blocked('work_order_deadline_exceeded')

    def test_work_order_future_creation_blocks(self):
        self.docs['work_order']['created_at'] = '2026-09-12T14:02:00Z'
        self.assert_work_blocked('work_order_from_future')

    def test_work_order_exact_byte_and_deadline_limits_pass(self):
        self.docs['patch']['changed_bytes'] = self.docs['work_order']['max_changed_bytes']
        self.relink()
        self.persist()
        self.assertEqual(self.check()['status'], 'claims_consistent')

    def test_foreign_task_test_request_rejects(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['test_request']['task_id'] = other
        self.persist_request()
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_test_request_hash_mismatch_rejects(self):
        self.docs['test_request']['command']['timeout_seconds'] = 2
        self.persist()
        with self.assertRaises(Rejected):
            self.check()

    def test_test_request_operation_mismatch_blocks(self):
        self.docs['test_request']['operation_id'] = 'other-operation'
        self.assert_request_blocked('test_request_binding_mismatch')

    def test_test_request_patch_mismatch_blocks(self):
        self.docs['test_request']['patch']['sha256'] = 'b' * 64
        self.assert_request_blocked('test_request_binding_mismatch')

    def test_test_request_snapshot_mismatch_blocks(self):
        self.docs['test_request']['snapshot_sha256'] = 'b' * 64
        self.assert_request_blocked('test_request_binding_mismatch')

    def test_test_request_work_order_mismatch_blocks(self):
        self.docs['test_request']['work_order']['sha256'] = 'b' * 64
        self.assert_request_blocked('test_request_binding_mismatch')

    def test_test_request_command_limits_must_match(self):
        self.docs['test_request']['command']['timeout_seconds'] = 2
        self.assert_request_blocked('test_request_command_mismatch')

    def test_test_request_runner_image_mismatch_blocks(self):
        self.docs['test_request']['runner_image_digest'] = 'b' * 64
        self.assert_request_blocked('test_runner_image_mismatch')

    def test_test_request_before_patch_blocks(self):
        self.docs['test_request']['created_at'] = '2026-09-12T13:59:59Z'
        self.assert_request_blocked('test_request_timeline_invalid')

    def test_test_request_after_execution_starts_blocks(self):
        self.docs['test_request']['created_at'] = '2026-09-12T14:00:01Z'
        self.assert_request_blocked('test_request_timeline_invalid')

    def test_future_test_request_blocks(self):
        self.docs['test_request']['created_at'] = '2026-09-12T14:02:00Z'
        self.assert_request_blocked('test_request_from_future')

    def test_test_receipt_before_execution_ends_blocks(self):
        self.docs['test']['ended_at'] = '2026-09-12T14:00:01Z'
        self.assert_timeline_blocked('test_execution_window_invalid')

    def test_test_before_patch_receipt_blocks(self):
        self.docs['test']['started_at'] = '2026-09-12T13:59:59Z'
        self.assert_timeline_blocked('test_predates_patch')

    def test_review_request_before_test_receipt_blocks(self):
        self.docs['review_request']['created_at'] = '2026-09-12T13:59:59Z'
        self.assert_timeline_blocked('review_timeline_invalid')

    def test_review_decision_before_request_blocks(self):
        self.docs['review']['created_at'] = '2026-09-12T13:59:59Z'
        self.assert_timeline_blocked('review_timeline_invalid')

    def test_tool_request_before_review_blocks(self):
        self.docs['tool']['created_at'] = '2026-09-12T13:59:59Z'
        self.assert_timeline_blocked('policy_timeline_invalid')

    def test_policy_decision_before_request_blocks(self):
        self.docs['policy']['created_at'] = '2026-09-12T13:59:59Z'
        self.assert_timeline_blocked('policy_timeline_invalid')

    def test_future_evidence_blocks(self):
        self.docs['test']['created_at'] = '2026-09-12T14:01:00.001Z'
        self.assert_timeline_blocked('evidence_from_future')

    def test_command_before_patch_execution_blocks(self):
        command = self.command()
        command['started_at'] = '2026-09-12T13:59:59Z'
        self.docs['patch']['executed_commands'] = [command]
        self.assert_timeline_blocked('patch_command_window_invalid')

    def test_command_after_patch_execution_blocks(self):
        command = self.command()
        command['ended_at'] = '2026-09-12T14:00:01Z'
        self.docs['patch']['executed_commands'] = [command]
        self.assert_timeline_blocked('patch_command_window_invalid')

    def test_reversed_command_window_blocks(self):
        self.docs['patch']['started_at'] = '2026-09-12T13:59:00Z'
        command = self.command()
        command['ended_at'] = '2026-09-12T13:59:30Z'
        self.docs['patch']['executed_commands'] = [command]
        self.assert_timeline_blocked('patch_command_window_invalid')

    def test_equal_instants_with_different_fractional_precision_are_valid(self):
        for document in self.docs.values():
            for field in ('created_at', 'started_at', 'ended_at'):
                if field in document:
                    document[field] = '2026-09-12T14:00:00.000Z'
        self.docs['patch']['started_at'] = '2026-09-12T14:00:00Z'
        link_plan_evidence(self.docs)
        self.relink()
        self.docs['policy']['request'] = ref(self.docs['tool'])
        self.persist()
        self.assertEqual(self.check('2026-09-12T14:00:00Z')['status'], 'claims_consistent')

    def test_snapshot_and_review_links_must_match(self):
        self.docs['test']['snapshot_sha256'] = 'b' * 64
        self.persist()
        blockers = self.check()['binding']['claims']['blockers']
        self.assertIn('test_patch_mismatch', blockers)
        self.assertIn('review_evidence_mismatch', blockers)

    def test_artifact_without_matching_stored_record_rejects(self):
        self.persist(omit='test')
        with self.assertRaises(Rejected):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_policy_window_and_decision_must_match(self):
        self.docs['policy']['decision'] = 'deny'
        self.persist()
        blockers = self.check('2026-09-12T14:15:00Z')['binding']['claims']['blockers']
        self.assertIn('policy_not_current', blockers)
        self.assertIn('policy_request_mismatch', blockers)

    def test_reviewer_identity_must_be_distinct_and_bound(self):
        self.docs['review_request']['implementer_invocation_id'] = 'reviewer'
        self.docs['review']['request'] = ref(self.docs['review_request'])
        self.persist()
        self.assertIn('reviewer_binding_mismatch', self.check()['binding']['claims']['blockers'])

    def test_cli_reports_current_and_blocked_without_actions(self):
        from orch.__main__ import main
        self.persist()
        evidence_path = Path(self.temp.name) / 'evidence.json'
        evidence_path.write_text(json.dumps(self.evidence))
        argv = ['orch', 'github-check-evidence-claims', '--data', str(self.store.root), '--evidence', str(evidence_path)]
        for at, code in [('2026-09-12T14:01:00Z', 0), ('2026-09-12T14:15:00Z', 2)]:
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), patch('orch.github_evidence.now', return_value=at), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                if code:
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(error.exception.code, code)
                else:
                    main()
            self.assertFalse(json.loads(output.getvalue())['live_authorized'])
        (self.store.root / 'artifacts' / self.docs['patch']['diff']['sha256']).unlink()
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
