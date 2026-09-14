from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical
from orch.github_journal import GitHubJournal, bound_scope
from test_github_preview import intent


def proposal(number):
    source = intent()
    source['task_id'] = f'{number:032x}'
    source['operation_id'] = f'{number + 100:032x}'
    source['head'] = 'orchd/' + source['task_id'] + '/' + source['operation_id']
    return source


class JournalCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.sqlite'
        self.journal = GitHubJournal(self.path)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def stage(self, number):
        return self.journal.stage(proposal(number), 'example/project', 123)

    def test_record_limit_preserves_idempotency_and_reservation(self):
        with patch('orch.github_journal.MAX_RECORDS', 1):
            saved = self.stage(1)
            before = self.path.read_bytes()
            with self.assertRaisesRegex(Rejected, 'capacity exhausted'):
                self.stage(2)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.stage(1), saved)
            reserved = self.journal.reserve(saved['operation_id'], saved['sha256'])
            self.assertEqual(reserved['state'], 'uncertain')
            self.assertEqual(self.stage(1), reserved)
            with self.assertRaises(Rejected):
                self.stage(2)
            self.assertEqual(self.journal.usage()['records'], 1)

    def test_utf8_scope_budget_exact_boundary_and_rollback(self):
        source = proposal(1)
        source['body'] = '\u00e9' * 100
        size = len(canonical(bound_scope(source, 'example/project', 123)))
        with patch('orch.github_journal.MAX_TOTAL_SCOPE_BYTES', size - 1):
            with self.assertRaises(Rejected):
                self.journal.stage(source, 'example/project', 123)
            self.assertEqual(self.journal.usage()['records'], 0)
        with patch('orch.github_journal.MAX_TOTAL_SCOPE_BYTES', size):
            saved = self.journal.stage(source, 'example/project', 123)
            self.assertEqual(self.journal.usage()['scope_bytes'], size)
            self.assertEqual(self.journal.usage()['remaining_scope_bytes'], 0)
            with self.assertRaises(Rejected):
                self.stage(2)
            self.assertEqual(self.journal.get(saved['operation_id']), saved)

    def test_existing_over_capacity_journal_remains_readable(self):
        saved = self.stage(1)
        self.stage(2)
        with patch('orch.github_journal.MAX_RECORDS', 1), patch('orch.github_journal.MAX_TOTAL_SCOPE_BYTES', 1):
            self.assertEqual(self.journal.usage()['remaining_records'], 0)
            self.assertEqual(self.journal.usage()['remaining_scope_bytes'], 0)
            self.assertEqual(self.stage(1), saved)
            with self.assertRaises(Rejected):
                self.stage(3)
            self.assertEqual(self.journal.reserve(saved['operation_id'], saved['sha256'])['state'], 'uncertain')

    def test_oversized_scope_is_rejected_before_decode(self):
        saved = self.stage(1)
        self.journal.db.execute('DROP TRIGGER immutable_scope')
        self.journal.db.execute('DROP TRIGGER one_reservation')
        self.journal.db.execute('UPDATE intents SET scope=?', ('x' * 65537,))
        with patch('orch.github_journal.json.loads', side_effect=AssertionError('No oversized decoding')) as decode:
            with self.assertRaises(Rejected):
                self.journal.get(saved['operation_id'])
            decode.assert_not_called()
        self.assertEqual(self.journal.usage()['scope_bytes'], 65537)

    def test_scope_admission_limit_has_no_partial_insert(self):
        with patch('orch.github_journal.MAX_SCOPE_BYTES', 1):
            with self.assertRaisesRegex(Rejected, 'scope exceeds'):
                self.stage(1)
        self.assertEqual(self.journal.usage()['records'], 0)

    def test_two_processes_cannot_take_last_slot(self):
        code = '''import json,sys
import orch.github_journal as module
from orch.contracts import Rejected
module.MAX_RECORDS = 1
journal = module.GitHubJournal(sys.argv[1])
try:
 journal.stage(json.loads(sys.argv[2]), 'example/project', 123)
except Rejected:
 sys.exit(2)
finally:
 journal.close()
'''
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.path), json.dumps(proposal(n))], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) for n in (1, 2)]
        try:
            outputs = [process.communicate(timeout=15) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2], outputs)
            self.assertEqual(self.journal.usage()['records'], 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_usage_cli_is_read_only_and_does_not_create_missing_paths(self):
        from orch.__main__ import main
        self.stage(1)
        before = self.path.read_bytes()
        argv = ['orch', 'github-journal-usage', '--journal', str(self.path)]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')):
            main()
        report = json.loads(output.getvalue())
        self.assertEqual(report['records'], 1)
        self.assertEqual(report['limits']['records'], 1000)
        self.assertNotIn('scope', report)
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.path.parent / 'missing' / 'journal.sqlite'
        argv[-1] = str(missing)
        with patch.object(sys, 'argv', argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(missing.parent.exists())
