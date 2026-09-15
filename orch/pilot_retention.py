"""Read-only pilot usage and reviewed workspace reclamation. Journal rows are never deleted."""
import hashlib
import os
from pathlib import Path
import sqlite3

from .contracts import Rejected, canonical, digest, now
from .maintenance import plain

LIVE_CAP = 128        # Pilots whose workspace has not been reclaimed.
JOURNAL_CAP = 1024    # Retained journal rows, including reclaimed pilots.
FILE_LIMIT = 1024 * 1024
TERMINAL_PILOT = ('passed', 'failed', 'abandoned', 'interrupted')
TERMINAL_TASK = ('COMPLETED', 'FAILED', 'CANCELLED')


def reclamation(pilot, identifier):
    """Retained reclamation record, or None. Legacy journals have no table."""
    try:
        row = pilot.db.execute('SELECT status,record,hash FROM pilot_reclamations WHERE id=?', (identifier,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row: return None
    record = canonical_load(row[1])
    if digest(record) != row[2] or record['pilot_id'] != identifier or record['status'] != row[0] or row[0] not in ('reclaiming', 'reclaimed'):
        raise Rejected('Reclamation record integrity mismatch')
    return record


def canonical_load(text):
    import json
    return json.loads(text)


def live_count(db):
    try:
        return db.execute("SELECT count(*) FROM pilots WHERE id NOT IN (SELECT id FROM pilot_reclamations WHERE status='reclaimed')").fetchone()[0]
    except sqlite3.OperationalError:
        return db.execute('SELECT count(*) FROM pilots').fetchone()[0]


def inventory(root, identifier):
    """Regular files under the workspace only. Links, special files and oversized files refuse."""
    workspace = plain(root / identifier)
    if not workspace.exists(): return [], []
    if not workspace.is_dir(): raise Rejected('Workspace is not a directory')
    files, directories, entries = [], [], 0
    for current, names, leaves in os.walk(workspace, followlinks=False):
        entries += len(names) + len(leaves)
        if entries > 256: raise Rejected('Workspace inventory exceeds cap')
        for name in names:
            path = plain(Path(current) / name)
            if not path.is_dir(): raise Rejected('Workspace contains a non-directory entry')
            directories.append(path.relative_to(workspace).as_posix())
        for name in leaves:
            path = plain(Path(current) / name)
            if not path.is_file() or path.stat().st_nlink != 1: raise Rejected('Workspace contains a special or linked file')
            size = path.stat().st_size
            if size > FILE_LIMIT: raise Rejected('Workspace file exceeds reclamation limit')
            files.append({'path': path.relative_to(workspace).as_posix(), 'bytes': size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    files.sort(key=lambda f: f['path'])
    directories.sort()
    return files, directories


def task_binding_state(scope):
    """Read the bound kernel task without opening a writable store."""
    from .storage import Store
    binding = scope['task_binding']
    store = Store(binding['store'], read_only=True)
    try:
        state, _ = store.task(binding['task_id'])
        plan = store.get(state['plan'], binding['task_id'], 'RepositoryTaskPlan') if state.get('plan') else None
        if not plan or plan['pilot_id'] != scope['id'] or plan['scope_sha256'] != digest(scope):
            raise Rejected('Bound task does not reference this pilot')
        return {'task_id': binding['task_id'], 'state': state['state'], 'revision': state['revision']}
    finally:
        store.close()


def eligibility(pilot, identifier, report=None):
    """Reasons a workspace may not be reclaimed. Empty list means eligible."""
    reasons = []
    try:
        report = report or pilot.inspect(identifier)
    except Rejected as exc:
        return [str(exc)], None
    record = reclamation(pilot, identifier)
    if record and record['status'] == 'reclaimed':
        reasons.append('Workspace already reclaimed')
    task = None
    if 'task_binding' in report['scope']:
        try:
            task = task_binding_state(report['scope'])
            if task['state'] not in TERMINAL_TASK: reasons.append('Bound kernel task is not terminal')
        except (Rejected, OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            reasons.append('Bound kernel task state unavailable: ' + (str(exc) if isinstance(exc, Rejected) else type(exc).__name__))
    status = report['status']
    if status == 'prepared' and not abandonable(report['scope'], task):
        reasons.append('Prepared pilot may still run; abandon it first')
    elif status == 'reserved': reasons.append('Reserved pilot requires reconciliation first')
    elif status not in TERMINAL_PILOT + ('prepared',): reasons.append('Pilot is not terminal')
    workers = report.get('workers')
    if workers and any(entry['status'] == 'dispatched' for entry in workers['phases'].values()):
        reasons.append('A worker dispatch is still unresolved')
    if report['unexpected_files'] and not record:
        reasons.append('Workspace contains unexpected entries; retain for investigation')
    return reasons, task


def abandonable(scope, task):
    """A prepared pilot bound to a terminal kernel task can never gain admission."""
    return 'task_binding' in scope and task is not None and task['state'] in TERMINAL_TASK


def usage(pilot):
    """Read-only journal and workspace usage with per-pilot reclamation eligibility."""
    rows = pilot.db.execute('SELECT id,status FROM pilots ORDER BY id').fetchall()
    pilots, total_bytes, counts, reclaimable = [], 0, {}, 0
    for identifier, status in rows:
        counts[status] = counts.get(status, 0) + 1
        entry = {'id': identifier, 'status': status, 'workspace_bytes': 0, 'workspace_files': 0, 'reclamation': None, 'eligible': False, 'reasons': []}
        try:
            record = reclamation(pilot, identifier)
            entry['reclamation'] = record['status'] if record else None
            files, _ = inventory(pilot.root, identifier)
            entry['workspace_bytes'] = sum(f['bytes'] for f in files)
            entry['workspace_files'] = len(files)
            entry['reasons'], _ = eligibility(pilot, identifier)
        except Rejected as exc:
            entry['reasons'] = [str(exc)]
        entry['eligible'] = not entry['reasons']
        reclaimable += entry['eligible']
        total_bytes += entry['workspace_bytes']
        pilots.append(entry)
    live = live_count(pilot.db)
    from .pilot_succession import state
    succession = state(pilot)
    return {'journal_root': str(pilot.root), 'generated_at': now(), 'journal_rows': len(rows), 'journal_cap': JOURNAL_CAP,
            'journal_status': succession['status'], 'successor': succession['successor'],
            'predecessor': succession['predecessor'],
            'live_pilots': live, 'live_cap': LIVE_CAP, 'live_headroom': max(LIVE_CAP - live, 0),
            'status_counts': dict(sorted(counts.items())), 'workspace_bytes': total_bytes, 'reclaimable': reclaimable, 'pilots': pilots}


def review_reason(reason):
    """One audited line, like a cancellation reason: bounded, printable and never secret-shaped."""
    from .contracts import redact
    if not isinstance(reason, str): raise Rejected('Reclamation reason required')
    reason = reason.strip()
    if not 1 <= len(reason) <= 500 or any(ord(c) < 32 or ord(c) == 127 for c in reason) or redact(reason) != reason:
        raise Rejected('Invalid reclamation reason')
    return reason


def reclaim(pilot, identifier, expected_hash, revision, dry_run=False, reason=None, principal='local-operator'):
    """Delete only the disposable workspace after explicit review. Never retries work or deletes evidence."""
    from .broker import file_lock
    if not dry_run or reason is not None: reason = review_reason(reason)
    lock = plain(pilot.root / '.pilot-worker.lock')
    if lock.exists() and lock.stat().st_nlink != 1: raise Rejected('Worker lock hardlink')
    with file_lock(lock):
        pilot.db.execute('BEGIN IMMEDIATE')
        try:
            scope, row = pilot._load(identifier)
            if type(revision) is not int or row[1] != expected_hash or row[2] != revision or row[3] not in TERMINAL_PILOT + ('prepared',):
                raise Rejected('Stale or inapplicable reclamation review')
            report = pilot.inspect(identifier)
            reasons, task = eligibility(pilot, identifier, report)
            if reasons: raise Rejected('Workspace not reclaimable: ' + '; '.join(reasons))
            abandons = row[3] == 'prepared'
            if abandons and not abandonable(scope, task): raise Rejected('Prepared pilot requires explicit abandonment')
            status, current_revision = ('abandoned', row[2] + 1) if abandons else (row[3], row[2])
            files, directories = inventory(pilot.root, identifier)
            record = reclamation(pilot, identifier)
            if record:
                # Resume an interrupted reclamation: only files already recorded may remain.
                if record['scope_sha256'] != expected_hash: raise Rejected('Reclamation record scope mismatch')
                recorded = {f['path']: f['sha256'] for f in record['inventory']}
                if any(f['path'] not in recorded or recorded[f['path']] != f['sha256'] for f in files):
                    raise Rejected('Workspace changed after reclamation started; retain for investigation')
            else:
                record = {'pilot_id': identifier, 'scope_sha256': expected_hash, 'pilot_status': status, 'pilot_revision': current_revision,
                          'status': 'reclaiming', 'inventory': files, 'directories': directories, 'bytes': sum(f['bytes'] for f in files),
                          'task': task, 'started_at': now(), 'completed_at': None, 'reason': reason, 'principal': principal}
            # A resumed reclamation keeps the reason recorded with its durable intent.
            plan = {'id': identifier, 'scope_sha256': expected_hash, 'dry_run': dry_run, 'recorded': False, 'deleted': False, 'abandons': abandons,
                    'files': [f['path'] for f in files], 'bytes': sum(f['bytes'] for f in files), 'reclamation': record['status'],
                    'reason': record.get('reason')}
            if dry_run:
                pilot.db.execute('ROLLBACK')
                return plan
            if abandons:
                pilot.db.execute("UPDATE pilots SET status='abandoned',revision=revision+1 WHERE id=? AND status='prepared'", (identifier,))
            if record['status'] == 'reclaiming' and reclamation(pilot, identifier) is None:
                pilot.db.execute('INSERT INTO pilot_reclamations VALUES(?,?,?,?)', (identifier, 'reclaiming', canonical(record).decode(), digest(record)))
            pilot.db.execute('COMMIT')
        except BaseException:
            pilot.db.execute('ROLLBACK')
            raise
        plan['recorded'] = True
        # The intent is durable before any deletion. A crash here leaves a resumable record.
        workspace = plain(pilot.root / identifier)
        if workspace.exists():
            for entry in files:
                path = plain(workspace / entry['path'])
                if path.is_symlink() or not path.is_file(): raise Rejected('Workspace changed during reclamation')
                path.chmod(0o600)  # Docker workers freeze files read-only; Windows refuses to unlink them otherwise.
                path.unlink()
            for directory in sorted(directories, key=lambda d: d.count('/'), reverse=True):
                plain(workspace / directory).rmdir()
            workspace.rmdir()
        record = {**record, 'status': 'reclaimed', 'completed_at': now()}
        pilot.db.execute('UPDATE pilot_reclamations SET status=?,record=?,hash=? WHERE id=?',
                         ('reclaimed', canonical(record).decode(), digest(record), identifier))
        plan.update(deleted=True, reclamation='reclaimed')
    return plan
