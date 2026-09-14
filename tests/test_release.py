import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical
from orch.readiness import EXPECTED
from orch.release import browser_count, check_release, source_manifest, stages, verify_release, _execute

MANIFEST = {'orch/release.py': 'a'*64, 'tests/test_ui_fixture.cjs': 'b'*64}


def passed(stage, *args):
    return {'id': stage, 'status': 'passed', 'reason': None,
            'tests_run': 600 if stage == 'python' else 11 if stage == 'browser' else None,
            'skipped': EXPECTED if stage == 'python' else []}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'report.json'
        self.manifest = patch('orch.release.source_manifest', return_value=MANIFEST).start()
        self.addCleanup(patch.stopall)
        self.runner = patch('orch.release._execute', side_effect=passed).start()

    def produce(self, lane='full'):
        return verify_release(self.path, lane)

    def assess(self, document=None, lane='full', **kwargs):
        if document is not None: self.path.write_bytes(canonical(document))
        sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return check_release(self.path, sha, lane, **kwargs)

    def test_complete_run_and_portable_read_only_assessment(self):
        result = self.produce()
        report = json.loads(self.path.read_bytes())
        self.assertEqual([c.args[0] for c in self.runner.call_args_list], stages('full', MANIFEST))
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['live_authorized'])
        before = self.path.read_bytes()
        assessment = check_release(self.path, result['sha256'])
        self.assertEqual(assessment['status'], 'current')
        self.assertEqual(assessment['provenance'], 'local_unsigned_report')
        self.assertFalse(assessment['live_authorized'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report['stages'][1]['skipped'], EXPECTED)

    def test_source_drift_during_and_after_verification(self):
        self.manifest.side_effect = [MANIFEST, {**MANIFEST, 'new.py': 'c'*64}]
        self.assertEqual(self.produce()['status'], 'failed')
        self.manifest.side_effect = None
        with self.assertRaises(Rejected): self.assess()
        self.path.unlink()
        self.produce()
        self.manifest.return_value = {**MANIFEST, 'new.py': 'c'*64}
        with self.assertRaises(Rejected): self.assess()

    def test_failure_is_reported_and_remaining_checks_run(self):
        self.runner.side_effect = lambda stage, *args: {**passed(stage), **({'status':'failed', 'reason':'timeout'} if stage == 'python' else {})}
        result = self.produce()
        self.assertEqual(result['failed_stages'], ['python'])
        self.assertEqual(self.runner.call_count, len(stages('full', MANIFEST)))
        with self.assertRaises(Rejected): self.assess()

    def test_lanes_are_exact_and_cannot_satisfy_full_acceptance(self):
        self.produce('offline')
        self.assertEqual(self.assess(lane='offline')['status'], 'current')
        with self.assertRaises(Rejected): self.assess()
        with self.assertRaises(Rejected): stages('unknown', MANIFEST)
        with self.assertRaises(Rejected): stages('offline', {})

    def test_expired_future_or_timing_tampering_is_rejected(self):
        self.produce()
        report = json.loads(self.path.read_bytes())
        expires = datetime.fromisoformat(report['expires_at'])
        with self.assertRaises(Rejected): self.assess(checked_at=expires)
        with self.assertRaises(Rejected): self.assess(checked_at=datetime.fromisoformat(report['started_at'])-timedelta(seconds=1))
        for value in ('invalid', expires.replace(tzinfo=None).isoformat(), (expires+timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')):
            with self.subTest(value=value), self.assertRaises(Rejected):
                self.assess({**report, 'expires_at': value})

    def test_stage_inventory_status_counts_skips_and_authority_are_strict(self):
        self.produce()
        report = json.loads(self.path.read_bytes())
        cases = [{**report, 'stages': report['stages'][:-1]}, {**report, 'stages': report['stages'][::-1]},
                 {**report, 'live_authorized': True}, {**report, 'deferred_gates': []},
                 {**report, 'extra': True}, {**report, 'source_files': True}]
        for updates in ({'skipped': []}, {'tests_run': True}, {'tests_run': 0}, {'reason': 'timeout'}, {'status':'failed'}):
            value = copy.deepcopy(report)
            value['stages'][1].update(updates)
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(Rejected): self.assess(value)

    def test_digest_canonical_bounds_and_exclusive_destination(self):
        result = self.produce()
        with self.assertRaises(Rejected): self.produce()
        for sha in ('a'*64, '', None):
            with self.assertRaises(Rejected): check_release(self.path, sha)
        self.path.write_bytes(self.path.read_bytes()+b' ')
        with self.assertRaises(Rejected): self.assess()
        self.path.write_bytes(b' '*262145)
        with self.assertRaises(Rejected): self.assess()
        self.assertEqual(len(result['sha256']), 64)

    def test_process_failure_never_retains_secret_output(self):
        with patch('orch.release.capture', return_value={'code':1, 'failure':None, 'stdout':'SECRET CANARY', 'stderr':'SECRET CANARY'}):
            report = _execute('contracts', Path(self.tmp.name), 'python', 'node', {})
        self.assertEqual(report['reason'], 'nonzero_exit')
        self.assertNotIn('CANARY', str(report))
        with patch('orch.release.capture', side_effect=OSError('SECRET CANARY')):
            self.assertEqual(_execute('contracts', Path(self.tmp.name), 'python', 'node', {})['reason'], 'runner_or_receipt_error')
        self.assertEqual(_execute('browser', Path(self.tmp.name), 'python', None, {})['reason'], 'executable_missing')

    def test_malformed_python_receipt_cannot_leak_values(self):
        def execute(argv, **kwargs):
            Path(argv[-1]).write_text(json.dumps({'status':'passed', 'tests_run':'SECRET CANARY', 'skipped':['SECRET CANARY']}))
            return {'code':0, 'failure':None, 'stdout':'', 'stderr':''}
        with patch('orch.release.capture', side_effect=execute):
            report = _execute('python', Path(self.tmp.name), 'python', 'node', {})
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('CANARY', str(report))

    def test_output_cannot_change_source_and_report_directories_rejected(self):
        from orch.release import ROOT
        with self.assertRaises(Rejected): verify_release(ROOT/'docs'/'new-release-test-report.json')
        self.runner.assert_not_called()
        with self.assertRaises(Rejected): check_release(Path(self.tmp.name), 'a'*64)

    def test_cli_requires_explicit_evidence_and_failure_is_nonzero(self):
        from orch.__main__ import main
        for argv in (['orch', 'verify-release'], ['orch', 'check-release', '--release-report', 'report.json']):
            with patch('sys.argv', argv), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
        with patch('sys.argv', ['orch', 'verify-release', '--destination', str(self.path)]), \
                patch('orch.release.verify_release', return_value={'status':'failed'}), \
                patch('sys.stdout', new=io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)

    def test_browser_empty_skipped_retried_flaky_and_expected_failures_rejected(self):
        good = {'expectedStatus':'passed', 'status':'expected', 'results':[{'status':'passed'}]}
        def document(test): return {'suites':[{'suites':[{'specs':[{'tests':[test]}]}]}], 'errors':[]}
        self.assertEqual(browser_count(document(good)), 1)
        for changes in ({'results':[]}, {'results':[{'status':'skipped'}]}, {'results':[{'status':'passed'}]*2},
                        {'expectedStatus':'failed'}, {'status':'flaky'}):
            with self.assertRaises(Rejected): browser_count(document({**good, **changes}))
        for value in ({'suites':[]}, {'suites':[], 'errors':['error']}):
            with self.assertRaises(Rejected): browser_count(value)


class ReleaseSourceTests(unittest.TestCase):
    def test_unreadable_source_directory_cannot_be_silently_omitted(self):
        def walk(*args, **kwargs):
            kwargs['onerror'](PermissionError('denied'))
            return iter(())
        with patch('orch.release.os.walk', side_effect=walk), self.assertRaises(Rejected):
            source_manifest()

    def test_manifest_covers_acceptance_policy_and_ignores_runtime_outputs(self):
        actual = source_manifest()
        for name in ('orch/release.py', 'tests/test_release.py', 'scripts/ci_tests.py', '.github/workflows/validation.yml',
                     'package-lock.json', 'playwright.config.cjs', 'docs/progress.md'):
            self.assertIn(name, actual)
        self.assertFalse(any('.runtime/' in name or '__pycache__' in name for name in actual))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Rejected): source_manifest(Path(directory))


if __name__ == '__main__': unittest.main()
