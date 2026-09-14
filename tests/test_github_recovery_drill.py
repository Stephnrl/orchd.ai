from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, digest
from orch.github_backup import backup_journal, drill_journal_backup
from orch.github_journal import GitHubJournal
from test_github_journal_capacity import proposal


class RecoveryDrillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / 'source.sqlite'
        self.bundle = Path(self.temp.name) / 'backup'
        journal = GitHubJournal(self.source)
        try:
            journal.stage(proposal(1), 'example/project', 123)
            saved = journal.stage(proposal(2), 'example/project', 123)
            journal.reserve(saved['operation_id'], saved['sha256'])
        finally:
            journal.close()
        self.backup = backup_journal(self.source, self.bundle)

    def tearDown(self):
        self.temp.cleanup()

    def drill(self):
        return drill_journal_backup(self.bundle, self.backup['sha256'])

    def test_mixed_states_are_exercised_without_changing_source_or_bundle(self):
        files = [self.source, *self.bundle.iterdir()]
        before = {path: path.read_bytes() for path in files}
        report = self.drill()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['binding']['prepared_exercised'], 1)
        self.assertEqual(report['binding']['existing_uncertain_checked'], 1)
        self.assertEqual(report['binding']['repeat_reservations_rejected'], 2)
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report, self.drill())
        for flag in ('restore_allowed', 'retry_allowed', 'live_authorized'):
            self.assertIs(report[flag], False)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_wrong_digest_and_corrupt_bundle_never_allocate_scratch(self):
        with patch('orch.github_backup.tempfile.TemporaryDirectory', side_effect=AssertionError('No scratch')):
            with self.assertRaises(Rejected):
                drill_journal_backup(self.bundle, 'f' * 64)
            (self.bundle / 'manifest.json').write_bytes(b'{}')
            with self.assertRaises(Rejected):
                self.drill()

    def test_scratch_is_removed_after_success_and_failure(self):
        original = tempfile.TemporaryDirectory
        paths = []

        def scratch(**kwargs):
            directory = original(dir=self.temp.name, **kwargs)
            paths.append(Path(directory.name))
            return directory

        with patch('orch.github_backup.tempfile.TemporaryDirectory', side_effect=scratch):
            self.drill()
            with patch('orch.github_backup.time.monotonic', side_effect=[0, 31]):
                with self.assertRaisesRegex(Rejected, 'timed out'):
                    self.drill()
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_changed_copy_is_rejected_before_reservation(self):
        def corrupt_copy(source, destination):
            Path(destination).write_bytes(b'changed')

        with patch('orch.github_backup.shutil.copyfile', side_effect=corrupt_copy), patch.object(GitHubJournal, 'reserve', side_effect=AssertionError('No reservation')):
            with self.assertRaisesRegex(Rejected, 'changed before drill'):
                self.drill()

    def test_broken_repeat_rejection_causes_drill_failure(self):
        original = GitHubJournal.reserve

        def faulty(journal, operation, sha):
            if journal.get(operation)['state'] == 'uncertain':
                return journal.get(operation)
            return original(journal, operation, sha)

        with patch.object(GitHubJournal, 'reserve', faulty):
            with self.assertRaisesRegex(Rejected, 'allowed a consumed'):
                self.drill()

    def test_cli_is_isolated_and_rejects_active_journal(self):
        from orch.__main__ import main
        argv = ['orch', 'github-journal-recovery-drill', '--destination', str(self.bundle), '--expected-sha256', self.backup['sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            main()
        self.assertEqual(json.loads(output.getvalue())['status'], 'passed')
        argv.extend(['--journal', str(self.source)])
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
