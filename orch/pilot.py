"""Local Git data-change pilot. Repository programs are never executed."""
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .contracts import Rejected, canonical, digest, now, uid
from .maintenance import plain
from .process import capture
from .broker import file_lock
from . import pilot_worker

LIMIT = 65536
POLICY = 'git-json-data-v1'


def source_hash():
    return digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ('pilot.py', 'pilot_worker.py', 'process.py', 'contracts.py', 'maintenance.py', 'broker.py', 'readiness.py', 'execution.py')})


def path_name(value):
    if not isinstance(value, str) or len(value) > 180:
        raise Rejected('Invalid pilot path')
    parts = value.split('/')
    if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', p) or p.endswith('.')
           or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(10)), *('LPT'+str(i) for i in range(10))}
           for p in parts) or not value.endswith('.json'):
        raise Rejected('Only portable non-hidden JSON paths are admitted')
    return value


def json_data(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise Rejected('Duplicate JSON key')
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Rejected('Non-finite JSON')))
        canonical(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected('Invalid JSON data') from exc


def git(root, *args):
    env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP') if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    result = capture(['git', '--no-replace-objects', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath='+os.devnull,
                      '-C', str(root), *args], env=env, limit=1024*1024)
    if result['code'] != 0 or result['failure']: raise Rejected('Pilot Git read failed')
    return result['stdout']


def baseline(repository, commit, paths):
    root = plain(Path(repository).absolute())
    if not root.is_dir() or not plain(root / '.git').is_dir():
        raise Rejected('Pilot requires an ordinary local Git checkout')
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise Rejected('Exact SHA-1 baseline required')
    if git(root, 'rev-parse', '--show-toplevel').strip().replace('\\', '/') != root.as_posix():
        raise Rejected('Repository root mismatch')
    if git(root, 'rev-parse', 'HEAD').strip() != commit or git(root, 'ls-files', '--others', '--exclude-standard', '-z'):
        raise Rejected('Pilot requires a clean exact baseline')
    # Never run status/diff: Git clean filters can execute repository-configured
    # programs while refreshing the index. Compare raw bytes and index directly.
    tree = git(root, 'ls-tree', '-r', '-z', commit).rstrip('\0').split('\0')
    index = git(root, 'ls-files', '--stage', '-z').rstrip('\0').split('\0')
    if len(tree) > 128: raise Rejected('Pilot baseline file cap exceeded')
    expected_index, total, names = [], 0, set()
    for item in tree:
        metadata, name = item.split('\t', 1)
        mode, kind, blob = metadata.split()
        if mode not in ('100644', '100755') or kind != 'blob': raise Rejected('Baseline contains links or submodules')
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part in ('.', '..') for part in name.split('/')):
            raise Rejected('Non-portable baseline path')
        if name.casefold() in names: raise Rejected('Case-colliding baseline paths')
        names.add(name.casefold())
        path = plain(root / name)
        if not path.is_file() or path.stat().st_size > 1024*1024 or path.stat().st_nlink != 1:
            raise Rejected('Invalid baseline file')
        raw = path.read_bytes()
        total += len(raw)
        if total > 2*1024*1024: raise Rejected('Pilot baseline byte cap exceeded')
        if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest() != blob:
            raise Rejected('Checkout bytes differ from Git baseline')
        expected_index.append(f'{mode} {blob} 0\t{name}')
    if sorted(index) != sorted(expected_index): raise Rejected('Index differs from baseline')
    result = {}
    for name in paths:
        path_name(name)
        entry = git(root, 'ls-tree', commit, '--', name).strip().split()
        if len(entry) != 4 or entry[0] != '100644' or entry[1] != 'blob' or entry[3] != name:
            raise Rejected('Only existing regular non-executable blobs are admitted')
        path = plain(root / name)
        if not path.is_file() or path.stat().st_size > LIMIT or path.stat().st_nlink != 1:
            raise Rejected('Invalid baseline file')
        raw = path.read_bytes()
        blob = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if blob != entry[2]: raise Rejected('Checkout bytes differ from Git baseline')
        try: text = raw.decode('utf-8')
        except UnicodeError as exc: raise Rejected('UTF-8 JSON required') from exc
        json_data(text)
        result[name] = text
    return result


def validate_intent(intent):
    if not isinstance(intent, dict) or set(intent) != {'repository', 'base_commit', 'replacements', 'checks'}:
        raise Rejected('Invalid pilot intent')
    if not isinstance(intent['repository'], str) or not 1 <= len(intent['repository']) <= 1024:
        raise Rejected('Invalid repository path')
    replacements, checks = intent['replacements'], intent['checks']
    if not isinstance(replacements, dict) or not 1 <= len(replacements) <= 8:
        raise Rejected('Pilot requires one to eight replacements')
    for name, value in replacements.items():
        path_name(name)
        if not isinstance(value, str) or len(value.encode()) > LIMIT: raise Rejected('Replacement too large')
        json_data(value)
    if len({p.casefold() for p in replacements}) != len(replacements): raise Rejected('Case-colliding paths')
    if sum(len(v.encode()) for v in replacements.values()) > LIMIT: raise Rejected('Patch byte budget exceeded')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32: raise Rejected('Bounded checks required')
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'path', 'keys', 'equals'} or not isinstance(check['path'], str) or check['path'] not in replacements:
            raise Rejected('Invalid trusted JSON check')
        if not isinstance(check['keys'], list) or len(check['keys']) > 16 or any(not isinstance(k, str) or len(k) > 100 for k in check['keys']):
            raise Rejected('Invalid JSON key traversal')
        if len(canonical(check['equals'])) > 4096: raise Rejected('Check expectation too large')
    if {c['path'] for c in checks} != set(replacements): raise Rejected('Every changed file requires a check')


def evaluate(files, checks):
    results = []
    for check in checks:
        value = json_data(files[check['path']])
        found = True
        for key in check['keys']:
            if not isinstance(value, dict) or key not in value:
                found = False
                break
            value = value[key]
        results.append({'check': check, 'passed': found and canonical(value) == canonical(check['equals'])})
    return results


def workspace_files(scope, workspace):
    """Treat worker output as untrusted; never read devices, links or unbounded files."""
    expected = set(scope['replacements'])
    directories = {str(parent).replace('\\', '/') for p in expected for parent in Path(p).parents if str(parent) != '.'}
    seen, count = set(), 0
    def unreadable(_): raise Rejected('Unreadable worker workspace')
    for current, subdirs, names in os.walk(plain(workspace), followlinks=False, onerror=unreadable):
        count += len(subdirs) + len(names)
        if count > 256: raise Rejected('Worker workspace inventory exceeded')
        for name in subdirs + names:
            path = plain(Path(current) / name)
            relative = path.relative_to(workspace).as_posix()
            if name in subdirs:
                if relative not in directories: raise Rejected('Unexpected worker directory')
            else:
                if relative not in expected: raise Rejected('Unexpected worker file')
                seen.add(relative)
    if seen != expected: raise Rejected('Incomplete worker snapshot')
    files = {}
    for name in sorted(expected):
        path = plain(workspace / name)
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > LIMIT:
            raise Rejected('Unsafe worker output file')
        with path.open('rb') as stream: raw = stream.read(LIMIT+1)
        if len(raw) > LIMIT: raise Rejected('Worker output exceeds budget')
        try: files[name] = raw.decode('utf-8')
        except UnicodeError as exc: raise Rejected('Invalid worker UTF-8') from exc
    return files


class Pilot:
    """Separate CLI-only journal; does not manufacture fixture TaskState authority."""
    def __init__(self, root, read_only=False, executor='local-data', image=None):
        if executor not in ('local-data', 'docker') or (executor == 'local-data' and image is not None):
            raise Rejected('Invalid pilot executor configuration')
        self.executor, self.image = executor, image
        self.root = plain(Path(root).absolute())
        if not read_only: self.root.mkdir(parents=True, exist_ok=True)
        for name in ('pilot.sqlite', 'pilot.sqlite-journal', 'pilot.sqlite-wal', 'pilot.sqlite-shm'):
            path = plain(self.root / name)
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1): raise Rejected('Invalid pilot journal file')
        target = (self.root / 'pilot.sqlite').as_uri() + '?mode=ro' if read_only else str(self.root / 'pilot.sqlite')
        self.db = sqlite3.connect(target, uri=read_only, isolation_level=None, timeout=1)
        if not read_only:
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS pilots (id TEXT PRIMARY KEY, scope TEXT NOT NULL, hash TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS pilot_workers (id TEXT PRIMARY KEY, journal TEXT NOT NULL, hash TEXT NOT NULL)')

    def close(self):
        self.db.close()

    def prepare(self, intent):
        validate_intent(intent)
        repository = str(plain(Path(intent['repository']).absolute()))
        if self.root.is_relative_to(Path(repository)) or Path(repository).is_relative_to(self.root):
            raise Rejected('Pilot journal and source repository must be separate')
        before = baseline(repository, intent['base_commit'], intent['replacements'])
        if any(before[k] == v for k, v in intent['replacements'].items()): raise Rejected('Every replacement must change bytes')
        scope = {**intent, 'repository': repository, 'before': before, 'policy': POLICY, 'source_sha256': source_hash(),
                 'journal_root': str(self.root), 'id': uid(), 'created_at': now(),
                 'executor': pilot_worker.profile(self.executor, self.image),
                 'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}
        if self.executor == 'docker':
            for phase in ('edit', 'test'): pilot_worker.command(scope, phase, self.root / scope['id'])
        encoded = canonical(scope).decode()
        if len(encoded.encode()) > 512*1024: raise Rejected('Scope too large')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT count(*) FROM pilots').fetchone()[0] >= 128: raise Rejected('Pilot retention cap reached')
            self.db.execute('INSERT INTO pilots VALUES(?,?,?,0,?,NULL)', (scope['id'], encoded, digest(scope), 'prepared'))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.inspect(scope['id'])

    def _load(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier): raise Rejected('Invalid pilot ID')
        row = self.db.execute('SELECT scope,hash,revision,status,receipt FROM pilots WHERE id=?', (identifier,)).fetchone()
        if not row: raise Rejected('Unknown pilot')
        scope = json.loads(row[0])
        try:
            validate_intent({k: scope[k] for k in ('repository', 'base_commit', 'replacements', 'checks')})
            if set(scope['before']) != set(scope['replacements']) or any(not isinstance(v, str) or len(v.encode()) > LIMIT for v in scope['before'].values()):
                raise Rejected('Invalid retained baseline')
        except (KeyError, TypeError, AttributeError) as exc:
            raise Rejected('Invalid retained pilot scope') from exc
        if digest(scope) != row[1] or scope['id'] != identifier or scope['journal_root'] != str(self.root):
            raise Rejected('Pilot scope integrity mismatch')
        revisions = {'prepared': {0}, 'reserved': range(1, 1002), 'passed': {2}, 'failed': {2}, 'abandoned': {1}, 'interrupted': range(2, 1002)}
        if row[3] not in revisions or row[2] not in revisions[row[3]]:
            raise Rejected('Pilot lifecycle integrity mismatch')
        if (row[4] is not None) != (row[3] in ('passed', 'failed')): raise Rejected('Pilot receipt missing or premature')
        return scope, row

    def _workers(self, scope):
        if scope.get('executor', {}).get('mode') != 'docker': return None
        row = self.db.execute('SELECT journal,hash FROM pilot_workers WHERE id=?', (scope['id'],)).fetchone()
        if not row: return None
        journal = json.loads(row[0])
        if digest(journal) != row[1] or journal['scope_sha256'] != digest(scope): raise Rejected('Worker journal integrity mismatch')
        if set(journal['phases']) != {'edit', 'test'}: raise Rejected('Incomplete worker journal')
        for phase, entry in journal['phases'].items():
            if entry['identity'] != pilot_worker.identity(scope, phase) or entry['status'] not in ('planned', 'dispatched', 'observed'):
                raise Rejected('Invalid worker journal identity')
        return journal

    def _save_workers(self, scope, journal):
        self.db.execute('INSERT OR REPLACE INTO pilot_workers VALUES(?,?,?)',
                        (scope['id'], canonical(journal).decode(), digest(journal)))

    def inspect(self, identifier):
        scope, row = self._load(identifier)
        workers = self._workers(scope)
        if scope.get('executor', {}).get('mode') == 'docker' and ((workers is None) != (row[3] in ('prepared', 'abandoned'))):
            raise Rejected('Worker journal missing or premature')
        workspace = plain(self.root / identifier)
        observed = {}
        if workspace.exists():
            for name in scope['replacements']:
                path = plain(workspace / name)
                observed[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and path.stat().st_size <= LIMIT else None
        envelope = json.loads(row[4]) if row[4] else None
        receipt = envelope['receipt'] if envelope else None
        if receipt and (digest(receipt) != envelope['sha256'] or receipt['scope_sha256'] != row[1]
                        or receipt['checks'] != evaluate(scope['replacements'], scope['checks'])
                        or receipt['files'] != {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope['replacements'].items()}):
            raise Rejected('Pilot receipt integrity mismatch')
        if receipt and (row[3] == 'passed') != all(c['passed'] for c in receipt['checks']): raise Rejected('Pilot outcome mismatch')
        if receipt and workers and receipt.get('worker_journal_sha256') != digest(workers): raise Rejected('Worker receipt binding mismatch')
        extra = []
        entries = 0
        if workspace.exists():
            for current, directories, files in os.walk(workspace, followlinks=False):
                entries += len(directories) + len(files)
                if entries > 256: raise Rejected('Workspace inventory exceeds cap')
                for name in directories + files:
                    plain(Path(current) / name)
                extra.extend((Path(current) / name).relative_to(workspace).as_posix() for name in files
                             if (Path(current) / name).relative_to(workspace).as_posix() not in scope['replacements'])
                if len(extra) > 128: raise Rejected('Unexpected workspace inventory exceeds cap')
        matches = receipt is not None and observed == receipt['files'] and not extra
        return {'id': identifier, 'scope_sha256': row[1], 'revision': row[2], 'status': row[3], 'scope': scope,
                'diff': ''.join(''.join(difflib.unified_diff(scope['before'][p].splitlines(True), v.splitlines(True), fromfile='a/'+p, tofile='b/'+p)) for p, v in scope['replacements'].items()),
                'receipt': receipt, 'workers': workers, 'observed_files': observed, 'unexpected_files': extra, 'matches_receipt': matches,
                'execution_authorized': False, 'recovery': 'retain_and_inspect' if row[3] == 'reserved' else 'none'}

    def _fence(self, identifier, expected_hash, revision):
        scope, row = self._load(identifier)
        if type(revision) is not int or row[1] != expected_hash or row[2] != revision or row[3] != 'prepared':
            raise Rejected('Stale or already consumed pilot approval')
        return scope

    def abandon(self, identifier, expected_hash, revision):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._fence(identifier, expected_hash, revision)
            self.db.execute("UPDATE pilots SET status='abandoned',revision=revision+1 WHERE id=?", (identifier,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.inspect(identifier)

    def run(self, identifier, expected_hash, revision):
        lock = plain(self.root / '.pilot-worker.lock')
        if lock.exists() and lock.stat().st_nlink != 1: raise Rejected('Worker lock hardlink')
        with file_lock(lock):
            return self._run(identifier, expected_hash, revision)

    def _run(self, identifier, expected_hash, revision):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            scope = self._fence(identifier, expected_hash, revision)
            if scope['source_sha256'] != source_hash() or datetime.now(timezone.utc) >= datetime.fromisoformat(scope['expires_at']):
                raise Rejected('Pilot source or approval expired')
            if scope.get('executor') != pilot_worker.profile(self.executor, self.image):
                raise Rejected('Pilot executor, image or daemon changed')
            if baseline(scope['repository'], scope['base_commit'], scope['replacements']) != scope['before']:
                raise Rejected('Baseline changed')
            self.db.execute("UPDATE pilots SET status='reserved',revision=revision+1 WHERE id=?", (identifier,))
            if self.executor == 'docker':
                self._save_workers(scope, {'scope_sha256': expected_hash,
                    'phases': {p: {'identity': pilot_worker.identity(scope, p), 'status': 'planned', 'observation': None} for p in ('edit', 'test')},
                    'cleanup': []})
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        # Reservation is durable before any workspace effects. No automatic retry,
        # even if a crash happens before the directory is created.
        workspace = plain(self.root / identifier)
        workspace.mkdir()
        workspace.chmod(0o755)
        for name, value in scope['replacements'].items():
            path = plain(workspace / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            for parent in path.parents:
                if parent == self.root: break
                parent.chmod(0o755)
            with path.open('xb') as stream:
                if self.executor == 'local-data': stream.write(value.encode())
                stream.flush()
                os.fsync(stream.fileno())
            if self.executor == 'docker': path.chmod(0o666)
        hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope['replacements'].items()}
        if self.executor == 'docker': self._dispatch(scope, 'edit', workspace, {'files': hashes})
        files = workspace_files(scope, workspace)
        if files != scope['replacements']: raise Rejected('Workspace bytes changed')
        checks = evaluate(files, scope['checks'])
        if self.executor == 'docker':
            for name in files: plain(workspace / name).chmod(0o444)
            self._dispatch(scope, 'test', workspace, {'files': hashes, 'passed': [c['passed'] for c in checks]})
            # Re-read actual bytes after the test process and before evidence commit.
            if workspace_files(scope, workspace) != files: raise Rejected('Test snapshot changed')
        receipt = {'policy': POLICY, 'scope_sha256': expected_hash, 'completed_at': now(), 'checks': checks,
                   'files': {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()}}
        if self.executor == 'docker': receipt['worker_journal_sha256'] = digest(self._workers(scope))
        status = 'passed' if all(c['passed'] for c in checks) else 'failed'
        self.db.execute('UPDATE pilots SET status=?,revision=revision+1,receipt=? WHERE id=? AND status=?',
                        (status, canonical({'receipt': receipt, 'sha256': digest(receipt)}).decode(), identifier, 'reserved'))
        return self.inspect(identifier)

    def _dispatch(self, scope, phase, workspace, expected):
        if pilot_worker.profile(self.executor, self.image) != scope['executor']:
            raise Rejected('Worker runtime changed before dispatch')
        journal = self._workers(scope)
        entry = journal['phases'][phase]
        if entry['status'] != 'planned': raise Rejected('Worker dispatch already consumed')
        entry['status'] = 'dispatched'
        self._save_workers(scope, journal)  # Autocommit, before the Docker call.
        observation, output = pilot_worker.execute(scope, phase, workspace)
        entry.update(status='observed', observation=observation)
        self._save_workers(scope, journal)
        if (observation['code'] != 0 or observation['failure'] or observation['truncated']
                or not observation['container_absent'] or canonical(output) != canonical(expected)):
            raise Rejected('Worker failed or returned uncertain evidence; inspect and reconcile')

    def reconcile(self, identifier, expected_hash, revision):
        """Explicit cleanup consumes no new execution authority and never retries work."""
        lock = plain(self.root / '.pilot-worker.lock')
        if lock.exists() and lock.stat().st_nlink != 1: raise Rejected('Worker lock hardlink')
        with file_lock(lock):
            self.db.execute('BEGIN IMMEDIATE')
            try:
                scope, row = self._load(identifier)
                if type(revision) is not int or row[1] != expected_hash or row[2] != revision or row[3] != 'reserved' or revision >= 1000:
                    raise Rejected('Stale or inapplicable worker reconciliation')
                retained = {k: v for k, v in scope.get('executor', {}).items() if k != 'recipe_sha256'}
                current = {k: v for k, v in pilot_worker.profile(self.executor, self.image).items() if k != 'recipe_sha256'}
                if self.executor != 'docker' or retained != current:
                    raise Rejected('Worker reconciliation requires the same Docker host and image')
                journal = self._workers(scope)
                if not journal: raise Rejected('Missing worker reservation')
                absent = {phase: pilot_worker.reconcile(scope, phase) for phase in ('edit', 'test')}
                journal['cleanup'].append({'at': now(), 'absent': absent})
                self._save_workers(scope, journal)
                self.db.execute('UPDATE pilots SET status=?,revision=revision+1 WHERE id=?',
                                ('interrupted' if all(absent.values()) else 'reserved', identifier))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return self.inspect(identifier)
