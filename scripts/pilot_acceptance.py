"""Exercise CLI review, approval, restart and interrupted-run inspection in disposable Git."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orch.pilot import Pilot
from orch.contracts import Rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Opt in to real Docker workers using this preloaded digest-pinned image')
    parser.add_argument('--broker', action='store_true', help='Dispatch the Docker workers through a real broker-serve process (requires --image)')
    args = parser.parse_args()
    if args.broker and not args.image: parser.error('--broker requires --image')
    worker_args = ['--pilot-executor', 'docker', '--image', args.image] if args.image else []
    worker_config = {'executor': 'docker', 'image': args.image} if args.image else {}
    broker = None
    with tempfile.TemporaryDirectory(prefix='orchd-pilot-') as directory:
        root = Path(directory)
        repo, data = root / 'repository', root / 'journal'
        repo.mkdir()
        if args.broker:
            # The broker owns Docker for this journal; this script never launches a worker itself.
            secret = root / 'broker-secret'
            secret.write_text(secrets.token_hex(32), encoding='ascii')
            os.chmod(secret, 0o600)
            broker = subprocess.Popen([sys.executable, '-m', 'orch', 'broker-serve', '--broker-root', str(root / 'broker-journal'),
                                       '--broker-secret', str(secret), '--workspaces', str(root / 'unused-workspaces'),
                                       '--pilot-root', str(data), '--image', args.image, '--port', '0'],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            line = broker.stdout.readline().strip()
            assert line.startswith('Broker service: http://127.0.0.1:'), line
            config = {'endpoint': line.split('http://', 1)[1], 'secret': str(secret)}
            worker_args += ['--broker-endpoint', config['endpoint'], '--broker-secret', config['secret']]
            worker_config['broker_service'] = config
        try:
            walkthrough(root, repo, data, worker_args, worker_config, args)
        finally:
            if broker is not None:
                broker.terminate()
                broker.communicate(timeout=10)
    print('PASS: disposable Git pilot CLI review, exact approval, trusted checks, restart, no-retry recovery and reviewed reclamation ('
          + ('Docker via broker service' if args.broker else 'Docker' if args.image else 'local data') + ')')


def walkthrough(root, repo, data, worker_args, worker_config, args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull,
                                        '-C', str(repo), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
    def cli(*args):
        return json.loads(subprocess.check_output([sys.executable, '-m', 'orch', *args, '--data', str(data), *worker_args], cwd=ROOT))
    git('init')
    (repo / 'settings.json').write_bytes(b'{"service":{"retries":1}}\n')
    git('add', '.')
    git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Pilot baseline')
    intent = {'repository': str(repo), 'base_commit': git('rev-parse', 'HEAD'),
              'replacements': {'settings.json': '{"service":{"retries":3}}\n'},
              'checks': [{'path': 'settings.json', 'keys': ['service', 'retries'], 'equals': 3}]}
    intent_file = root / 'intent.json'
    intent_file.write_text(json.dumps(intent), encoding='utf-8')
    prepared = cli('pilot-prepare', '--intent', str(intent_file))
    assert prepared['status'] == 'prepared' and not (data / prepared['id']).exists()
    reviewed = cli('pilot-inspect', '--pilot-id', prepared['id'])
    assert reviewed['scope_sha256'] == prepared['scope_sha256'] and '+{"service":{"retries":3}}' in reviewed['diff']
    result = cli('pilot-run', '--pilot-id', prepared['id'], '--expected-sha256', prepared['scope_sha256'], '--expected-revision', '0')
    assert result['status'] == 'passed' and result['matches_receipt']
    assert cli('pilot-inspect', '--pilot-id', prepared['id']) == result
    pilot = Pilot(data, **worker_config)
    try:
        interrupted = pilot.prepare(intent)
        with patch('orch.pilot.evaluate', side_effect=RuntimeError('Injected process interruption')):
            try: pilot.run(interrupted['id'], interrupted['scope_sha256'], 0)
            except RuntimeError: pass
            else: raise AssertionError('Crash injection did not run')
    finally: pilot.close()
    recovery = cli('pilot-inspect', '--pilot-id', interrupted['id'])
    assert recovery['status'] == 'reserved' and recovery['recovery'] == 'retain_and_inspect'
    pilot = Pilot(data, **worker_config)
    try:
        try: pilot.run(interrupted['id'], interrupted['scope_sha256'], 0)
        except Rejected: pass
        else: raise AssertionError('Interrupted reservation was reused')
    finally: pilot.close()
    if worker_args:
        recovered = cli('pilot-reconcile', '--pilot-id', interrupted['id'], '--expected-sha256', interrupted['scope_sha256'], '--expected-revision', '1')
        assert recovered['status'] == 'interrupted' and recovered['receipt'] is None
        assert all(recovered['workers']['cleanup'][-1]['absent'].values())
        assert all(entry['observation']['container_absent'] for entry in result['workers']['phases'].values())
        assert result['workers']['phases']['edit']['identity']['name'] != result['workers']['phases']['test']['identity']['name']
    # Retention: usage is read-only; reclamation is reviewed, fenced and keeps the journal row.
    report = cli('pilot-usage')
    entries = {entry['id']: entry for entry in report['pilots']}
    assert report['journal_rows'] == report['live_pilots'] == 2 and entries[prepared['id']]['eligible']
    assert entries[interrupted['id']]['eligible'] == bool(worker_args), entries[interrupted['id']]['reasons']
    preview = cli('pilot-reclaim', '--pilot-id', prepared['id'], '--expected-sha256', prepared['scope_sha256'], '--expected-revision', '2', '--dry-run')
    assert preview['files'] == ['settings.json'] and not preview['recorded'] and (data / prepared['id']).is_dir()
    reclaimed = cli('pilot-reclaim', '--pilot-id', prepared['id'], '--expected-sha256', prepared['scope_sha256'], '--expected-revision', '2')
    assert reclaimed['deleted'] and not (data / prepared['id']).exists()
    retained = cli('pilot-inspect', '--pilot-id', prepared['id'])
    assert retained['receipt'] == result['receipt'] and retained['reclamation'] == 'reclaimed' and not retained['matches_receipt']
    after = cli('pilot-usage')
    assert after['journal_rows'] == 2 and after['live_pilots'] == 1 and after['workspace_bytes'] < report['workspace_bytes']
    assert (repo / 'settings.json').read_bytes() == b'{"service":{"retries":1}}\n'
    assert git('status', '--porcelain') == ''
    if args.broker:
        broker_identity = prepared['scope']['executor']['broker']
        assert all(e['observation']['transport'] == 'service' and e['observation']['broker'] == broker_identity
                   for e in result['workers']['phases'].values())
        assert not any(p.name.startswith('orch-') for p in data.iterdir() if p.is_dir()), 'orchestrator journal holds no broker files'


if __name__ == '__main__':
    main()
