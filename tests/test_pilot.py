import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orch.contracts import Rejected
from orch.pilot import Pilot, baseline, path_name, json_data


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        (self.repo / 'config.json').write_bytes(b'{"enabled":false}\n')
        self.git('add', 'config.json')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'Disposable baseline')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.pilot = Pilot(self.root / 'journal')
        self.intent = {'repository': str(self.repo), 'base_commit': self.commit,
                       'replacements': {'config.json': '{"enabled":true}\n'},
                       'checks': [{'path': 'config.json', 'keys': ['enabled'], 'equals': True}]}

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
        return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull,
                                        '-C', str(self.repo), *args], env=env, stderr=subprocess.DEVNULL).decode()

    def tearDown(self):
        self.pilot.close()
        self.tmp.cleanup()

    def run_pilot(self, prepared):
        return self.pilot.run(prepared['id'], prepared['scope_sha256'], prepared['revision'])

    def test_end_to_end_and_restart(self):
        prepared = self.pilot.prepare(self.intent)
        self.assertIn('+{"enabled":true}', prepared['diff'])
        self.assertFalse((self.pilot.root / prepared['id']).exists())
        result = self.run_pilot(prepared)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['matches_receipt'])
        self.assertEqual((self.repo / 'config.json').read_bytes(), b'{"enabled":false}\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal')
        self.assertEqual(self.pilot.inspect(prepared['id']), result)
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def spellings(self, path):
        """Other spellings of one directory. `.` and `..` segments exist everywhere;
        hosted Windows CI additionally gives every temporary path an 8.3 short name."""
        found = [path.parent / '.' / path.name, path / '..' / path.name]
        if os.name == 'nt':
            import ctypes
            buffer = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024) and Path(buffer.value) != path:
                found.append(Path(buffer.value))
        return found

    def test_every_repository_spelling_resolves_to_the_same_checkout(self):
        """Git reports one canonical path; a differently spelled argument names the same checkout."""
        for spelling in self.spellings(self.repo):
            with self.subTest(spelling=str(spelling)):
                prepared = self.pilot.prepare({**self.intent, 'repository': str(spelling)})
                # The reviewed scope records the canonical repository, not the spelling supplied.
                self.assertEqual(prepared['scope']['repository'], str(self.repo.resolve()))
                self.assertEqual(self.pilot.abandon(prepared['id'], prepared['scope_sha256'], 0)['status'], 'abandoned')
        prepared = self.pilot.prepare({**self.intent, 'repository': str(self.spellings(self.repo)[0])})
        self.assertEqual(self.run_pilot(prepared)['status'], 'passed')
        # A journal written through one spelling still verifies when opened through another.
        for spelling in self.spellings(self.pilot.root):
            with self.subTest(journal=str(spelling)):
                reader = Pilot(spelling, read_only=True)
                try:
                    self.assertEqual(reader.inspect(prepared['id'])['status'], 'passed')
                finally:
                    reader.close()

    def test_contained_journal_is_refused_however_the_two_are_spelled(self):
        for spelling in self.spellings(self.repo):
            with self.subTest(spelling=str(spelling)):
                contained = Pilot(spelling / 'journal-inside')
                try:
                    with self.assertRaisesRegex(Rejected, 'must be separate'):
                        contained.prepare(self.intent)
                finally:
                    contained.close()

    def test_failed_check_is_retained_and_consumed(self):
        self.intent['checks'][0]['equals'] = False
        prepared = self.pilot.prepare(self.intent)
        self.assertEqual(self.run_pilot(prepared)['status'], 'failed')
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_missing_key_and_bool_number_are_not_equal(self):
        self.intent['checks'][0]['keys'] = ['absent']
        self.intent['checks'][0]['equals'] = None
        self.assertEqual(self.run_pilot(self.pilot.prepare(self.intent))['status'], 'failed')
        self.intent['checks'][0].update(keys=['enabled'], equals=1)
        self.assertEqual(self.run_pilot(self.pilot.prepare(self.intent))['status'], 'failed')

    def test_stale_revision_hash_source_and_expiry(self):
        prepared = self.pilot.prepare(self.intent)
        for hash_value, revision in [('0'*64, 0), (prepared['scope_sha256'], 1), (prepared['scope_sha256'], False)]:
            with self.assertRaises(Rejected): self.pilot.run(prepared['id'], hash_value, revision)
        with patch('orch.pilot.source_hash', return_value='changed'):
            with self.assertRaises(Rejected): self.run_pilot(prepared)
        with patch('orch.pilot.datetime') as clock:
            from datetime import datetime, timezone
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.assertEqual(self.pilot.inspect(prepared['id'])['status'], 'prepared')

    def test_dirty_source_and_moved_head_block_effects(self):
        prepared = self.pilot.prepare(self.intent)
        (self.repo / 'config.json').write_text('{}')
        with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.assertFalse((self.pilot.root / prepared['id']).exists())
        self.git('add', 'config.json')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Changed')
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_crash_after_reservation_never_retries(self):
        prepared = self.pilot.prepare(self.intent)
        with patch('orch.pilot.evaluate', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.run_pilot(prepared)
        report = self.pilot.inspect(prepared['id'])
        self.assertEqual(report['status'], 'reserved')
        self.assertEqual(report['recovery'], 'retain_and_inspect')
        self.assertIsNone(report['receipt'])
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_second_connection_cannot_reuse_approval(self):
        prepared = self.pilot.prepare(self.intent)
        second = Pilot(self.pilot.root)
        try:
            self.run_pilot(prepared)
            with self.assertRaises(Rejected): second.run(prepared['id'], prepared['scope_sha256'], 0)
        finally: second.close()

    def test_abandonment_consumes_scope(self):
        prepared = self.pilot.prepare(self.intent)
        result = self.pilot.abandon(prepared['id'], prepared['scope_sha256'], 0)
        self.assertEqual(result['status'], 'abandoned')
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_workspace_tampering_is_visible(self):
        prepared = self.pilot.prepare(self.intent)
        self.run_pilot(prepared)
        (self.pilot.root / prepared['id'] / 'config.json').write_text('{}')
        self.assertFalse(self.pilot.inspect(prepared['id'])['matches_receipt'])

    def test_unexpected_workspace_file_and_corrupt_receipt(self):
        prepared = self.pilot.prepare(self.intent)
        self.run_pilot(prepared)
        (self.pilot.root / prepared['id'] / 'extra.txt').write_text('unexpected')
        self.assertFalse(self.pilot.inspect(prepared['id'])['matches_receipt'])
        row = self.pilot.db.execute('SELECT receipt FROM pilots WHERE id=?', (prepared['id'],)).fetchone()
        envelope = json.loads(row[0])
        envelope['receipt']['checks'][0]['passed'] = False
        self.pilot.db.execute('UPDATE pilots SET receipt=? WHERE id=?', (json.dumps(envelope), prepared['id']))
        with self.assertRaises(Rejected): self.pilot.inspect(prepared['id'])

    def test_crlf_bytes_are_preserved(self):
        self.intent['replacements']['config.json'] = '{"enabled":true}\r\n'
        self.assertTrue(self.run_pilot(self.pilot.prepare(self.intent))['matches_receipt'])

    def test_git_clean_filter_is_never_executed(self):
        (self.repo / '.gitattributes').write_bytes(b'config.json filter=tripwire\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Filter attribute')
        self.intent['base_commit'] = self.git('rev-parse', 'HEAD').strip()
        self.git('config', 'filter.tripwire.clean', 'exit 99')
        self.git('config', 'filter.tripwire.required', 'true')
        self.assertEqual(self.run_pilot(self.pilot.prepare(self.intent))['status'], 'passed')

    def test_staged_changes_rejected_even_when_worktree_restored(self):
        (self.repo / 'config.json').write_bytes(b'{}\n')
        self.git('add', '.')
        (self.repo / 'config.json').write_bytes(b'{"enabled":false}\n')
        with self.assertRaises(Rejected): self.pilot.prepare(self.intent)

    def test_scope_tampering_is_rejected(self):
        prepared = self.pilot.prepare(self.intent)
        self.pilot.db.execute("UPDATE pilots SET scope='{}' WHERE id=?", (prepared['id'],))
        with self.assertRaises(Rejected): self.pilot.inspect(prepared['id'])

    def test_noop_unknown_fields_missing_checks_and_byte_budget(self):
        mutations = [lambda i: i.update(shell='echo unsafe'),
                     lambda i: i.update(checks=[]),
                     lambda i: i['replacements'].update({'config.json': '{"enabled":false}\n'}),
                     lambda i: i['replacements'].update({'config.json': '"'+'x'*65536+'"'})]
        for mutate in mutations:
            intent = copy.deepcopy(self.intent)
            mutate(intent)
            with self.assertRaises(Rejected): self.pilot.prepare(intent)

    def test_unsafe_paths_and_json(self):
        for value in ('../config.json', '.git/config.json', 'a/../b.json', '/a.json', 'C:/a.json', 'a\\b.json', 'CON.json', 'a./b.json', 'test.py'):
            with self.subTest(value=value), self.assertRaises(Rejected): path_name(value)
        for value in ('{"a":1,"a":2}', 'NaN', 'Infinity', 'not json'):
            with self.assertRaises(Rejected): json_data(value)

    def test_untracked_files_and_linked_input_rejected(self):
        (self.repo / 'extra.txt').write_text('untracked')
        with self.assertRaises(Rejected): self.pilot.prepare(self.intent)
        (self.repo / 'extra.txt').unlink()
        os.link(self.repo / 'config.json', self.root / 'hardlink.json')
        with self.assertRaises(Rejected): self.pilot.prepare(self.intent)

    def test_nested_path_and_multiple_replacements(self):
        (self.repo / 'data').mkdir()
        (self.repo / 'data/settings.json').write_bytes(b'{"limit":1}\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Second file')
        self.intent['base_commit'] = self.git('rev-parse', 'HEAD').strip()
        self.intent['replacements']['data/settings.json'] = '{"limit":2}\n'
        self.intent['checks'].append({'path': 'data/settings.json', 'keys': ['limit'], 'equals': 2})
        self.assertEqual(self.run_pilot(self.pilot.prepare(self.intent))['status'], 'passed')

    def test_repository_programs_never_run(self):
        (self.repo / 'test.py').write_text('raise RuntimeError("Never execute")')
        self.git('add', '.')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Untrusted program')
        self.intent['base_commit'] = self.git('rev-parse', 'HEAD').strip()
        self.assertEqual(self.run_pilot(self.pilot.prepare(self.intent))['status'], 'passed')

    def test_journal_inside_source_rejected(self):
        other = Pilot(self.repo / 'journal')
        try:
            with self.assertRaises(Rejected): other.prepare(self.intent)
        finally: other.close()
