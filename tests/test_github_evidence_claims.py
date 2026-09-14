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
from github_evidence_support import register_support


class EvidenceClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'workflow')
        self.task = 'a' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (self.task, '{}', '{}'))
        examples = json.loads((Path(__file__).resolve().parents[1] / 'contracts/v1/examples.json').read_text())
        self.docs = {role: copy.deepcopy(examples[kind]) for role, kind in (
            ('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'),
            ('policy', 'ToolDecision'), ('review_request', 'ReviewRequest'), ('tool', 'ToolRequest'))}
        for role, document in self.docs.items():
            document.update(id=role, task_id=self.task)
        register_support(self.store, self.task, self.docs)
        self.docs['test']['patch'] = ref(self.docs['patch'])
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
        self.docs['test']['patch'] = ref(self.docs['patch'])
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
