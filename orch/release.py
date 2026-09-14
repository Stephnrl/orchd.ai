"""Fixed offline acceptance lanes and portable, non-authorizing evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile

from .contracts import Rejected, canonical, digest, now
from .maintenance import plain
from .process import capture
from .readiness import EXPECTED

ROOT = Path(__file__).resolve().parents[1]
LANES = ('offline', 'browser', 'full')
DEFERRED = ['docker_host_verification', 'provider_containment_review',
            'credential_identity_review', 'live_integration_admission']
MAX_REPORT = 262144


def source_manifest(root=ROOT):
    """Bind code, tests, fixtures, documentation, dependency locks and CI policy."""
    root = Path(root).absolute()
    plain(root)
    files = []
    def unreadable(_):
        raise Rejected('Release source inventory is unreadable')
    for name in ('orch', 'tests', 'scripts', 'contracts', 'examples', 'docs', '.github'):
        directory = root / name
        plain(directory)
        if not directory.is_dir(): raise Rejected('Release source directory is missing')
        for current, directories, names in os.walk(directory, followlinks=False, onerror=unreadable):
            directories[:] = sorted(n for n in directories if n != '__pycache__')
            for entry in directories + names:
                plain(Path(current) / entry)
            files.extend(Path(current) / n for n in names if not n.endswith('.pyc'))
    files.extend(root / name for name in ('README.md', 'requirements-dev.txt', 'package.json',
                                        'package-lock.json', 'playwright.config.cjs'))
    result = {}
    for path in sorted(files):
        plain(path)
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise Rejected('Release source must contain bounded regular files')
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stages(lane, manifest):
    if lane not in LANES: raise Rejected('Unknown release lane')
    result = []
    if lane in ('offline', 'full'):
        result = ['contracts', 'python', 'jira_acceptance', 'batch_acceptance', 'javascript_syntax']
        ui = sorted(name for name in manifest if re.fullmatch(r'tests/test_ui_[a-z0-9_]+\.cjs', name))
        if not ui: raise Rejected('UI acceptance inventory is empty')
        result += ui
    if lane in ('browser', 'full'): result.append('browser')
    return result


def browser_count(document):
    """No skipped, flaky, retried, expected-failure or empty browser acceptance."""
    if document.get('errors'): raise Rejected('Browser runner errors')
    count = 0
    def visit(suites):
        nonlocal count
        for suite in suites:
            for spec in suite.get('specs', []):
                for test in spec.get('tests', []):
                    results = test.get('results', [])
                    if (test.get('expectedStatus') != 'passed' or test.get('status') != 'expected'
                            or len(results) != 1 or results[0].get('status') != 'passed'):
                        raise Rejected('Browser acceptance did not pass cleanly')
                    count += 1
            visit(suite.get('suites', []))
    visit(document['suites'])
    if count < 1: raise Rejected('Browser acceptance inventory is empty')
    return count


def _execute(stage, root, python, node, env):
    count, skipped = None, []
    with tempfile.TemporaryDirectory(prefix='orchd-release-') as directory:
        receipt = Path(directory) / 'python.json'
        commands = {
            'contracts': [python, 'scripts/validate_contracts.py'],
            'python': [python, 'scripts/ci_tests.py', '--result', str(receipt)],
            'jira_acceptance': [python, 'scripts/jira_acceptance.py'],
            'batch_acceptance': [python, 'scripts/batch_acceptance.py'],
            'javascript_syntax': [node, '--check', 'orch/ui/app.js'],
            'browser': [node, 'node_modules/@playwright/test/cli.js', 'test', '--reporter=json', '--forbid-only'],
        }
        argv = commands.get(stage, [node, stage])
        if not argv[0]:
            return {'id': stage, 'status': 'failed', 'reason': 'executable_missing', 'tests_run': None, 'skipped': []}
        try:
            output = capture(argv, cwd=root, env=env, timeout=600, limit=4 * 1024 * 1024)
            if output['code'] != 0 or output['failure']:
                reason = output['failure'] or 'nonzero_exit'
            else:
                reason = None
                if stage == 'python':
                    report = json.loads(receipt.read_text(encoding='utf-8'))
                    count, skipped = report['tests_run'], report['skipped']
                    if (report['status'] != 'passed' or type(count) is not int or count <= len(EXPECTED)
                            or skipped != EXPECTED): raise Rejected('Invalid Python acceptance receipt')
                elif stage == 'browser':
                    count = browser_count(json.loads(output['stdout']))
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            reason = 'runner_or_receipt_error'
        # Never copy subprocess logs, tokens, paths or exception text into evidence.
        if reason: count, skipped = None, []
        return {'id': stage, 'status': 'failed' if reason else 'passed', 'reason': reason,
                'tests_run': count, 'skipped': skipped}


def verify_release(destination, lane='full', *, progress=None):
    destination = Path(destination).absolute()
    plain(destination)
    if destination.exists(): raise Rejected('Use a new report path')
    before = source_manifest()
    try:
        relative = destination.relative_to(ROOT).as_posix()
    except ValueError:
        relative = None
    if relative is not None and (relative in before or relative.split('/')[0] in ('orch', 'tests', 'scripts', 'contracts', 'examples', 'docs', '.github')):
        raise Rejected('Release output must be outside the bound source tree')
    plan = stages(lane, before)
    started = now()
    env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'HOME',
                                     'USERPROFILE', 'LOCALAPPDATA', 'PYTHONPATH', 'PLAYWRIGHT_BROWSERS_PATH') if k in os.environ}
    env.update(ORCH_TEST_PYTHON=sys.executable, CI='1', PYTHONUTF8='1')
    results = []
    for stage in plan:
        if progress: progress(stage)
        results.append(_execute(stage, ROOT, sys.executable, shutil.which('node'), env))
    after = source_manifest()
    ended = now()
    report = {'schema_version': '1.0.0', 'kind': 'OfflineReleaseVerification', 'lane': lane,
              'started_at': started, 'ended_at': ended,
              'expires_at': (datetime.fromisoformat(ended) + timedelta(hours=24)).isoformat().replace('+00:00', 'Z'),
              'source_sha256': digest(before), 'source_files': len(before),
              'source_unchanged': before == after,
              'host': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
              'stages': results, 'deferred_gates': DEFERRED, 'live_authorized': False,
              'status': 'passed' if before == after and all(r['status'] == 'passed' for r in results) else 'failed'}
    raw = canonical(report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    plain(destination)
    # Exclusive create: existing reports are never replaced, including concurrent runs.
    with destination.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'kind': 'OfflineReleaseResult', 'status': report['status'], 'lane': lane,
            'sha256': hashlib.sha256(raw).hexdigest(), 'live_authorized': False,
            'failed_stages': [r['id'] for r in results if r['status'] != 'passed'],
            'source_unchanged': report['source_unchanged']}


def check_release(path, expected_sha256, lane='full', *, checked_at=None):
    if not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256):
        raise Rejected('An independently retained report SHA-256 is required')
    source = Path(path).absolute()
    plain(source)
    if not source.is_file(): raise Rejected('Release report must be a regular file')
    with source.open('rb') as stream: raw = stream.read(MAX_REPORT + 1)
    if len(raw) > MAX_REPORT or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise Rejected('Release report size or digest mismatch')
    try:
        report = json.loads(raw)
        if canonical(report) != raw: raise Rejected('Release report must be canonical JSON')
        keys = {'schema_version', 'kind', 'lane', 'started_at', 'ended_at', 'expires_at', 'source_sha256',
                'source_files', 'source_unchanged', 'host', 'stages', 'deferred_gates', 'live_authorized', 'status'}
        manifest = source_manifest()
        if (set(report) != keys or report['schema_version'] != '1.0.0'
                or report['kind'] != 'OfflineReleaseVerification' or report['lane'] != lane
                or report['status'] != 'passed' or report['source_unchanged'] is not True
                or report['source_sha256'] != digest(manifest) or type(report['source_files']) is not int
                or report['source_files'] != len(manifest) or report['live_authorized'] is not False
                or report['deferred_gates'] != DEFERRED): raise Rejected('Release scope or source mismatch')
        start, end, expiry = (datetime.fromisoformat(report[k]) for k in ('started_at', 'ended_at', 'expires_at'))
        checked = checked_at or datetime.now(timezone.utc)
        if (any(not report[k].endswith('Z') for k in ('started_at', 'ended_at', 'expires_at'))
                or not start <= end <= checked < expiry or expiry - end != timedelta(hours=24)):
            raise Rejected('Release report is expired or has invalid timing')
        host = report['host']
        if set(host) != {'system', 'machine', 'python'} or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in host.values()):
            raise Rejected('Invalid observed host metadata')
        expected = stages(lane, manifest)
        if [row['id'] for row in report['stages']] != expected: raise Rejected('Incomplete release stage inventory')
        for row in report['stages']:
            if set(row) != {'id', 'status', 'reason', 'tests_run', 'skipped'} or row['status'] != 'passed' or row['reason'] is not None:
                raise Rejected('Release stage did not pass')
            minimum = len(EXPECTED) if row['id'] == 'python' else 0
            if row['id'] in ('python', 'browser'):
                if type(row['tests_run']) is not int or row['tests_run'] <= minimum: raise Rejected('Empty release test inventory')
            elif row['tests_run'] is not None: raise Rejected('Unexpected test count')
            if row['skipped'] != (EXPECTED if row['id'] == 'python' else []): raise Rejected('Unexpected release skips')
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise Rejected('Invalid, incomplete or stale release evidence') from exc
    return {'kind': 'OfflineReleaseAssessment', 'status': 'current', 'lane': lane,
            'source_sha256': report['source_sha256'], 'observed_host': host,
            'deferred_gates': DEFERRED, 'live_authorized': False,
            'provenance': 'local_unsigned_report', 'sha256': expected_sha256}
