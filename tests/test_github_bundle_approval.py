import copy
import io
import json
import sys
import sqlite3
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch

import test_github_combined_assessment as fixtures
from orch.contracts import Rejected, canonical, digest, ref
from orch.github_approval import prepare_approval
from orch.github_bundle import export_bundle
from orch.github_bundle_approval import prepare_bundle_approval, assess_bundle_approval, _tool_scope
from orch.github_journal import GitHubJournal


class BundleApprovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CombinedAssessmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.clock = patch('orch.github_approval.now', return_value='2026-01-01T00:01:00Z')
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.journal, self.store = self.fixture.journal, self.fixture.store
        self.saved = self.fixture.saved
        self.selection = {'task_id': self.fixture.evidence['task_id'],
                          **{role: ref(self.fixture.docs[role]) for role in ('patch', 'test', 'review', 'policy')}}
        self.source = self.fixture.root / 'bundle.json'
        report = export_bundle(self.store, self.selection, self.source)
        self.bundle_sha = report['binding']['bundle_sha256']
        self.preview = self.prepare()

    def tearDown(self):
        self.fixture.tearDown()

    def prepare(self, journal=None):
        return prepare_bundle_approval(journal or self.journal, self.saved['operation_id'], self.saved['sha256'], self.source, self.bundle_sha)

    def assess(self, journal=None, preview=None):
        preview = preview or self.preview
        return assess_bundle_approval(journal or self.journal, preview, preview['sha256'], self.source, self.bundle_sha)

    def variant(self, updates, tag, recompute=True):
        tool = {**self.fixture.docs['tool'], **updates, 'id': 'tool-' + tag}
        if recompute:
            tool['request_sha256'] = digest({key: value for key, value in tool.items() if key != 'request_sha256'})
        self.store.put(tool)
        policy = {**self.fixture.docs['policy'], 'id': 'policy-' + tag, 'request': ref(tool), 'operation_id': tool['operation_id']}
        self.store.put(policy)
        selection = {**self.selection, 'policy': ref(policy)}
        source = self.fixture.root / ('bundle-' + tag + '.json')
        exported = export_bundle(self.store, selection, source)
        bundle_sha = exported['binding']['bundle_sha256']
        evidence = {'task_id': selection['task_id'], **{role + '_sha256': selection[role]['sha256'] for role in ('patch', 'test', 'review', 'policy')}}
        approval = prepare_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], evidence)
        binding = {**self.preview['binding'], 'bundle_sha256': bundle_sha, 'approval': approval}
        return source, bundle_sha, {**self.preview, 'binding': binding, 'sha256': digest(binding)}

    def test_end_to_end_uses_read_only_journal_and_no_source_workflow(self):
        reader = GitHubJournal(self.fixture.root / 'journal.sqlite', read_only=True)
        try:
            original = self.store.root / 'artifacts'
            hidden = self.store.root / 'hidden-artifacts'
            original.rename(hidden)
            try:
                with patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('socket.socket', side_effect=AssertionError('No network')):
                    preview = self.prepare(reader)
                    report = self.assess(reader, preview)
            finally:
                hidden.rename(original)
            self.assertEqual(reader.db.total_changes, 0)
            self.assertFalse(reader.db.in_transaction)
        finally:
            reader.close()
        self.assertEqual(report['status'], 'current')
        self.assertTrue(report['bundle_bytes_verified'])
        self.assertEqual(report['binding']['blockers'], [])
        self.assertEqual(preview['binding']['bundle_sha256'], self.bundle_sha)
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(preview['binding']['approval']['binding']['evidence'], self.fixture.evidence)
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertFalse(preview[flag])
            self.assertFalse(report[flag])
        self.assertNotIn('hello world', json.dumps(report))
        self.assertEqual(self.journal.get(self.saved['operation_id'])['state'], 'prepared')

    def test_wrong_scope_or_bundle_digest_cannot_prepare(self):
        for scope, bundle in (('b' * 64, self.bundle_sha), (self.saved['sha256'], 'b' * 64)):
            with self.subTest(scope=scope, bundle=bundle), self.assertRaises(Rejected):
                prepare_bundle_approval(self.journal, self.saved['operation_id'], scope, self.source, bundle)
        self.assertFalse(self.journal.db.in_transaction)

    def test_wrapper_tampering_and_bundle_substitution_reject_before_io(self):
        changes = [lambda p: p.update(live_authorized=True), lambda p: p.update(extra=True),
                   lambda p: p['binding'].update(schema_version='2.0.0'),
                   lambda p: p['binding'].update(bundle_sha256='b' * 64)]
        for change in changes:
            preview = copy.deepcopy(self.preview)
            change(preview)
            preview['sha256'] = digest(preview['binding'])
            with patch('orch.github_bundle_approval._verified_bundle', side_effect=AssertionError('No bundle read')), self.assertRaises(Rejected):
                self.assess(preview=preview)
        with self.assertRaises(Rejected):
            assess_bundle_approval(self.journal, self.preview, 'b' * 64, self.source, self.bundle_sha)
        with self.assertRaises(Rejected):
            assess_bundle_approval(self.journal, self.preview, self.preview['sha256'], self.source, 'b' * 64)

    def test_malformed_nested_preview_is_rejected(self):
        for approval in (None, {}, {'binding': {}}, {'binding': {'evidence': {}}, 'sha256': 'b' * 64}):
            binding = {**self.preview['binding'], 'approval': approval}
            preview = {**self.preview, 'binding': binding, 'sha256': digest(binding)}
            with self.subTest(approval=approval), self.assertRaises(Rejected):
                self.assess(preview=preview)

    def test_reserved_journal_skips_bundle_io_and_cannot_prepare(self):
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        with patch('orch.github_bundle_approval._verified_bundle', side_effect=AssertionError('No bundle read')):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['approval:journal_not_prepared'])
        self.assertFalse(report['bundle_bytes_verified'])
        self.assertIsNone(report['binding']['bundle'])
        with self.assertRaises(Rejected):
            self.prepare()

    def test_expired_preview_skips_missing_bundle(self):
        self.source.unlink()
        with patch('orch.github_approval.now', return_value='2026-01-01T00:16:00Z'):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['approval:expired'])
        self.assertFalse(report['bundle_bytes_verified'])

    def test_corrupted_bundle_fails_without_changing_journal(self):
        before = self.journal.db.total_changes
        self.source.write_bytes(b'corrupt')
        with self.assertRaises(Rejected):
            self.assess()
        self.assertEqual(self.journal.db.total_changes, before)
        self.assertFalse(self.journal.db.in_transaction)

    def test_policy_expiry_during_preparation_rejects(self):
        with patch('orch.github_approval.now', return_value='2026-01-01T00:10:00Z'), self.assertRaises(Rejected):
            self.prepare()
        self.assertEqual(self.journal.get(self.saved['operation_id'])['state'], 'prepared')

    def test_expired_bundle_cannot_prepare(self):
        with patch('orch.github_evidence.now', return_value='2026-01-01T00:10:00Z'), self.assertRaises(Rejected):
            self.prepare()

    def test_final_policy_expiry_blocks_even_with_current_preview(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:09:59Z', '2026-01-01T00:10:00Z']):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['evidence:policy_not_current'])
        self.assertEqual(report['binding']['approval']['status'], 'current')
        self.assertTrue(report['bundle_bytes_verified'])

    def test_final_preview_expiry_is_rechecked(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:15:59Z', '2026-01-01T00:16:00Z']):
            report = self.assess()
        self.assertIn('approval:expired', report['binding']['blockers'])
        self.assertTrue(report['bundle_bytes_verified'])

    def test_reservation_during_tool_check_blocks_assessment(self):
        def reserve(*args):
            result = _tool_scope(*args)
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return result
        with patch('orch.github_bundle_approval._tool_scope', side_effect=reserve):
            report = self.assess()
        self.assertIn('approval:journal_not_prepared', report['binding']['blockers'])
        self.assertTrue(report['bundle_bytes_verified'])

    def test_reservation_during_preparation_prevents_preview(self):
        def reserve(*args):
            result = _tool_scope(*args)
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return result
        with patch('orch.github_bundle_approval._tool_scope', side_effect=reserve), self.assertRaises(Rejected):
            self.prepare()
        self.assertFalse(self.journal.db.in_transaction)

    def test_invalid_tool_scope_cannot_prepare_and_blocks_assessment(self):
        for tag, updates, reason in (
            ('operation', {'operation_id': 'f' * 32}, 'operation_mismatch'),
            ('checksum', {'request_sha256': 'f' * 64}, 'request_digest_mismatch'),
            ('paths', {'target_paths': ['greeting.txt']}, 'unsupported_tool_scope'),
            ('missing', {'payload': None}, 'missing_payload')):
            with self.subTest(tag=tag):
                source, sha, preview = self.variant(updates, tag, recompute=tag != 'checksum')
                with self.assertRaises(Rejected):
                    prepare_bundle_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], source, sha)
                report = assess_bundle_approval(self.journal, preview, preview['sha256'], source, sha)
                self.assertIn('tool:' + reason, report['binding']['blockers'])
                self.assertFalse(report['live_authorized'])

    def test_simulated_and_changed_destination_payloads_are_blocked(self):
        for tag in ('simulated', 'destination'):
            payload = {'adapter': 'github-draft-preview', 'scope': copy.deepcopy(self.saved['scope'])}
            if tag == 'simulated':
                payload['adapter'] = 'simulated-github'
            else:
                payload['scope']['repository_id'] = 999
            item = self.store.artifact(self.selection['task_id'], payload)
            source, sha, preview = self.variant({'payload': item}, tag)
            with self.assertRaises(Rejected):
                prepare_bundle_approval(self.journal, self.saved['operation_id'], self.saved['sha256'], source, sha)
            report = assess_bundle_approval(self.journal, preview, preview['sha256'], source, sha)
            self.assertIn('tool:payload_scope_mismatch', report['binding']['blockers'])

    def test_changed_evidence_claims_are_compared_to_saved_preview(self):
        preview = copy.deepcopy(self.preview)
        approval = preview['binding']['approval']
        approval['binding']['evidence']['review_sha256'] = 'f' * 64
        approval['sha256'] = digest(approval['binding'])
        preview['sha256'] = digest(preview['binding'])
        report = self.assess(preview=preview)
        self.assertIn('approval:evidence_mismatch', report['binding']['blockers'])

    def test_composed_checks_receive_query_only_scratch_storage(self):
        roots = []
        def checked(store, *args):
            roots.append(store.root)
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute('INSERT INTO tasks VALUES(?,?,?)', ('f' * 32, '{}', '{}'))
            return _tool_scope(store, *args)
        with patch('orch.github_bundle_approval._tool_scope', side_effect=checked):
            self.assertEqual(self.assess()['status'], 'current')
        self.assertTrue(roots)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_composed_failure_cleans_scratch_and_leaves_journal_unchanged(self):
        roots = []
        before = self.journal.db.total_changes
        def failed(store, *args):
            roots.append(store.root)
            raise Rejected('failed scope check')
        with patch('orch.github_bundle_approval._tool_scope', side_effect=failed), self.assertRaises(Rejected):
            self.assess()
        self.assertTrue(roots)
        self.assertTrue(all(not root.exists() for root in roots))
        self.assertEqual(self.journal.db.total_changes, before)
        self.assertFalse(self.journal.db.in_transaction)

    def test_cli_prepare_assess_and_error_output(self):
        from orch.__main__ import main
        common = ['--journal', str(self.fixture.root / 'journal.sqlite'), '--bundle', str(self.source), '--expected-bundle-sha256', self.bundle_sha]
        args = ['orch', 'github-bundle-approval-preview', *common, '--operation-id', self.saved['operation_id'], '--expected-sha256', self.saved['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        preview = json.loads(output.getvalue())
        path = self.fixture.root / 'bundle-preview.json'
        path.write_bytes(canonical(preview))
        args = ['orch', 'github-assess-bundle-approval', *common, '--approval-preview', str(path), '--expected-sha256', preview['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())['status'], 'current')
        args[-1] = 'bad'
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')

    def test_cli_blocked_result_and_missing_arguments_exit_two(self):
        from orch.__main__ import main
        path = self.fixture.root / 'preview.json'
        path.write_bytes(canonical(self.preview))
        args = ['orch', 'github-assess-bundle-approval', '--journal', str(self.fixture.root / 'journal.sqlite'),
                '--bundle', str(self.source), '--expected-bundle-sha256', self.bundle_sha,
                '--approval-preview', str(path), '--expected-sha256', self.preview['sha256']]
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')
        for command in ('github-bundle-approval-preview', 'github-assess-bundle-approval'):
            output = io.StringIO()
            with patch.object(sys, 'argv', ['orch', command]), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(output.getvalue(), '')
