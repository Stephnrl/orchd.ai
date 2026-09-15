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

LIMIT = 65536
POLICY = 'git-json-data-v1'


def source_hash():
    return digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ('pilot.py', 'process.py', 'contracts.py', 'maintenance.py')})


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


class Pilot:
    """Separate CLI-only journal; does not manufacture fixture TaskState authority."""
    def __init__(self, root, read_only=False):
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
                 'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}
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
        if digest(scope) != row[1] or scope['id'] != identifier or scope['journal_root'] != str(self.root):
            raise Rejected('Pilot scope integrity mismatch')
        if row[3] not in {'prepared', 'reserved', 'passed', 'failed', 'abandoned'} or row[2] != {'prepared': 0, 'reserved': 1, 'passed': 2, 'failed': 2, 'abandoned': 1}[row[3]]:
            raise Rejected('Pilot lifecycle integrity mismatch')
        if (row[4] is not None) != (row[3] in ('passed', 'failed')): raise Rejected('Pilot receipt missing or premature')
        return scope, row

    def inspect(self, identifier):
        scope, row = self._load(identifier)
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
                'receipt': receipt, 'observed_files': observed, 'unexpected_files': extra, 'matches_receipt': matches,
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
        self.db.execute('BEGIN IMMEDIATE')
        try:
            scope = self._fence(identifier, expected_hash, revision)
            if scope['source_sha256'] != source_hash() or datetime.now(timezone.utc) >= datetime.fromisoformat(scope['expires_at']):
                raise Rejected('Pilot source or approval expired')
            if baseline(scope['repository'], scope['base_commit'], scope['replacements']) != scope['before']:
                raise Rejected('Baseline changed')
            self.db.execute("UPDATE pilots SET status='reserved',revision=revision+1 WHERE id=?", (identifier,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        # Reservation is durable before any workspace effects. No automatic retry,
        # even if a crash happens before the directory is created.
        workspace = plain(self.root / identifier)
        workspace.mkdir()
        for name, value in scope['replacements'].items():
            path = plain(workspace / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(value.encode())
                stream.flush()
                os.fsync(stream.fileno())
        files = {name: plain(workspace / name).read_bytes().decode('utf-8') for name in scope['replacements']}
        if files != scope['replacements']: raise Rejected('Workspace bytes changed')
        checks = evaluate(files, scope['checks'])
        receipt = {'policy': POLICY, 'scope_sha256': expected_hash, 'completed_at': now(), 'checks': checks,
                   'files': {p: hashlib.sha256(v.encode()).hexdigest() for p, v in files.items()}}
        status = 'passed' if all(c['passed'] for c in checks) else 'failed'
        self.db.execute('UPDATE pilots SET status=?,revision=revision+1,receipt=? WHERE id=? AND status=?',
                        (status, canonical({'receipt': receipt, 'sha256': digest(receipt)}).decode(), identifier, 'reserved'))
        return self.inspect(identifier)
