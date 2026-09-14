import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from orch.contracts import ref
from orch.engine import Engine
from orch.execution import Executor
from orch.github_evidence import check_record_claims, _fixture_test_command, _fixture_edit_command
from orch.fixtures import EDIT_CODE, TEST_CODE, recipe
from orch.github_bundle import export_bundle, verify_bundle


class FixtureEvidenceIntegrationTests(unittest.TestCase):
    def test_edit_wrapper_checks_scripts_arguments_paths_and_isolation_flags(self):
        for mode in ('trusted-fixture', 'docker'):
            for workspace in (PureWindowsPath('C:/historical/workspace'), PurePosixPath('/historical/workspace')):
                for value in ('hello world\n', 'wrong\n'):
                    with self.subTest(mode=mode, workspace=str(workspace), value=value):
                        image = 'python@sha256:' + 'a' * 64 if mode == 'docker' else None
                        request = dict(mode=mode, image=image, workspace=str(workspace), operation_id='b' * 32, recipe='edit', args=[value])
                        argv = Executor(mode, image).command_for(workspace, EDIT_CODE, [value], request['operation_id'], python_executable='historical-python')
                        with patch('subprocess.Popen', side_effect=AssertionError('No execution')), patch('pathlib.Path.resolve', side_effect=AssertionError('No host path resolution')):
                            self.assertTrue(_fixture_edit_command(request, argv))
                            for changed in ([], argv + ['extra'], argv[:-1] + ['different'],
                                            [part if part != EDIT_CODE else 'print("injected")' for part in argv],
                                            [part for part in argv if part != '-I']):
                                self.assertFalse(_fixture_edit_command(request, changed))
                            self.assertFalse(_fixture_edit_command({**request, 'workspace': '/different'}, argv))
                            self.assertFalse(_fixture_edit_command({**request, 'recipe': 'test'}, argv))
                            self.assertFalse(_fixture_edit_command({**request, 'args': ['unsupported']}, argv))
                            if mode == 'docker':
                                for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=256m'):
                                    self.assertFalse(_fixture_edit_command(request, [part for part in argv if part != flag]))
                                self.assertFalse(_fixture_edit_command({**request, 'operation_id': 'c' * 32}, argv))
                                self.assertFalse(_fixture_edit_command({**request, 'image': 'python:latest'}, argv))

    def test_rendered_recipe_rejects_extra_flags_scripts_and_changed_logical_command(self):
        for mode in ('trusted-fixture', 'docker'):
            for workspace in (PureWindowsPath('C:/historical/workspace'), PurePosixPath('/historical/workspace')):
                image = 'python@sha256:' + 'a' * 64 if mode == 'docker' else None
                request = dict(mode=mode, image=image, workspace=str(workspace), operation_id='b' * 32, recipe='test', args=[])
                argv = Executor(mode, image).command_for(workspace, TEST_CODE, [], request['operation_id'], python_executable='historical-python')
                receipt = dict(requested_command=recipe(), executed_argv=argv)
                self.assertTrue(_fixture_test_command(request, receipt))
                for changed in (argv + ['extra'], [part if part != TEST_CODE else 'print("injected")' for part in argv]):
                    self.assertFalse(_fixture_test_command(request, {**receipt, 'executed_argv': changed}))
                command = {**recipe(), 'timeout_seconds': recipe()['timeout_seconds'] + 1}
                self.assertFalse(_fixture_test_command(request, {**receipt, 'requested_command': command}))

    def test_real_fixture_records_pass_diagnostic_assessment_without_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(root, Executor('trusted-fixture'))
            try:
                task = engine.create_task()
                engine.run(task)
                state = engine.task(task)['state']
                engine.approve(task, state['pending_approval']['id'], 'approve', state['revision'])
                engine.run(task)
                self.assertEqual(engine.task(task)['state']['state'], 'AWAITING_ACTION_APPROVAL')
                records = [json.loads(row[0]) for row in engine.store.db.execute(
                    'SELECT payload FROM records WHERE task_id=? ORDER BY rowid', (task,))]
                selection = {'task_id': task}
                for role, kind in (('patch', 'PatchReceipt'), ('test', 'TestReceipt'), ('review', 'ReviewDecision'), ('policy', 'ToolDecision')):
                    selection[role] = ref([record for record in records if record['contract_type'] == kind][-1])
                before = engine.store.db.total_changes
                with patch('subprocess.Popen', side_effect=AssertionError('No execution during assessment')), patch('socket.socket', side_effect=AssertionError('No network')):
                    report = check_record_claims(engine.store, selection)
                    with tempfile.TemporaryDirectory() as output:
                        bundle = Path(output) / 'evidence.json'
                        exported = export_bundle(engine.store, selection, bundle)
                        verified = verify_bundle(bundle, exported['binding']['bundle_sha256'])
                        self.assertEqual(verified['status'], 'claims_consistent')
                        self.assertEqual(verified['binding']['selection'], selection)
                        self.assertFalse(verified['live_authorized'])
                self.assertEqual(report['status'], 'claims_consistent', report['binding']['claims']['blockers'])
                self.assertEqual(engine.store.db.total_changes, before)
                self.assertFalse(report['live_authorized'])
                self.assertEqual(engine.store.db.execute("SELECT count(*) FROM records WHERE kind='ExternalActionReceipt'").fetchone()[0], 0)
            finally:
                engine.close()
