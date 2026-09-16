"""SQLite records/operations and content-addressed artifacts; no workflow logic."""
import contextlib
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .contracts import Rejected, canonical, now, redact, ref, uid, validate

AUDIT_RESERVE_BYTES = 4 * 1024 * 1024
OWNERSHIP_VERSION = 3
SESSION_VERSION = 4
PRESENCE_VERSION = 5
# What a store migrates to. Name it once so a new schema step changes this line and the
# accepted set, and never a number spelled out somewhere else.
CURRENT_VERSION = PRESENCE_VERSION
SUPPORTED_VERSIONS = (2, OWNERSHIP_VERSION, SESSION_VERSION, PRESENCE_VERSION)
# How many tasks may be advancing at once in one store. Each advancing task can hold a
# container, a workspace and a provider process, so this is a host-resource bound, not a
# throughput target: work is refused before it starts rather than failing halfway and
# leaving an operation to reconcile. It is a fixed application limit like every other cap
# here, and it counts tasks whose worker is alive, never claims left by a crash.
MAX_ACTIVE_TASKS = 4
# The store-wide lock is held for milliseconds when a worker registers a claim, so a
# worker waits for it instead of failing a race it would win on the next attempt. It
# still gives up, because a lock held this long means whole-store work is running.
CLAIM_TIMEOUT = 10.0
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Store:
    def __init__(self, root, task_quota=64 * 1024 * 1024, disk_quota=256 * 1024 * 1024, read_only=False, immutable=False):
        if immutable and not read_only:
            raise Rejected("Immutable storage requires read-only mode")
        self.task_quota, self.disk_quota = task_quota, disk_quota
        self.root = Path(root).resolve()
        if read_only:
            self.db = sqlite3.connect((self.root / "orch.sqlite").as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in SUPPORTED_VERSIONS:
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
        if version not in (0, 1, *SUPPORTED_VERSIONS):
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
            self.db.execute("CREATE TABLE IF NOT EXISTS broker_identities(operation_id TEXT NOT NULL REFERENCES operations(id), generation INTEGER NOT NULL, record TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(operation_id,generation))")
            # Current ownership, not evidence: a claim is taken and released, while the
            # append-only event log keeps the history of who advanced what.
            self.db.execute("CREATE TABLE IF NOT EXISTS task_owners(task_id TEXT PRIMARY KEY REFERENCES tasks(id), owner TEXT NOT NULL, claimed_at TEXT NOT NULL, revision INTEGER NOT NULL, generation INTEGER NOT NULL)")
            # A lease expires; a worker's lock does not, and leaves this column NULL.
            if "expires_at" not in {r[1] for r in self.db.execute("PRAGMA table_info(task_owners)")}:
                self.db.execute("ALTER TABLE task_owners ADD COLUMN expires_at TEXT")
            # When each agent was last heard from. Current state rather than evidence, like
            # ownership: an agent that is running and idle holds nothing, and without this it
            # is indistinguishable from one that stopped an hour ago. Nothing is appended and
            # nothing is kept, so it carries no append-only trigger.
            self.db.execute("CREATE TABLE IF NOT EXISTS agent_presence(agent TEXT PRIMARY KEY, last_seen TEXT NOT NULL, route TEXT NOT NULL)")
            self.db.execute("PRAGMA user_version=%d" % CURRENT_VERSION)
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_execution_receipt ON records(task_id,kind,json_extract(payload,'$.operation_id')) WHERE kind IN ('PatchReceipt','TestReceipt','ExternalActionReceipt')")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_invocation_receipt ON records(task_id,json_extract(payload,'$.invocation_id')) WHERE kind='AgentInvocationReceipt'")
        for table in ("records", "events", "artifacts", "approvals", "effects", "broker_requests", "execution_provenance", "broker_identities"):
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

    def lock_path(self, task=None):
        if task is None:
            return self.root / "dispatcher.lock"
        if not SAFE_ID.fullmatch(str(task)):
            raise Rejected("Unsafe task identifier")
        locks = self.root / "locks"
        locks.mkdir(exist_ok=True)
        return locks / (str(task) + ".lock")

    @contextlib.contextmanager
    def exclusive(self, task=None, wait=0.0):
        """One task's advancement lock, or the store-wide dispatcher lock.

        Task work takes its own lock, so two workers can advance two different tasks at
        once. The store-wide lock guards the claim registry and whole-store maintenance:
        holding it stops new claims without waiting for work already under way, and
        `active_owners()` says what is still running. Kernel locks release on crash,
        unlike a stale PID file, so the durable `task_owners` row is what survives to
        show that a worker was interrupted.
        """
        with self.lock_path(task).open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deadline = time.monotonic() + wait
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise Rejected("Task is being advanced elsewhere" if task else "Dispatcher busy") from exc
                    time.sleep(0.02)
                stream.seek(0)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def held(self, task):
        """Whether some process currently holds this task's advancement lock."""
        try:
            with self.exclusive(task):
                return False
        except Rejected:
            return True

    def owner(self, task):
        row = self.db.execute("SELECT * FROM task_owners WHERE task_id=?", (task,)).fetchone()
        return dict(row) if row else None

    def seen(self, agent, route):
        """Record that this agent called, which is the only liveness anyone needs.

        Derived from work the agent already does rather than from a heartbeat it could
        forget: every authenticated request proves the container is running and that its
        secret still verifies, which is more than a liveness ping would prove.
        """
        self.db.execute("INSERT INTO agent_presence VALUES(?,?,?) ON CONFLICT(agent) DO UPDATE "
                        "SET last_seen=excluded.last_seen, route=excluded.route", (agent, now(), route))

    def presence(self):
        return {row["agent"]: {"last_seen": row["last_seen"], "route": row["route"]}
                for row in self.db.execute("SELECT * FROM agent_presence")}

    def owners(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM task_owners ORDER BY task_id")]

    def live(self, row):
        """Whether a claim still holds: a lease until it expires, a worker while it runs.

        A worker's liveness cannot go stale because it is the lock itself; an agent's is a
        time it must keep renewing, since an agent between calls has no process at all.
        """
        if row["expires_at"]:
            return row["expires_at"] > now()
        return self.held(row["task_id"])

    def active_owners(self):
        """Every claim that still holds, of either kind."""
        return [row for row in self.owners() if self.live(row)]

    def admitted(self, task):
        """Claims held by a live worker other than this task's, for the admission bound."""
        return [row for row in self.active_owners() if row["task_id"] != task]

    def quiescent(self):
        """Refuse whole-store work while any worker is actually advancing a task.

        Call it under the store-wide lock, which stops new claims: a claim is registered
        while holding that lock, so a worker that has not registered yet cannot proceed.
        """
        active = self.active_owners()
        if active:
            raise Rejected("Task " + active[0]["task_id"] + " is being advanced; retry when workers are idle")

    def claim(self, task, owner, revision, generation, expires_in=None):
        """Record current ownership inside the caller's transaction.

        `expires_in` seconds makes it an agent's lease; without it the holder is a worker
        process, live for exactly as long as it holds the task's lock.
        """
        moment = now()
        expires_at = None
        if expires_in is not None:
            expires_at = (datetime.fromisoformat(moment) + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")
        self.db.execute("INSERT INTO task_owners(task_id,owner,claimed_at,revision,generation,expires_at) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(task_id) DO UPDATE SET owner=excluded.owner, claimed_at=excluded.claimed_at,"
                        " revision=excluded.revision, generation=excluded.generation, expires_at=excluded.expires_at",
                        (task, owner, moment, revision, generation, expires_at))

    def disclaim(self, task, owner):
        """Release ownership, but never another worker's."""
        self.db.execute("DELETE FROM task_owners WHERE task_id=? AND owner=?", (task, owner))

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
