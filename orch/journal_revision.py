"""Withdraw a prepared operation so its task can be prepared again, releasing no attempt.

One operation per task and one attempt per operation are the journal's two hard rules, and
until now they left a task no way forward when its prepared scope stopped being correct. A
GitHub intent binds `expected_base_sha`; when the branch moves before dispatch the prepared
operation can never be dispatched, and the task can never be prepared again, because its
one operation is already bound to the stale scope. The documentation called this "no intent
revision" and left it there.

Withdrawal releases the task without releasing an attempt. A `prepared` operation, whose
attempt slot has never been consumed, becomes `withdrawn`: it keeps its scope, its digest
and its place in the journal, and the task may then be prepared again under a **new**
operation identifier. An `uncertain` operation can never be withdrawn, because its attempt
may already have reached the remote; that record has to be reconciled, exactly as an
uncertain task must be reconciled rather than cancelled.

Storage enforces the rule rather than trusting this code: `one_live_operation` is a unique
index over `task_id` restricted to operations that are not withdrawn, so a second live
operation for one task is refused by SQLite itself, and the update trigger permits only
`prepared` to `uncertain` or `prepared` to `withdrawn`.

Reaching that shape needs the `intents` table rebuilt, since SQLite cannot drop the column
constraint the old one carries. `upgrade()` does it in one transaction against a verified
snapshot, copies through a scratch table so the stored schema text stays exactly what this
module declares, and refuses to commit unless every row comes through unchanged.
"""
import json

from .contracts import Rejected, canonical, digest, now, redact
from .maintenance import real_path
from . import journal_succession

VERSION = 3
TABLE = "journal_withdrawals"
STATES = ("prepared", "uncertain", "withdrawn")
SCHEMA_SQL = (
    "CREATE TABLE intents(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','uncertain','withdrawn')), reserved_at TEXT)",
    "CREATE UNIQUE INDEX one_live_operation ON intents(task_id) WHERE state<>'withdrawn'",
    "CREATE TRIGGER immutable_scope BEFORE UPDATE OF operation_id,task_id,scope,sha256 ON intents BEGIN SELECT RAISE(ABORT,'immutable scope'); END",
    "CREATE TRIGGER no_delete BEFORE DELETE ON intents BEGIN SELECT RAISE(ABORT,'retained intent'); END",
    "CREATE TRIGGER one_reservation BEFORE UPDATE ON intents WHEN NOT (OLD.state='prepared' AND ((NEW.state='uncertain' AND OLD.reserved_at IS NULL AND NEW.reserved_at IS NOT NULL) OR (NEW.state='withdrawn' AND OLD.reserved_at IS NULL AND NEW.reserved_at IS NULL))) BEGIN SELECT RAISE(ABORT,'reservation consumed'); END",
)
WITHDRAWAL_SQL = (
    "CREATE TABLE journal_withdrawals(operation_id TEXT PRIMARY KEY, record TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "CREATE TRIGGER immutable_withdrawal BEFORE UPDATE ON journal_withdrawals BEGIN SELECT RAISE(ABORT,'immutable withdrawal'); END",
    "CREATE TRIGGER retained_withdrawal BEFORE DELETE ON journal_withdrawals BEGIN SELECT RAISE(ABORT,'retained withdrawal'); END",
)
# The live query both journals use: an operation identifier is never reused, in any state,
# while a task is only taken by an operation that has not been withdrawn.
LIVE_CLAUSE = "operation_id=? OR (task_id=? AND state<>'withdrawn')"
OBJECTS = {("table", "intents", "intents", SCHEMA_SQL[0]),
           ("index", "sqlite_autoindex_intents_1", "intents", None),
           ("index", "one_live_operation", "intents", SCHEMA_SQL[1]),
           ("table", TABLE, TABLE, WITHDRAWAL_SQL[0]),
           ("index", "sqlite_autoindex_" + TABLE + "_1", TABLE, None)}
OBJECTS.update(("trigger", name, "intents", sql) for name, sql in
               zip(("immutable_scope", "no_delete", "one_reservation"), SCHEMA_SQL[2:]))
OBJECTS.update(("trigger", name, TABLE, sql) for name, sql in
               zip(("immutable_withdrawal", "retained_withdrawal"), WITHDRAWAL_SQL[1:]))


def schema_objects(base_sql, version):
    """Every `sqlite_master` row a journal at this schema version must have, and no other.

    Version 1 is the original journal, 2 adds succession, 3 rebuilds `intents` so a task
    can be prepared again after a withdrawal and adds the withdrawal evidence table.
    """
    if version == VERSION:
        return set(OBJECTS) | journal_succession.OBJECTS
    expected = {("table", "intents", "intents", base_sql[0]),
                ("index", "sqlite_autoindex_intents_1", "intents", None),
                ("index", "sqlite_autoindex_intents_2", "intents", None)}
    expected.update(("trigger", name, "intents", sql) for name, sql in
                    zip(("immutable_scope", "no_delete", "one_reservation"), base_sql[1:]))
    if version == journal_succession.VERSION:
        return expected | journal_succession.OBJECTS
    if version != 1:
        raise Rejected("Unsupported journal schema version")
    return expected


def revisable(db):
    """Whether this journal has the rebuilt table that withdrawal needs."""
    return db.execute("PRAGMA user_version").fetchone()[0] == VERSION


def require_revisable(db):
    if not revisable(db):
        raise Rejected("This journal predates intent revision; upgrade it first")


def audited_reason(reason):
    """One audited line, exactly as a cancellation reason is validated."""
    if not isinstance(reason, str):
        raise Rejected("An audited withdrawal reason is required")
    reason = reason.strip()
    if not 1 <= len(reason) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in reason) or redact(reason) != reason:
        raise Rejected("Invalid withdrawal reason")
    return reason


def read_record(db, operation_id, journal=None):
    """The withdrawal record for an operation, or None.

    The record names the journal that wrote it, and a verified backup copy carries that
    name unchanged, so readers of a record attached to a row leave `journal` unset and read
    the provenance from the record itself. Its digest and operation binding are always
    checked; the state column, not this record, is what refuses a second attempt.
    """
    if not revisable(db):
        return None
    row = db.execute("SELECT record,sha256 FROM " + TABLE + " WHERE operation_id=?", (operation_id,)).fetchone()
    if not row:
        return None
    try:
        saved = json.loads(row[0])
        # Rejected is a ValueError, so decide first and raise outside this handler.
        intact = canonical(saved).decode() == row[0] and digest(saved) == row[1]
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid journal withdrawal record") from exc
    if not intact or saved.get("operation_id") != operation_id:
        raise Rejected("Journal withdrawal record integrity mismatch")
    if journal is not None and saved.get("journal") != str(journal):
        raise Rejected("Journal withdrawal record names another journal")
    return saved


def counts(db):
    """Live and withdrawn record counts, for usage reporting."""
    row = db.execute("SELECT sum(state='withdrawn') AS withdrawn FROM intents").fetchone()
    return {"withdrawn_records": row["withdrawn"] or 0}


def withdraw(journal, operation_id, expected_sha256, reason, principal="local-operator"):
    """Release a prepared operation's task without releasing its attempt.

    The attempt slot is not returned to anyone: this operation identifier stays in the
    journal, withdrawn, and the replacement must be a new one. An uncertain operation is
    refused, because a consumed attempt may already have reached the remote and has to be
    reconciled instead.
    """
    if journal.read_only:
        raise Rejected("Journal is read-only")
    reason = audited_reason(reason)
    journal.db.execute("BEGIN IMMEDIATE")
    try:
        journal._check_schema()
        require_revisable(journal.db)
        row = journal.db.execute("SELECT task_id,sha256,state FROM intents WHERE operation_id=?",
                                 (operation_id,)).fetchone()
        if row is None:
            raise Rejected("Unknown operation")
        if row["state"] == "withdrawn":
            raise Rejected("That operation is already withdrawn")
        if row["state"] != "prepared":
            raise Rejected("That operation consumed its attempt; reconcile it instead of withdrawing it")
        if row["sha256"] != expected_sha256:
            raise Rejected("Withdrawal requires the expected retained scope digest")
        record = {"schema_version": "1.0.0", "journal": str(journal.path), "operation_id": operation_id,
                  "task_id": row["task_id"], "scope_sha256": row["sha256"], "reason": reason,
                  "principal": principal, "withdrawn_at": now()}
        journal.db.execute("INSERT INTO " + TABLE + " VALUES(?,?,?)",
                           (operation_id, canonical(record).decode(), digest(record)))
        journal.db.execute("UPDATE intents SET state='withdrawn' WHERE operation_id=?", (operation_id,))
        journal.db.execute("COMMIT")
    except BaseException:
        if journal.db.in_transaction:
            journal.db.execute("ROLLBACK")
        raise
    return {"kind": journal.FAMILY + "JournalWithdrawal", "status": "withdrawn",
            "journal": str(journal.path), "operation_id": operation_id, "task_id": record["task_id"],
            "scope_sha256": record["scope_sha256"], "reason": reason,
            "withdrawn_at": record["withdrawn_at"], "records_deleted": 0,
            "attempt_released": False, "retry_allowed": False, "live_authorized": False}


def upgrade(path, snapshot, expected_sha256, label, principal="local-operator"):
    """Rebuild `intents` so a withdrawn operation's task can be prepared again.

    SQLite cannot drop the old table's `UNIQUE` column constraint, so the rows are copied
    through a scratch table and back into a table created from this module's exact schema
    text. Everything happens in one transaction, against a verified snapshot, and the copy
    is refused unless every row comes through unchanged.
    """
    cls, backup = journal_succession.family(label)
    path = real_path(path)
    if not path.is_file():
        raise Rejected("An existing journal is required")
    comparison = backup.compare_journal_backup(path, snapshot, expected_sha256)
    if comparison["status"] != "matches":
        raise Rejected("Upgrade only against a snapshot that matches the journal exactly")
    journal = cls(path)
    try:
        before = journal.audit()
        if revisable(journal.db):
            raise Rejected("This journal already supports intent revision")
        journal.db.execute("BEGIN IMMEDIATE")
        try:
            journal._check_schema()
            rows = journal.db.execute("SELECT * FROM intents ORDER BY operation_id").fetchall()
            original = [tuple(row) for row in rows]
            journal.db.execute("CREATE TABLE upgrade_scratch(operation_id TEXT, task_id TEXT, scope TEXT, sha256 TEXT, state TEXT, reserved_at TEXT)")
            journal.db.execute("INSERT INTO upgrade_scratch SELECT * FROM intents")
            journal.db.execute("DROP TABLE intents")   # Takes the old table's triggers with it.
            for statement in SCHEMA_SQL[:1] + WITHDRAWAL_SQL[:1]:
                journal.db.execute(statement)
            journal.db.execute("INSERT INTO intents SELECT * FROM upgrade_scratch")
            journal.db.execute("DROP TABLE upgrade_scratch")
            for statement in SCHEMA_SQL[1:] + WITHDRAWAL_SQL[1:]:
                journal.db.execute(statement)
            journal_succession.install(journal.db)     # Version 3 carries succession as well.
            copied = [tuple(row) for row in
                      journal.db.execute("SELECT * FROM intents ORDER BY operation_id").fetchall()]
            if copied != original:
                raise Rejected("Upgrade did not reproduce every retained record")
            journal.db.execute("PRAGMA user_version=%d" % VERSION)
            journal._check_schema()
            journal.db.execute("COMMIT")
        except BaseException:
            if journal.db.in_transaction:
                journal.db.execute("ROLLBACK")
            raise
        after = journal.audit()
    finally:
        journal.close()
    if after["sha256"] != before["sha256"]:
        raise Rejected("Upgrade changed the journal audit; restore the verified snapshot")
    return {"kind": label + "JournalUpgrade", "status": "upgraded", "journal": str(path),
            "schema_version": VERSION, "snapshot_sha256": expected_sha256,
            "audit_sha256": after["sha256"], "records": after["binding"]["record_count"],
            "records_deleted": 0, "records_changed": 0, "principal": principal,
            "upgraded_at": now(), "retry_allowed": False, "live_authorized": False}
