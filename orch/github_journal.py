"""Durable offline GitHub intent/reservation journal, separate from workflow storage."""
import json
from datetime import datetime
import re
import sqlite3

from .contracts import Rejected, canonical, digest, now
from .github_preview import prepare_pull_request
from .maintenance import real_path
from . import journal_succession

MAX_RECORDS = 1000
MAX_TOTAL_SCOPE_BYTES = 16 * 1024 * 1024
MAX_SCOPE_BYTES = 65536
SCHEMA_SQL = (
    "CREATE TABLE intents(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, scope TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','uncertain')), reserved_at TEXT)",
    "CREATE TRIGGER immutable_scope BEFORE UPDATE OF operation_id,task_id,scope,sha256 ON intents BEGIN SELECT RAISE(ABORT,'immutable scope'); END",
    "CREATE TRIGGER no_delete BEFORE DELETE ON intents BEGIN SELECT RAISE(ABORT,'retained intent'); END",
    "CREATE TRIGGER one_reservation BEFORE UPDATE ON intents WHEN NOT (OLD.state='prepared' AND NEW.state='uncertain' AND OLD.reserved_at IS NULL AND NEW.reserved_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'reservation consumed'); END",
)


def bound_scope(intent, allowed_repository, repository_id):
    if type(repository_id) is not int or not 0 < repository_id <= 9007199254740991:
        raise Rejected("An explicit repository ID is required")
    preview = prepare_pull_request(intent, allowed_repository)
    return {"preview": preview, "repository_id": repository_id}


def validated_intent(scope):
    """Recover intent only when the entire stored preview matches its reconstruction."""
    binding = scope['preview']['binding']
    request = binding['request']
    destination = re.fullmatch(r"https://api\.github\.com/repos/([^/]+/[^/]+)/pulls", request['url'])
    if not destination:
        raise Rejected("Invalid journal destination")
    intent = {key: binding[key] for key in ('schema_version', 'task_id', 'operation_id', 'expected_base_sha', 'expected_head_sha')}
    intent.update({key: request['body'][key] for key in ('title', 'body', 'base', 'head')})
    intent['repository'] = destination[1]
    expected = bound_scope(intent, destination[1], scope['repository_id'])
    if canonical(expected) != canonical(scope):
        raise Rejected("Invalid journal scope binding")
    return intent


class GitHubJournal:
    """One immutable operation per task. Reservation is never dispatch authority."""
    FAMILY = "GitHub"

    def __init__(self, path, read_only=False):
        if str(path) == ":memory:":
            raise Rejected("GitHub journal requires persistent storage")
        # Canonical, link-free and spelled one way: succession records name journals by
        # path, and Windows can spell the same file in 8.3 short form.
        path = self.path = real_path(path)
        self.read_only = read_only
        if read_only:
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            try:
                if (self.db.execute("PRAGMA user_version").fetchone()[0] not in (1, journal_succession.VERSION)
                        or self.db.execute("PRAGMA application_id").fetchone()[0] != 1330792264):
                    raise Rejected("Not a supported GitHub journal")
            except BaseException:
                self.db.close()
                raise
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            identity = self.db.execute("PRAGMA application_id").fetchone()[0]
            if version == 0 and identity == 0 and not self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                for statement in SCHEMA_SQL:
                    self.db.execute(statement)
                self.db.execute("PRAGMA application_id=1330792264")
                self.db.execute("PRAGMA user_version=1")
            elif version not in (1, journal_succession.VERSION) or identity != 1330792264:
                raise Rejected("Not a supported GitHub journal")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
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
        # A retired journal and its successor carry the succession table as well; every
        # other shape, including that table without its version or triggers, is refused.
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version == journal_succession.VERSION:
            expected |= journal_succession.OBJECTS
        elif version != 1:
            raise Rejected("Unsupported GitHub journal schema or constraint configuration")
        actual = {tuple(row) for row in self.db.execute(
            "SELECT type,name,tbl_name,CASE WHEN length(sql)<=4096 THEN sql END FROM main.sqlite_master LIMIT ?",
            (len(expected) + 1,))}
        if (actual != expected or self.db.execute('SELECT 1 FROM sqlite_temp_master LIMIT 1').fetchone()
                or self.db.execute('PRAGMA application_id').fetchone()[0] != 1330792264
                or self.db.execute('PRAGMA ignore_check_constraints').fetchone()[0] != 0):
            raise Rejected("Unsupported GitHub journal schema or constraint configuration")

    def get(self, operation_id):
        # Do not materialize an oversized stored scope in Python, even after file tampering.
        row = self.db.execute("SELECT operation_id, task_id, sha256, state, reserved_at, CASE WHEN length(CAST(scope AS BLOB)) <= ? THEN scope END AS scope FROM intents WHERE operation_id=?", (MAX_SCOPE_BYTES, operation_id)).fetchone()
        if row is None:
            raise Rejected("Unknown GitHub operation")
        try:
            if row['scope'] is None:
                raise Rejected("Stored scope exceeds byte limit")
            scope = json.loads(row['scope'])
            intent = validated_intent(scope)
            if (canonical(scope).decode() != row['scope']
                    or digest(scope) != row['sha256'] or intent['task_id'] != row['task_id']
                    or intent['operation_id'] != row['operation_id']):
                raise Rejected("Invalid journal scope binding")
            if row['state'] == 'prepared':
                if row['reserved_at'] is not None:
                    raise Rejected("Prepared intent has reservation evidence")
            elif row['state'] == 'uncertain':
                reserved = row['reserved_at']
                if not isinstance(reserved, str) or not reserved.endswith('Z'):
                    raise Rejected("Uncertain intent lacks reservation evidence")
                datetime.fromisoformat(reserved)
            else:
                raise Rejected("Invalid journal state")
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise Rejected("Invalid GitHub journal record") from exc
        return {"operation_id": row['operation_id'], "task_id": row['task_id'], "scope": scope,
                "sha256": row['sha256'], "state": row['state'], "reserved_at": row['reserved_at'],
                "live_authorized": False, "retry_allowed": False}

    def usage(self):
        """Logical admission usage only; not a disk-size or record-integrity check."""
        row = self.db.execute("SELECT count(*) AS records, coalesce(sum(length(CAST(scope AS BLOB))), 0) AS scope_bytes FROM intents").fetchone()
        # Read the succession state unbound: a verified backup copy legitimately carries
        # the record its source wrote. `github-journal-chain` is the bound, checked walk.
        succession = journal_succession.summary(self.db)
        return {"records": row['records'], "scope_bytes": row['scope_bytes'],
                "journal_status": succession['status'], "successor": succession['successor'],
                "predecessor": succession['predecessor'],
                "limits": {"records": MAX_RECORDS, "scope_bytes": MAX_TOTAL_SCOPE_BYTES, "record_scope_bytes": MAX_SCOPE_BYTES},
                "remaining_records": max(0, MAX_RECORDS - row['records']),
                "remaining_scope_bytes": max(0, MAX_TOTAL_SCOPE_BYTES - row['scope_bytes']),
                "live_authorized": False, "retry_allowed": False}

    def reconcile(self, operation_id, expected_sha256, observations):
        """Classify supplied observations against one validated, retained reservation."""
        from .github_reconcile import reconcile_pull_request

        record = self.get(operation_id)
        if record['sha256'] != expected_sha256 or record['state'] != 'uncertain':
            raise Rejected("Reconciliation requires the expected scope and an uncertain reservation")
        intent = validated_intent(record['scope'])
        assessment = reconcile_pull_request(intent, intent['repository'], record['scope']['repository_id'], observations)
        binding = {"schema_version": "1.0.0", "operation_id": record['operation_id'],
                   "task_id": record['task_id'], "scope_sha256": record['sha256'],
                   "state": record['state'], "reserved_at": record['reserved_at'],
                   "assessment": assessment}
        return {"kind": "GitHubJournalReconciliation", "binding": binding, "sha256": digest(binding),
                "status": assessment['status'], "retry_allowed": False,
                "remote_effect_confirmed": False, "live_authorized": False}

    def audit(self):
        """Validate a bounded, consistent snapshot; emit no proposal text."""
        self.db.execute("BEGIN")
        try:
            self._check_schema()
            usage = self.usage()
            if usage['records'] > MAX_RECORDS or usage['scope_bytes'] > MAX_TOTAL_SCOPE_BYTES:
                raise Rejected("Journal exceeds audit budget")
            if self.db.execute("PRAGMA quick_check(1)").fetchone()[0] != 'ok':
                raise Rejected("Journal storage check failed")
            records = []
            for row in self.db.execute("SELECT operation_id FROM intents ORDER BY operation_id").fetchall():
                record = self.get(row['operation_id'])
                records.append({key: record[key] for key in ('operation_id', 'task_id', 'sha256', 'state', 'reserved_at')})
            binding = {"schema_version": "1.0.0", "records": records,
                       "record_count": usage['records'], "scope_bytes": usage['scope_bytes']}
            result = {"kind": "GitHubJournalAudit", "binding": binding, "sha256": digest(binding),
                      "status": "valid", "live_authorized": False, "retry_allowed": False}
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def stage(self, intent, allowed_repository, repository_id):
        if self.read_only:
            raise Rejected("Journal is read-only")
        scope = bound_scope(intent, allowed_repository, repository_id)
        encoded = canonical(scope)
        if len(encoded) > MAX_SCOPE_BYTES:
            raise Rejected("GitHub journal scope exceeds byte limit")
        sha = digest(scope)
        operation = intent['operation_id']
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._check_schema()
            existing = self.db.execute("SELECT operation_id FROM intents WHERE operation_id=? OR task_id=?", (operation, intent['task_id'])).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['operation_id'] != operation or self.get(operation)['sha256'] != sha:
                    raise Rejected("Task or operation is already bound to another scope")
            else:
                # New admission only: a retired journal keeps reserving and reconciling
                # what it already holds, and the chain refuses a second attempt slot.
                journal_succession.check_admission(self, intent['task_id'], operation)
                # The check shares the insertion's writer transaction across processes.
                usage = self.usage()
                if usage['remaining_records'] == 0 or len(encoded) > usage['remaining_scope_bytes']:
                    raise Rejected("GitHub journal capacity exhausted; retained evidence must not be deleted")
                self.db.execute("INSERT INTO intents VALUES(?,?,?,?, 'prepared',NULL)", (operation, intent['task_id'], encoded.decode(), sha))
            result = self.get(operation)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reserve(self, operation_id, expected_sha256):
        if self.read_only:
            raise Rejected("Journal is read-only")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._check_schema()
            current = self.get(operation_id)
            if current['sha256'] != expected_sha256 or current['state'] != 'prepared':
                raise Rejected("Scope changed or reservation already consumed; reconcile before further action")
            self.db.execute("UPDATE intents SET state='uncertain', reserved_at=? WHERE operation_id=?", (now(), operation_id))
            result = self.get(operation_id)
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
