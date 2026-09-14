from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock

from orch.api import Application
from orch.contracts import Rejected
from orch.operator_session import OperatorSession


class OperatorSessionTests(unittest.TestCase):
    def setUp(self):
        self.time = 100.0
        self.session = OperatorSession(clock=lambda: self.time, idle_seconds=10, lifetime_seconds=30)
        self.token = self.session.token

    def test_idle_expiry_and_status_do_not_extend_lease(self):
        self.time = 109
        self.assertEqual(self.session.status(self.token)['expires_in_seconds'], 1)
        self.assertTrue(self.session.authenticate(self.token, touch=False))
        self.time = 110
        self.assertFalse(self.session.authenticate(self.token))
        self.time = 100
        self.assertFalse(self.session.authenticate(self.token))

    def test_activity_extends_idle_but_never_absolute_lifetime(self):
        for when in (109, 118, 127):
            self.time = when
            self.assertTrue(self.session.authenticate(self.token))
        self.assertEqual(self.session.status(self.token)['absolute_remaining_seconds'], 3)
        self.time = 130
        self.assertFalse(self.session.authenticate(self.token))

    def test_rotation_invalidates_old_token_and_preserves_absolute_deadline(self):
        self.time = 105
        result = self.session.rotate(self.token)
        self.assertNotEqual(result['session'], self.token)
        self.assertEqual(result['status']['generation'], 2)
        self.assertEqual(result['status']['absolute_remaining_seconds'], 25)
        self.assertFalse(self.session.authenticate(self.token))
        self.assertTrue(self.session.authenticate(result['session']))
        self.assertNotIn(result['session'], str(result['status']))

    def test_rotation_has_one_winner_for_same_generation(self):
        def rotate(_):
            try: return self.session.rotate(self.token)
            except Rejected: return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(rotate, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_revocation_is_irreversible_and_restart_uses_another_token(self):
        self.assertTrue(self.session.revoke(self.token)['restart_required'])
        self.assertFalse(self.session.authenticate(self.token))
        self.assertFalse(self.session.authenticate(''))
        for action in (self.session.status, self.session.rotate, self.session.revoke):
            with self.assertRaises(Rejected): action(self.token)
        replacement = OperatorSession()
        self.assertFalse(replacement.authenticate(self.token))

    def test_bad_tokens_do_not_refresh_idle_and_clock_rollback_invalidates(self):
        self.time = 109
        for token in (None, '', 'é'*43, 'x'*100000, self.token+' ', self.token[:-1]):
            self.assertFalse(self.session.authenticate(token))
        self.time = 110
        self.assertFalse(self.session.authenticate(self.token))
        session = OperatorSession(clock=lambda: self.time)
        token = session.token
        self.time = 109
        self.assertFalse(session.authenticate(token))

    def test_api_session_routes_and_expiry_precede_engine_access(self):
        engine = Mock()
        app = Application(engine, self.session)
        code, status = app.dispatch('GET', '/session', {}, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(status['generation'], 1)
        self.assertEqual(app.dispatch('POST', '/session/rotate', {'lifetime': 100}, self.token)[0], 409)
        self.assertEqual(app.dispatch('GET', '/session/rotate', {}, self.token)[0], 405)
        code, rotation = app.dispatch('POST', '/session/rotate', {}, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(app.dispatch('GET', '/tasks', {}, self.token)[0], 401)
        self.assertEqual(app.dispatch('POST', '/session/revoke', {}, rotation['session'])[0], 200)
        self.assertEqual(app.dispatch('POST', '/tasks', {}, rotation['session'])[0], 401)
        self.assertEqual(engine.mock_calls, [])

    def test_status_poll_does_not_refresh_application_session(self):
        app = Application(Mock(), self.session)
        self.time = 109
        self.assertEqual(app.dispatch('GET', '/session', {}, self.token)[0], 200)
        self.time = 110
        self.assertEqual(app.dispatch('GET', '/session', {}, self.token)[0], 401)

    def test_invalid_policy_rejected(self):
        for idle, lifetime in ((0, 30), (True, 30), (31, 30), (10, 86401)):
            with self.assertRaises(Rejected): OperatorSession(idle_seconds=idle, lifetime_seconds=lifetime)
