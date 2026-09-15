"""Retire a full pilot journal and start its successor, without deleting any row.

A journal refuses a new pilot once it holds `JOURNAL_CAP` rows, and rows are never
deleted, so the documented remedy has been "use a new journal" with nothing to carry
the relationship. Starting one by hand loses the link: the old evidence stays on disk
with nothing recording where work continued, and nothing stops an operator preparing
into a journal that is meant to be finished.

Retirement records that link in both directions and closes the old journal to new
preparations. It deletes nothing, reclaims nothing and changes no pilot's status, so
every retired row stays exactly where it was and keeps verifying. Reconciliation and
reclamation of an already-retired journal stay available; only preparation stops.
"""
import json
from pathlib import Path
import sqlite3

from .contracts import Rejected, canonical, digest, now
from .maintenance import real_path
from .pilot import Pilot
from . import pilot_backup, pilot_retention

TABLE = "pilot_journal_succession"
SETTLED = ("passed", "failed", "abandoned", "interrupted")


def ensure_table(db):
    db.execute("CREATE TABLE IF NOT EXISTS " + TABLE + " (role TEXT PRIMARY KEY, record TEXT NOT NULL, hash TEXT NOT NULL)")


def read_record(db, role, journal_root=None):
    """A succession record read from any connection, or None on a legacy journal.

    `journal_root` binds the record to the journal it was written in, so a record
    copied from another journal, or rewritten to describe one, is refused.
    """
    try:
        row = db.execute("SELECT record,hash FROM " + TABLE + " WHERE role=?", (role,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    saved = json.loads(row[0])
    if digest(saved) != row[1] or saved.get("role") != role:
        raise Rejected("Journal succession record integrity mismatch")
    if journal_root is not None and saved.get("journal_root") != str(journal_root):
        raise Rejected("Journal succession record names another journal")
    return saved


def record(pilot, role):
    """The retained succession record for this journal, or None on a legacy one."""
    return read_record(pilot.db, role, pilot.root)


def summary(db, journal_root=None):
    """Succession state from any connection, small enough to bind into an audit."""
    retired = read_record(db, "retired", journal_root)
    follows = read_record(db, "follows", journal_root)
    return {"status": "retired" if retired else "active",
            "successor": retired["successor"] if retired else None,
            "predecessor": follows["predecessor"] if follows else None,
            "snapshot_sha256": retired["snapshot_sha256"] if retired else None,
            "retired_at": retired["retired_at"] if retired else None}


def state(pilot):
    """Read-only succession state: retired or active, and either neighbour."""
    retired, follows = record(pilot, "retired"), record(pilot, "follows")
    return {"status": "retired" if retired else "active",
            "retired": retired, "follows": follows,
            "successor": retired["successor"] if retired else None,
            "predecessor": follows["predecessor"] if follows else None}


def require_open(pilot):
    """Preparation is the only thing a retired journal refuses."""
    if state(pilot)["status"] == "retired":
        raise Rejected("This pilot journal is retired; prepare in its successor")


def _settled(pilot):
    unsettled = [row[0] for row in pilot.db.execute(
        "SELECT id FROM pilots WHERE status NOT IN (?,?,?,?) ORDER BY id", SETTLED).fetchall()]
    if unsettled:
        raise Rejected("Retire a journal only once every pilot is settled: " + ", ".join(unsettled[:8]))


def retire(root, snapshot, expected_sha256, successor, reason, principal="local-operator"):
    """Close a journal to new preparations and open its successor. Nothing is deleted.

    A verified snapshot that matches the journal exactly is required first, so the
    retired evidence is both retained in place and captured in a bundle the record names.
    """
    reason = pilot_retention.review_reason(reason)
    root, successor = real_path(root), real_path(successor)
    if root == successor:
        raise Rejected("A successor journal must be a separate directory")
    if root in successor.parents or successor in root.parents:
        raise Rejected("A successor journal must not contain, or sit inside, its predecessor")
    pilot = Pilot(root)
    try:
        ensure_table(pilot.db)
        # Report the journal's own reasons first: a journal retired earlier would also
        # fail the snapshot comparison below, with a message about the wrong thing.
        if state(pilot)["status"] == "retired":
            raise Rejected("This pilot journal is already retired")
        _settled(pilot)
        if (successor / "pilot.sqlite").exists():
            raise Rejected("A successor journal must not already exist")
        comparison = pilot_backup.compare_journal_backup(root, snapshot, expected_sha256)
        if comparison["status"] != "matches":
            raise Rejected("Retire only against a snapshot that matches the journal exactly")
        rows = pilot.db.execute("SELECT count(*) FROM pilots").fetchone()[0]
        retired = {"schema_version": "1.0.0", "role": "retired", "journal_root": str(root),
                   "successor": str(successor), "snapshot_sha256": expected_sha256,
                   "journal_audit_sha256": comparison["binding"]["journal_audit_sha256"],
                   "retained_rows": rows, "reason": reason, "principal": principal, "retired_at": now()}
        follows = {"schema_version": "1.0.0", "role": "follows", "journal_root": str(successor),
                   "predecessor": str(root), "snapshot_sha256": expected_sha256,
                   "predecessor_audit_sha256": retired["journal_audit_sha256"],
                   "predecessor_rows": rows, "reason": reason, "principal": principal, "started_at": retired["retired_at"]}
        # The successor exists and names its predecessor before the old journal closes,
        # so an interruption can leave an unreferenced new journal but never a retired
        # journal with nowhere to continue.
        heir = Pilot(successor)
        try:
            if heir.db.execute("SELECT count(*) FROM pilots").fetchone()[0]:
                raise Rejected("A successor journal must be empty")
            ensure_table(heir.db)
            # The check above refuses an existing successor; these two close the race
            # where another process creates or claims that directory in between.
            if state(heir)["follows"]:
                raise Rejected("That journal already follows another")
            heir.db.execute("INSERT INTO " + TABLE + " VALUES('follows',?,?)",
                            (canonical(follows).decode(), digest(follows)))
        finally:
            heir.close()
        pilot.db.execute("INSERT INTO " + TABLE + " VALUES('retired',?,?)",
                         (canonical(retired).decode(), digest(retired)))
    finally:
        pilot.close()
    return {"kind": "PilotJournalRetirement", "status": "retired", "journal_root": str(root),
            "successor": str(successor), "snapshot_sha256": expected_sha256,
            "retained_rows": rows, "reason": reason, "retired_at": retired["retired_at"],
            "rows_deleted": 0, "restore_allowed": False, "live_authorized": False}


def forward(root, limit=64):
    """Every journal from `root` to the active one, oldest first, verifying each link.

    A journal that does not exist yet is its own active journal, so a store can name
    its pilot journal before the first pilot is prepared in it.
    """
    roots, cursor = [], real_path(root)
    while True:
        if str(cursor) in roots:
            raise Rejected("Pilot journal succession forms a cycle")
        if len(roots) >= limit:
            raise Rejected("Pilot journal succession exceeds its inspection bound")
        roots.append(str(cursor))
        if not (cursor / "pilot.sqlite").is_file():
            if len(roots) > 1:
                raise Rejected("A successor journal named in the chain is missing")
            return roots
        pilot = Pilot(cursor, read_only=True)
        try:
            current = state(pilot)
        finally:
            pilot.close()
        if current["successor"] is None:
            return roots
        successor = real_path(current["successor"])
        if not (successor / "pilot.sqlite").is_file():
            raise Rejected("A successor journal named in the chain is missing")
        heir = Pilot(successor, read_only=True)
        try:
            back = state(heir)
        finally:
            heir.close()
        if back["predecessor"] != str(cursor):
            raise Rejected("A successor does not name this journal as its predecessor")
        cursor = successor


def active_journal(root):
    """The journal at the end of the chain: the one a new pilot belongs in."""
    return Path(forward(root)[-1])


def chain(root, limit=64):
    """Walk from a journal to its oldest predecessor, read-only, verifying each link."""
    entries, seen, cursor = [], set(), real_path(root)
    while cursor is not None:
        if str(cursor) in seen:
            raise Rejected("Pilot journal succession forms a cycle")
        seen.add(str(cursor))
        if len(entries) >= limit:
            raise Rejected("Pilot journal succession exceeds its inspection bound")
        if not (cursor / "pilot.sqlite").is_file():
            raise Rejected("A predecessor journal named in the chain is missing")
        pilot = Pilot(cursor, read_only=True)
        try:
            current = state(pilot)
            rows = pilot.db.execute("SELECT count(*) FROM pilots").fetchone()[0]
            live = pilot_retention.live_count(pilot.db)
        finally:
            pilot.close()
        entries.append({"journal_root": str(cursor), "status": current["status"],
                        "journal_rows": rows, "live_pilots": live,
                        "successor": current["successor"], "predecessor": current["predecessor"],
                        "snapshot_sha256": (current["retired"] or {}).get("snapshot_sha256"),
                        "retired_at": (current["retired"] or {}).get("retired_at")})
        previous = current["predecessor"]
        if previous is None:
            break
        previous = real_path(previous)
        heir = Pilot(previous, read_only=True) if (previous / "pilot.sqlite").is_file() else None
        if heir is None:
            raise Rejected("A predecessor journal named in the chain is missing")
        try:
            back = state(heir)
        finally:
            heir.close()
        if back["successor"] != str(cursor):
            raise Rejected("A predecessor does not name this journal as its successor")
        cursor = previous
    return {"kind": "PilotJournalChain", "generated_at": now(), "journals": entries,
            "journal_count": len(entries), "retained_rows": sum(e["journal_rows"] for e in entries),
            "live_pilots": sum(e["live_pilots"] for e in entries),
            "journal_cap": pilot_retention.JOURNAL_CAP, "live_cap": pilot_retention.LIVE_CAP,
            "rows_deleted": 0, "restore_allowed": False, "live_authorized": False}
