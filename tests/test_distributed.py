"""Real HTTP adapters, fenced leases and optional real Redis consumer delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

from enterprise_ai.engine import Engine
from enterprise_ai.mock_erp import MockERP, make_erp_server, fingerprint, intent
from enterprise_ai.server import demo_csv, make_server
from enterprise_ai.distributed import Coordinator
from enterprise_ai.worker import WorkerClient, consume_one
from enterprise_ai.api_adapter import run_api
from enterprise_ai.broker import RedisBroker


class ExecutionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(Path(self.directory.name)/'engine.sqlite')
        self.addCleanup(self.engine.close)
        self.engine.analyze(*demo_csv())
        self.erp = MockERP(Path(self.directory.name)/'erp.sqlite', self.engine._records('suppliers'), api_enabled=True)
        self.addCleanup(self.erp.close)
        self.erp_server = make_erp_server(self.erp, 0)
        self.engine.browser_erp = self.erp
        self.token = 'synthetic-worker-token-' + 'x'*32
        self.coordinator = Coordinator(self.engine, self.token)
        self.server = make_server(self.engine, 0)
        for server in (self.erp_server, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.origin = 'http://127.0.0.1:'+str(self.server.server_port)
        self.client = WorkerClient(self.origin, self.token)

    def approved(self, members=('SUP-001', 'SUP-002')):
        entity = next(e for e in self.engine.state()['entities'] if e['source_supplier_ids'] == list(members))
        return self.engine.approve_action(self.engine.stage_action(entity['entity_id'])['action_id'])

    def execute(self, grant):
        return run_api(grant['action'], grant['password'], grant['signature'])

    def finish(self, grant, result):
        return self.client.request('complete', {'action_id': grant['action']['action_id'], 'lease': grant['lease'], 'receipt': result['receipt']})

    def expire(self, action):
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('UPDATE execution_jobs SET lease_until=0 WHERE action_id=?', (action['action_id'],))

    def change_dataset(self):
        suppliers, spend = demo_csv()
        self.engine.analyze(suppliers, spend.replace('GBP', 'CAD'))


class DistributedTests(ExecutionFixture):
    def test_http_api_delivery_and_receipt_verification(self):
        action = self.approved()
        self.engine.approve_action(action['action_id'])
        self.assertEqual(consume_one(self.client)['state'], 'verified')
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'executed')
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM dispatch_outbox').fetchone()[0], 1)
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM portal').fetchone()[0], 0)
        self.assertEqual(consume_one(self.client)['state'], 'empty')

    def test_crash_after_commit_recovers_without_resubmission(self):
        action = self.approved()
        first = self.client.request('claim', {})
        result = self.execute(first)
        self.assertFalse(result['recovered'])
        self.expire(action)
        second = self.client.request('claim', {})
        recovered = self.execute(second)
        self.assertTrue(recovered['recovered'])
        with self.assertRaises(HTTPError): self.finish(first, result)
        self.assertEqual(self.finish(second, recovered)['state'], 'verified')
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)

    def test_old_grant_cannot_commit_after_lease_replacement(self):
        action = self.approved()
        first = self.client.request('claim', {})
        self.expire(action)
        second = self.client.request('claim', {})
        with self.assertRaises(HTTPError): self.execute(first)
        self.assertIsNone(self.erp.receipt(action['action_id']))
        self.assertEqual(self.finish(second, self.execute(second))['state'], 'verified')

    def test_snapshot_change_before_destination_commit_is_rejected(self):
        action = self.approved()
        grant = self.client.request('claim', {})
        self.change_dataset()
        with self.assertRaises(HTTPError): self.execute(grant)
        self.assertIsNone(self.erp.receipt(action['action_id']))

    def test_snapshot_change_after_destination_commit_records_history(self):
        self.approved()
        grant = self.client.request('claim', {})
        result = self.execute(grant)
        self.change_dataset()
        self.assertEqual(self.finish(grant, result)['state'], 'verified')
        event = self.engine.db.execute("SELECT payload FROM audit WHERE event='detached_execution_verified'").fetchone()
        self.assertTrue(json.loads(event[0])['superseded_snapshot'])

    def test_forged_completion_requires_authoritative_receipt(self):
        self.approved()
        grant = self.client.request('claim', {})
        with self.assertRaises(HTTPError): self.finish(grant, {'receipt': {}})
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)

    def test_worker_endpoints_require_authentication(self):
        bad = WorkerClient(self.origin, 'wrong')
        for path, data in [('identity', None), ('claim', {}), ('complete', {})]:
            with self.assertRaises(HTTPError) as error: bad.request(path, data)
            self.assertEqual(error.exception.code, 403)

    def test_invalid_job_reference_is_rejected(self):
        for reference in ([], {}, {'action_id': []}):
            with self.assertRaises(HTTPError) as error:
                self.client.request('claim', {'reference': reference})
            self.assertEqual(error.exception.code, 400)

    def test_rest_requires_capability_idempotency_and_source_preconditions(self):
        self.approved()
        grant = self.client.request('claim', {})
        action = grant['action']
        headers = {'Authorization': 'Bearer '+grant['signature'], 'Content-Type': 'application/json',
                   'Idempotency-Key': action['action_id'], 'If-Match': '"'+fingerprint(action['source_records'])+'"'}
        opener = build_opener(ProxyHandler({}))
        for missing in ('Authorization', 'Idempotency-Key', 'If-Match'):
            selected = {k: v for k, v in headers.items() if k != missing}
            with self.assertRaises(HTTPError):
                opener.open(Request(self.erp.origin+'/api/supplier-sync', json.dumps(intent(action)).encode(), selected, method='PUT'), timeout=3)
            self.assertIsNone(self.erp.receipt(action['action_id']))

    def test_outbox_survives_broker_failure_and_republishes(self):
        action = self.approved()
        class Broker:
            def publish(self, *args): raise OSError('offline')
        self.coordinator.broker = Broker()
        with self.assertRaises(OSError): self.coordinator.publish()
        self.assertEqual(self.engine.db.execute('SELECT published_epoch FROM dispatch_outbox').fetchone()[0], 0)
        with patch.object(self.coordinator.broker, 'publish') as publish:
            self.assertEqual(self.coordinator.publish(), 1)
            self.assertEqual(set(publish.call_args.args), {action['action_id'], self.engine._action(action['action_id'])['execution']['intent_hash'], 1})
            self.assertEqual(self.coordinator.publish(), 0)
            with self.engine.db:
                self.engine.db.execute('UPDATE dispatch_outbox SET last_published=0')
            self.assertEqual(self.coordinator.publish(), 1)

    def test_expired_running_job_is_republished_after_broker_data_loss(self):
        action = self.approved()
        class Broker:
            def publish(self, *args): pass
        self.coordinator.broker = Broker()
        self.assertEqual(self.coordinator.publish(), 1)
        self.client.request('claim', {})
        self.expire(action)
        with self.engine.db:
            self.engine.db.execute('UPDATE dispatch_outbox SET last_published=0')
        self.assertEqual(self.coordinator.publish(), 1)

    def test_duplicate_reference_busy_and_retry_epoch_obsolete(self):
        action = self.approved()
        reference = {'action_id': action['action_id'], 'intent_hash': action['execution']['intent_hash'], 'epoch': '1'}
        grant = self.client.request('claim', {'reference': reference})
        self.assertEqual(self.client.request('claim', {'reference': reference})['state'], 'busy')
        self.client.request('fail', {'action_id': action['action_id'], 'lease': grant['lease']})
        self.engine.retry_browser_action(action['action_id'])
        self.assertEqual(self.client.request('claim', {'reference': reference})['state'], 'obsolete')
        self.assertEqual(consume_one(self.client)['state'], 'verified')

    def test_network_execution_does_not_hold_engine_lock(self):
        self.approved()
        entered, release = threading.Event(), threading.Event()
        original = run_api
        def delayed(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test timed out')
            return original(*args)
        with patch('enterprise_ai.worker.run_api', delayed), ThreadPoolExecutor(2) as pool:
            future = pool.submit(consume_one, self.client)
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(pool.submit(self.engine.state).result(timeout=2)['entities'])
            finally: release.set()
            self.assertEqual(future.result(timeout=10)['state'], 'verified')

    def test_two_separate_cli_worker_processes(self):
        self.approved()
        self.approved(('SUP-005', 'SUP-006'))
        token_file = Path(self.directory.name)/'worker.token'
        token_file.write_text(self.token)
        env = {**os.environ, 'OUTCOME_REDIS_URL': ''}
        command = [sys.executable, '-m', 'enterprise_ai', 'worker', '--coordinator', self.origin, '--token-file', str(token_file), '--once']
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True, timeout=25, env=env), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['state'], 'verified')
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 2)

    @unittest.skipUnless(os.environ.get('OUTCOME_TEST_BROWSER') == '1', 'Optional genuine detached browser test')
    def test_detached_dom_worker(self):
        self.erp.api_enabled = False
        self.approved()
        self.assertEqual(consume_one(self.client)['state'], 'verified')


class BrokerConfigurationTests(unittest.TestCase):
    def test_unsafe_broker_urls_and_invalid_namespace(self):
        for url in ('redis://remote.example', 'http://127.0.0.1', 'redis://127.0.0.1/1', 'redis://127.0.0.1?x=1'):
            with self.assertRaises(ValueError): RedisBroker(url, 'a'*32)
        with self.assertRaises(ValueError): RedisBroker('redis://127.0.0.1', 'invalid')


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_REDIS') == '1', 'Optional genuine Redis service test')
class RedisDeliveryTests(ExecutionFixture):
    # Only these service tests use Redis; ordinary HTTP tests above are not duplicated.
    def test_real_redis_reclaims_pending_delivery(self):
        broker = RedisBroker(os.environ.get('OUTCOME_REDIS_URL', 'redis://127.0.0.1:6379'), self.engine._meta('coordinator_id'))
        broker.initialize()
        broker.initialize()
        message = broker.publish('action-synthetic', 'a'*64, 1)
        first = broker.pop('first')
        self.assertEqual(first[0], message)
        self.assertEqual(set(first[1]), {'action_id', 'intent_hash', 'epoch'})
        second = broker.pop('replacement', idle_ms=0)
        self.assertEqual(second, first)
        self.assertEqual(broker.ack(message), 1)
        self.assertIsNone(broker.pop('replacement', idle_ms=0))

    def test_real_redis_two_concurrent_workers(self):
        url = os.environ.get('OUTCOME_REDIS_URL', 'redis://127.0.0.1:6379')
        broker = RedisBroker(url, self.engine._meta('coordinator_id'))
        broker.initialize()
        self.coordinator.broker = broker
        self.approved()
        self.approved(('SUP-005', 'SUP-006'))
        self.assertEqual(self.coordinator.publish(), 2)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(consume_one, WorkerClient(self.origin, self.token), RedisBroker(url, self.engine._meta('coordinator_id')), 'node-'+str(i)) for i in range(2)]
            self.assertEqual([f.result(timeout=20)['state'] for f in futures], ['verified', 'verified'])
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 2)
