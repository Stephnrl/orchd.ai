from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest, ref
from orch.github_evidence import check_evidence_contents, check_record_claims, list_evidence_records, resolve_record_selection
from orch.storage import Store
from github_evidence_support import register_support, link_plan_evidence, register_operations


class EvidenceClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'workflow')
        self.task = 'a' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (self.task, '{}', '{}'))
        examples = json.loads((Path(__file__).resolve().parents[1] / 'contracts/v1/examples.json').read_text())
        self.docs = {role: copy.deepcopy(examples[kind]) for role, kind in (
            ('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'),
            ('policy', 'ToolDecision'), ('review_request', 'ReviewRequest'), ('tool', 'ToolRequest'), ('test_request', 'TestRequest'), ('work_order', 'WorkOrder'), ('plan', 'ImplementationPlan'), ('plan_request', 'ApprovalRequest'), ('plan_decision', 'ApprovalDecision'), ('spec', 'TaskSpec'))}
        for role, document in self.docs.items():
            document.update(id=role, task_id=self.task)
        self.docs['patch']['operation_id'] = 'c' * 32
        self.docs['test']['operation_id'] = self.docs['test_request']['operation_id'] = 'd' * 32
        self.docs['spec']['confirmed_by'] = 'fixture-operator'
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

    def persist(self, omit=None, register=True, operations=True):
        self.evidence = {'task_id': self.task}
        for role, document in self.docs.items():
            if role != omit:
                self.store.put(document)
            if role in ('patch', 'test', 'review', 'policy'):
                self.evidence[role + '_sha256'] = self.store.artifact(self.task, document)['sha256']
        if operations:
            register_operations(self.store, self.docs)
        if register and omit not in ('plan_request', 'plan_decision'):
            self.store.db.execute('INSERT INTO approvals VALUES(?,?)',
                                  (self.docs['plan_request']['id'], self.docs['plan_decision']['id']))

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

    def selection(self):
        return {'task_id': self.task, **{role: ref(self.docs[role]) for role in ('patch', 'test', 'review', 'policy')}}

    def anchors(self):
        return {'task_id': self.task, 'review': ref(self.docs['review']), 'policy': ref(self.docs['policy'])}

    def test_selection_resolves_exact_review_links_without_writes(self):
        self.persist()
        other = {**self.docs['review'], 'id': 'later-review'}
        self.store.put(other)
        before = self.store.db.total_changes
        self.assertEqual(resolve_record_selection(self.store, self.anchors()), self.selection())
        self.assertEqual(self.store.db.total_changes, before)

    def test_selection_rejects_multiple_test_references(self):
        self.docs['review_request']['test_receipts'].append(ref(self.docs['test']))
        self.docs['review']['request'] = ref(self.docs['review_request'])
        self.persist()
        with self.assertRaisesRegex(Rejected, 'exactly one'):
            resolve_record_selection(self.store, self.anchors())
        self.assertFalse(self.store.db.in_transaction)

    def test_selection_rejects_conflicting_patch_reference(self):
        self.docs['test']['patch'] = {'id': 'different', 'sha256': 'b' * 64}
        self.docs['review_request']['test_receipts'] = [ref(self.docs['test'])]
        self.docs['review']['request'] = ref(self.docs['review_request'])
        self.persist()
        with self.assertRaisesRegex(Rejected, 'different patches'):
            resolve_record_selection(self.store, self.anchors())

    def test_selection_requires_exact_task_owned_anchors(self):
        self.persist()
        for anchors in ({**self.anchors(), 'extra': True}, {**self.anchors(), 'task_id': 'b' * 32},
                        {**self.anchors(), 'review': {'id': 'review', 'sha256': 'b' * 64}}):
            with self.assertRaises(Rejected):
                resolve_record_selection(self.store, anchors)
            self.assertFalse(self.store.db.in_transaction)

    def test_selection_cli_emits_usable_json_without_assessing_outcomes(self):
        from orch.__main__ import main
        self.docs['review']['decision'] = 'REJECT'
        self.persist()
        path = Path(self.temp.name) / 'anchors.json'
        path.write_text(json.dumps(self.anchors()))
        argv = ['orch', 'github-resolve-evidence-selection', '--records', str(path), '--data', str(self.store.root)]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        self.assertEqual(json.loads(output.getvalue()), self.selection())
        path.write_text('{}')
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()
        self.assertEqual(output.getvalue(), '')

    def test_record_catalog_returns_exact_references_without_contents(self):
        self.persist()
        before = self.store.db.total_changes
        report = list_evidence_records(self.store, self.task)
        selected = {item['role']: item['record'] for item in report['binding']['records']}
        self.assertEqual({'task_id': self.task, **selected}, self.selection())
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(report['evidence_verified'])
        self.assertNotIn('summary', json.dumps(report))

    def test_record_catalog_keeps_multiple_candidates(self):
        self.persist()
        other = {**self.docs['review'], 'id': 'other-review'}
        self.store.put(other)
        reviews = [item for item in list_evidence_records(self.store, self.task)['binding']['records'] if item['role'] == 'review']
        self.assertEqual(len(reviews), 2)
        self.assertEqual({item['record']['id'] for item in reviews}, {'review', 'other-review'})

    def test_record_catalog_rejects_unknown_task_and_over_budget(self):
        with self.assertRaises(Rejected):
            list_evidence_records(self.store, 'b' * 32)
        self.store.db.executemany('INSERT INTO records VALUES(?,?,?,?)',
                                 [(f'candidate-{i}', self.task, 'ReviewDecision', '{}') for i in range(201)])
        with self.assertRaisesRegex(Rejected, 'record budget'):
            list_evidence_records(self.store, self.task)
        self.assertFalse(self.store.db.in_transaction)

    def test_record_catalog_rejects_noncanonical_payload(self):
        self.persist()
        self.store.db.execute('DROP TRIGGER records_update')
        self.store.db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(self.docs['review'], indent=2), 'review'))
        with self.assertRaises(Rejected):
            list_evidence_records(self.store, self.task)

    def test_record_catalog_cli_is_read_only(self):
        from orch.__main__ import main
        self.persist()
        output = io.StringIO()
        argv = ['orch', 'github-list-evidence-records', '--task', self.task, '--data', str(self.store.root)]
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        self.assertEqual(len(json.loads(output.getvalue())['binding']['records']), 4)

    def test_direct_record_claims_need_no_primary_receipt_artifacts(self):
        self.persist()
        for role in ('patch', 'test', 'review', 'policy'):
            (self.store.root / 'artifacts' / self.evidence[role + '_sha256']).unlink()
        before = self.store.db.total_changes
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:01:00Z'):
            report = check_record_claims(self.store, self.selection())
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertFalse(report['live_authorized'])
        self.assertEqual(self.store.db.total_changes, before)
        self.assertNotIn('artifact_bytes_verified', report)

    def test_record_selection_is_closed_and_hash_bound(self):
        self.persist()
        for selection in ({**self.selection(), 'extra': True}, {**self.selection(), 'task_id': 'other'},
                          {**self.selection(), 'review': {'id': 'review', 'sha256': 'b' * 64}},
                          {**self.selection(), 'review': ref(self.docs['test'])}):
            with self.assertRaises(Rejected):
                check_record_claims(self.store, selection)
            self.assertFalse(self.store.db.in_transaction)

    def test_direct_record_cli_is_read_only_and_reports_blockers(self):
        from orch.__main__ import main
        self.persist(register=False)
        path = Path(self.temp.name) / 'records.json'
        path.write_text(json.dumps(self.selection()))
        output = io.StringIO()
        argv = ['orch', 'github-check-record-claims', '--data', str(self.store.root), '--records', str(path)]
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.github_evidence.now', return_value='2026-09-12T14:01:00Z'), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        self.assertIn('plan_approval_not_registered', json.loads(output.getvalue())['binding']['claims']['blockers'])
        path.write_text('{"unexpected": true}')
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')

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

    def test_missing_execution_operations_reject(self):
        self.persist(operations=False)
        with self.assertRaisesRegex(Rejected, 'Missing task-owned execution'):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_current_generation_requires_its_own_broker_request(self):
        self.persist()
        self.store.db.execute('UPDATE operations SET generation=2 WHERE id=?', ('d' * 32,))
        with self.assertRaisesRegex(Rejected, 'Missing or oversized broker request'):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_missing_broker_request_rejects(self):
        self.persist()
        self.store.db.execute('DROP TRIGGER broker_requests_delete')
        self.store.db.execute('DELETE FROM broker_requests WHERE operation_id=?', ('d' * 32,))
        with self.assertRaisesRegex(Rejected, 'Missing or oversized broker request'):
            self.check()

    def replace_runtime_request(self, role, **updates):
        operation = self.docs[role]['operation_id']
        request = json.loads(self.store.db.execute('SELECT request FROM broker_requests WHERE operation_id=?', (operation,)).fetchone()[0])
        request.update(updates)
        item = json.loads(self.store.db.execute('SELECT artifact FROM execution_provenance WHERE operation_id=?', (operation,)).fetchone()[0])
        envelope = json.loads((self.store.root / 'artifacts' / item['sha256']).read_bytes())
        envelope.update(request_sha256=digest(request), source_digest=request['source_digest'])
        artifact = self.store.artifact(self.task, envelope)
        self.store.db.execute('DROP TRIGGER IF EXISTS broker_requests_update')
        self.store.db.execute('DROP TRIGGER IF EXISTS execution_provenance_update')
        self.store.db.execute('UPDATE broker_requests SET request=? WHERE operation_id=?', (canonical(request).decode(), operation))
        self.store.db.execute('UPDATE execution_provenance SET envelope_sha256=?,artifact=? WHERE operation_id=?',
                              (digest(envelope), canonical(artifact).decode(), operation))

    def test_patch_and_test_broker_sources_must_agree(self):
        self.persist()
        self.replace_runtime_request('test', source_digest='b' * 64)
        self.assertIn('broker_runtime_mismatch', self.check()['binding']['claims']['blockers'])

    def test_broker_workspace_must_match_receipt(self):
        self.persist()
        for role in ('patch', 'test'):
            self.replace_runtime_request(role, workspace='different-private-workspace')
            report = self.check()
            self.assertIn(role + '_broker_receipt_workspace_mismatch', report['binding']['claims']['blockers'])
            self.assertNotIn('different-private-workspace', json.dumps(report))

    def test_fixture_cannot_claim_docker_image(self):
        self.persist()
        self.replace_runtime_request('test', image='python@sha256:' + 'a' * 64)
        self.assertIn('test_fixture_image_not_supported', self.check()['binding']['claims']['blockers'])

    def test_docker_requires_pinned_image(self):
        self.persist()
        for image in (None, 'python:latest', 'python@sha256:invalid'):
            self.replace_runtime_request('test', mode='docker', image=image)
            self.assertIn('test_broker_image_not_pinned', self.check()['binding']['claims']['blockers'])

    def test_docker_environment_must_match_request(self):
        self.persist()
        self.replace_runtime_request('test', mode='docker', image='python@sha256:' + 'b' * 64)
        blockers = self.check()['binding']['claims']['blockers']
        self.assertIn('test_broker_environment_mismatch', blockers)
        self.assertIn('broker_runtime_mismatch', blockers)

    def test_consistent_docker_claims_do_not_authorize_execution(self):
        self.docs['test']['environment']['os'] = 'linux'
        self.relink()
        self.persist()
        for role in ('patch', 'test'):
            self.replace_runtime_request(role, mode='docker', image='python@sha256:' + 'a' * 64)
        report = self.check()
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertFalse(report['evidence_verified'])
        self.assertFalse(report['live_authorized'])

    def saved_envelope(self):
        row = self.store.db.execute('SELECT artifact FROM execution_provenance WHERE operation_id=?', ('d' * 32,)).fetchone()
        item = json.loads(row[0])
        return json.loads((self.store.root / 'artifacts' / item['sha256']).read_bytes()), item

    def replace_envelope(self, envelope, owner=None):
        item = self.store.artifact(owner or self.task, envelope)
        self.store.db.execute('DROP TRIGGER IF EXISTS execution_provenance_update')
        self.store.db.execute('UPDATE execution_provenance SET envelope_sha256=?,artifact=? WHERE operation_id=?',
                              (digest(envelope), canonical(item).decode(), 'd' * 32))

    def test_missing_broker_envelope_rejects(self):
        self.persist()
        self.store.db.execute('DROP TRIGGER execution_provenance_delete')
        self.store.db.execute('DELETE FROM execution_provenance WHERE operation_id=?', ('d' * 32,))
        with self.assertRaisesRegex(Rejected, 'Missing or oversized broker envelope'):
            self.check()

    def test_envelope_identity_nonce_source_and_request_digest_must_match(self):
        self.persist()
        original, _ = self.saved_envelope()
        for field, value in (('task_id', 'b' * 32), ('operation_id', 'f' * 32), ('generation', 2),
                             ('nonce', 'f' * 32), ('source_digest', 'b' * 64), ('request_sha256', 'b' * 64)):
            with self.subTest(field=field):
                self.replace_envelope({**original, field: value})
                self.assertIn('test_broker_envelope_binding_mismatch', self.check()['binding']['claims']['blockers'])

    def test_failed_or_truncated_broker_result_blocks(self):
        self.persist()
        original, _ = self.saved_envelope()
        for field, value in (('code', 1), ('code', None), ('failure', 'cleanup_uncertain'), ('truncated', True)):
            self.replace_envelope({**original, 'result': {**original['result'], field: value}})
            self.assertIn('test_broker_execution_not_successful', self.check()['binding']['claims']['blockers'])

    def test_broker_command_and_times_must_match_test_receipt(self):
        self.persist()
        original, _ = self.saved_envelope()
        for field, value in (('command', ['different']), ('started', '2026-09-12T13:59:59Z'),
                             ('ended', '2026-09-12T14:00:01Z'), ('code', 1)):
            self.replace_envelope({**original, 'result': {**original['result'], field: value}})
            self.assertIn('test_broker_receipt_execution_mismatch', self.check()['binding']['claims']['blockers'])

    def test_broker_streams_must_match_verified_test_artifacts(self):
        self.persist()
        original, _ = self.saved_envelope()
        for channel in ('stdout', 'stderr'):
            self.replace_envelope({**original, 'result': {**original['result'], channel: 'different output'}})
            self.assertIn('test_broker_receipt_' + channel + '_mismatch', self.check()['binding']['claims']['blockers'])

    def test_patch_must_report_exactly_one_broker_command(self):
        self.docs['patch']['executed_commands'] = []
        self.relink()
        self.persist()
        self.assertIn('patch_broker_receipt_command_count_mismatch', self.check()['binding']['claims']['blockers'])

    def test_patch_cannot_add_unbacked_commands(self):
        self.docs['patch']['executed_commands'].append(copy.deepcopy(self.docs['patch']['executed_commands'][0]))
        self.relink()
        self.persist()
        self.assertIn('patch_broker_receipt_command_count_mismatch', self.check()['binding']['claims']['blockers'])

    def test_broker_result_must_match_patch_command(self):
        self.persist()
        row = self.store.db.execute('SELECT artifact FROM execution_provenance WHERE operation_id=?', ('c' * 32,)).fetchone()
        item = json.loads(row[0])
        original = json.loads((self.store.root / 'artifacts' / item['sha256']).read_bytes())
        self.store.db.execute('DROP TRIGGER execution_provenance_update')
        for field, value, reason in (('command', ['different'], 'execution'), ('stdout', 'changed', 'stdout'), ('stderr', 'changed', 'stderr')):
            envelope = {**original, 'result': {**original['result'], field: value}}
            artifact = self.store.artifact(self.task, envelope)
            self.store.db.execute('UPDATE execution_provenance SET envelope_sha256=?,artifact=? WHERE operation_id=?',
                                  (digest(envelope), canonical(artifact).decode(), 'c' * 32))
            self.assertIn('patch_broker_receipt_' + reason + '_mismatch', self.check()['binding']['claims']['blockers'])

    def test_envelope_artifact_must_belong_to_task(self):
        self.persist()
        original, _ = self.saved_envelope()
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.replace_envelope(original, owner=other)
        with self.assertRaises(Rejected):
            self.check()

    def test_envelope_corruption_or_missing_file_rejects(self):
        self.persist()
        _, item = self.saved_envelope()
        path = self.store.root / 'artifacts' / item['sha256']
        path.write_bytes(b'changed envelope')
        with self.assertRaises(Rejected):
            self.check()
        path.unlink()
        with self.assertRaises(Rejected):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_envelope_registered_digest_must_match(self):
        self.persist()
        self.store.db.execute('DROP TRIGGER execution_provenance_update')
        self.store.db.execute('UPDATE execution_provenance SET envelope_sha256=? WHERE operation_id=?', ('b' * 64, 'd' * 32))
        with self.assertRaises(Rejected):
            self.check()

    def test_envelope_output_budget_counts_utf8_bytes(self):
        self.persist()
        original, _ = self.saved_envelope()
        self.replace_envelope({**original, 'result': {**original['result'], 'stdout': '\u00e9' * 131073}})
        with self.assertRaisesRegex(Rejected, 'output exceeds byte budget'):
            self.check()

    def test_broker_request_identity_recipe_and_args_must_match(self):
        self.persist()
        original = json.loads(self.store.db.execute('SELECT request FROM broker_requests WHERE operation_id=?', ('d' * 32,)).fetchone()[0])
        self.store.db.execute('DROP TRIGGER broker_requests_update')
        for field, value in (('operation_id', 'f' * 32), ('task_id', 'b' * 32), ('generation', 2),
                             ('recipe', 'edit'), ('args', ['hello world\n'])):
            with self.subTest(field=field):
                request = {**original, field: value}
                self.store.db.execute('UPDATE broker_requests SET request=? WHERE operation_id=?', (canonical(request).decode(), 'd' * 32))
                report = self.check()
                self.assertEqual(report['status'], 'blocked')
                self.assertIn('test_broker_request_mismatch', report['binding']['claims']['blockers'])

    def test_invalid_noncanonical_and_oversized_broker_requests_reject(self):
        self.persist()
        original = json.loads(self.store.db.execute('SELECT request FROM broker_requests WHERE operation_id=?', ('d' * 32,)).fetchone()[0])
        self.store.db.execute('DROP TRIGGER broker_requests_update')
        for raw in ('not json', '{}', json.dumps(original, indent=2), 'x' * (1024 * 1024 + 1)):
            self.store.db.execute('UPDATE broker_requests SET request=? WHERE operation_id=?', (raw, 'd' * 32))
            with self.assertRaises(Rejected):
                self.check()
            self.assertFalse(self.store.db.in_transaction)

    def test_broker_request_report_binds_digest_without_arguments(self):
        self.persist()
        request = json.loads(self.store.db.execute('SELECT request FROM broker_requests WHERE operation_id=?', ('d' * 32,)).fetchone()[0])
        report = self.check()
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertEqual(report['binding']['claims']['execution']['operations']['test']['broker_request_sha256'], digest(request))
        self.assertNotIn('private-fixture-workspace', json.dumps(report))
        self.assertFalse(report['live_authorized'])

    def test_foreign_task_execution_operation_rejects(self):
        self.persist()
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.store.db.execute('UPDATE operations SET task_id=? WHERE id=?', (other, 'd' * 32))
        with self.assertRaisesRegex(Rejected, 'Missing task-owned execution'):
            self.check()

    def test_execution_stage_attempt_status_and_generation_must_match(self):
        self.persist()
        for role, stage in (('patch', 'implementation'), ('test', 'tests')):
            for field, value in (('stage', 'other'), ('slot', '2'), ('status', 'started'), ('generation', 0)):
                with self.subTest(role=role, field=field):
                    self.store.db.execute('UPDATE operations SET stage=?,slot=?,status=?,generation=? WHERE id=?',
                                          (stage, '1', 'done', 1, self.docs[role]['operation_id']))
                    self.store.db.execute(f'UPDATE operations SET {field}=? WHERE id=?', (value, self.docs[role]['operation_id']))
                    report = self.check()
                    self.assertEqual(report['status'], 'blocked')
                    self.assertIn(role + '_operation_not_completed', report['binding']['claims']['blockers'])
            self.store.db.execute('UPDATE operations SET generation=1 WHERE id=?', (self.docs[role]['operation_id'],))

    def test_execution_result_must_bind_successful_receipt(self):
        self.persist()
        for result in ({'test': {'id': 'other', 'sha256': 'b' * 64}, 'failure': None},
                       {'test': ref(self.docs['test']), 'failure': 'failed'},
                       {'failure': None}):
            self.store.db.execute('UPDATE operations SET result=? WHERE id=?', (canonical(result).decode(), 'd' * 32))
            self.assertIn('test_operation_result_mismatch', self.check()['binding']['claims']['blockers'])

    def test_missing_or_oversized_execution_result_blocks(self):
        self.persist()
        for raw in (None, 'x' * (1024 * 1024 + 1)):
            self.store.db.execute('UPDATE operations SET result=? WHERE id=?', (raw, 'd' * 32))
            self.assertIn('test_operation_result_missing', self.check()['binding']['claims']['blockers'])

    def test_malformed_or_noncanonical_execution_result_rejects(self):
        self.persist()
        for raw in ('not json', '[]', json.dumps({'test': ref(self.docs['test']), 'failure': None}, indent=2)):
            self.store.db.execute('UPDATE operations SET result=? WHERE id=?', (raw, 'd' * 32))
            with self.assertRaises(Rejected):
                self.check()
            self.assertFalse(self.store.db.in_transaction)

    def test_unregistered_plan_decision_blocks_without_writes(self):
        self.persist(register=False)
        before = self.store.db.total_changes
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('plan_approval_not_registered', report['binding']['claims']['blockers'])
        self.assertFalse(report['binding']['claims']['plan_approval']['registered'])
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM approvals').fetchone()[0], 0)

    def test_registration_for_another_decision_blocks(self):
        self.persist(register=False)
        other = {**self.docs['plan_decision'], 'id': 'other-decision'}
        self.store.put(other)
        self.store.db.execute('INSERT INTO approvals VALUES(?,?)', (self.docs['plan_request']['id'], other['id']))
        report = self.check()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('plan_approval_not_registered', report['binding']['claims']['blockers'])
        self.assertNotIn('other-decision', json.dumps(report))

    def test_registration_for_another_request_does_not_qualify(self):
        self.persist(register=False)
        other = {**self.docs['plan_request'], 'id': 'other-request'}
        self.store.put(other)
        self.store.db.execute('INSERT INTO approvals VALUES(?,?)', (other['id'], self.docs['plan_decision']['id']))
        self.assertIn('plan_approval_not_registered', self.check()['binding']['claims']['blockers'])

    def test_registered_approval_is_bound_but_not_authenticated(self):
        self.persist()
        report = self.check()
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertTrue(report['binding']['claims']['plan_approval']['registered'])
        self.assertFalse(report['live_authorized'])
        self.assertFalse(report['evidence_verified'])
        self.assertEqual(report['sha256'], digest(report['binding']))

    def test_missing_spec_rejects(self):
        self.persist(omit='spec')
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_foreign_task_spec_rejects(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['spec']['task_id'] = other
        link_plan_evidence(self.docs)
        self.relink()
        self.persist()
        with self.assertRaisesRegex(Rejected, 'Missing task-owned'):
            self.check()

    def test_changed_spec_hash_rejects(self):
        self.docs['spec']['title'] = 'changed'
        self.persist()
        with self.assertRaises(Rejected):
            self.check()

    def test_unconfirmed_spec_blocks(self):
        self.docs['spec']['confirmed_by'] = None
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('spec_not_confirmed')

    def test_spec_repository_mismatch_blocks(self):
        self.docs['spec']['repository']['base_commit'] = 'c' * 40
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('spec_plan_scope_mismatch')

    def test_plan_cannot_expand_spec_paths(self):
        self.docs['spec']['allowed_paths'] = ['different.txt']
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('spec_plan_scope_mismatch')

    def test_spec_created_after_plan_blocks(self):
        self.docs['spec']['created_at'] = '2026-09-12T14:00:00.001Z'
        link_plan_evidence(self.docs)
        self.assert_plan_blocked('spec_created_after_plan')

    def test_original_request_missing_or_corrupt_rejects(self):
        self.persist()
        path = self.store.root / 'artifacts' / self.docs['spec']['request']['sha256']
        path.write_bytes(b'altered request')
        with self.assertRaises(Rejected):
            self.check()
        path.unlink()
        with self.assertRaises(Rejected):
            self.check()
        self.assertFalse(self.store.db.in_transaction)

    def test_original_request_must_belong_to_task(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        self.docs['spec']['request'] = self.store.artifact(other, 'original task request', media_type='text/plain')
        link_plan_evidence(self.docs)
        self.relink()
        self.persist()
        with self.assertRaisesRegex(Rejected, 'not owned'):
            self.check()

    def test_spec_request_report_contains_metadata_only(self):
        self.persist()
        report = self.check()
        binding = report['binding']['claims']['plan_approval']
        self.assertEqual(binding['spec'], ref(self.docs['spec']))
        self.assertEqual(binding['spec_request']['sha256'], self.docs['spec']['request']['sha256'])
        self.assertNotIn('original task request', json.dumps(report))
        self.assertNotIn('fixture-operator', json.dumps(report))
        self.assertEqual(report['sha256'], digest(report['binding']))

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
