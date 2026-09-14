"""Real loopback protocol tests, including authentication before body consumption."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit


class HTTPHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.process = subprocess.Popen([sys.executable, '-m', 'orch', 'serve', '--trusted-fixture',
                                        '--data', self.temp.name, '--port', '0'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.addCleanup(self.cleanup)
        self.address = urlsplit(self.process.stdout.readline().strip().split(' ')[-1])
        self.token = self.process.stdout.readline().strip().split(' ')[-1]
        self.host = self.address.netloc

    def cleanup(self):
        self.process.terminate()
        self.process.communicate(timeout=10)
        self.temp.cleanup()

    def request(self, method, path, *, token=None, extra=(), body=b'{}', length=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.address.port, timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', self.host)
            connection.putheader('Authorization', 'Bearer '+(self.token if token is None else token))
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(body)) if length is None else length)
            for key, value in extra: connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_ambiguous_framing_and_json_reject_without_task_creation(self):
        cases = [([('Host', self.host)], 403), ([('Origin', 'http://'+self.host), ('Origin', 'http://'+self.host)], 403),
                 ([('Authorization', 'Bearer '+self.token)], 401), ([('Content-Length', '2')], 400),
                 ([('Content-Type', 'application/json')], 400), ([('Transfer-Encoding', 'chunked')], 400),
                 ([('Content-Encoding', 'gzip')], 400), ([('Last-Event-ID', '1'), ('Last-Event-ID', '2')], 400)]
        for headers, status in cases:
            self.assertEqual(self.request('POST', '/tasks', extra=headers)[0], status)
        for body in (b'{"title":"first","title":"second"}', b'{"title":NaN}', b'{"title":Infinity}',
                     b'\xff', b'[]', b'{"title":{"nested":1,"nested":2}}', b'['*1500+b']'*1500):
            self.assertEqual(self.request('POST', '/tasks', body=body)[0], 400)
        for length in ('-1', '+2', '99999999'):
            self.assertEqual(self.request('POST', '/tasks', length=length)[0], 400)
        self.assertEqual(self.request('GET', '/tasks', body=b'{"title":"ignored"}')[0], 400)
        status, raw, _ = self.request('GET', '/tasks')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['items'], [])

    def test_rotation_revocation_security_headers_and_early_authentication(self):
        # No body is sent despite its advertised size. Authentication must reject
        # immediately rather than waiting for body bytes or entering the engine.
        status, _, _ = self.request('POST', '/tasks', token='invalid', body=b'', length='4000')
        self.assertEqual(status, 401)
        status, raw, headers = self.request('GET', '/session')
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), raw)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        old = self.token
        status, raw, headers = self.request('POST', '/session/rotate')
        self.assertEqual(status, 200)
        self.token = json.loads(raw)['session']
        self.assertNotEqual(self.token, old)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('GET', '/tasks', token=old)[0], 401)
        self.assertEqual(self.request('POST', '/evidence-bundles/verify', token=old, body=b'', length='1000')[0], 401)
        self.assertEqual(self.request('GET', '/tasks/not-a-task/stream', token=old)[0], 401)
        self.assertEqual(self.request('POST', '/tasks')[0], 201)
        self.assertEqual(self.request('POST', '/session/revoke')[0], 200)
        self.assertEqual(self.request('GET', '/tasks')[0], 401)
        self.assertEqual(self.request('POST', '/session/rotate')[0], 401)
