"""SQLite records/operations and content-addressed artifacts; no workflow logic."""
import contextlib
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .contracts import Rejected, canonical, redact, ref, uid, validate

AUDIT_RESERVE_BYTES = 4 * 1024 * 1024


class Store:
    def __init__(self, root, task_quota=64 * 1024 * 1024, disk_quota=256 * 1024 * 1024, read_only=False):
        self.task_quota, self.disk_quota = task_quota, disk_quota
        self.root = Path(root).resolve()
        if read_only:
            self.db = sqlite3.connect((self.root / "orch.sqlite").as_uri() + "?mode=ro", uri=True, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA user_version").fetchone()[0] != 2:
                self.db.close()
                raise Rejected("Usage reporting requires an existing current-version database")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "orch.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise Rejected("Unsupported database version")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, state TEXT NOT NULL, context TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(task_id TEXT NOT NULL REFERENCES tasks(id), sequence INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id,sequence));
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals(request_id TEXT PRIMARY KEY REFERENCES records(id), decision_id TEXT NOT NULL REFERENCES records(id));
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), stage TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL, result TEXT, UNIQUE(task_id,stage,slot));
        CREATE TABLE IF NOT EXISTS effects(operation_id TEXT PRIMARY KEY REFERENCES operations(id), receipt_id TEXT NOT NULL REFERENCES records(id));
        ''')
        with self.transaction():
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(operations)")}
            if "generation" not in columns:
                self.db.execute("ALTER TABLE operations ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            self.db.execute("CREATE TABLE IF NOT EXISTS broker_requests(operation_id TEXT NOT NULL REFERENCES operations(id), generation INTEGER NOT NULL, request TEXT NOT NULL, PRIMARY KEY(operation_id,generation))")
            self.db.execute("CREATE TABLE IF NOT EXISTS execution_provenance(operation_id TEXT NOT NULL REFERENCES operations(id), generation INTEGER NOT NULL, envelope_sha256 TEXT NOT NULL, artifact TEXT NOT NULL, PRIMARY KEY(operation_id,generation))")
            self.db.execute("PRAGMA user_version=2")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_execution_receipt ON records(task_id,kind,json_extract(payload,'$.operation_id')) WHERE kind IN ('PatchReceipt','TestReceipt','ExternalActionReceipt')")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_invocation_receipt ON records(task_id,json_extract(payload,'$.invocation_id')) WHERE kind='AgentInvocationReceipt'")
        for table in ("records", "events", "artifacts", "approvals", "effects", "broker_requests", "execution_provenance"):
            for action in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only'); END")

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @contextlib.contextmanager
    def exclusive(self):
        """Single host dispatcher. Kernel lock releases on crash, unlike a stale PID file."""
        with (self.root / "dispatcher.lock").open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise Rejected("Dispatcher busy") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise Rejected("Dispatcher busy") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def put(self, value):
        value = validate(value)
        old = self.db.execute("SELECT payload FROM records WHERE id=?", (value["id"],)).fetchone()
        data = canonical(value).decode()
        if old and old[0] != data:
            raise Rejected("Conflicting duplicate receipt")
        if not old:
            try:
                self.db.execute("INSERT INTO records VALUES(?,?,?,?)", (value["id"], value["task_id"], value["contract_type"], data))
            except sqlite3.IntegrityError as exc:
                raise Rejected("Duplicate receipt or invalid task reference") from exc
        return ref(value)

    def get(self, document, task_id, kind=None):
        row = self.db.execute("SELECT payload FROM records WHERE id=? AND task_id=?", (document["id"], task_id)).fetchone()
        if not row:
            raise Rejected("Missing or cross-task record")
        value = validate(json.loads(row[0]), kind)
        if ref(value) != document:
            raise Rejected("Record hash mismatch")
        return value

    def artifact(self, task_id, content, media_type="application/json", producer="orchestrator", trust="untrusted"):
        if not isinstance(content, str):
            content = canonical(content).decode()
        clean = redact(content)
        raw = clean.encode()
        if len(raw) > 1024 * 1024:
            raise Rejected("Artifact quota exceeded")
        used = self.db.execute("SELECT coalesce(sum(json_extract(payload,'$.size_bytes')),0) FROM artifacts WHERE task_id=?", (task_id,)).fetchone()[0]
        # Small audit reserve lets the engine record a fail-closed quota incident.
        reserve = AUDIT_RESERVE_BYTES if producer == "orchestrator" and trust == "trusted_receipt" else 0
        if used + len(raw) > self.task_quota + reserve:
            raise Rejected("Task artifact quota exceeded")
        sha = hashlib.sha256(raw).hexdigest()
        path = self.root / "artifacts" / sha
        if path.is_symlink():
            raise Rejected("Artifact link rejected")
        if not path.exists():
            disk_used = sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file() and not p.is_symlink())
            if disk_used + len(raw) > self.disk_quota + reserve:
                raise Rejected("Artifact disk quota exceeded")
            temp = path.with_suffix("." + uid())
            with temp.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        item = dict(artifact_id=uid(), sha256=sha, media_type=media_type, size_bytes=len(raw), trust=trust, producer_id=producer, redacted=clean != content)
        self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (item["artifact_id"], task_id, canonical(item).decode()))
        return item

    def read_artifact(self, item, task_id):
        row = self.db.execute("SELECT payload FROM artifacts WHERE id=? AND task_id=?", (item["artifact_id"], task_id)).fetchone()
        if not row or json.loads(row[0]) != item:
            raise Rejected("Artifact not authorized")
        path = self.root / "artifacts" / item["sha256"]
        if path.is_symlink():
            raise Rejected("Artifact link rejected")
        raw = path.read_bytes()
        if len(raw) != item["size_bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise Rejected("Artifact integrity failure")
        return raw.decode()

    def task(self, task_id):
        row = self.db.execute("SELECT state,context FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise Rejected("Unknown task")
        return json.loads(row[0]), json.loads(row[1])

    def events(self, task_id):
        self.task(task_id)
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events WHERE task_id=? ORDER BY sequence", (task_id,))]

    def reconstruct(self, task_id):
        result = None
        for sequence, event in enumerate(self.events(task_id), 1):
            validate(event, "TaskEvent")
            if event["sequence"] != sequence:
                raise Rejected("Event gap")
            result = json.loads(self.read_artifact(event["payload"], task_id))
        return result

    def complete_operation(self, operation_id, generation, result):
        updated = self.db.execute("UPDATE operations SET status='done',result=? WHERE id=? AND generation=? AND status='started'", (canonical(result).decode(), operation_id, generation))
        if updated.rowcount != 1:
            raise Rejected("Stale operation completion")
