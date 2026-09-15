import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_pilot as fixtures
from orch.broker import file_lock
from orch.contracts import Rejected, canonical, digest
from orch.pilot import Pilot, evaluate
from orch import pilot_worker as worker

IMAGE = 'python@sha256:' + 'a'*64
PREFLIGHT = {'status': 'ready', 'image_id': 'sha256:'+'b'*64,
             'host': {'system': 'Test', 'hostname_sha256': 'c'*64},
             'docker': {'id': 'test-daemon', 'os_type': 'linux'}}


class PilotWorkerTests(unittest.TestCase):
    git = fixtures.PilotTests.git
    run_pilot = fixtures.PilotTests.run_pilot

    def setUp(self):
        fixtures.PilotTests.setUp(self)
        self.pilot.close()
        self.pilot = Pilot(self.root / 'journal', executor='docker', image=IMAGE)
        self.doctor = patch('orch.pilot_worker.doctor', return_value=copy.deepcopy(PREFLIGHT)).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        fixtures.PilotTests.tearDown(self)

    def fake_execute(self, scope, phase, workspace):
        # Observe through another connection: dispatch must commit before effects.
        reader = Pilot(self.pilot.root, read_only=True)
        try:
            report = reader.inspect(scope['id'])
            self.assertEqual(report['workers']['phases'][phase]['status'], 'dispatched')
            self.assertEqual(report['status'], 'reserved')
        finally: reader.close()
        if phase == 'edit':
            for name, value in scope['replacements'].items():
                (workspace / name).write_bytes(value.encode())
        hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope['replacements'].items()}
        output = {'files': hashes}
        if phase == 'test': output['passed'] = [c['passed'] for c in evaluate(scope['replacements'], scope['checks'])]
        return {'code': 0, 'failure': None, 'truncated': False, 'container_absent': True}, output

    def test_two_fresh_dispatches_and_receipt_binding(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
            result = self.run_pilot(prepared)
        self.assertEqual([c.args[1] for c in execute.call_args_list], ['edit', 'test'])
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['matches_receipt'])
        self.assertEqual(result['receipt']['worker_journal_sha256'], digest(result['workers']))
        self.assertNotEqual(*[e['identity']['name'] for e in result['workers']['phases'].values()])
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_expected_failed_check_is_a_complete_failed_result(self):
        self.intent['checks'][0]['equals'] = False
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute):
            result = self.run_pilot(prepared)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['matches_receipt'])

    def test_profile_image_daemon_and_downgrade_fences(self):
        prepared = self.pilot.prepare(self.intent)
        self.doctor.return_value['docker']['id'] = 'other-daemon'
        with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.doctor.return_value = copy.deepcopy(PREFLIGHT)
        self.pilot.image = 'python@sha256:'+'f'*64
        with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.pilot.image, self.pilot.executor = None, 'local-data'
        with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.assertFalse((self.pilot.root / prepared['id']).exists())

    def test_missing_daemon_rejects_preparation(self):
        self.doctor.return_value = {'status': 'unavailable'}
        with self.assertRaises(Rejected): self.pilot.prepare(self.intent)
        self.assertEqual(self.pilot.db.execute('SELECT count(*) FROM pilots').fetchone()[0], 0)

    def test_dispatch_crash_remains_owned_and_reconciliation_never_retries(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): self.run_pilot(prepared)
        report = self.pilot.inspect(prepared['id'])
        self.assertEqual(report['workers']['phases']['edit']['status'], 'dispatched')
        with patch.object(worker, 'reconcile', return_value=True) as cleanup:
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(result['status'], 'interrupted')
        self.assertIsNone(result['receipt'])
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_uncertain_cleanup_is_retained_with_new_revision(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): self.run_pilot(prepared)
        with patch.object(worker, 'reconcile', return_value=False):
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(result['revision'], 2)
        with self.assertRaises(Rejected): self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
        with patch.object(worker, 'reconcile', return_value=True):
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 2)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(len(result['workers']['cleanup']), 2)

    def test_reconcile_cannot_touch_active_runner_or_changed_daemon(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): self.run_pilot(prepared)
        with file_lock(self.pilot.root / '.pilot-worker.lock'), patch.object(worker, 'reconcile') as cleanup:
            with self.assertRaises(Rejected): self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
            cleanup.assert_not_called()
        self.doctor.return_value['docker']['id'] = 'other-daemon'
        with patch.object(worker, 'reconcile') as cleanup:
            with self.assertRaises(Rejected): self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
            cleanup.assert_not_called()

    def test_invalid_test_protocol_cannot_pass(self):
        prepared = self.pilot.prepare(self.intent)
        def wrong(scope, phase, workspace):
            observation, output = self.fake_execute(scope, phase, workspace)
            if phase == 'test': output['passed'] = [1]  # Python equality would confuse 1 and True.
            return observation, output
        with patch.object(worker, 'execute', side_effect=wrong):
            with self.assertRaises(Rejected): self.run_pilot(prepared)
        self.assertEqual(self.pilot.inspect(prepared['id'])['status'], 'reserved')

    def test_timeout_and_unknown_cleanup_block_receipt(self):
        for failure, absent in [('timeout', True), (None, False)]:
            prepared = self.pilot.prepare(self.intent)
            def failed(scope, phase, workspace):
                observation, output = self.fake_execute(scope, phase, workspace)
                observation.update(failure=failure, container_absent=absent)
                return observation, output
            with patch.object(worker, 'execute', side_effect=failed):
                with self.assertRaises(Rejected): self.run_pilot(prepared)
            report = self.pilot.inspect(prepared['id'])
            self.assertIsNone(report['receipt'])
            self.assertEqual(report['workers']['phases']['test']['status'], 'planned')

    def test_worker_journal_corruption_rejected(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute): self.run_pilot(prepared)
        self.pilot.db.execute("UPDATE pilot_workers SET journal='{}' WHERE id=?", (prepared['id'],))
        with self.assertRaises(Rejected): self.pilot.inspect(prepared['id'])

    def test_mount_entrypoint_and_resource_policy(self):
        prepared = self.pilot.prepare(self.intent)
        scope = prepared['scope']
        for phase in ('edit', 'test'):
            argv = worker.command(scope, phase, self.pilot.root / scope['id'])
            for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                         '--user=65534:65534', '--memory=256m', '--memory-swap=256m', '--pids-limit=64', '--cpus=1', '--pull=never', '--entrypoint=python'):
                self.assertIn(flag, argv)
            mount = argv[argv.index('--mount')+1]
            self.assertEqual(mount.endswith(',readonly'), phase == 'test')
            self.assertIn(scope['executor']['image_id'], argv)
            self.assertNotIn(str(self.repo), ' '.join(argv))
            self.assertNotIn('--env', argv)
        with self.assertRaises(Rejected): worker.command(scope, 'test', self.repo)

    def test_cleanup_requires_all_labels_and_exact_name(self):
        scope = self.pilot.prepare(self.intent)['scope']
        owned = worker.identity(scope, 'edit')
        info = {'Id': 'd'*64, 'Name': '/'+owned['name'], 'Config': {'Labels': owned['labels']}}
        for key in owned['labels']:
            foreign = copy.deepcopy(info)
            foreign['Config']['Labels'][key] = 'foreign'
            with patch.object(worker, 'query', side_effect=['d'*64, json.dumps([foreign])]) as query:
                self.assertFalse(worker.reconcile(scope, 'edit'))
                self.assertEqual(query.call_count, 2)
        with patch.object(worker, 'query', side_effect=['d'*64, json.dumps([info]), '', '']) as query:
            self.assertTrue(worker.reconcile(scope, 'edit'))
            self.assertEqual(query.call_args_list[2].args[0], ['rm', '-f', 'd'*64])

    def test_cleanup_errors_are_not_absence_and_auto_removal_races(self):
        scope = self.pilot.prepare(self.intent)['scope']
        with patch.object(worker, 'query', side_effect=Rejected('unavailable')):
            self.assertFalse(worker.reconcile(scope, 'edit'))
        with patch.object(worker, 'query', side_effect=['d'*64, Rejected('gone'), '']):
            self.assertTrue(worker.reconcile(scope, 'edit'))

    def test_execute_bounds_output_and_rejects_duplicate_keys(self):
        scope = self.pilot.prepare(self.intent)['scope']
        result = {'code': 0, 'failure': None, 'truncated': False, 'stdout': '{"files":{},"files":{}}', 'stderr': ''}
        with patch.object(worker, 'capture', return_value=result) as capture, patch.object(worker, 'reconcile', return_value=True):
            observation, parsed = worker.execute(scope, 'edit', self.pilot.root / scope['id'])
        self.assertIsNone(parsed)
        self.assertEqual(capture.call_args.kwargs['timeout'], 30)
        self.assertEqual(capture.call_args.kwargs['limit'], 65536)
        self.assertNotIn('DOCKER_HOST', capture.call_args.kwargs['env'])
        self.assertEqual(json.loads(capture.call_args.kwargs['input_bytes']), {'replacements': scope['replacements']})

    def test_invalid_profile_combinations(self):
        for mode, image in [('shell', None), ('local-data', IMAGE), ('docker', None), ('docker', 'python:latest')]:
            with self.assertRaises(Rejected): worker.profile(mode, image)

    def test_worker_extra_files_and_large_outputs_block_testing(self):
        for variant in ('extra', 'large', 'directory'):
            prepared = self.pilot.prepare(self.intent)
            def contaminated(scope, phase, workspace):
                observation, output = self.fake_execute(scope, phase, workspace)
                if variant == 'extra': (workspace / 'extra.txt').write_text('unexpected')
                elif variant == 'directory': (workspace / 'extra').mkdir()
                else: (workspace / 'config.json').write_bytes(b'x'*65537)
                return observation, output
            with patch.object(worker, 'execute', side_effect=contaminated) as execute:
                with self.assertRaises(Rejected): self.run_pilot(prepared)
                self.assertEqual(execute.call_count, 1)

    def test_reconciliation_survives_recipe_update_without_readmitting_execution(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError): self.run_pilot(prepared)
        with patch.object(worker, 'WORKER', 'updated trusted recipe'), patch.object(worker, 'reconcile', return_value=True):
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
        self.assertEqual(result['status'], 'interrupted')
        with self.assertRaises(Rejected): self.run_pilot(prepared)
