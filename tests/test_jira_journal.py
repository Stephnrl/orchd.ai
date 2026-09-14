import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orch.contracts import Rejected, canonical, digest
from orch.jira_journal import JiraJournal, bound_scope, SCHEMA_SQL
from orch.jira_preview import review_issue, prepare_comment
from test_jira_preview import target, observation, intent


class JiraJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'jira.sqlite'
        self.journal = JiraJournal(self.path)
        self.addCleanup(self.journal.close)
        self.target, self.issue = target(), observation()
        self.intent = intent(review_issue(self.target, self.issue))
        self.preview = prepare_comment(self.intent, self.target, self.issue)
        self.args = (self.intent, self.target, self.issue, self.preview['sha256'], 'author-key')
        self.operation = self.intent['operation_id']

    def stage(self):
        return self.journal.stage(*self.args)

    def reserved(self):
        saved = self.stage()
        sha = saved['binding']['sha256']
        return self.journal.reserve(self.operation, sha), sha

    def capture(self, empty=False):
        plan = bound_scope(*self.args)['read_plan']
        url = plan['binding']['first_request']['url']
        rows = [] if empty else [{'id': '301', 'self': url.split('?')[0]+'/301',
                                  'author': {'key': 'author-key'}, 'body': self.intent['body']}]
        return {'kind': 'JiraCommentCapture', 'schema_version': '1.0.0', 'plan_sha256': plan['sha256'],
                'pages': [{'url': url, 'redirected': False, 'status': 200,
                           'body': {'startAt': 0, 'maxResults': 50, 'total': len(rows), 'comments': rows}}]}

    def test_stage_idempotent_reopen_and_detached_inputs(self):
        saved = self.stage()
        self.assertEqual(saved, self.stage())
        self.assertEqual(saved['binding']['state'], 'prepared')
        other = JiraJournal(self.path, read_only=True)
        try:
            self.assertEqual(saved, other.inspect(self.operation))
            scope = other.get(self.operation)['scope']
            self.assertEqual(scope, bound_scope(*self.args))
            scope['intent']['body'] = 'mutated result'
            self.assertEqual(saved, other.inspect(self.operation))
        finally:
            other.close()
        self.intent['body'] = 'mutated caller'
        self.assertEqual(saved, self.journal.inspect(self.operation))
        self.assertNotIn('Untrusted task notes.', canonical(saved).decode())
        self.assertNotIn('Prepared a change', canonical(saved).decode())

    def test_scope_conflicts_do_not_replace_saved_operation(self):
        saved = self.stage()
        for change in ({'body': 'Changed body'}, {'operation_id': 'c'*32}, {'task_id': 'c'*32},
                       {'visibility': {'type': 'role', 'value': 'Reviewers'}}):
            changed = {**self.intent, **change}
            preview = prepare_comment(changed, self.target, self.issue)
            with self.assertRaises(Rejected):
                self.journal.stage(changed, self.target, self.issue, preview['sha256'], 'author-key')
        with self.assertRaises(Rejected): self.journal.stage(*self.args[:-1], 'other-author')
        issue = copy.deepcopy(self.issue)
        issue['body']['fields']['summary'] = 'Changed context'
        changed = {**self.intent, 'snapshot_sha256': review_issue(self.target, issue)['sha256']}
        preview = prepare_comment(changed, self.target, issue)
        with self.assertRaises(Rejected): self.journal.stage(changed, self.target, issue, preview['sha256'], 'author-key')
        self.assertEqual(saved, self.journal.inspect(self.operation))

    def test_wrong_digest_and_prepared_recovery_leave_state_unchanged(self):
        saved = self.stage()
        for action in (lambda: self.journal.reserve(self.operation, 'f'*64),
                       lambda: self.journal.recovery_plan(self.operation, saved['binding']['sha256']),
                       lambda: self.journal.reconcile(self.operation, saved['binding']['sha256'], self.capture())):
            with self.assertRaises(Rejected): action()
        self.assertEqual(saved, self.stage())

    def test_reservation_survives_abrupt_exit_and_cannot_be_reset(self):
        saved = self.stage()
        code = 'from orch.jira_journal import JiraJournal; import sys,os; j=JiraJournal(sys.argv[1]); j.reserve(sys.argv[2],sys.argv[3]); os._exit(0)'
        result = subprocess.run([sys.executable, '-c', code, str(self.path), self.operation, saved['binding']['sha256']],
                                capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        current = self.journal.inspect(self.operation)
        self.assertEqual(current['binding']['state'], 'uncertain')
        self.assertTrue(current['binding']['reserved_at'].endswith('Z'))
        self.assertEqual(current, self.stage())
        with self.assertRaises(Rejected): self.journal.reserve(self.operation, saved['binding']['sha256'])
        for sql in ("DELETE FROM intents", "UPDATE intents SET scope='{}'", "UPDATE intents SET state='prepared',reserved_at=NULL"):
            with self.assertRaises(sqlite3.IntegrityError): self.journal.db.execute(sql)

    def test_cross_process_reservation_has_exactly_one_winner(self):
        saved = self.stage()
        code = ('from orch.jira_journal import JiraJournal\nfrom orch.contracts import Rejected\nimport sys\n'
                'j=JiraJournal(sys.argv[1])\ntry:\n j.reserve(sys.argv[2],sys.argv[3])\n'
                'except Rejected:\n sys.exit(2)\nfinally:\n j.close()\n')
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.path), self.operation, saved['binding']['sha256']],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) for _ in range(2)]
        try:
            outputs = [process.communicate(timeout=20) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2], outputs)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_recovery_uses_saved_scope_and_never_mutates_or_confirms_delivery(self):
        saved, sha = self.reserved()
        before = self.path.read_bytes()
        reader = JiraJournal(self.path, read_only=True)
        try:
            with patch('socket.socket', side_effect=AssertionError('No network')), patch('subprocess.Popen', side_effect=AssertionError('No process')):
                plan = reader.recovery_plan(self.operation, sha)
                result = reader.reconcile(self.operation, sha, self.capture())
                empty = reader.reconcile(self.operation, sha, self.capture(empty=True))
                audit = reader.audit()
            self.assertEqual(plan['binding']['read_plan'], bound_scope(*self.args)['read_plan'])
            self.assertEqual(result['status'], 'candidate_observed')
            self.assertEqual(result['binding']['assessment']['binding']['candidate']['id'], '301')
            self.assertEqual(empty['status'], 'unresolved')
            self.assertEqual(audit['binding']['record_count'], 1)
            for report in (plan, result, empty, audit):
                self.assertEqual(report['sha256'], digest(report['binding']))
                for flag in ('live_authorized', 'retry_allowed', 'remote_effect_confirmed'): self.assertFalse(report[flag])
                self.assertNotIn(self.intent['body'], canonical(report).decode())
            for action in (lambda: reader.stage(*self.args), lambda: reader.reserve(self.operation, sha)):
                with self.assertRaises(Rejected): action()
            with self.assertRaises(sqlite3.OperationalError): reader.db.execute('DELETE FROM intents')
        finally:
            reader.close()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(saved, self.journal.inspect(self.operation))

    def test_wrong_recovery_scope_and_capture_rejected(self):
        saved, sha = self.reserved()
        for operation, expected, capture in ((self.operation, 'f'*64, self.capture()),
                                             ('c'*32, sha, self.capture()),
                                             (self.operation, sha, None),
                                             (self.operation, sha, {**self.capture(), 'plan_sha256': 'f'*64})):
            with self.assertRaises(Rejected): self.journal.reconcile(operation, expected, capture)
        self.assertEqual(saved, self.journal.inspect(self.operation))

    def test_capacity_is_atomic_and_idempotent_staging_survives_full_journal(self):
        saved = self.stage()
        changed = {**self.intent, 'task_id': 'c'*32, 'operation_id': 'd'*32}
        preview = prepare_comment(changed, self.target, self.issue)
        for limits in ({'MAX_RECORDS': 1}, {'MAX_TOTAL_SCOPE_BYTES': self.journal.usage()['scope_bytes']}):
            with patch.multiple('orch.jira_journal', **limits):
                self.assertEqual(saved, self.stage())
                with self.assertRaises(Rejected): self.journal.stage(changed, self.target, self.issue, preview['sha256'], 'author-key')
        self.assertEqual(self.journal.usage()['records'], 1)
        with patch('orch.jira_journal.MAX_SCOPE_BYTES', 1):
            with self.assertRaises(Rejected): bound_scope(*self.args)
        with patch('orch.jira_journal.MAX_TOTAL_SCOPE_BYTES', 1):
            with self.assertRaises(Rejected): self.journal.audit()

    def test_schema_changes_fail_before_read_or_write(self):
        self.stage()
        self.journal.db.execute('DROP TRIGGER no_delete')
        for action in (lambda: self.journal.inspect(self.operation), self.journal.audit, self.stage,
                       lambda: JiraJournal(self.path, read_only=True), lambda: JiraJournal(self.path)):
            with self.assertRaises(Rejected): action()

    def test_tampered_rows_canonical_form_and_state_fail_closed(self):
        self.stage()
        db = self.journal.db
        scope = bound_scope(*self.args)
        encoded, sha = canonical(scope).decode(), digest(scope)
        changed = copy.deepcopy(scope)
        changed['intent']['body'] = 'Rewritten proposal'
        for values in ((encoded+' ', sha, 'prepared', None), (encoded, 'f'*64, 'prepared', None),
                       (canonical(changed).decode(), digest(changed), 'prepared', None),
                       ('x'*131073, sha, 'prepared', None), (encoded, sha, 'uncertain', None),
                       (encoded, sha, 'uncertain', 'invalidZ'), (encoded, sha, 'prepared', '2026-09-14T12:00:00Z')):
            db.execute('DROP TRIGGER immutable_scope')
            db.execute('DROP TRIGGER one_reservation')
            db.execute('UPDATE intents SET scope=?,sha256=?,state=?,reserved_at=?', values)
            db.execute(SCHEMA_SQL[1])
            db.execute(SCHEMA_SQL[3])
            with self.assertRaises(Rejected): self.journal.inspect(self.operation)
            with self.assertRaises(Rejected): self.journal.audit()

    def test_failure_before_commit_rolls_back_stage_and_reservation(self):
        with patch.object(self.journal, 'inspect', side_effect=RuntimeError('Injected before commit')):
            with self.assertRaises(RuntimeError): self.stage()
        self.assertEqual(self.journal.usage()['records'], 0)
        saved = self.stage()
        with patch.object(self.journal, 'inspect', side_effect=RuntimeError('Injected before commit')):
            with self.assertRaises(RuntimeError): self.journal.reserve(self.operation, saved['binding']['sha256'])
        self.assertEqual(saved, self.journal.inspect(self.operation))

    def test_cross_process_capacity_admission_has_one_winner(self):
        other_intent = {**self.intent, 'task_id': 'c'*32, 'operation_id': 'd'*32}
        other_preview = prepare_comment(other_intent, self.target, self.issue)
        proposals = [self.args, (other_intent, self.target, self.issue, other_preview['sha256'], 'author-key')]
        paths = []
        for index, proposal in enumerate(proposals):
            path = Path(self.temp.name)/f'proposal-{index}.json'
            path.write_bytes(canonical(proposal))
            paths.append(path)
        code = ('import sys,json\nimport orch.jira_journal as module\nfrom orch.contracts import Rejected\n'
                'module.MAX_RECORDS=1\nj=module.JiraJournal(sys.argv[1])\n'
                'with open(sys.argv[2]) as stream: args=json.load(stream)\n'
                'try:\n j.stage(*args)\nexcept Rejected:\n sys.exit(2)\nfinally:\n j.close()\n')
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.path), str(path)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) for path in paths]
        try:
            outputs = [process.communicate(timeout=20) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2], outputs)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        self.assertEqual(self.journal.audit()['binding']['record_count'], 1)

    def test_wrong_database_and_missing_read_paths_are_unchanged(self):
        from orch.github_journal import GitHubJournal
        other = Path(self.temp.name)/'github.sqlite'
        github = GitHubJournal(other)
        github.close()
        before = other.read_bytes()
        for read_only in (True, False):
            with self.assertRaises(Rejected): JiraJournal(other, read_only=read_only)
        self.assertEqual(other.read_bytes(), before)
        missing = Path(self.temp.name)/'missing'/'jira.sqlite'
        with self.assertRaises(sqlite3.OperationalError): JiraJournal(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(Rejected): JiraJournal(':memory:')

    def test_linked_journal_path_is_rejected(self):
        # Mock the platform predicate as well as testing real links when supported.
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, 'lstat', return_value=info):
            with self.assertRaises(Rejected): JiraJournal(self.path)
        link = Path(self.temp.name)/'linked.sqlite'
        try:
            link.symlink_to(self.path)
        except OSError:
            return
        with self.assertRaises(Rejected): JiraJournal(link, read_only=True)

    def run_cli(self, command, extra=()):
        from orch.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['orch', command, '--journal', str(self.path), *extra]), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_full_lifecycle_and_fixed_errors_without_workflow_or_network(self):
        paths = {key: Path(self.temp.name)/(key+'.json') for key in ('intent', 'target', 'issue', 'capture')}
        for key, value in (('intent', self.intent), ('target', self.target), ('issue', self.issue), ('capture', self.capture())):
            paths[key].write_bytes(canonical(value))
        stage_args = ['--intent', str(paths['intent']), '--jira-target', str(paths['target']), '--observations', str(paths['issue']),
                      '--expected-sha256', self.preview['sha256'], '--jira-author-key', 'author-key']
        with patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')), patch('socket.socket', side_effect=AssertionError('No network')):
            code, out, err = self.run_cli('jira-stage', stage_args)
            self.assertEqual(code, 0, err)
            sha = json.loads(out)['binding']['sha256']
            self.journal.reserve(self.operation, sha)
            common = ['--operation-id', self.operation, '--expected-sha256', sha]
            before = self.path.read_bytes()
            for command, extra in (('jira-inspect', common), ('jira-journal-usage', []), ('jira-journal-audit', []),
                                   ('jira-journal-read-plan', common), ('jira-reconcile-journal', [*common, '--transcript', str(paths['capture'])])):
                code, out, err = self.run_cli(command, extra)
                self.assertEqual(code, 0, err)
                self.assertFalse(json.loads(out)['live_authorized'])
                self.assertNotIn('Untrusted task notes.', out)
            paths['capture'].write_bytes(canonical(self.capture(empty=True)))
            code, out, err = self.run_cli('jira-reconcile-journal', [*common, '--transcript', str(paths['capture'])])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['status'], 'unresolved')
            for extra in ([*common, '--jira-author-key', 'replacement'], [*common, '--intent', str(paths['intent'])], []):
                code, out, err = self.run_cli('jira-journal-read-plan', extra)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
            paths['capture'].write_text('{"kind":1,"kind":2}')
            code, out, err = self.run_cli('jira-reconcile-journal', [*common, '--transcript', str(paths['capture'])])
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            self.assertEqual(self.path.read_bytes(), before)

    def test_cli_invalid_stage_does_not_create_storage(self):
        missing = Path(self.temp.name)/'new'/'jira.sqlite'
        code, out, err = self.run_cli('jira-stage', ['--journal', str(missing), '--intent', 'absent.json',
                                '--jira-target', 'absent.json', '--observations', 'absent.json',
                                '--expected-sha256', 'f'*64, '--jira-author-key', 'author-key'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertFalse(missing.parent.exists())
