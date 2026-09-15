"""Opt-in real Docker pilot gates with retained source/host/image-bound evidence."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orch.contracts import Rejected, canonical, digest, now, uid
from orch.maintenance import plain
from orch.pilot import Pilot
from orch import pilot_worker as worker
from orch.process import capture
from orch.release import source_manifest

CHECKS = ['cli_lifecycle', 'isolation_and_limits', 'timeout_cleanup', 'output_cleanup', 'interrupted_worker_cleanup']

PROBE = r'''
import os, socket
from pathlib import Path
assert os.getuid() == 65534
status = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
assert not Path('/var/run/docker.sock').exists()
assert not os.path.exists('/root/.ssh')
assert not Path('/workspace/pilot.sqlite').exists()
assert not Path('/workspace/.git').exists()
for target in ['/forbidden-write', '/workspace/settings.json']:
    try: Path(target).write_text('forbidden')
    except OSError: pass
    else: raise AssertionError('Write permitted: '+target)
sock = socket.socket()
sock.settimeout(1)
try: sock.connect(('192.0.2.1', 80))
except OSError: pass
else: raise AssertionError('Network egress permitted')
finally: sock.close()
cg = Path('/sys/fs/cgroup')
if (cg / 'cgroup.controllers').exists():
    memory = int((cg / 'memory.max').read_text())
    swap = int((cg / 'memory.swap.max').read_text())
    pids = int((cg / 'pids.max').read_text())
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
else:
    memory = int((cg / 'memory/memory.limit_in_bytes').read_text())
    swap = int((cg / 'memory/memory.memsw.limit_in_bytes').read_text()) - memory
    pids = int((cg / 'pids/pids.max').read_text())
    quota = int((cg / 'cpu/cpu.cfs_quota_us').read_text())
    period = int((cg / 'cpu/cpu.cfs_period_us').read_text())
assert 0 < memory <= 256*1024*1024 and swap == 0 and 0 < pids <= 64
assert 0 < quota <= period
space = os.statvfs('/tmp')
assert space.f_blocks * space.f_frsize <= 16*1024*1024
print('isolation and limits passed')
'''


def checked(argv, **kwargs):
    result = capture(argv, **kwargs)
    if result['code'] != 0 or result['failure']: raise Rejected('Acceptance command failed')
    return result['stdout'].strip()


def probe(executor, script, timeout=30, expected_failure=None):
    with tempfile.TemporaryDirectory(prefix='orchd-worker-probe-') as directory:
        root = Path(directory)
        identifier = uid()
        workspace = root / identifier
        workspace.mkdir(mode=0o755)
        (workspace / 'settings.json').write_bytes(b'{}')
        (workspace / 'settings.json').chmod(0o444)
        scope = {'id': identifier, 'journal_root': str(root), 'executor': executor}
        argv = worker.command(scope, 'test', workspace)
        argv[argv.index(worker.WORKER)] = script  # Test instrumentation only; no public arbitrary-recipe API.
        try:
            result = capture(argv, env=worker.environment(), timeout=timeout, limit=65536)
            if result['failure'] != expected_failure or (expected_failure is None and result['code'] != 0):
                raise Rejected('Isolation probe failed: '+result['stderr'][:2000])
        finally:
            if not worker.reconcile(scope, 'test'): raise Rejected('Probe cleanup uncertain')
        assert not worker.query(['ps', '-aq', '--filter', 'name=^/'+worker.identity(scope, 'test')['name']+'$']).strip()


def interrupted_worker(executor):
    with tempfile.TemporaryDirectory(prefix='orchd-worker-recovery-') as directory:
        root = Path(directory)
        repo = root / 'repository'
        repo.mkdir()
        def git(*args):
            return checked(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull,
                            '-C', str(repo), *args], env={**worker.environment(), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
        git('init')
        (repo / 'settings.json').write_bytes(b'{"enabled":false}\n')
        git('add', '.')
        git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Disposable baseline')
        pilot = Pilot(root / 'journal', executor='docker', image=executor['image'])
        scope = None
        try:
            prepared = pilot.prepare({'repository': str(repo), 'base_commit': git('rev-parse', 'HEAD'),
                                      'replacements': {'settings.json': '{"enabled":true}\n'},
                                      'checks': [{'path': 'settings.json', 'keys': ['enabled'], 'equals': True}]})
            scope = prepared['scope']
            def leave_running(scope, phase, workspace):
                argv = worker.command(scope, phase, workspace)
                argv.remove('-i')
                argv.insert(2, '-d')
                argv[argv.index(worker.WORKER)] = 'import time; time.sleep(120)'
                checked(argv, env=worker.environment(), timeout=30)
                raise RuntimeError('Injected interruption after a real container launch')
            with patch.object(worker, 'execute', side_effect=leave_running):
                try: pilot.run(prepared['id'], prepared['scope_sha256'], 0)
                except RuntimeError: pass
                else: raise Rejected('Interruption did not occur')
            running = json.loads(worker.query(['inspect', worker.identity(scope, 'edit')['name']]))[0]
            assert running['State']['Running']
            pilot.close()
            pilot = Pilot(root / 'journal', executor='docker', image=executor['image'])
            report = pilot.inspect(prepared['id'])
            assert report['workers']['phases']['edit']['status'] == 'dispatched'
            recovered = pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
            assert recovered['status'] == 'interrupted' and recovered['receipt'] is None
            assert all(recovered['workers']['cleanup'][-1]['absent'].values())
            try: pilot.run(prepared['id'], prepared['scope_sha256'], 0)
            except Rejected: pass
            else: raise Rejected('Interrupted operation retried')
            assert (repo / 'settings.json').read_bytes() == b'{"enabled":false}\n'
        finally:
            pilot.close()
            if scope:
                for phase in ('edit', 'test'):
                    if not worker.reconcile(scope, phase): raise Rejected('Recovery probe cleanup uncertain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--check', action='store_true', help='Read and verify a retained report without running probes')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    destination = plain(Path(args.destination).absolute())
    source = digest(source_manifest())
    executor = worker.profile('docker', args.image)
    if args.check:
        with destination.open('rb') as stream: raw = stream.read(65537)
        report = json.loads(raw)
        if (len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.expected_sha256
                or canonical(report) != raw or report['kind'] != 'PilotWorkerAcceptance'
                or report['status'] != 'passed' or report['checks'] != CHECKS
                or report['source_sha256'] != source or report['executor'] != executor
                or not datetime.fromisoformat(report['ended_at']) <= datetime.now(timezone.utc) < datetime.fromisoformat(report['expires_at'])):
            raise Rejected('Worker acceptance evidence is invalid or stale')
        print('PASS: current source, host, daemon, image and retained worker acceptance report')
        return
    if destination.exists(): raise Rejected('Use a new worker report destination')
    report = {'kind': 'PilotWorkerAcceptance', 'started_at': now(), 'executor': executor,
              'source_sha256': source, 'checks': [], 'status': 'failed'}
    try:
        env = {**worker.environment(), 'PYTHONPATH': os.pathsep.join(sys.path)}
        checked([sys.executable, 'scripts/pilot_acceptance.py', '--image', args.image], cwd=ROOT, env=env, timeout=120)
        report['checks'].append('cli_lifecycle')
        probe(executor, PROBE)
        report['checks'].append('isolation_and_limits')
        probe(executor, 'import time; time.sleep(120)', timeout=3, expected_failure='timeout')
        report['checks'].append('timeout_cleanup')
        probe(executor, "import sys; sys.stdout.write('x'*200000); sys.stdout.flush()", expected_failure='output_limit')
        report['checks'].append('output_cleanup')
        interrupted_worker(executor)
        report['checks'].append('interrupted_worker_cleanup')
        if source != digest(source_manifest()) or executor != worker.profile('docker', args.image): raise Rejected('Source or worker runtime changed')
        report['status'] = 'passed'
    finally:
        report['ended_at'] = now()
        report['expires_at'] = (datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = canonical(report)
        with destination.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({'status': report['status'], 'checks': report['checks'], 'sha256': hashlib.sha256(raw).hexdigest()}))


if __name__ == '__main__':
    main()
