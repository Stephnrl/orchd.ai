"""Retire a full integration operation journal into a successor that still refuses a second attempt.

The GitHub and Jira operation journals admit a bounded number of intents and delete
nothing, so capacity exhaustion has been a documented dead end: usage reports no
remaining records, staging is refused, and the docs could only warn that the obvious
move is the dangerous one. A hand-made replacement journal loses the consumed-attempt
evidence. The same task is staged again in the new file, reserved again, and dispatched
a second time for work whose first attempt may already have reached the remote.

Retirement records the link in both directions, closes the old journal to new admissions
and leaves every retained record exactly where it is. The successor then refuses any task
or operation identifier its chain already retains, which is the property a replacement
journal cannot have. Nothing is deleted and no record changes state, so the retired
journal still reserves, reconciles, inspects, audits and backs up. Only new admission
stops, because an attempt slot can only be consumed where its evidence lives.
"""
import json

from .contracts import Rejected, canonical, digest, now, redact
from .maintenance import real_path

TABLE = "journal_succession"
VERSION = 2
CHAIN_LIMIT = 16
SCHEMA_SQL = (
    "CREATE TABLE journal_succession(role TEXT PRIMARY KEY CHECK(role IN ('retired','follows')), record TEXT NOT NULL, sha256 TEXT NOT NULL)",
    "CREATE TRIGGER immutable_succession BEFORE UPDATE ON journal_succession BEGIN SELECT RAISE(ABORT,'immutable succession'); END",
    "CREATE TRIGGER retained_succession BEFORE DELETE ON journal_succession BEGIN SELECT RAISE(ABORT,'retained succession'); END",
)
OBJECTS = {("table", TABLE, TABLE, SCHEMA_SQL[0]),
           ("index", "sqlite_autoindex_" + TABLE + "_1", TABLE, None)}
OBJECTS.update(("trigger", name, TABLE, sql) for name, sql in
               zip(("immutable_succession", "retained_succession"), SCHEMA_SQL[1:]))


def family(label):
    """Resolve a journal family late: both journals call into this module while staging."""
    from . import github_backup, github_journal, jira_backup, jira_journal
    families = {"GitHub": (github_journal.GitHubJournal, github_backup),
                "Jira": (jira_journal.JiraJournal, jira_backup)}
    if label not in families:
        raise Rejected("Unknown journal family")
    return families[label]


def audited_reason(reason):
    """One audited line, bounded, printable and never secret-shaped, like a cancellation reason."""
    if not isinstance(reason, str):
        raise Rejected("An audited retirement reason is required")
    reason = reason.strip()
    if not 1 <= len(reason) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in reason) or redact(reason) != reason:
        raise Rejected("Invalid retirement reason")
    return reason


def installed(db):
    # Every schema version from this one onward carries the succession table.
    return db.execute("PRAGMA user_version").fetchone()[0] >= VERSION


def install(db):
    """Add the succession table inside the caller's writer transaction. Never on open.

    A journal is migrated only by retiring it or by becoming a successor, so opening an
    existing journal never rewrites its schema and a backup copy stays byte-comparable.
    """
    if installed(db):
        return
    for statement in SCHEMA_SQL:
        db.execute(statement)
    db.execute("PRAGMA user_version=%d" % VERSION)


def read_record(db, role, journal=None):
    """A succession record from any connection, or None on a journal that has none.

    `journal` binds the record to the file it was written in, so a record copied from
    another journal, or rewritten to name a different successor, is refused. A backup
    copy legitimately names its source, so bundle readers pass None and compare the
    recorded path themselves.
    """
    if not installed(db):
        return None
    row = db.execute("SELECT record,sha256 FROM " + TABLE + " WHERE role=?", (role,)).fetchone()
    if not row:
        return None
    try:
        saved = json.loads(row[0])
        # Rejected is a ValueError, so decide first and raise outside this handler.
        intact = canonical(saved).decode() == row[0] and digest(saved) == row[1]
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid journal succession record") from exc
    if not intact or saved.get("role") != role:
        raise Rejected("Journal succession record integrity mismatch")
    if journal is not None and saved.get("journal") != str(journal):
        raise Rejected("Journal succession record names another journal")
    return saved


def summary(db, journal=None):
    """Succession state from any connection, small enough to bind into a comparison."""
    retired = read_record(db, "retired", journal)
    follows = read_record(db, "follows", journal)
    return {"status": "retired" if retired else "active",
            "journal": (retired or follows or {}).get("journal"),
            "successor": retired["successor"] if retired else None,
            "predecessor": follows["predecessor"] if follows else None,
            "snapshot_sha256": retired["snapshot_sha256"] if retired else None,
            "retired_at": retired["retired_at"] if retired else None}


def state(journal):
    """Read-only succession state for an open journal, bound to its own path."""
    retired = read_record(journal.db, "retired", journal.path)
    follows = read_record(journal.db, "follows", journal.path)
    return {"status": "retired" if retired else "active", "retired": retired, "follows": follows,
            "successor": retired["successor"] if retired else None,
            "predecessor": follows["predecessor"] if follows else None}


def require_open(journal):
    """New admission is the only thing a retired journal refuses."""
    current = state(journal)
    if current["status"] == "retired":
        raise Rejected("This journal is retired; stage new work in its successor " + current["successor"])


def predecessors(journal, limit=CHAIN_LIMIT):
    """Every journal this one follows, newest first, verifying each link in both directions."""
    cls, _ = family(journal.FAMILY)
    child, link = journal.path, read_record(journal.db, "follows", journal.path)
    seen, older = {str(journal.path)}, []
    while link is not None:
        path = real_path(link["predecessor"])
        if str(path) in seen:
            raise Rejected("Journal succession forms a cycle")
        if len(older) >= limit:
            raise Rejected("Journal succession exceeds its inspection bound")
        if not path.is_file():
            raise Rejected("A predecessor journal named in the chain is missing")
        previous = cls(path, read_only=True)
        try:
            back = summary(previous.db, previous.path)
            if back["successor"] != str(child):
                raise Rejected("A predecessor does not name this journal as its successor")
            link = read_record(previous.db, "follows", previous.path)
        finally:
            previous.close()
        seen.add(str(path))
        older.append(path)
        child = path
    return older


def check_admission(journal, task_id, operation_id, limit=CHAIN_LIMIT):
    """Refuse new admission in a retired journal, or of an identifier its chain already holds.

    This is what a hand-made replacement journal cannot do. One attempt per operation is
    enforced by the record that holds it, so a task re-staged in a fresh journal would get
    a second slot for work whose first attempt may already have left the host. Preparation
    therefore fails closed when a predecessor cannot be read: a chain that cannot be
    checked is not evidence that the identifier is new.
    """
    require_open(journal)
    cls, _ = family(journal.FAMILY)
    for path in predecessors(journal, limit):
        previous = cls(path, read_only=True)
        try:
            from . import journal_revision
            row = previous.db.execute(
                "SELECT operation_id,task_id,state FROM intents WHERE " + journal_revision.LIVE_CLAUSE + " LIMIT 1",
                (operation_id, task_id)).fetchone()
        finally:
            previous.close()
        if row is not None:
            raise Rejected("A predecessor journal already retains operation " + row["operation_id"]
                           + " for task " + row["task_id"] + " (" + row["state"]
                           + "); its one attempt stays in " + str(path))


def retire(path, snapshot, expected_sha256, successor, reason, label, principal="local-operator"):
    """Close a journal to new admissions and open its successor. Nothing is deleted.

    A verified snapshot that matches the journal exactly is required first, so the
    retired evidence is both retained in place and captured in a bundle the record names.
    """
    reason = audited_reason(reason)
    cls, backup = family(label)
    path, successor = real_path(path), real_path(successor)
    if path == successor:
        raise Rejected("A successor journal must be a separate file")
    if str(successor).startswith(str(path)) or str(path).startswith(str(successor)):
        raise Rejected("A successor journal must not share a name with another journal's storage")
    if not path.is_file():
        raise Rejected("An existing journal is required")
    if not successor.parent.is_dir():
        raise Rejected("A successor journal needs an existing parent directory")
    journal = cls(path)
    try:
        # Report the journal's own state first: one retired earlier would also fail the
        # snapshot comparison below, with a message about the wrong thing.
        current = state(journal)
        if current["status"] == "retired":
            raise Rejected("This journal is already retired; its successor is " + current["successor"])
        if successor.exists() or successor.is_symlink():
            raise Rejected("A successor journal must not already exist")
        comparison = backup.compare_journal_backup(path, snapshot, expected_sha256)
        if comparison["status"] != "matches":
            raise Rejected("Retire only against a snapshot that matches the journal exactly")
        counts = journal.db.execute(
            "SELECT count(*) AS records,sum(state='uncertain') AS uncertain FROM intents").fetchone()
        records, uncertain = counts["records"], counts["uncertain"] or 0
        retired = {"schema_version": "1.0.0", "role": "retired", "journal": str(path),
                   "successor": str(successor), "snapshot_sha256": expected_sha256,
                   "journal_audit_sha256": comparison["binding"]["journal_audit_sha256"],
                   "retained_records": records, "uncertain_records": uncertain,
                   "reason": reason, "principal": principal, "retired_at": now()}
        follows = {"schema_version": "1.0.0", "role": "follows", "journal": str(successor),
                   "predecessor": str(path), "snapshot_sha256": expected_sha256,
                   "predecessor_audit_sha256": retired["journal_audit_sha256"],
                   "predecessor_records": records, "reason": reason, "principal": principal,
                   "started_at": retired["retired_at"]}
        # The successor exists and names its predecessor before the old journal closes,
        # so an interruption can leave an unreferenced new journal, which admits nothing
        # until its predecessor names it, but never a retired journal with nowhere to go.
        heir = cls(successor)
        try:
            heir.db.execute("BEGIN IMMEDIATE")
            try:
                if heir.db.execute("SELECT 1 FROM intents LIMIT 1").fetchone():
                    raise Rejected("A successor journal must be empty")
                install(heir.db)
                # The existence check above refuses an occupied path; this closes the race
                # where another process claims the same successor in between.
                if read_record(heir.db, "follows", heir.path):
                    raise Rejected("That journal already follows another")
                heir.db.execute("INSERT INTO " + TABLE + " VALUES('follows',?,?)",
                                (canonical(follows).decode(), digest(follows)))
                heir.db.execute("COMMIT")
            except BaseException:
                if heir.db.in_transaction:
                    heir.db.execute("ROLLBACK")
                raise
        finally:
            heir.close()
        journal.db.execute("BEGIN IMMEDIATE")
        try:
            install(journal.db)
            if read_record(journal.db, "retired", journal.path):
                raise Rejected("This journal is already retired")
            journal.db.execute("INSERT INTO " + TABLE + " VALUES('retired',?,?)",
                               (canonical(retired).decode(), digest(retired)))
            journal.db.execute("COMMIT")
        except BaseException:
            if journal.db.in_transaction:
                journal.db.execute("ROLLBACK")
            raise
    finally:
        journal.close()
    return {"kind": label + "JournalRetirement", "status": "retired", "journal": str(path),
            "successor": str(successor), "snapshot_sha256": expected_sha256,
            "retained_records": records, "uncertain_records": uncertain, "reason": reason,
            "retired_at": retired["retired_at"], "records_deleted": 0, "restore_allowed": False,
            "retry_allowed": False, "live_authorized": False}


def chain(path, label, limit=CHAIN_LIMIT):
    """Walk from a journal back through its predecessors, read-only, verifying every link."""
    cls, _ = family(label)
    path = real_path(path)
    if not path.is_file():
        raise Rejected("An existing journal is required")
    journal = cls(path, read_only=True)
    try:
        entries = [_entry(journal)]
        older = predecessors(journal, limit)
    finally:
        journal.close()
    for previous in older:
        current = cls(previous, read_only=True)
        try:
            entries.append(_entry(current))
        finally:
            current.close()
    return {"kind": label + "JournalChain", "generated_at": now(), "journals": entries,
            "journal_count": len(entries),
            "retained_records": sum(entry["records"] for entry in entries),
            "prepared_records": sum(entry["prepared"] for entry in entries),
            "uncertain_records": sum(entry["uncertain"] for entry in entries),
            "records_deleted": 0, "restore_allowed": False, "retry_allowed": False,
            "live_authorized": False}


def _entry(journal):
    current = state(journal)
    counts = journal.db.execute(
        "SELECT count(*) AS records,sum(state='prepared') AS prepared,sum(state='uncertain') AS uncertain FROM intents").fetchone()
    return {"journal": str(journal.path), "status": current["status"],
            "records": counts["records"], "prepared": counts["prepared"] or 0,
            "uncertain": counts["uncertain"] or 0, "successor": current["successor"],
            "predecessor": current["predecessor"],
            "snapshot_sha256": (current["retired"] or {}).get("snapshot_sha256"),
            "retired_at": (current["retired"] or {}).get("retired_at"),
            "reason": (current["retired"] or {}).get("reason")}
