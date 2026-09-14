"""Durable offline GitHub intent/reservation journal, separate from workflow storage."""
import json
from datetime import datetime
from pathlib import Path
import re
import sqlite3

from .contracts import Rejected, canonical, digest, now
from .github_preview import prepare_pull_request


def bound_scope(intent, allowed_repository, repository_id):
    if type(repository_id) is not int or not 0 < repository_id <= 9007199254740991:
        raise Rejected("An explicit repository ID is required")
    preview = prepare_pull_request(intent, allowed_repository)
    return {"preview": preview, "repository_id": repository_id}


class GitHubJournal:
    """One immutable operation per task. Reservation is never dispatch authority."""
    def __init__(self, path, read_only=False):
        if str(path) == ":memory:":
            raise Rejected("GitHub journal requires persistent storage")
        path = Path(path)
        if path.is_symlink():
            raise Rejected("Journal must not be a symlink")
        self.read_only = read_only
        if read_only:
            self.db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            try:
                if (self.db.execute("PRAGMA user_version").fetchone()[0] != 1
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
                self.db.execute("CREATE TABLE intents(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, scope TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','uncertain')), reserved_at TEXT)")
                self.db.execute("CREATE TRIGGER immutable_scope BEFORE UPDATE OF operation_id,task_id,scope,sha256 ON intents BEGIN SELECT RAISE(ABORT,'immutable scope'); END")
                self.db.execute("CREATE TRIGGER no_delete BEFORE DELETE ON intents BEGIN SELECT RAISE(ABORT,'retained intent'); END")
                self.db.execute("CREATE TRIGGER one_reservation BEFORE UPDATE ON intents WHEN NOT (OLD.state='prepared' AND NEW.state='uncertain' AND OLD.reserved_at IS NULL AND NEW.reserved_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'reservation consumed'); END")
                self.db.execute("PRAGMA application_id=1330792264")
                self.db.execute("PRAGMA user_version=1")
            elif version != 1 or identity != 1330792264:
                raise Rejected("Not a supported GitHub journal")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def get(self, operation_id):
        row = self.db.execute("SELECT * FROM intents WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise Rejected("Unknown GitHub operation")
        try:
            scope = json.loads(row['scope'])
            binding = scope['preview']['binding']
            request = binding['request']
            destination = re.fullmatch(r"https://api\.github\.com/repos/([^/]+/[^/]+)/pulls", request['url'])
            if not destination:
                raise Rejected("Invalid journal destination")
            intent = {key: binding[key] for key in ('schema_version', 'task_id', 'operation_id', 'expected_base_sha', 'expected_head_sha')}
            intent.update({key: request['body'][key] for key in ('title', 'body', 'base', 'head')})
            intent['repository'] = destination[1]
            expected = bound_scope(intent, destination[1], scope['repository_id'])
            if (canonical(expected) != canonical(scope) or canonical(scope).decode() != row['scope']
                    or digest(scope) != row['sha256'] or binding['task_id'] != row['task_id']
                    or binding['operation_id'] != row['operation_id']):
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

    def stage(self, intent, allowed_repository, repository_id):
        if self.read_only:
            raise Rejected("Journal is read-only")
        scope = bound_scope(intent, allowed_repository, repository_id)
        sha = digest(scope)
        operation = intent['operation_id']
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT operation_id FROM intents WHERE operation_id=? OR task_id=?", (operation, intent['task_id'])).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['operation_id'] != operation or self.get(operation)['sha256'] != sha:
                    raise Rejected("Task or operation is already bound to another scope")
            else:
                self.db.execute("INSERT INTO intents VALUES(?,?,?,?, 'prepared',NULL)", (operation, intent['task_id'], canonical(scope).decode(), sha))
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
