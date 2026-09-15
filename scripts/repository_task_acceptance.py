"""Disposable repository task lifecycle, local snapshot sign-off and crash recovery."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orch.engine import Engine
from orch.execution import Executor
from orch.maintenance import audit, backup, verify_backup
from orch.pilot import Pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Opt in to the preloaded pinned Docker image')
    args = parser.parse_args()
    flags = ['--image', args.image] if args.image else ['--trusted-fixture']
    with tempfile.TemporaryDirectory(prefix='orchd-repository-task-') as directory:
        root = Path(directory)
        repo, store = root / 'repository', root / 'store'
        repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        def git(*argv):
            return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull, '-C', str(repo), *argv], env=env, stderr=subprocess.DEVNULL).decode().strip()
        def cli(*argv):
            return json.loads(subprocess.check_output([sys.executable, '-m', 'orch', *argv, '--data', str(store), *flags], cwd=ROOT))
        git('init')
        (repo / 'settings.json').write_bytes(b'{"retries":1}\n')
        git('add', '.')
        git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'Disposable baseline')
        intent = {'repository': str(repo), 'base_commit': git('rev-parse', 'HEAD'),
                  'replacements': {'settings.json': '{"retries":3}\n'},
                  'checks': [{'path': 'settings.json', 'keys': ['retries'], 'equals': 3}]}
        intent_file = root / 'intent.json'
        intent_file.write_text(json.dumps(intent), encoding='utf-8')
        created = cli('repository-task-create', '--intent', str(intent_file))
        task = created['state']['task_id']
        assert created['state']['state'] == 'AWAITING_PLAN_APPROVAL'
        for expected in ('AWAITING_ACTION_APPROVAL', 'COMPLETED'):
            state = created['state']
            cli('repository-task-approve', '--task', task, '--request-id', state['pending_approval']['id'], '--expected-revision', str(state['revision']), '--decision', 'approve')
            created = cli('repository-task-run', '--task', task)
            assert created['state']['state'] == expected
        executor = Executor('docker', args.image) if args.image else Executor('trusted-fixture')
        engine = Engine(store, executor)
        try:
            audit(engine.store)
            backup(engine.store, root / 'backup')
            assert verify_backup(root / 'backup')['status'] == 'passed'
            assert engine.store.db.execute("SELECT count(*) FROM records WHERE kind='ExternalActionReceipt'").fetchone()[0] == 0
            interrupted = engine.create_repository_task(intent)
            state = engine.task(interrupted)['state']
            engine.approve(interrupted, state['pending_approval']['id'], 'approve', state['revision'])
            run = Pilot.run
            def crash(pilot, *argv):
                run(pilot, *argv)
                raise RuntimeError('Injected interruption between pilot and task commits')
            with patch.object(Pilot, 'run', new=crash):
                try: engine.run(interrupted)
                except RuntimeError: pass
                else: raise AssertionError('Interruption not injected')
            assert engine.run(interrupted)['state']['state'] == 'BLOCKED'
            state = engine.task(interrupted)['state']
            assert engine.recover(interrupted, state['revision'])['state']['state'] == 'FAILED'
            audit(engine.store)
        finally: engine.close()
        # Retention: both bound pilots are terminal in the kernel, so their workspaces may be reclaimed
        # through the pilot CLI while task records, approvals and snapshots stay in the common store.
        journal = str(store / 'repository-pilots')
        def pilot_cli(*argv):
            return json.loads(subprocess.check_output([sys.executable, '-m', 'orch', *argv, '--data', journal], cwd=ROOT))
        report = pilot_cli('pilot-usage')
        assert report['journal_rows'] == 2 and report['reclaimable'] == 2, report
        for entry in report['pilots']:
            assert pilot_cli('pilot-reclaim', '--pilot-id', entry['id'], '--expected-sha256', pilot_cli('pilot-inspect', '--pilot-id', entry['id'])['scope_sha256'],
                             '--expected-revision', str(pilot_cli('pilot-inspect', '--pilot-id', entry['id'])['revision']),
                             '--reason', 'Acceptance task finished; kernel snapshots retained')['deleted']
        assert pilot_cli('pilot-usage')['live_pilots'] == 0
        engine = Engine(store, executor)
        try:
            audit(engine.store)
            assert engine.run(task)['state']['state'] == 'COMPLETED'
            result = engine.store.get(engine.task(task)['context']['repository_result'], task, 'RepositoryTaskResult')
            assert [engine.store.read_artifact(item['content'], task) for item in result['snapshots']] == ['{"retries":3}\n']
        finally: engine.close()
        assert (repo / 'settings.json').read_bytes() == b'{"retries":1}\n'
        assert git('status', '--porcelain') == ''
    print('PASS: repository task CLI approvals, retained snapshot, replay/backup, no-retry recovery and reviewed reclamation ('+('Docker' if args.image else 'local data')+')')


if __name__ == '__main__':
    main()
