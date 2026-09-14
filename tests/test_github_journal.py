from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.github_journal import GitHubJournal
from test_github_preview import intent


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'github.sqlite'
        self.journal = GitHubJournal(self.path)
        self.source = intent()

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def stage(self, source=None):
        return self.journal.stage(source or self.source, 'example/project', 123)

    def test_scope_is_idempotent_immutable_and_task_unique(self):
        saved = self.stage()
        self.assertEqual(saved, self.stage())
        self.assertEqual(saved['state'], 'prepared')
        for source in ({**self.source, 'title': 'Changed'}, {**self.source, 'operation_id': 'c' * 32, 'head': 'orchd/' + self.source['task_id'] + '/' + 'c' * 32}):
            with self.assertRaises(Rejected):
                self.stage(source)
        with self.assertRaises(Rejected):
            self.journal.stage(self.source, 'example/project', 999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.db.execute("DELETE FROM intents")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.db.execute("UPDATE intents SET sha256='changed'")
        self.assertEqual(saved, self.stage())

    def test_reservation_survives_process_exit_and_cannot_be_reset(self):
        saved = self.stage()
        code = "from orch.github_journal import GitHubJournal; import os,sys; j=GitHubJournal(sys.argv[1]); j.reserve(sys.argv[2],sys.argv[3]); os._exit(0)"
        result = subprocess.run([sys.executable, '-c', code, str(self.path), self.source['operation_id'], saved['sha256']], capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        current = self.journal.get(self.source['operation_id'])
        self.assertEqual(current['state'], 'uncertain')
        self.assertIsNotNone(current['reserved_at'])
        self.assertFalse(current['live_authorized'])
        self.assertFalse(current['retry_allowed'])
        self.assertEqual(current, self.stage())  # Idempotent staging never resets the reservation.
        with self.assertRaises(Rejected):
            self.journal.reserve(self.source['operation_id'], saved['sha256'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.db.execute("UPDATE intents SET state='prepared', reserved_at=NULL")

    def test_cross_process_reservation_has_one_winner(self):
        saved = self.stage()
        code = "from orch.github_journal import GitHubJournal\nfrom orch.contracts import Rejected\nimport sys\nj=GitHubJournal(sys.argv[1])\ntry:\n j.reserve(sys.argv[2],sys.argv[3])\nexcept Rejected:\n sys.exit(2)\nfinally:\n j.close()\n"
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.path), self.source['operation_id'], saved['sha256']], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) for _ in range(2)]
        try:
            results = [process.communicate(timeout=15) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2], results)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_wrong_digest_and_wrong_database_fail_without_consuming(self):
        with self.assertRaises(Rejected):
            GitHubJournal(':memory:')
        saved = self.stage()
        with self.assertRaises(Rejected):
            self.journal.reserve(self.source['operation_id'], 'f' * 64)
        self.assertEqual(saved, self.stage())
        other = Path(self.temp.name) / 'other.sqlite'
        connection = sqlite3.connect(other)
        connection.execute('CREATE TABLE unrelated(value TEXT)')
        connection.commit()
        connection.close()
        before = other.read_bytes()
        with self.assertRaises(Rejected):
            GitHubJournal(other)
        self.assertEqual(other.read_bytes(), before)

    def test_cli_only_stages_and_never_opens_workflow_or_network(self):
        from orch.__main__ import main
        source = Path(self.temp.name) / 'intent.json'
        source.write_text(json.dumps(self.source))
        output = io.StringIO()
        argv = ['orch', 'github-stage', '--intent', str(source), '--allow-repository', 'example/project', '--repository-id', '123', '--journal', str(self.path)]
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow store')):
            main()
        self.assertEqual(json.loads(output.getvalue())['state'], 'prepared')
