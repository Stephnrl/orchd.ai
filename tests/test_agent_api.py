"""The loopback agent API: who you are is the secret you signed with.

Agent sessions alone were cooperative — an agent container needed the store mounted, so it
could claim any role or write the evidence directly. These tests fix the enforced version:
the service holds the store, the agent holds a secret, and the name and the permitted roles
come from the service's own configuration.
"""
import json
import os
from pathlib import Path
import secrets as randomness
import subprocess
import sys
import tempfile
import threading
import unittest

from orch.agent_client import AgentClient
from orch.agent_service import AgentService, registry
from orch.broker import SecretFile, rotate_secret, sign
from orch.contracts import Rejected, canonical, uid
from orch.engine import Engine
from orch.execution import Executor

ROOT = Path(__file__).resolve().parents[1]


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store, self.agents = self.root / 'store', self.root / 'agents'
        engine = Engine(self.store, Executor('trusted-fixture'))
        try:
            self.task = engine.create_task()
            self.second = engine.create_task()
        finally:
            engine.close()
        self.enrol('lead-devops', 'lead_planner')
        self.enrol('junior-devops', 'junior')
        self.service = AgentService(self.store, self.agents)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = '127.0.0.1:' + str(self.service.port)

    def tearDown(self):
        self.service.server.shutdown()
        self.thread.join(timeout=30)
        self.temp.cleanup()

    def enrol(self, name, *roles):
        folder = self.agents / name
        folder.mkdir(parents=True)
        (folder / 'role').write_text("\n".join(roles) + "\n", encoding='ascii')
        (folder / 'secret').write_text(randomness.token_hex(32) + "\n", encoding='ascii')
        return folder / 'secret'

    def client(self, name):
        return AgentClient(self.endpoint, self.agents / name / 'secret')

    # Identity comes from the secret ----------------------------------------------------------

    def test_the_secret_decides_which_agent_is_calling(self):
        for name, role in (('lead-devops', 'lead_planner'), ('junior-devops', 'junior')):
            with self.subTest(name=name):
                identity = self.client(name).whoami()
                self.assertEqual((identity['agent'], identity['roles']), (name, [role]))
                self.assertFalse(identity['live_authorized'])
        # No request carries an agent name, so none can claim to be another.
        wire = json.loads((ROOT / 'contracts/agent-v1.schema.json').read_text())['$defs']
        for kind in ('AgentClaimRequest', 'AgentRenewRequest', 'AgentReleaseRequest', 'AgentSessionsRequest'):
            with self.subTest(kind=kind):
                self.assertNotIn('agent', wire[kind]['properties'])

    def test_an_unknown_secret_is_nobody(self):
        stranger = self.root / 'stranger.secret'
        stranger.write_text(randomness.token_hex(32) + "\n", encoding='ascii')
        with self.assertRaisesRegex(Rejected, 'unavailable|refused|authentication'):
            AgentClient(self.endpoint, stranger).whoami()
        self.assertEqual(self.client('lead-devops').sessions()['held'], 0)

    def test_a_tampered_body_never_reaches_the_store(self):
        import http.client
        body = canonical({"schema_version": "1.0.0", "kind": "agent-claim", "nonce": uid(),
                          "task_id": self.task, "role": "lead_planner"})
        tag = sign(SecretFile(self.agents / 'lead-devops' / 'secret').current,
                   "request", "agent-claim", body)
        changed = body.replace(b'"lead_planner"', b'"junior"      ')
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=30)
        try:
            connection.request('POST', '/agent-claim', changed,
                               {"Content-Type": "application/json", "X-Orch-Agent-Auth": tag})
            self.assertEqual(connection.getresponse().status, 401)
        finally:
            connection.close()
        self.assertEqual(self.client('lead-devops').sessions()['held'], 0)

    # The role an agent may claim is configuration, not a request field ------------------------

    def test_an_agent_may_only_claim_the_roles_it_was_enrolled_for(self):
        with self.assertRaises(Rejected):
            self.client('junior-devops').claim(self.task, 'lead_planner')
        self.assertEqual(self.client('lead-devops').claim(self.task, 'lead_planner')['status'], 'claimed')

    def test_an_enrolled_role_still_has_to_match_the_task(self):
        # The junior may claim as a junior, but this task's next step is the planner's.
        with self.assertRaises(Rejected):
            self.client('junior-devops').claim(self.task, 'junior')
        self.assertEqual(self.client('lead-devops').sessions()['held'], 0)

    def test_an_agent_may_be_enrolled_for_several_roles(self):
        self.enrol('utility', 'lead_planner', 'reviewer')
        service = AgentService(self.store, self.agents)
        try:
            self.assertEqual(sorted(service.agents['utility']['roles']), ['lead_planner', 'reviewer'])
        finally:
            service.close()

    # Session behaviour over the wire -----------------------------------------------------------

    def test_claim_renew_release_round_trip(self):
        lead = self.client('lead-devops')
        claimed = lead.claim(self.task, 'lead_planner', 300)
        self.assertEqual(claimed['session']['agent'], 'lead-devops')
        renewed = lead.renew(self.task, 900)
        self.assertEqual(renewed['status'], 'renewed')
        self.assertGreater(renewed['session']['expires_at'], claimed['session']['expires_at'])
        self.assertEqual(lead.sessions()['held'], 1)
        self.assertEqual(lead.release(self.task)['status'], 'released')
        self.assertEqual(lead.sessions()['held'], 0)

    def test_one_agent_cannot_release_another_agents_session(self):
        self.client('lead-devops').claim(self.task, 'lead_planner', 300)
        with self.assertRaises(Rejected):
            self.client('junior-devops').release(self.task)
        self.assertEqual(self.client('lead-devops').sessions()['held'], 1)

    def test_a_reply_is_signed_and_answers_the_request_it_was_sent(self):
        lead = self.client('lead-devops')
        first = lead.claim(self.task, 'lead_planner', 300)
        second = lead.sessions()
        self.assertNotEqual(first['nonce'], second['nonce'])   # Each reply names its request.

    def test_a_rotated_secret_keeps_working_without_a_restart(self):
        lead = self.client('lead-devops')
        self.assertEqual(lead.whoami()['agent'], 'lead-devops')
        rotate_secret(self.agents / 'lead-devops' / 'secret')
        self.assertEqual(self.client('lead-devops').whoami()['agent'], 'lead-devops')  # New secret.
        self.assertEqual(lead.whoami()['agent'], 'lead-devops')                        # Old one too.
        rotate_secret(self.agents / 'lead-devops' / 'secret', complete=True)
        self.assertEqual(self.client('lead-devops').whoami()['agent'], 'lead-devops')

    # What the service refuses to be ------------------------------------------------------------

    def test_only_the_five_routes_exist_and_only_by_post(self):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=30)
        try:
            connection.request('GET', '/agent-sessions')
            self.assertEqual(connection.getresponse().status, 405)
            connection.close()
            connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=30)
            connection.request('POST', '/tasks', b'{}', {"Content-Type": "application/json",
                                                         "X-Orch-Agent-Auth": "x"})
            self.assertEqual(connection.getresponse().status, 404)
        finally:
            connection.close()

    def test_an_oversized_body_is_refused_before_authentication(self):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=30)
        try:
            connection.request('POST', '/agent-claim', b'x' * 20000,
                               {"Content-Type": "application/json", "X-Orch-Agent-Auth": "x"})
            self.assertEqual(connection.getresponse().status, 413)
        finally:
            connection.close()

    def test_a_registry_needs_a_role_and_a_secret_for_every_agent(self):
        broken = self.root / 'broken'
        (broken / 'nameless').mkdir(parents=True)
        with self.assertRaises(Rejected):
            registry(broken)
        (broken / 'nameless' / 'secret').write_text(randomness.token_hex(32) + "\n", encoding='ascii')
        with self.assertRaisesRegex(Rejected, 'role and a secret'):
            registry(broken)
        (broken / 'nameless' / 'role').write_text("not_a_role\n", encoding='ascii')
        with self.assertRaisesRegex(Rejected, 'known roles'):
            registry(broken)
        (broken / 'nameless' / 'role').write_text("junior\n", encoding='ascii')
        self.assertEqual(sorted(registry(broken)), ['nameless'])

    def test_every_route_has_a_checked_contract_on_both_sides(self):
        from jsonschema import Draft202012Validator
        from orch.agent_service import ROUTES, WIRE_SCHEMA
        Draft202012Validator.check_schema(WIRE_SCHEMA)
        named = {kind for pair in ROUTES.values() for kind in pair}
        self.assertTrue(named <= set(WIRE_SCHEMA['$defs']), named - set(WIRE_SCHEMA['$defs']))
        # Each reply names its own route, so a reply cannot be replayed onto another.
        for route, (_, reply) in ROUTES.items():
            with self.subTest(route=route):
                self.assertEqual(WIRE_SCHEMA['$defs'][reply]['properties']['kind'], {"const": route})

    def test_an_agent_needs_no_store_path_at_all(self):
        """The whole point: a container gets an endpoint and a secret, and nothing else."""
        secret = self.agents / 'lead-devops' / 'secret'
        result = subprocess.run(
            [sys.executable, '-m', 'orch', 'agent-claim', '--agent-endpoint', self.endpoint,
             '--agent-secret', str(secret), '--task', self.task, '--role', 'lead_planner'],
            cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertEqual(json.loads(result.stdout)['session']['agent'], 'lead-devops')


if __name__ == '__main__':
    unittest.main()
