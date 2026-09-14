from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.github_evidence import check_evidence
from orch.storage import Store


class GitHubEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'workflow'
        self.store = Store(self.root)
        self.task = 'a' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (self.task, '{}', '{}'))
        self.items = {role: self.store.artifact(self.task, {'role': role, 'result': 'unverified'}) for role in ('patch', 'test', 'review', 'policy')}
        self.evidence = {'task_id': self.task, **{role + '_sha256': item['sha256'] for role, item in self.items.items()}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_owned_bytes_match_without_semantic_authority_or_contents(self):
        before = {path: path.read_bytes() for path in (self.root / 'artifacts').iterdir()}
        report = check_evidence(self.store, self.evidence)
        self.assertEqual(report['status'], 'bytes_match')
        self.assertEqual(report['sha256'], digest(report['binding']))
        self.assertEqual(report['binding']['evidence_sha256'], digest(self.evidence))
        self.assertTrue(report['artifact_bytes_verified'])
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertIs(report[flag], False)
        self.assertNotIn('unverified', json.dumps(report))
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_other_task_cannot_use_existing_content_hashes(self):
        other = 'b' * 32
        self.store.db.execute('INSERT INTO tasks VALUES(?,?,?)', (other, '{}', '{}'))
        with self.assertRaisesRegex(Rejected, 'not owned'):
            check_evidence(self.store, {**self.evidence, 'task_id': other})
        with self.assertRaisesRegex(Rejected, 'Unknown'):
            check_evidence(self.store, {**self.evidence, 'task_id': 'c' * 32})
        self.assertFalse(self.store.db.in_transaction)

    def test_same_size_corruption_missing_and_oversized_files_reject(self):
        path = self.root / 'artifacts' / self.items['review']['sha256']
        original = path.read_bytes()
        for raw in (b'x' * len(original), b'x' * (1024 * 1024 + 1)):
            path.write_bytes(raw)
            with self.assertRaises(Rejected):
                check_evidence(self.store, self.evidence)
            self.assertFalse(self.store.db.in_transaction)
        path.unlink()
        with self.assertRaises(Rejected):
            check_evidence(self.store, self.evidence)

    def test_invalid_metadata_and_missing_role_fail_closed(self):
        with self.assertRaises(Rejected):
            check_evidence(self.store, {**self.evidence, 'policy_sha256': 'f' * 64})
        self.store.db.execute('DROP TRIGGER artifacts_update')
        item = {**self.items['test'], 'size_bytes': True}
        self.store.db.execute('UPDATE artifacts SET payload=? WHERE id=?', (canonical(item).decode(), item['artifact_id']))
        with self.assertRaises(Rejected):
            check_evidence(self.store, self.evidence)

        self.store.db.execute('UPDATE artifacts SET payload=? WHERE id=?', (json.dumps(self.items['test'], indent=2), item['artifact_id']))
        with self.assertRaises(Rejected):
            check_evidence(self.store, self.evidence)

    def test_artifact_directory_links_are_rejected(self):
        original = Path.is_symlink

        def linked(path):
            return path == self.root / 'artifacts' or original(path)

        with patch.object(Path, 'is_symlink', linked):
            with self.assertRaisesRegex(Rejected, 'directory'):
                check_evidence(self.store, self.evidence)

    def test_cli_opens_existing_store_read_only_and_never_runs_workflow(self):
        from orch.__main__ import main
        evidence = Path(self.temp.name) / 'evidence.json'
        evidence.write_text(json.dumps(self.evidence))
        argv = ['orch', 'github-check-evidence', '--data', str(self.root), '--evidence', str(evidence)]
        output = io.StringIO()
        before = self.store.db.total_changes
        with patch.object(sys, 'argv', argv), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No engine')), patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
            main()
        self.assertEqual(json.loads(output.getvalue())['status'], 'bytes_match')
        self.assertEqual(self.store.db.total_changes, before)
        missing = Path(self.temp.name) / 'missing'
        argv[3] = str(missing)
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), '')
        self.assertFalse(missing.exists())
