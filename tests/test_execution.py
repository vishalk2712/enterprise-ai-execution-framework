"""Approval, crash recovery and genuine optional headless DOM executions."""
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

from enterprise_ai.engine import Engine
from enterprise_ai.execution import run_one, run_browser
from enterprise_ai.mock_erp import MockERP, make_erp_server, fingerprint, intent
from enterprise_ai.server import demo_csv


class ExecutionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(Path(self.directory.name)/'engine.sqlite')
        self.addCleanup(self.engine.close)
        self.engine.analyze(*demo_csv())
        self.erp = MockERP(Path(self.directory.name)/'erp.sqlite', self.engine._records('suppliers'))
        self.addCleanup(self.erp.close)
        self.server = make_erp_server(self.erp, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.engine.browser_erp = self.erp
        self.entity = next(e for e in self.engine.state()['entities'] if e['source_supplier_ids'] == ['SUP-001', 'SUP-002'])

    def staged(self):
        return self.engine.stage_action(self.entity['entity_id'])

    def approved(self):
        return self.engine.approve_action(self.staged()['action_id'])

    @staticmethod
    def simulated_browser(action, erp):
        # Queue policy unit test only; genuine DOM execution lives below.
        old = erp.receipt(action['action_id'])
        receipt = erp.apply(action, erp.sign(action))
        return {'receipt': receipt, 'trace': [], 'recovered': bool(old)}


class QueueTests(ExecutionFixture):
    def test_label_is_not_approval_and_pending_cannot_execute(self):
        action = self.staged()
        evaluation = next(e for e in self.engine.evaluations()['evaluations'] if e.get('authority_grouped'))
        self.engine.review(evaluation['evaluation_id'], 'Match', 'Synthetic fixture', 'Workflow test only')
        self.assertIsNone(run_one(self.engine, self.simulated_browser))
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'worker queue'):
            self.engine.execute_action(action['action_id'])
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'pending')

    def test_approval_queues_once_and_receipt_gates_completion(self):
        action = self.approved()
        self.engine.approve_action(action['action_id'])
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0], 1)
        self.assertEqual(action['execution']['state'], 'queued')
        self.assertEqual(run_one(self.engine, self.simulated_browser)['state'], 'verified')
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'executed')
        self.assertIsNone(run_one(self.engine, self.simulated_browser))
        self.assertEqual(self.staged()['action_id'], action['action_id'])
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM portal').fetchone()[0], 0)

    def test_stale_dataset_cannot_reach_destination(self):
        action = self.approved()
        suppliers, spend = demo_csv()
        self.engine.analyze(suppliers, spend.replace('GBP', 'CAD'))
        with patch('enterprise_ai.execution.run_browser') as browser:
            self.assertEqual(run_one(self.engine, browser)['state'], 'failed')
            browser.assert_not_called()
        self.assertIsNone(self.erp.receipt(action['action_id']))
        with self.assertRaises(ValueError):
            self.engine.retry_browser_action(action['action_id'])

    def test_changed_target_or_payload_requires_fresh_approval(self):
        action = self.approved()
        self.erp.instance_id = 'different-instance'
        self.assertEqual(run_one(self.engine, self.simulated_browser)['state'], 'failed')
        with self.assertRaises(ValueError):
            self.engine.retry_browser_action(action['action_id'])
        fresh = self.staged()
        self.assertNotEqual(fresh['action_id'], action['action_id'])
        self.assertEqual(fresh['status'], 'pending')
        self.engine.approve_action(fresh['action_id'])
        payload = json.loads(self.engine.db.execute('SELECT payload FROM actions WHERE action_id=?', (fresh['action_id'],)).fetchone()[0])
        payload['payload']['display_name'] = 'Tampered'
        with self.engine.db:
            self.engine.db.execute('UPDATE actions SET payload=? WHERE action_id=?', (json.dumps(payload), fresh['action_id']))
        self.assertEqual(run_one(self.engine, self.simulated_browser)['state'], 'failed')
        self.assertIsNone(self.erp.receipt(fresh['action_id']))

    def test_crash_after_erp_commit_recovers_without_duplicate_write(self):
        action = self.approved()
        def crash(a, erp):
            erp.apply(a, erp.sign(a))
            raise SystemExit('Simulated process death after destination commit')
        with self.assertRaises(SystemExit):
            run_one(self.engine, crash)
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'approved')
        self.assertEqual(self.engine._action(action['action_id'])['execution']['state'], 'running')
        with self.engine.db:
            self.engine.db.execute('UPDATE execution_jobs SET lease_until=0')
        self.assertEqual(run_one(self.engine, self.simulated_browser)['state'], 'verified')
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        self.assertTrue(json.loads(self.engine.db.execute("SELECT payload FROM audit WHERE event='browser_erp_verified'").fetchone()[0])['recovered'])

    def test_false_done_or_changed_erp_does_not_mark_executed(self):
        action = self.approved()
        def false_done(a, erp):
            return {'receipt': {'intent_hash': fingerprint(intent(a))}, 'done': True}
        self.assertEqual(run_one(self.engine, false_done)['state'], 'failed')
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'approved')
        self.engine.retry_browser_action(action['action_id'])
        def corrupt(a, erp):
            result = self.simulated_browser(a, erp)
            with erp.db:
                erp.db.execute('DELETE FROM supplier_groups')
            return result
        self.assertEqual(run_one(self.engine, corrupt)['state'], 'failed')
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'approved')

    def test_import_writer_is_fenced_during_destination_submit(self):
        self.approved()
        def attempt_write(action, erp):
            other = sqlite3.connect(Path(self.directory.name)/'engine.sqlite', timeout=.05)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute('BEGIN IMMEDIATE')
            finally:
                other.close()
            return self.simulated_browser(action, erp)
        self.assertEqual(run_one(self.engine, attempt_write)['state'], 'verified')

    def test_erp_rejects_missing_capability_changed_source_and_conflicts(self):
        action = self.approved()
        with self.assertRaises(ValueError):
            self.erp.apply(action, '0'*64)
        with self.assertRaises(ValueError):
            self.erp.apply(action, self.erp.sign(action, time.time()-1))
        tampered = copy.deepcopy(action)
        tampered['source_records'][1]['registration_id'] = 'different'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.erp.apply(tampered, self.erp.sign(tampered))
        with self.erp.db:
            self.erp.db.execute("UPDATE source_suppliers SET payload='{}' WHERE supplier_id='SUP-001'")
        with self.assertRaisesRegex(ValueError, 'source record'):
            self.erp.apply(action, self.erp.sign(action))

    def test_html_form_requires_origin_session_csrf_and_no_mutation_api(self):
        opener = build_opener(ProxyHandler({}))
        for headers in ({}, {'Origin': self.erp.origin}, {'Origin': 'https://example.org'}):
            request = Request(self.erp.origin+'/merge', b'action=x&signature=y&csrf=z', {**headers, 'Content-Type': 'application/x-www-form-urlencoded'})
            with self.assertRaises(HTTPError) as error:
                opener.open(request, timeout=3)
            self.assertEqual(error.exception.code, 403)
        request = Request(self.erp.origin+'/api/merge', b'{}', {'Origin': self.erp.origin, 'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            opener.open(request, timeout=3)
        self.assertEqual(error.exception.code, 415)

    def test_group_rationale_is_cached_and_local_model_cannot_invent_facts(self):
        result = self.engine.explain_entity(self.entity['entity_id'])
        self.assertIn('Northbridge Components Ltd', result['text'])
        self.assertIn('REGGB41001', result['text'])
        self.assertIn('external execution requires', result['text'])
        self.assertFalse(result['model_used'])
        self.assertEqual(result, self.engine.explain_entity(self.entity['entity_id']))
        responses = [io.BytesIO(json.dumps({'details': {'format': 'gguf'}, 'model_info': {'architecture': 'fixture'}}).encode()),
                     io.BytesIO(json.dumps({'response': json.dumps({'fact_ids': ['authority', 'policy']})}).encode())]
        with patch('enterprise_ai.explanations.build_opener') as mocked:
            mocked.return_value.open.side_effect = responses
            generated = self.engine.explain_entity(self.entity['entity_id'], 'fixture:1b')
            prompt = json.loads(mocked.return_value.open.call_args[0][0].data)['prompt']
            self.assertNotIn('Northbridge', prompt)
        self.assertTrue(generated['model_used'])
        self.assertIn('Shared VAT', generated['text'])
        bad = [io.BytesIO(json.dumps({'details': {'format': 'gguf'}, 'model_info': {'architecture': 'fixture'}}).encode()),
               io.BytesIO(json.dumps({'response': json.dumps({'fact_ids': ['authority', 'invented_bank_merge']})}).encode())]
        with patch('enterprise_ai.explanations.build_opener') as mocked:
            mocked.return_value.open.side_effect = bad
            failed = self.engine.explain_entity(self.entity['entity_id'], 'bad-fixture:1b')
        self.assertIn('fallback', failed)
        self.assertNotIn('invented_bank_merge', failed['text'])
        self.assertEqual(self.engine._actions(), [])


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_BROWSER') == '1', 'Optional Node/Playwright browser environment required')
class ActualDOMTests(ExecutionFixture):
    def test_headless_login_indexed_controls_submit_receipt_and_idempotent_recovery(self):
        action = self.approved()
        result = run_one(self.engine)
        self.assertEqual(result['state'], 'verified', self.engine._action(action['action_id']))
        evidence = json.loads(self.engine.db.execute("SELECT payload FROM audit WHERE event='browser_erp_verified'").fetchone()[0])
        self.assertTrue(any(s['control'] == 'Apply approved supplier sync' for s in evidence['dom_trace']))
        self.assertTrue(all(len(s['observed_dom_hash']) == 64 for s in evidence['dom_trace']))
        self.assertNotIn(self.erp.password, json.dumps(evidence))
        recovered = run_browser(self.engine._action(action['action_id']), self.erp)
        self.assertTrue(recovered['recovered'])
        self.assertFalse(any(s['control'] == 'Apply approved supplier sync' for s in recovered['trace']))
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)

    def test_changed_destination_source_fails_real_browser_and_keeps_action_approved(self):
        action = self.approved()
        with self.erp.db:
            self.erp.db.execute("UPDATE source_suppliers SET payload='{}' WHERE supplier_id='SUP-001'")
        self.assertEqual(run_one(self.engine)['state'], 'failed')
        self.assertEqual(self.engine._action(action['action_id'])['status'], 'approved')
        self.assertIsNone(self.erp.receipt(action['action_id']))


if __name__ == '__main__':
    unittest.main()
