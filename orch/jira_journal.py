"""Retained offline Jira comment operations. No dispatch or retry authority."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import stat

from .contracts import Rejected, canonical, digest, now
from .jira_reconcile import prepare_comment_read_plan, assess_comments

APPLICATION_ID = 1330793034
MAX_RECORDS = 1000
MAX_SCOPE_BYTES = 128 * 1024
MAX_TOTAL_SCOPE_BYTES = 16 * 1024 * 1024
SCHEMA_SQL = (
    "CREATE TABLE intents(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, scope TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','uncertain')), reserved_at TEXT)",
    "CREATE TRIGGER immutable_scope BEFORE UPDATE OF operation_id,task_id,scope,sha256 ON intents BEGIN SELECT RAISE(ABORT,'immutable scope'); END",
    "CREATE TRIGGER no_delete BEFORE DELETE ON intents BEGIN SELECT RAISE(ABORT,'retained intent'); END",
    "CREATE TRIGGER one_reservation BEFORE UPDATE ON intents WHEN NOT (OLD.state='prepared' AND NEW.state='uncertain' AND OLD.reserved_at IS NULL AND NEW.reserved_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'reservation consumed'); END",
)
FLAGS = {'live_authorized': False, 'retry_allowed': False, 'remote_effect_confirmed': False}


def _linked_path(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # lstat covers Windows junctions/reparse points on Python 3.11 as well.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def bound_scope(intent, target, issue_observations, expected_preview_sha256, author_key):
    plan = prepare_comment_read_plan(intent, target, issue_observations, expected_preview_sha256, author_key)
    scope = {'schema_version': '1.0.0', 'intent': intent, 'target': target,
             'issue_observations': issue_observations, 'preview_sha256': expected_preview_sha256,
             'author_key': author_key, 'read_plan': plan}
    if len(canonical(scope)) > MAX_SCOPE_BYTES:
        raise Rejected('Jira journal scope exceeds byte limit')
    # Detach caller-owned objects before admission and return.
    return json.loads(canonical(scope))


def _validated_scope(scope):
    if isinstance(scope, dict) and scope.get('kind') == 'JiraActionScope':
        from .jira_actions import action_scope
        expected = action_scope(scope['intent'], scope['profile'], scope['capture'], scope['preview_sha256'])
    else:
        expected = bound_scope(scope['intent'], scope['target'], scope['issue_observations'],
                               scope['preview_sha256'], scope['author_key'])
    if canonical(scope) != canonical(expected):
        raise Rejected('Invalid Jira journal scope')
    return expected


class JiraJournal:
    """One immutable comment operation per task, in a separate identified database."""
    def __init__(self, path, read_only=False):
        if str(path) == ':memory:':
            raise Rejected('Jira journal requires persistent storage')
        path = Path(path).absolute()
        if any(_linked_path(item) for item in (path, *path.parents)):
            raise Rejected('Jira journal path must not contain links')
        self.read_only = read_only
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path.as_uri() + ('?mode=ro' if read_only else '?mode=rwc'),
                                  uri=True, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA trusted_schema=OFF')
            if read_only:
                self.db.execute('PRAGMA query_only=ON')
                self._check_schema()
            else:
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('BEGIN IMMEDIATE')
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                identity = self.db.execute('PRAGMA application_id').fetchone()[0]
                if version == 0 and identity == 0 and not self.db.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                    for statement in SCHEMA_SQL:
                        self.db.execute(statement)
                    self.db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    self.db.execute('PRAGMA user_version=1')
                self._check_schema()
                self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _check_schema(self):
        expected = {('table', 'intents', 'intents', SCHEMA_SQL[0]),
                    ('index', 'sqlite_autoindex_intents_1', 'intents', None),
                    ('index', 'sqlite_autoindex_intents_2', 'intents', None)}
        expected.update(('trigger', name, 'intents', sql) for name, sql in zip(
            ('immutable_scope', 'no_delete', 'one_reservation'), SCHEMA_SQL[1:]))
        actual = {tuple(row) for row in self.db.execute(
            'SELECT type,name,tbl_name,CASE WHEN length(sql)<=4096 THEN sql END FROM main.sqlite_master LIMIT 7')}
        if (actual != expected or self.db.execute('SELECT 1 FROM sqlite_temp_master LIMIT 1').fetchone()
                or self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or self.db.execute('PRAGMA user_version').fetchone()[0] != 1
                or self.db.execute('PRAGMA ignore_check_constraints').fetchone()[0] != 0):
            raise Rejected('Unsupported Jira journal schema or constraints')

    def get(self, operation_id):
        """Internal record including retained untrusted inputs; use inspect for reports."""
        self._check_schema()
        row = self.db.execute(
            'SELECT operation_id,task_id,sha256,state,reserved_at,CASE WHEN length(CAST(scope AS BLOB))<=? THEN scope END AS scope FROM intents WHERE operation_id=?',
            (MAX_SCOPE_BYTES, operation_id)).fetchone()
        if row is None:
            raise Rejected('Unknown Jira operation')
        try:
            scope = _validated_scope(json.loads(row['scope']))
            if (canonical(scope).decode() != row['scope'] or digest(scope) != row['sha256']
                    or scope['intent']['operation_id'] != row['operation_id']
                    or scope['intent']['task_id'] != row['task_id']):
                raise Rejected('Invalid Jira journal binding')
            if row['state'] == 'prepared':
                if row['reserved_at'] is not None:
                    raise Rejected('Prepared operation has a reservation')
            elif row['state'] == 'uncertain':
                reserved = row['reserved_at']
                if not isinstance(reserved, str) or not reserved.endswith('Z'):
                    raise Rejected('Uncertain operation lacks reservation time')
                parsed = datetime.fromisoformat(reserved)
                if parsed.utcoffset() is None:
                    raise Rejected('Invalid reservation time')
            else:
                raise Rejected('Invalid Jira journal state')
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise Rejected('Invalid Jira journal record') from exc
        return {key: row[key] for key in ('operation_id', 'task_id', 'sha256', 'state', 'reserved_at')} | {'scope': scope, **FLAGS}

    @staticmethod
    def _metadata(record):
        return {key: record[key] for key in ('operation_id', 'task_id', 'sha256', 'state', 'reserved_at')}

    def inspect(self, operation_id):
        record = self.get(operation_id)
        scope = record['scope']
        binding = {'schema_version': '1.0.0', **self._metadata(record), 'target': scope['target'],
                   'preview_sha256': scope['preview_sha256'], 'read_plan_sha256': scope['read_plan']['sha256']}
        if scope.get('kind') == 'JiraActionScope':
            binding['action'] = scope['intent']['action']
        return {'kind': 'JiraJournalRecord', 'binding': binding, 'sha256': digest(binding), **FLAGS}

    def usage(self):
        """Logical admission budget, not disk usage or full record validation."""
        self._check_schema()
        row = self.db.execute('SELECT count(*) AS records,coalesce(sum(length(CAST(scope AS BLOB))),0) AS scope_bytes FROM intents').fetchone()
        return {'records': row['records'], 'scope_bytes': row['scope_bytes'],
                'limits': {'records': MAX_RECORDS, 'scope_bytes': MAX_TOTAL_SCOPE_BYTES, 'record_scope_bytes': MAX_SCOPE_BYTES},
                'remaining_records': max(0, MAX_RECORDS-row['records']),
                'remaining_scope_bytes': max(0, MAX_TOTAL_SCOPE_BYTES-row['scope_bytes']), **FLAGS}

    def stage(self, intent, target, issue_observations, expected_preview_sha256, author_key):
        if self.read_only:
            raise Rejected('Jira journal is read-only')
        scope = bound_scope(intent, target, issue_observations, expected_preview_sha256, author_key)
        return self._stage_scope(scope)

    def stage_action(self, intent, profile, capture, expected_preview_sha256):
        from .jira_actions import action_scope
        if self.read_only:
            raise Rejected('Jira journal is read-only')
        return self._stage_scope(action_scope(intent, profile, capture, expected_preview_sha256))

    def _stage_scope(self, scope):
        encoded = canonical(scope)
        if len(encoded) > MAX_SCOPE_BYTES:
            raise Rejected('Jira journal scope exceeds byte limit')
        scope = json.loads(encoded)
        operation, task = scope['intent']['operation_id'], scope['intent']['task_id']
        encoded, sha = canonical(scope), digest(scope)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._check_schema()
            existing = self.db.execute('SELECT operation_id FROM intents WHERE operation_id=? OR task_id=?', (operation, task)).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['operation_id'] != operation or self.get(operation)['sha256'] != sha:
                    raise Rejected('Jira task or operation already bound to another scope')
            else:
                usage = self.usage()
                if usage['remaining_records'] == 0 or len(encoded) > usage['remaining_scope_bytes']:
                    raise Rejected('Jira journal capacity exhausted; retain existing evidence')
                self.db.execute("INSERT INTO intents VALUES(?,?,?,?,'prepared',NULL)", (operation, task, encoded.decode(), sha))
            result = self.inspect(operation)
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def reserve(self, operation_id, expected_sha256):
        """Internal broker/test primitive only. Consumes one slot, never sends a request."""
        if self.read_only:
            raise Rejected('Jira journal is read-only')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            record = self.get(operation_id)
            if record['sha256'] != expected_sha256 or record['state'] != 'prepared':
                raise Rejected('Expected prepared Jira scope required; reservation cannot be reused')
            self.db.execute("UPDATE intents SET state='uncertain',reserved_at=? WHERE operation_id=?", (now(), operation_id))
            result = self.inspect(operation_id)
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _recovery(self, operation_id, expected_sha256, capture=None):
        self.db.execute('BEGIN')
        try:
            record = self.get(operation_id)
            if record['sha256'] != expected_sha256 or record['state'] != 'uncertain':
                raise Rejected('Jira recovery requires the expected retained uncertain scope')
            scope = record['scope']
            binding = {'schema_version': '1.0.0', **self._metadata(record)}
            if capture is None:
                if scope.get('kind') == 'JiraActionScope':
                    from .jira_actions import recovery_plan
                    binding['read_plan'] = recovery_plan(scope)
                else:
                    binding['read_plan'] = scope['read_plan']
                result = {'kind': 'JiraJournalRecoveryPlan', 'binding': binding, 'sha256': digest(binding), **FLAGS}
            else:
                if scope.get('kind') == 'JiraActionScope':
                    from .jira_actions import reconcile_action
                    assessment = reconcile_action(scope, capture)
                else:
                    assessment = assess_comments(scope['intent'], scope['target'], scope['issue_observations'],
                                                  scope['preview_sha256'], scope['author_key'], capture)
                binding['assessment'] = assessment
                result = {'kind': 'JiraJournalReconciliation', 'binding': binding, 'sha256': digest(binding),
                          'status': assessment['status'], **FLAGS}
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def recovery_plan(self, operation_id, expected_sha256):
        return self._recovery(operation_id, expected_sha256)

    def reconcile(self, operation_id, expected_sha256, capture):
        if capture is None:
            raise Rejected('Jira recovery capture required')
        return self._recovery(operation_id, expected_sha256, capture)

    def audit(self):
        self.db.execute('BEGIN')
        try:
            usage = self.usage()
            if usage['records'] > MAX_RECORDS or usage['scope_bytes'] > MAX_TOTAL_SCOPE_BYTES:
                raise Rejected('Jira journal exceeds audit budget')
            if self.db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                raise Rejected('Jira journal storage check failed')
            records = [self._metadata(self.get(row['operation_id'])) for row in
                       self.db.execute('SELECT operation_id FROM intents ORDER BY operation_id').fetchall()]
            binding = {'schema_version': '1.0.0', 'records': records,
                       'record_count': usage['records'], 'scope_bytes': usage['scope_bytes']}
            result = {'kind': 'JiraJournalAudit', 'binding': binding, 'sha256': digest(binding), 'status': 'valid', **FLAGS}
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
