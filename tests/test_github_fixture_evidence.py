import json
import tempfile
import unittest
from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import patch

from orch.contracts import ref
from orch.engine import Engine
from orch.execution import Executor
from orch.github_evidence import check_record_claims, _fixture_test_command
from orch.fixtures import TEST_CODE, recipe


class FixtureEvidenceIntegrationTests(unittest.TestCase):
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
                self.assertEqual(report['status'], 'claims_consistent', report['binding']['claims']['blockers'])
                self.assertEqual(engine.store.db.total_changes, before)
                self.assertFalse(report['live_authorized'])
                self.assertEqual(engine.store.db.execute("SELECT count(*) FROM records WHERE kind='ExternalActionReceipt'").fetchone()[0], 0)
            finally:
                engine.close()
