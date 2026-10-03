"""Failure budgets, owner clocks and archive-before-delete acceptance checks."""
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_distributed import ExecutionFixture
from enterprise_ai.broker import RedisBroker
from enterprise_ai.distributed import INVESTIGATION
from enterprise_ai.maintenance import compact
from enterprise_ai.runtime_clock import LeaseClock
from enterprise_ai.worker import consume_one


class HardeningTests(ExecutionFixture):
    def exhaust(self, committed=False):
        action = self.approved()
        for attempt in range(3):
            grant = self.client.request('claim',{})
            self.assertEqual(grant['state'],'granted')
            if committed and attempt==2:
                self.execute(grant)
                self.expire(action)
            else:
                result = self.client.request('fail',{'action_id':action['action_id'],'lease':grant['lease']})
                if attempt<2: self.engine.retry_browser_action(action['action_id'])
        if committed:
            ref = {'action_id':action['action_id'],'intent_hash':action['execution']['intent_hash'],'epoch':'3'}
            result = self.client.request('claim',{'reference':ref})
        self.assertEqual(result['state'],INVESTIGATION)
        return action

    def test_three_failures_quarantine_without_a_fourth_execution(self):
        action = self.exhaust()
        with self.assertRaisesRegex(ValueError,'budget'): self.engine.retry_browser_action(action['action_id'])
        self.assertEqual(consume_one(self.client)['state'],'empty')
        row = self.engine.db.execute('SELECT * FROM execution_jobs').fetchone()
        self.assertEqual((row['attempts'],row['state']),(3,INVESTIGATION))
        dead = self.coordinator.investigations()['jobs'][0]
        self.assertEqual(set(self.coordinator.dead_reference(dead)),{'action_id','intent_hash','epoch','attempts','reason_code'})
        self.assertIsNone(self.erp.receipt(action['action_id']))
        self.coordinator.investigate(action['action_id'],'investigator','Destination has no matching receipt')
        self.assertEqual(self.engine._action(action['action_id'])['execution']['state'],INVESTIGATION)

    def test_three_crashes_reach_the_same_persistent_budget(self):
        action = self.approved()
        for _ in range(3):
            grant = self.client.request('claim',{})
            self.assertEqual(grant['state'],'granted')
            # Restart has no in-memory lease; persisted wall-clock values
            # cannot restore authority, even when far in the future.
            self.coordinator.leases = LeaseClock()
        result = self.client.request('claim',{})
        self.assertEqual(result['state'],INVESTIGATION)
        self.assertEqual(self.engine._action(action['action_id'])['execution']['attempts'],3)

    def test_investigator_recovers_third_commit_without_resubmission(self):
        action = self.exhaust(committed=True)
        receipt = self.erp.receipt(action['action_id'])
        result = self.coordinator.investigate(action['action_id'],'investigator','Confirmed exact destination receipt')
        self.assertEqual(result['resolution'],'VERIFIED_EXISTING_RECEIPT')
        self.assertEqual(self.engine._action(action['action_id'])['status'],'executed')
        self.assertEqual(self.erp.receipt(action['action_id']),receipt)
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],1)

    def test_wall_clock_changes_do_not_expire_or_extend_grants(self):
        self.approved()
        with patch('time.time',return_value=1): grant=self.client.request('claim',{})
        with patch('time.time',return_value=2_000_000_000): result=self.execute(grant)
        self.assertEqual(self.finish(grant,result)['state'],'verified')

    def test_elapsed_owner_time_expires_lease_and_destination_capability(self):
        self.approved()
        now = time.monotonic()
        self.coordinator.leases.clock = lambda: now
        self.erp.clock = lambda: now
        grant = self.client.request('claim',{})
        self.erp.clock = lambda: now+41
        with self.assertRaises(HTTPError): self.execute(grant)
        self.coordinator.leases.clock = lambda: now+91
        with self.assertRaises(HTTPError): self.client.request('fail',{'action_id':grant['action']['action_id'],'lease':grant['lease']})
        replacement = self.client.request('claim',{})
        self.assertNotEqual(replacement['lease'],grant['lease'])

    def old_verified(self):
        action = self.approved()
        self.assertEqual(consume_one(self.client)['state'],'verified')
        with self.engine.db:
            self.engine.db.execute('UPDATE execution_jobs SET completed_at=? WHERE action_id=?',(time.time()-100*86400,action['action_id']))
        return action

    def test_archive_preserves_receipt_and_action_idempotency(self):
        action = self.old_verified()
        result = compact(self.engine,Path(self.directory.name)/'archive',90)
        self.assertEqual(result['archived_jobs'],1)
        archive = Path(self.directory.name)/'archive'/(result['archive_id']+'.jsonl')
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),result['sha256'])
        self.assertIn('receipt_json',archive.read_text())
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0],0)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM dispatch_outbox').fetchone()[0],0)
        self.assertEqual(self.engine._action(action['action_id'])['execution']['archive_id'],result['archive_id'])
        self.assertEqual(self.engine.stage_action(action['entity_id'])['action_id'],action['action_id'])
        self.assertEqual(self.erp.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],1)
        self.assertEqual(compact(self.engine,archive.parent,90)['archived_jobs'],0)

    def test_archive_failure_never_deletes_operational_rows(self):
        self.old_verified()
        with patch('enterprise_ai.maintenance.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): compact(self.engine,Path(self.directory.name)/'archive',90)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0],1)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM execution_tombstones').fetchone()[0],0)

    def test_retention_leaves_queued_failed_and_investigation_jobs(self):
        self.exhaust()
        self.approved(('SUP-005','SUP-006'))
        with self.engine.db: self.engine.db.execute('UPDATE execution_jobs SET completed_at=1')
        self.assertEqual(compact(self.engine,Path(self.directory.name)/'archive',90)['archived_jobs'],0)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0],2)


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_REDIS')=='1','Optional genuine Redis DLQ acceptance test')
class RedisHardeningTests(ExecutionFixture):
    def test_atomic_dead_letter_deduplication_and_ack_compaction(self):
        broker = RedisBroker(os.environ.get('OUTCOME_REDIS_URL','redis://127.0.0.1:6379'),self.engine._meta('coordinator_id'))
        broker.initialize()
        self.coordinator.broker = broker
        action = self.approved()
        for attempt in range(3):
            self.coordinator.publish()
            message = broker.pop('poison-test',idle_ms=0)
            grant = self.client.request('claim',{'reference':message[1]})
            result = self.client.request('fail',{'action_id':action['action_id'],'lease':grant['lease']})
            if attempt<2:
                broker.ack(message[0])
                self.engine.retry_browser_action(action['action_id'])
            else:
                dead_id=broker.dead_letter(result['reference'],message[0])
                self.assertEqual(broker.dead_letter(result['reference']),dead_id)
        self.coordinator.publish()
        self.assertEqual(broker.command('XLEN',broker.stream),0)
        self.assertEqual(broker.command('XPENDING',broker.stream,broker.group)[0],0)
        entries=broker.command('XRANGE',broker.dlq,'-','+')
        self.assertEqual(len(entries),1)
        self.assertEqual(set(entries[0][1][::2]),{'action_id','intent_hash','epoch','attempts','reason_code'})
        self.assertNotIn('Northbridge',json.dumps(entries))
