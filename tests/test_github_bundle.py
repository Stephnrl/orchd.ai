import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import test_github_evidence_claims as fixture
from orch.contracts import Rejected, canonical, digest
from orch.github_bundle import export_bundle, verify_bundle
from orch.storage import Store


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EvidenceClaimsTests()
        self.fixture.setUp()
        self.fixture.persist()
        self.store = self.fixture.store
        self.root = Path(self.fixture.temp.name)
        self.destination = self.root / 'evidence.json'
        self.clock = patch('orch.github_evidence.now', return_value='2026-09-12T14:01:00Z')
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.fixture.tearDown()

    def export(self):
        return export_bundle(self.store, self.fixture.selection(), self.destination)

    def save_changed(self, bundle):
        bundle['sha256'] = digest(bundle['payload'])
        raw = canonical(bundle)
        self.destination.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_round_trip_without_source_storage_or_execution(self):
        before = self.store.db.total_changes
        report = self.export()
        sha = report['binding']['bundle_sha256']
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(report['binding']['record_count'], 12)
        source_artifacts = self.store.root / 'artifacts'
        hidden = self.store.root / 'hidden-artifacts'
        source_artifacts.rename(hidden)
        try:
            with patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('socket.socket', side_effect=AssertionError('No network')):
                checked = verify_bundle(self.destination, sha)
        finally:
            hidden.rename(source_artifacts)
        self.assertEqual(checked, report)
        self.assertEqual(checked['status'], 'claims_consistent')
        for flag in ('evidence_verified', 'live_authorized', 'retry_allowed'):
            self.assertFalse(checked[flag])
        self.assertNotIn('hello world', json.dumps(checked))

    def test_export_from_read_only_store_excludes_unselected_records(self):
        other = {**self.fixture.docs['review'], 'id': 'unselected-review', 'summary': 'not selected'}
        self.store.put(other)
        self.store.artifact(self.fixture.task, 'unrelated private artifact')
        reader = Store(self.store.root, read_only=True)
        try:
            report = export_bundle(reader, self.fixture.selection(), self.destination)
            self.assertEqual(reader.db.total_changes, 0)
        finally:
            reader.close()
        packet = json.loads(self.destination.read_bytes())
        self.assertNotIn('unselected-review', self.destination.read_text())
        self.assertNotIn(base64.b64encode(b'unrelated private artifact').decode(), self.destination.read_text())
        self.assertEqual(len(packet['payload']['operations']), 2)
        self.assertEqual(report['status'], 'claims_consistent')

    def test_file_and_payload_digests_are_required(self):
        report = self.export()
        original = self.destination.read_bytes()
        for expected in (None, 'invalid', 'b' * 64):
            with self.subTest(expected=expected), self.assertRaises(Rejected):
                verify_bundle(self.destination, expected)
        self.destination.write_bytes(original + b' ')
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, report['binding']['bundle_sha256'])
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, hashlib.sha256(original + b' ').hexdigest())
        packet = json.loads(original)
        packet['sha256'] = 'b' * 64
        raw = canonical(packet)
        self.destination.write_bytes(raw)
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, hashlib.sha256(raw).hexdigest())

    def test_rehashed_artifact_tampering_is_rejected(self):
        self.export()
        packet = json.loads(self.destination.read_bytes())
        packet['payload']['artifacts'][0]['base64'] = base64.b64encode(b'tampered').decode()
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, self.save_changed(packet))

    def test_versions_unknown_fields_and_duplicate_json_keys_are_rejected(self):
        self.export()
        original = json.loads(self.destination.read_bytes())
        for changed in ({**original, 'schema_version': '2.0.0'}, {**original, 'extra': True},
                        {**original, 'kind': 'WorkflowRestore'}):
            raw = canonical(changed)
            self.destination.write_bytes(raw)
            with self.assertRaises(Rejected):
                verify_bundle(self.destination, hashlib.sha256(raw).hexdigest())
        raw = b'{"kind":"discarded",' + canonical(original)[1:]
        self.destination.write_bytes(raw)
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, hashlib.sha256(raw).hexdigest())

    def test_missing_extra_duplicate_and_cross_task_dependencies_reject(self):
        self.export()
        original = json.loads(self.destination.read_bytes())
        changes = [
            lambda p: p['records'].pop(),
            lambda p: p['artifacts'].pop(),
            lambda p: p['broker_requests'].pop(),
            lambda p: p['provenance'].pop(),
            lambda p: p['operations'].pop(),
            lambda p: p['records'].append(copy.deepcopy(p['records'][0])),
            lambda p: p['records'][0].update(task_id='b' * 32),
            lambda p: p['records'].__setitem__(1, copy.deepcopy(p['records'][0])),
            lambda p: p['artifacts'].append(copy.deepcopy(p['artifacts'][0])),
            lambda p: p['operations'][0].update(task_id='b' * 32),
            lambda p: p['approval'].update(decision_id='missing'),
            lambda p: p.update(unrecognized=True),
            lambda p: p['artifacts'][0]['reference'].update(sha256='../escape'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                packet = copy.deepcopy(original)
                change(packet['payload'])
                with self.assertRaises(Rejected):
                    verify_bundle(self.destination, self.save_changed(packet))

    def test_unreferenced_valid_artifact_is_rejected(self):
        self.export()
        packet = json.loads(self.destination.read_bytes())
        item = copy.deepcopy(packet['payload']['artifacts'][0])
        item['reference']['artifact_id'] = 'f' * 32
        packet['payload']['artifacts'].append(item)
        with self.assertRaises(Rejected):
            verify_bundle(self.destination, self.save_changed(packet))

    def test_malformed_collections_and_broker_rows_reject(self):
        self.export()
        original = json.loads(self.destination.read_bytes())
        changes = [lambda p: p.update(records={}), lambda p: p.update(selection=[]),
                   lambda p: p['provenance'][0].update(generation=[]),
                   lambda p: p['broker_requests'][0].update(operation_id={}),
                   lambda p: p['operations'][0].update(generation=True),
                   lambda p: p['artifacts'][0].update(base64='!'),
                   lambda p: p['artifacts'][0]['reference'].update(size_bytes=True)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                packet = copy.deepcopy(original)
                change(packet['payload'])
                with self.assertRaises(Rejected):
                    verify_bundle(self.destination, self.save_changed(packet))

    def test_limits_reject_export_without_publication_and_bound_import(self):
        for constant in ('MAX_BUNDLE_BYTES', 'MAX_RECORD_BYTES', 'MAX_ARTIFACT_BYTES', 'MAX_ARTIFACTS'):
            with self.subTest(constant=constant), patch('orch.github_bundle.' + constant, 1), self.assertRaises(Rejected):
                self.export()
            self.assertFalse(self.destination.exists())
            self.assertFalse(self.store.db.in_transaction)
        report = self.export()
        for constant in ('MAX_BUNDLE_BYTES', 'MAX_RECORD_BYTES', 'MAX_ARTIFACT_BYTES', 'MAX_ARTIFACTS'):
            with self.subTest(constant=constant), patch('orch.github_bundle.' + constant, 1), self.assertRaises(Rejected):
                verify_bundle(self.destination, report['binding']['bundle_sha256'])

    def test_blocked_export_leaves_no_output(self):
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'), self.assertRaises(Rejected):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.store.db.in_transaction)

    def test_incomplete_operation_prevents_publication(self):
        self.store.db.execute('UPDATE operations SET status=?,result=NULL WHERE id=?', ('started', 'c' * 32))
        with self.assertRaises(Rejected):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.store.db.in_transaction)

    def test_verification_rechecks_expiry_instead_of_trusting_export(self):
        report = self.export()
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'):
            checked = verify_bundle(self.destination, report['binding']['bundle_sha256'])
        self.assertEqual(checked['status'], 'blocked')
        self.assertIn('policy_not_current', checked['binding']['assessment']['binding']['claims']['blockers'])
        self.assertFalse(checked['live_authorized'])

    def test_existing_destination_and_workflow_destination_are_preserved(self):
        self.destination.write_bytes(b'keep')
        with self.assertRaises(Rejected):
            self.export()
        self.assertEqual(self.destination.read_bytes(), b'keep')
        with self.assertRaises(Rejected):
            export_bundle(self.store, self.fixture.selection(), self.store.root / 'bundle.json')
        self.assertFalse((self.store.root / 'bundle.json').exists())

    def test_failed_publication_cleans_temporary_file(self):
        with patch('orch.github_bundle.os.link', side_effect=OSError('publication failed')), self.assertRaises(OSError):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.orch-evidence-*')), [])

    def test_failed_flush_does_not_publish(self):
        with patch('orch.github_bundle.os.fsync', side_effect=OSError('flush failed')), self.assertRaises(OSError):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.orch-evidence-*')), [])

    def test_corrupt_source_artifact_cannot_be_exported(self):
        item = self.fixture.docs['patch']['diff']
        (self.store.root / 'artifacts' / item['sha256']).write_bytes(b'corrupt')
        with self.assertRaises(Rejected):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.store.db.in_transaction)

    def test_destination_created_during_publication_is_never_overwritten(self):
        link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b'concurrent owner')
            return link(source, destination)

        with patch('orch.github_bundle.os.link', side_effect=race), self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.destination.read_bytes(), b'concurrent owner')
        self.assertEqual(list(self.root.glob('.orch-evidence-*')), [])

    def test_link_paths_are_rejected_before_read_or_publication(self):
        with patch('orch.github_bundle._linked', return_value=True), self.assertRaises(Rejected):
            self.export()
        report = self.export()
        with patch('orch.github_bundle._linked', return_value=True), self.assertRaises(Rejected):
            verify_bundle(self.destination, report['binding']['bundle_sha256'])

    def test_disposable_verification_storage_is_removed_on_success_and_failure(self):
        report = self.export()
        created = []
        original = tempfile.TemporaryDirectory

        def scratch(*args, **kwargs):
            directory = original(*args, **kwargs)
            created.append(Path(directory.name))
            return directory

        with patch('orch.github_bundle.tempfile.TemporaryDirectory', side_effect=scratch):
            verify_bundle(self.destination, report['binding']['bundle_sha256'])
            packet = json.loads(self.destination.read_bytes())
            packet['payload']['approval']['decision_id'] = 'missing'
            with self.assertRaises(Rejected):
                verify_bundle(self.destination, self.save_changed(packet))
        self.assertEqual(len(created), 2)
        self.assertTrue(all(not path.exists() for path in created))

    def test_cli_export_verify_and_failures_do_not_start_workflow(self):
        from orch.__main__ import main
        selection = self.root / 'selection.json'
        selection.write_bytes(canonical(self.fixture.selection()))
        args = ['orch', 'github-export-evidence-bundle', '--data', str(self.store.root), '--records', str(selection), '--destination', str(self.destination)]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        report = json.loads(output.getvalue())
        args = ['orch', 'github-verify-evidence-bundle', '--bundle', str(self.destination), '--expected-sha256', report['binding']['bundle_sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), patch('orch.__main__.Engine', side_effect=AssertionError('No workflow')):
            main()
        self.assertEqual(json.loads(output.getvalue())['status'], 'claims_consistent')
        args[-1] = 'bad'
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            main()
        self.assertEqual(failure.exception.code, 2)
        self.assertEqual(output.getvalue(), '')

    def test_cli_expired_bundle_reports_blocked_with_exit_two(self):
        from orch.__main__ import main
        report = self.export()
        args = ['orch', 'github-verify-evidence-bundle', '--bundle', str(self.destination), '--expected-sha256', report['binding']['bundle_sha256']]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output), patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'), self.assertRaises(SystemExit) as failure:
            main()
        self.assertEqual(failure.exception.code, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')
