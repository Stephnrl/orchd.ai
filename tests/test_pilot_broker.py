"""Git JSON pilot Docker workers dispatched through the broker service instead of the operator process."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_pilot as fixtures
import test_broker_service as service_tests
from orch.broker import ServiceClient, VERSION, identity
from orch.contracts import Rejected, canonical, digest, now, uid
from orch.engine import Engine
from orch.execution import Executor
from orch.pilot import Pilot, evaluate
from orch import pilot_worker as worker

IMAGE = 'python@sha256:' + 'a' * 64
PREFLIGHT = {'status': 'ready', 'image_id': 'sha256:' + 'b' * 64,
             'host': {'system': 'Test', 'release': '1', 'machine': 'x', 'hostname_sha256': 'c' * 64, 'python': '3.12'},
             'docker': {'id': 'test-daemon', 'os_type': 'linux', 'server_version': '29', 'architecture': 'x86_64'}}


class PilotBrokerTests(unittest.TestCase):
    git = fixtures.PilotTests.git

    def setUp(self):
        fixtures.PilotTests.setUp(self)
        self.pilot.close()
        self.doctor = patch('orch.pilot_worker.doctor', return_value=copy.deepcopy(PREFLIGHT)).start()
        self.addCleanup(patch.stopall)
        self.journal = self.root / 'journal'
        self.harness = service_tests.Harness(self.root / 'workspaces', 'docker', IMAGE)
        self.harness.service.pilot_root = self.journal.absolute()
        self.pilot = Pilot(self.journal, executor='docker', image=IMAGE, broker_service=self.harness.config)
        self.service_thread = self.harness.thread.ident
        self.calls = []

    def tearDown(self):
        self.pilot.close()
        self.harness.close()
        self.tmp.cleanup()

    def run_pilot(self, prepared, pilot=None):
        pilot = pilot or self.pilot
        return pilot.run(prepared['id'], prepared['scope_sha256'], prepared['revision'])

    def fake_execute(self, scope, phase, workspace, broker=None):
        self.calls.append((phase, threading.get_ident(), broker))
        if phase == 'edit':
            for name, value in scope['replacements'].items():
                (Path(workspace) / name).write_bytes(value.encode())
        hashes = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in scope['replacements'].items()}
        output = {'files': hashes}
        if phase == 'test': output['passed'] = [c['passed'] for c in evaluate(scope['replacements'], scope['checks'])]
        observation = {'identity': worker.identity(scope, phase), 'phase': phase, 'started_at': now(), 'ended_at': now(),
                       'argv_sha256': 'a' * 64, 'input_sha256': 'b' * 64, 'code': 0, 'failure': None, 'truncated': False,
                       'container_absent': True, 'stdout_sha256': 'c' * 64, 'stderr_sha256': 'd' * 64}
        return observation, output

    def test_workers_run_under_the_broker_and_bind_its_identity(self):
        prepared = self.pilot.prepare(self.intent)
        self.assertEqual(prepared['scope']['executor']['broker'], self.harness.service.identity)
        with patch.object(worker, 'execute', side_effect=self.fake_execute):
            result = self.run_pilot(prepared)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['matches_receipt'])
        self.assertEqual([c[0] for c in self.calls], ['edit', 'test'])
        self.assertTrue(all(c[1] == self.service_thread for c in self.calls), 'workers must launch from the service thread')
        self.assertTrue(all(c[2] == self.harness.service.identity for c in self.calls))
        for entry in result['workers']['phases'].values():
            self.assertEqual(entry['observation']['transport'], 'service')
            self.assertEqual(entry['observation']['broker'], self.harness.service.identity)
        self.assertEqual(result['receipt']['worker_journal_sha256'], digest(result['workers']))
        for phase in ('edit', 'test'):
            self.assertTrue((self.harness.journal / ('pilot-' + prepared['id'] + '-' + phase + '.json')).is_file())
        # Restart: the retained journal and receipt still verify with the broker identity bound in.
        self.pilot.close()
        self.pilot = Pilot(self.journal, executor='docker', image=IMAGE, broker_service=self.harness.config)
        self.assertEqual(self.pilot.inspect(prepared['id']), result)
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_another_spelling_of_the_pilot_journal_is_the_same_journal(self):
        """Hosted Windows CI hands every temporary path an 8.3 short name, and `.` or `..`
        segments do the same everywhere; the broker must not read those as another journal."""
        for spelling in (self.journal.parent / '.' / self.journal.name, self.journal / '..' / self.journal.name):
            with self.subTest(spelling=str(spelling)):
                self.harness.service.pilot_root = spelling
                prepared = self.pilot.prepare(self.intent)
                with patch.object(worker, 'execute', side_effect=self.fake_execute):
                    self.assertEqual(self.run_pilot(prepared)['status'], 'passed')
        self.harness.service.pilot_root = self.journal.absolute()
        # A genuinely different journal is still refused through every spelling.
        other = self.root / 'other-journal'
        other.mkdir()
        self.harness.service.pilot_root = other
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
            with self.assertRaises(Rejected): self.run_pilot(prepared)
            execute.assert_not_called()

    def test_service_scope_cannot_run_directly_and_direct_scope_cannot_use_service(self):
        prepared = self.pilot.prepare(self.intent)
        direct = Pilot(self.journal, executor='docker', image=IMAGE)
        try:
            with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
                with self.assertRaises(Rejected): self.run_pilot(prepared, direct)
                execute.assert_not_called()
            local = direct.prepare(self.intent)
            self.assertNotIn('broker', local['scope']['executor'])
        finally:
            direct.close()
        with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
            with self.assertRaises(Rejected): self.run_pilot(local)
            execute.assert_not_called()
        self.assertFalse((self.journal / local['id']).exists())

    def test_redelivery_returns_retained_observation_and_foreign_scopes_are_refused(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute):
            result = self.run_pilot(prepared)
        scope = result['scope']
        client = ServiceClient(self.harness.config)
        with patch.object(worker, 'execute', side_effect=AssertionError('No second launch')):
            for phase in ('edit', 'test'):
                reply = client.call('pilot-execute', {'schema_version': VERSION, 'kind': 'pilot-execute', 'nonce': uid(),
                                                      'scope': scope, 'phase': phase, 'workspace': str(self.journal / scope['id'])})
                retained = result['workers']['phases'][phase]['observation']
                self.assertEqual(reply['observation'], {k: v for k, v in retained.items() if k not in ('transport', 'broker')})
            other = {**scope, 'id': uid()}
            cases = [{'scope': other, 'workspace': str(self.journal / other['id'])},
                     {'scope': scope, 'workspace': str(self.journal / uid())},
                     {'scope': {**scope, 'journal_root': str(self.root / 'elsewhere')}, 'workspace': str(self.root / 'elsewhere' / scope['id'])},
                     {'scope': {**scope, 'executor': {**scope['executor'], 'image': 'python@sha256:' + 'e' * 64}}, 'workspace': str(self.journal / scope['id'])},
                     {'scope': {**scope, 'executor': {**scope['executor'], 'broker': {**scope['executor']['broker'], 'account_sha256': 'f' * 64}}}, 'workspace': str(self.journal / scope['id'])},
                     {'scope': {**scope, 'replacements': {}}, 'workspace': str(self.journal / scope['id'])}]
            for fields in cases:
                with self.subTest(fields=list(fields['scope'].keys())[:1]), self.assertRaises(Rejected):
                    client.call('pilot-execute', {'schema_version': VERSION, 'kind': 'pilot-execute', 'nonce': uid(), 'phase': 'edit', **fields})
            # A different scope for an already journaled pilot id is a conflict, not a relaunch.
            changed = {**scope, 'expires_at': '2099-01-01T00:00:00+00:00'}
            with self.assertRaises(Rejected):
                client.call('pilot-execute', {'schema_version': VERSION, 'kind': 'pilot-execute', 'nonce': uid(),
                                              'scope': changed, 'phase': 'edit', 'workspace': str(self.journal / scope['id'])})
        with self.assertRaises(Rejected):
            client.call('pilot-profile', {'schema_version': VERSION, 'kind': 'pilot-profile', 'nonce': uid(), 'image': 'python@sha256:' + 'e' * 64})

    def test_broker_crash_after_launch_is_retained_and_reconciled_through_the_broker(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=RuntimeError('crash after container launch')):
            with self.assertRaises(Rejected): self.run_pilot(prepared)
        report = self.pilot.inspect(prepared['id'])
        self.assertEqual(report['status'], 'reserved')
        self.assertEqual(report['workers']['phases']['edit']['status'], 'dispatched')
        self.assertFalse((self.harness.journal / ('pilot-' + prepared['id'] + '-edit.json')).exists())
        with self.assertRaises(Rejected): self.run_pilot(prepared)
        with patch.object(worker, 'reconcile', return_value=False) as cleanup:
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 1)
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(cleanup.call_count, 2)
        with patch.object(worker, 'reconcile', side_effect=lambda scope, phase: threading.get_ident() == self.service_thread) as cleanup:
            result = self.pilot.reconcile(prepared['id'], prepared['scope_sha256'], 2)
        self.assertEqual(result['status'], 'interrupted')
        self.assertTrue(all(result['workers']['cleanup'][-1]['absent'].values()))
        self.assertEqual(cleanup.call_count, 2)
        with self.assertRaises(Rejected): self.run_pilot(prepared)

    def test_unavailable_or_changed_broker_blocks_without_launching(self):
        prepared = self.pilot.prepare(self.intent)
        with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
            with patch.object(self.harness.service, 'identity', {**self.harness.service.identity, 'account_sha256': 'f' * 64}):
                with self.assertRaises(Rejected): self.run_pilot(prepared)
            execute.assert_not_called()
            self.assertEqual(self.pilot.inspect(prepared['id'])['status'], 'prepared')
            self.assertFalse((self.journal / prepared['id']).exists())
        with patch.object(worker, 'execute', side_effect=self.fake_execute) as execute:
            self.harness.service.shutdown()
            with self.assertRaises(Rejected): self.run_pilot(prepared)
            execute.assert_not_called()
        self.assertEqual(self.pilot.inspect(prepared['id'])['status'], 'prepared')
        self.harness.thread.join(5)
        self.harness.service.server.server_close = lambda: None

    def test_local_data_pilots_ignore_the_service_and_pilot_routes_need_docker_profile(self):
        local = Pilot(self.root / 'local-journal', broker_service=self.harness.config)
        try:
            self.assertIsNone(local.service)
            prepared = local.prepare(self.intent)
            self.assertEqual(local.run(prepared['id'], prepared['scope_sha256'], 0)['status'], 'passed')
        finally:
            local.close()
        fixture = service_tests.Harness(self.root / 'fixture-workspaces')
        try:
            with self.assertRaises(Rejected):
                ServiceClient(fixture.config).call('pilot-profile', {'schema_version': VERSION, 'kind': 'pilot-profile', 'nonce': uid(), 'image': IMAGE})
        finally:
            fixture.close()
        from orch.broker_service import BrokerService
        with self.assertRaises(Rejected):
            BrokerService(self.root / 'j', self.harness.secret, self.root / 'w', 'trusted-fixture', None, 0, self.root / 'p')
        with self.assertRaises(Rejected):
            BrokerService(self.root / 'j', self.harness.secret, self.root / 'w', 'docker', IMAGE, 0, self.root / 'j' / 'p')

    def test_repository_task_kernel_dispatches_pilot_through_the_broker(self):
        store = self.root / 'store'
        harness = service_tests.Harness(store.resolve() / 'workspaces', 'docker', IMAGE)
        harness.service.pilot_root = (store.resolve() / 'repository-pilots')
        engine = Engine(store, Executor('docker', IMAGE), broker_service=harness.config)
        try:
            with patch.object(worker, 'execute', side_effect=self.fake_execute), \
                    patch.object(engine.executor, 'reconcile', side_effect=AssertionError('Orchestrator must not touch Docker')):
                task = engine.create_repository_task(self.intent, 'Broker-dispatched pilot')
                for expected in ('AWAITING_ACTION_APPROVAL', 'COMPLETED'):
                    state = engine.task(task)['state']
                    engine.approve(task, state['pending_approval']['id'], 'approve', state['revision'])
                    self.assertEqual(engine.run(task)['state']['state'], expected)
            self.assertTrue(all(c[1] == harness.thread.ident for c in self.calls))
            pilot = Pilot(store.resolve() / 'repository-pilots', read_only=True)
            try:
                plan = engine.store.get(engine.task(task)['state']['plan'], task, 'RepositoryTaskPlan')
                report = pilot.inspect(plan['pilot_id'])
                self.assertEqual(report['scope']['executor']['broker'], harness.service.identity)
                self.assertEqual(report['workers']['phases']['test']['observation']['transport'], 'service')
            finally:
                pilot.close()
        finally:
            engine.close()
            harness.close()

    def test_cli_threads_broker_configuration_to_pilots(self):
        import io
        import sys
        from contextlib import redirect_stdout
        from orch.__main__ import main
        intent = self.root / 'intent.json'
        intent.write_text(json.dumps(self.intent), encoding='utf-8')
        argv = ['orch', 'pilot-prepare', '--data', str(self.journal), '--intent', str(intent), '--pilot-executor', 'docker', '--image', IMAGE,
                '--broker-endpoint', self.harness.config['endpoint'], '--broker-secret', self.harness.config['secret']]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), redirect_stdout(output):
            main()
        prepared = json.loads(output.getvalue())
        self.assertEqual(prepared['scope']['executor']['broker'], self.harness.service.identity)
        with patch.object(sys, 'argv', argv[:-2]), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_:
            main()
        self.assertEqual(exit_.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
