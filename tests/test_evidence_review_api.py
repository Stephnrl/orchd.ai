import copy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_github_evidence_claims as fixtures
from orch.api import Application, BundleDownload, BundleUpload
from orch.github_bundle import verify_bundle


class EvidenceReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceClaimsTests()
        self.fixture.setUp()
        self.fixture.persist()
        self.app = Application(SimpleNamespace(store=self.fixture.store))
        self.root = '/tasks/' + self.fixture.task
        self.clock = patch('orch.github_evidence.now', return_value='2026-09-12T14:01:00Z')
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.fixture.tearDown()

    def request(self, method, suffix, payload=None, token=None):
        return self.app.dispatch(method, self.root + suffix, payload or {}, self.app.session if token is None else token)

    def anchors(self):
        return {role: self.fixture.selection()[role] for role in ('review', 'policy')}

    def bundle_selection(self):
        return {key: value for key, value in self.fixture.selection().items() if key != 'task_id'}

    def test_received_bundle_verifies_without_original_store(self):
        _, bundle = self.request('POST', '/evidence-bundle', self.bundle_selection())
        reviewer = Application(SimpleNamespace())
        with patch('subprocess.Popen', side_effect=AssertionError('No process')), patch('socket.socket', side_effect=AssertionError('No network')):
            code, report = reviewer.dispatch('POST', '/evidence-bundles/verify', BundleUpload(bundle.content, bundle.sha256), reviewer.session)
        self.assertEqual(code, 200)
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertEqual(report['binding']['bundle_sha256'], bundle.sha256)
        self.assertFalse(report['live_authorized'])

    def test_received_bundle_auth_shape_hash_and_expiry(self):
        _, bundle = self.request('POST', '/evidence-bundle', self.bundle_selection())
        upload = BundleUpload(bundle.content, bundle.sha256)
        def verify(value=upload, path='/evidence-bundles/verify', token=None):
            return self.app.dispatch('POST', path, value, self.app.session if token is None else token)
        self.assertEqual(verify(token='invalid')[0], 401)
        self.assertEqual(verify(token='\u00e9')[0], 401)
        self.assertEqual(verify({})[0], 409)
        self.assertEqual(verify(path='/evidence-bundles/verify?task=override')[0], 409)
        for value in (BundleUpload(bundle.content, 'a' * 64), BundleUpload(b'', bundle.sha256), BundleUpload(bundle.content + b' ', bundle.sha256)):
            self.assertEqual(verify(value)[0], 409)
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'):
            code, report = verify()
        self.assertEqual(code, 200)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('policy_not_current', report['binding']['assessment']['binding']['claims']['blockers'])

    def test_bundle_download_is_readonly_and_standalone_verifiable(self):
        before = self.fixture.store.db.total_changes
        files = set(self.fixture.store.root.rglob('*'))
        with patch('subprocess.Popen', side_effect=AssertionError('No execution')), patch('socket.socket', side_effect=AssertionError('No network')):
            code, result = self.request('POST', '/evidence-bundle', self.bundle_selection())
        self.assertEqual(code, 200)
        self.assertIsInstance(result, BundleDownload)
        self.assertEqual(hashlib.sha256(result.content).hexdigest(), result.sha256)
        self.assertEqual(self.fixture.store.db.total_changes, before)
        self.assertEqual(set(self.fixture.store.root.rglob('*')), files)
        destination = self.fixture.store.root / 'download.json'
        destination.write_bytes(result.content)
        report = verify_bundle(destination, result.sha256)
        self.assertEqual(report['status'], 'claims_consistent')
        self.assertFalse(report['live_authorized'])

    def test_bundle_auth_scope_and_closed_contract(self):
        selection = self.bundle_selection()
        self.assertEqual(self.request('POST', '/evidence-bundle', selection, token='invalid')[0], 401)
        for payload in ({}, {**selection, 'task_id': self.fixture.task}, {**selection, 'execute': True}):
            self.assertEqual(self.request('POST', '/evidence-bundle', payload)[0], 409)
        self.assertEqual(self.request('POST', '/evidence-bundle?download=1', selection)[0], 409)
        self.assertEqual(self.request('GET', '/evidence-bundle')[0], 404)
        self.root = '/tasks/' + 'b' * 32
        self.assertEqual(self.request('POST', '/evidence-bundle', selection)[0], 409)

    def test_bundle_rechecks_expiry_corruption_and_size(self):
        selection = self.bundle_selection()
        self.assertEqual(self.request('POST', '/assess-evidence', self.anchors())[0], 200)
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'):
            self.assertEqual(self.request('POST', '/evidence-bundle', selection)[0], 409)
        with patch('orch.github_bundle.MAX_BUNDLE_BYTES', 1):
            self.assertEqual(self.request('POST', '/evidence-bundle', selection)[0], 409)
        (self.fixture.store.root / 'artifacts' / self.fixture.docs['patch']['diff']['sha256']).unlink()
        code, result = self.request('POST', '/evidence-bundle', selection)
        self.assertEqual(code, 409)
        self.assertEqual(set(result), {'error'})
        self.assertFalse(self.fixture.store.db.in_transaction)

    def test_catalog_and_assessment_do_not_mutate_or_execute(self):
        before = self.fixture.store.db.total_changes
        files = set(self.fixture.store.root.rglob('*'))
        with patch('subprocess.Popen', side_effect=AssertionError('No execution')), patch('socket.socket', side_effect=AssertionError('No network')):
            code, catalog = self.request('GET', '/evidence-catalog')
            self.assertEqual(code, 200)
            self.assertEqual(len(catalog['binding']['records']), 4)
            code, result = self.request('POST', '/assess-evidence', self.anchors())
        self.assertEqual(code, 200)
        self.assertEqual(result['selection'], self.fixture.selection())
        self.assertEqual(result['assessment']['status'], 'claims_consistent')
        self.assertFalse(result['assessment']['live_authorized'])
        self.assertEqual(self.fixture.store.db.total_changes, before)
        self.assertEqual(set(self.fixture.store.root.rglob('*')), files)

    def test_both_routes_require_session(self):
        for method, path in (('GET', '/evidence-catalog'), ('POST', '/assess-evidence')):
            self.assertEqual(self.request(method, path, self.anchors(), token='invalid')[0], 401)

    def test_unknown_tasks_and_foreign_references_reject(self):
        self.root = '/tasks/' + 'b' * 32
        self.assertEqual(self.request('GET', '/evidence-catalog')[0], 409)
        self.assertEqual(self.request('POST', '/assess-evidence', self.anchors())[0], 409)
        self.root = '/tasks/' + self.fixture.task
        anchors = copy.deepcopy(self.anchors())
        anchors['review']['sha256'] = 'b' * 64
        self.assertEqual(self.request('POST', '/assess-evidence', anchors)[0], 409)

    def test_closed_payload_and_query_contracts(self):
        for payload in ({}, {'review': self.anchors()['review']}, {**self.anchors(), 'task_id': self.fixture.task},
                        {**self.anchors(), 'run': True}, {'review': 'review', 'policy': 'policy'}):
            self.assertEqual(self.request('POST', '/assess-evidence', payload)[0], 409)
        for method, path in (('GET', '/evidence-catalog'), ('POST', '/assess-evidence')):
            self.assertEqual(self.request(method, path + '?limit=1', self.anchors())[0], 409)
        self.assertEqual(self.request('GET', '/assess-evidence')[0], 404)

    def test_blocked_assessment_is_a_successful_diagnostic_response(self):
        with patch('orch.github_evidence.now', return_value='2026-09-12T14:16:00Z'):
            code, result = self.request('POST', '/assess-evidence', self.anchors())
        self.assertEqual(code, 200)
        self.assertEqual(result['assessment']['status'], 'blocked')
        self.assertIn('policy_not_current', result['assessment']['binding']['claims']['blockers'])

    def test_corrupt_artifact_fails_without_partial_selection(self):
        (self.fixture.store.root / 'artifacts' / self.fixture.docs['patch']['diff']['sha256']).unlink()
        code, result = self.request('POST', '/assess-evidence', self.anchors())
        self.assertEqual(code, 409)
        self.assertEqual(set(result), {'error'})
        self.assertFalse(self.fixture.store.db.in_transaction)

    def test_catalog_limit_is_preserved(self):
        with patch('orch.github_evidence.MAX_CATALOG_RECORDS', 1):
            code, result = self.request('GET', '/evidence-catalog')
        self.assertEqual(code, 409)
        self.assertEqual(set(result), {'error'})

    def test_database_errors_are_generic(self):
        self.fixture.store.db.execute('PRAGMA foreign_keys=OFF')
        self.fixture.store.db.execute('DROP TABLE records')
        code, result = self.request('GET', '/evidence-catalog')
        self.assertEqual(code, 409)
        self.assertNotIn('records', result['error'])
