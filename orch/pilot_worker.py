"""Fixed JSON-only Docker worker and ownership-checked container cleanup."""
import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import Rejected, canonical, digest, now
from .maintenance import plain, real_path
from .process import capture
from .readiness import doctor


# Trusted source supplied as argv, never loaded from the source repository.
WORKER = r'''
import hashlib, json, os, re, sys
from pathlib import Path

def path(name):
    assert isinstance(name, str) and len(name) <= 180
    assert all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', p) and not p.endswith('.') for p in name.split('/'))
    target = Path('/workspace') / name
    assert not any(p.is_symlink() for p in (target, *target.parents))
    return target

def equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool): return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)): return a == b
    if type(a) is not type(b): return False
    if isinstance(a, dict): return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list): return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    return a == b

raw = sys.stdin.buffer.read(524289)
assert len(raw) <= 524288
request = json.loads(raw)
files = {}
if sys.argv[1] == 'edit':
    assert set(request) == {'replacements'} and 1 <= len(request['replacements']) <= 8
    assert sum(len(v.encode('utf-8')) for v in request['replacements'].values()) <= 65536
    for name, value in request['replacements'].items():
        target = path(name)
        # The host pre-creates only empty bounded target files for the unprivileged UID.
        assert target.is_file() and target.stat().st_size == 0
        with target.open('wb') as stream:
            stream.write(value.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    result = {'files': files}
else:
    assert sys.argv[1] == 'test' and set(request) == {'hashes', 'checks'}
    assert 1 <= len(request['hashes']) <= 8 and 1 <= len(request['checks']) <= 32
    values = {}
    for name, expected in request['hashes'].items():
        target = path(name)
        assert target.is_file() and target.stat().st_size <= 65536
        data = target.read_bytes()
        files[name] = hashlib.sha256(data).hexdigest()
        assert files[name] == expected
        values[name] = json.loads(data)
    passed = []
    for check in request['checks']:
        value, found = values[check['path']], True
        for key in check['keys']:
            if not isinstance(value, dict) or key not in value:
                found = False
                break
            value = value[key]
        passed.append(found and equal(value, check['equals']))
    result = {'files': files, 'passed': passed}
print(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
'''


def environment():
    return {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP') if k in os.environ}


def profile(mode, image):
    if mode == 'local-data' and image is None:
        return {'mode': mode, 'image': None}
    if mode != 'docker' or not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}', image):
        raise Rejected('Pilot Docker worker requires an explicit digest-pinned image')
    report = doctor(image)
    if report['status'] != 'ready': raise Rejected('Pilot Docker daemon or preloaded image unavailable')
    return {'mode': mode, 'image': image, 'image_id': report['image_id'],
            'host': report['host'], 'docker': report['docker'], 'recipe_sha256': hashlib.sha256(WORKER.encode()).hexdigest()}


def identity(scope, phase):
    if phase not in ('edit', 'test') or not re.fullmatch('[a-f0-9]{32}', scope['id']): raise Rejected('Invalid worker identity')
    token = digest({'pilot': scope['id'], 'phase': phase})[:32]
    return {'name': 'orch-pilot-' + token,
            'labels': {'ai.orchd.pilot': scope['id'], 'ai.orchd.scope': digest(scope), 'ai.orchd.phase': phase}}


def command(scope, phase, workspace):
    workspace = real_path(workspace)
    if any(c in str(workspace) for c in (',', '\n', '\r')): raise Rejected('Unsupported Docker mount path')
    if workspace != real_path(Path(scope['journal_root']) / scope['id']): raise Rejected('Worker workspace mismatch')
    image_id = scope['executor']['image_id']
    if not re.fullmatch('sha256:[a-f0-9]{64}', image_id): raise Rejected('Invalid local image identity')
    owned = identity(scope, phase)
    return ['docker', 'run', '--rm', '--pull=never', '--name', owned['name'],
            *[arg for key, value in owned['labels'].items() for arg in ('--label', key+'='+value)],
            '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user=65534:65534', '--pids-limit=64', '--cpus=1', '--memory=256m', '--memory-swap=256m',
            '--log-driver=none', '--tmpfs=/tmp:rw,noexec,nosuid,size=16m',
            '--mount', 'type=bind,source='+str(workspace)+',target=/workspace'+(',readonly' if phase == 'test' else ''),
            '--workdir=/workspace', '--entrypoint=python', '-i', image_id, '-I', '-c', WORKER, phase]


def query(args):
    result = capture(['docker', *args], env=environment(), timeout=10, limit=65536)
    if result['code'] != 0 or result['failure']: raise Rejected('Container observation failed')
    return result['stdout']


def reconcile(scope, phase):
    """Only exact name + all ownership labels may be removed. Errors are uncertainty."""
    owned = identity(scope, phase)
    def absent():
        return not query(['ps', '-aq', '--filter', 'name=^/'+owned['name']+'$']).strip()
    try:
        if absent(): return True
        try: info = json.loads(query(['inspect', owned['name']]))[0]
        except (Rejected, ValueError, IndexError): return absent()
        if info.get('Name') != '/'+owned['name'] or any(info['Config'].get('Labels', {}).get(k) != v for k, v in owned['labels'].items()):
            return False
        container_id = info['Id']
        if not re.fullmatch('[a-f0-9]{64}', container_id): return False
        try: query(['rm', '-f', container_id])
        except Rejected: pass  # --rm can win this race; a fresh observation is decisive.
        return absent()
    except (Rejected, OSError, ValueError, KeyError, TypeError):
        return False


def execute(scope, phase, workspace):
    argv = command(scope, phase, workspace)
    hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope['replacements'].items()}
    payload = {'replacements': scope['replacements']} if phase == 'edit' else {'hashes': hashes, 'checks': scope['checks']}
    started = now()
    try:
        result = capture(argv, env=environment(), input_bytes=canonical(payload), timeout=30, limit=65536)
    except OSError:
        result = {'code': None, 'failure': 'executor_unavailable', 'stdout': '', 'stderr': '', 'truncated': False}
    try: same_runtime = profile('docker', scope['executor']['image']) == scope['executor']
    except Rejected: same_runtime = False
    absent = reconcile(scope, phase) if same_runtime else False
    observation = {'identity': identity(scope, phase), 'phase': phase, 'started_at': started, 'ended_at': now(),
                   'argv_sha256': digest(argv), 'input_sha256': digest(payload), 'code': result['code'],
                   'failure': result['failure'], 'truncated': result['truncated'], 'container_absent': absent,
                   'stdout_sha256': hashlib.sha256(result['stdout'].encode()).hexdigest(),
                   'stderr_sha256': hashlib.sha256(result['stderr'].encode()).hexdigest()}
    parsed = None
    if result['code'] == 0 and not result['failure'] and not result['truncated'] and absent:
        from .pilot import json_data
        try: parsed = json_data(result['stdout'])
        except ValueError: pass
    return observation, parsed
