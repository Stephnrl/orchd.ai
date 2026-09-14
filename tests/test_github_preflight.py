import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import test_github_bundle_approval as fixtures
from test_github_preview import intent
from test_github_refs import observations
from orch.contracts import Rejected, canonical, digest
from orch.github_preflight import prepare_observation_snapshot, assess_preflight, _check_snapshot
from orch.github_journal import GitHubJournal


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BundleApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal, self.saved = self.fixture.journal, self.fixture.saved
        self.path = self.fixture.fixture.root / 'observations.json'
        self.snapshot = self.capture()
        self.save(self.snapshot)

    def tearDown(self):
        self.fixture.tearDown()

    def capture(self, seen=None, at='2026-01-01T00:01:00Z', journal=None):
        return prepare_observation_snapshot(journal or self.journal, self.saved['operation_id'], self.saved['sha256'],
                                            observations(intent()) if seen is None else seen, at)

    def save(self, snapshot):
        self.path.write_bytes(canonical(snapshot))

    def assess(self, journal=None, expected=None):
        return assess_preflight(journal or self.journal, self.fixture.preview, self.fixture.preview['sha256'],
                                self.fixture.source, self.fixture.bundle_sha, self.path,
                                self.snapshot['sha256'] if expected is None else expected)

    def test_complete_preflight_is_read_only_and_never_authorized(self):
        reader = GitHubJournal(self.fixture.fixture.root / 'journal.sqlite', read_only=True)
        try:
            with patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('socket.socket', side_effect=AssertionError('No network')):
                snapshot = self.capture(journal=reader)
                report = self.assess(journal=reader)
            self.assertEqual(reader.db.total_changes, 0)
            self.assertFalse(reader.db.in_transaction)
        finally:
            reader.close()
        self.assertEqual(snapshot, self.snapshot)
        self.assertEqual(report['status'], 'current')
        self.assertEqual(report['binding']['blockers'], [])
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertTrue(report['bundle_bytes_verified'])
        self.assertTrue(report['observations_checked'])
        for flag in ('remote_refs_verified', 'evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertFalse(report[flag])
        self.assertNotIn('hello world', json.dumps(report))
        self.assertEqual(self.journal.get(self.saved['operation_id'])['state'], 'prepared')

    def test_snapshot_preserves_failures_without_claiming_observation_authenticity(self):
        seen = observations(intent())
        seen['head'] = {'status': 503, 'body': {'message': 'private failure text'}}
        self.snapshot = self.capture(seen)
        self.save(self.snapshot)
        self.assertEqual(self.snapshot['binding']['observations'], seen)
        self.assertFalse(self.snapshot['remote_refs_verified'])
        report = self.assess()
        self.assertIn('refs:head_unavailable', report['binding']['blockers'])
        self.assertNotIn('private failure text', json.dumps(report))

    def test_repository_and_both_branch_tips_must_match(self):
        changes = [('repository', 'id', 999, 'repository_identity_mismatch'),
                   ('repository', 'archived', True, 'repository_not_active'),
                   ('base', 'sha', 'f' * 40, 'base_ref_mismatch'),
                   ('head', 'sha', 'f' * 40, 'head_ref_mismatch')]
        for kind, field, value, reason in changes:
            with self.subTest(kind=kind, field=field):
                seen = observations(intent())
                body = seen[kind]['body']
                (body if kind == 'repository' else body['object'])[field] = value
                self.snapshot = self.capture(seen)
                self.save(self.snapshot)
                self.assertIn('refs:' + reason, self.assess()['binding']['blockers'])

    def test_freshness_boundary_is_exclusive(self):
        with patch('orch.github_approval.now', return_value='2026-01-01T00:05:59Z'):
            self.assertEqual(self.assess()['status'], 'current')
        with patch('orch.github_approval.now', return_value='2026-01-01T00:06:00Z'):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['refs:stale'])
        self.assertEqual(report['binding']['observations']['binding']['expires_at'], '2026-01-01T00:06:00Z')

    def test_future_and_pre_preview_observations_block(self):
        for at, reason in (('2026-01-01T00:01:01Z', 'from_future'), ('2026-01-01T00:00:59Z', 'predates_approval_preview')):
            with self.subTest(at=at):
                self.snapshot = self.capture(at=at)
                self.save(self.snapshot)
                self.assertIn('refs:' + reason, self.assess()['binding']['blockers'])

    def test_fractional_timestamps_compare_as_instants(self):
        self.snapshot = self.capture(at='2026-01-01T00:01:00.000000Z')
        self.save(self.snapshot)
        self.assertEqual(self.assess()['status'], 'current')

    def test_invalid_timestamp_forms_fail_closed(self):
        for at in (None, True, '2026-01-01', '2026-01-01T00:01:00', '2026-01-01T00:01:00+00:00',
                   '2026-02-30T00:01:00Z', '2026-01-01T00:01:00.0000001Z'):
            with self.subTest(at=at), self.assertRaises(Rejected):
                self.capture(at=at)
        self.snapshot = self.capture(at='9999-12-31T23:59:59Z')
        self.save(self.snapshot)
        with self.assertRaises(Rejected):
            self.assess()

    def test_wrong_scope_and_preview_observation_bindings_block(self):
        changed = copy.deepcopy(self.snapshot)
        changed['binding']['scope_sha256'] = 'f' * 64
        changed['sha256'] = digest(changed['binding'])
        self.save(changed)
        self.assertIn('refs:scope_mismatch', self.assess(expected=changed['sha256'])['binding']['blockers'])
        seen = observations(intent())
        seen['preview_sha256'] = 'f' * 64
        self.snapshot = self.capture(seen)
        self.save(self.snapshot)
        self.assertIn('refs:preview_mismatch', self.assess()['binding']['blockers'])

    def test_wrong_expected_digest_or_snapshot_tampering_rejects(self):
        with self.assertRaises(Rejected):
            self.assess(expected='f' * 64)
        for change in (lambda s: s.update(live_authorized=True), lambda s: s.update(extra=True),
                       lambda s: s['binding'].update(schema_version='2.0.0'),
                       lambda s: s['binding'].update(observed_at='bad')):
            snapshot = copy.deepcopy(self.snapshot)
            change(snapshot)
            snapshot['sha256'] = digest(snapshot['binding'])
            self.save(snapshot)
            with self.assertRaises(Rejected):
                self.assess(expected=snapshot['sha256'])

    def test_malformed_and_oversized_snapshots_reject(self):
        for seen in ({}, {'preview_sha256': 'f' * 64}, {**observations(intent()), 'extra': True}):
            with self.assertRaises(Rejected):
                self.capture(seen)
        with patch('orch.github_preflight.MAX_SNAPSHOT_BYTES', 1), self.assertRaises(Rejected):
            self.capture()
        with patch('orch.github_preflight.MAX_SNAPSHOT_BYTES', 1), self.assertRaises(Rejected):
            self.assess()
        self.path.write_bytes(b'x' * 65537)
        with self.assertRaises(Rejected):
            self.assess()
        self.assertFalse(self.journal.db.in_transaction)

    def test_snapshot_preparation_requires_expected_prepared_scope(self):
        with self.assertRaises(Rejected):
            prepare_observation_snapshot(self.journal, self.saved['operation_id'], 'f' * 64, observations(intent()), '2026-01-01T00:01:00Z')
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        with self.assertRaises(Rejected):
            self.capture()
        self.assertFalse(self.journal.db.in_transaction)

    def test_blocked_review_skips_missing_observations(self):
        self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
        self.path.unlink()
        with patch('orch.github_preflight.load_intent', side_effect=AssertionError('No observation read')):
            report = self.assess()
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('approval:journal_not_prepared', report['binding']['blockers'])
        self.assertFalse(report['observations_checked'])
        self.assertFalse(report['bundle_bytes_verified'])
        self.assertIsNone(report['binding']['final_approval'])

    def test_reservation_during_observation_check_is_caught(self):
        def reserve(*args):
            result = _check_snapshot(*args)
            self.journal.reserve(self.saved['operation_id'], self.saved['sha256'])
            return result
        with patch('orch.github_preflight._check_snapshot', side_effect=reserve):
            report = self.assess()
        self.assertEqual(report['binding']['review']['status'], 'current')
        self.assertEqual(report['binding']['final_approval']['status'], 'blocked')
        self.assertIn('approval:journal_not_prepared', report['binding']['blockers'])

    def test_observation_age_is_checked_after_composed_work(self):
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:05:59Z', '2026-01-01T00:05:59Z', '2026-01-01T00:06:00Z']):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['refs:stale'])

    def test_policy_expiry_is_rechecked_after_observations(self):
        self.snapshot = self.capture(at='2026-01-01T00:09:59Z')
        self.save(self.snapshot)
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:09:59Z', '2026-01-01T00:09:59Z', '2026-01-01T00:10:00Z']):
            report = self.assess()
        self.assertEqual(report['binding']['blockers'], ['evidence:policy_not_current'])

    def test_preview_expiry_is_rechecked_after_observations(self):
        # Keep the policy clock current in the existing review to isolate the final preview guard.
        with patch('orch.github_approval.now', side_effect=['2026-01-01T00:01:00Z', '2026-01-01T00:01:00Z', '2026-01-01T00:16:00Z']):
            report = self.assess()
        self.assertIn('approval:expired', report['binding']['blockers'])

    def test_cli_snapshot_and_preflight_with_no_workflow_launch(self):
        from orch.__main__ import main
        raw = self.fixture.fixture.root / 'raw.json'
        raw.write_bytes(canonical(observations(intent())))
        common = ['--journal', str(self.fixture.fixture.root / 'journal.sqlite')]
        args = ['orch', 'github-ref-observation-snapshot', *common, '--operation-id', self.saved['operation_id'],
                '--expected-sha256', self.saved['sha256'], '--observations', str(raw), '--observed-at', '2026-01-01T00:01:00Z']
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        self.assertEqual(json.loads(output.getvalue()), self.snapshot)
        self.path.write_text(output.getvalue(), encoding='utf-8')
        preview = self.fixture.fixture.root / 'preview.json'
        preview.write_bytes(canonical(self.fixture.preview))
        args = ['orch', 'github-preflight', *common, '--approval-preview', str(preview), '--expected-sha256', self.fixture.preview['sha256'],
                '--bundle', str(self.fixture.source), '--expected-bundle-sha256', self.fixture.bundle_sha,
                '--observations', str(self.path), '--expected-observations-sha256', self.snapshot['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())['status'], 'current')
        with patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()), patch('orch.github_approval.now', return_value='2026-01-01T00:06:00Z'), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        args[-1] = 'bad'
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
