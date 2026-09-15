"""Exercise CLI review, approval, restart and interrupted-run inspection in disposable Git."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orch.pilot import Pilot
from orch.contracts import Rejected


def main():
    with tempfile.TemporaryDirectory(prefix='orchd-pilot-') as directory:
        root = Path(directory)
        repo, data = root / 'repository', root / 'journal'
        repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        def git(*args):
            return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath='+os.devnull,
                                            '-C', str(repo), *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
        def cli(*args):
            return json.loads(subprocess.check_output([sys.executable, '-m', 'orch', *args, '--data', str(data)], cwd=ROOT))
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
        pilot = Pilot(data)
        try:
            interrupted = pilot.prepare(intent)
            with patch('orch.pilot.evaluate', side_effect=RuntimeError('Injected process interruption')):
                try: pilot.run(interrupted['id'], interrupted['scope_sha256'], 0)
                except RuntimeError: pass
                else: raise AssertionError('Crash injection did not run')
        finally: pilot.close()
        recovery = cli('pilot-inspect', '--pilot-id', interrupted['id'])
        assert recovery['status'] == 'reserved' and recovery['recovery'] == 'retain_and_inspect'
        pilot = Pilot(data)
        try:
            try: pilot.run(interrupted['id'], interrupted['scope_sha256'], 0)
            except Rejected: pass
            else: raise AssertionError('Interrupted reservation was reused')
        finally: pilot.close()
        assert (repo / 'settings.json').read_bytes() == b'{"service":{"retries":1}}\n'
        assert git('status', '--porcelain') == ''
    print('PASS: disposable Git pilot CLI review, exact approval, trusted checks, restart and no-retry recovery')


if __name__ == '__main__':
    main()
