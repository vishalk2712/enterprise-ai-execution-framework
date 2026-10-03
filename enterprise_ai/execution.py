"""Durable approval outbox and one bounded browser consumer for the mock ERP."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from .mock_erp import ADAPTER, canonical, fingerprint, intent, source_record


def migrate(db):
    db.executescript("""CREATE TABLE IF NOT EXISTS execution_jobs(
      action_id TEXT PRIMARY KEY REFERENCES actions(action_id), intent_hash TEXT NOT NULL,
      state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, owner TEXT,
      lease_until REAL NOT NULL DEFAULT 0, receipt_json TEXT, error TEXT);
      CREATE INDEX IF NOT EXISTS execution_queue ON execution_jobs(state,lease_until);
    """)


def stage_payload(engine, entity):
    records = [source_record(json.loads(r[0])) for r in engine.db.execute("SELECT payload FROM suppliers WHERE entity_id=? ORDER BY supplier_id", (entity['entity_id'],))]
    payload = {"target_binding": engine.browser_erp.binding(), "source_records": records}
    if len(canonical({**payload, 'payload': entity}).encode()) > 64000:
        raise ValueError("Browser action exceeds its 64 KB payload limit")
    return payload


def enqueue(engine, action):
    if engine.browser_erp is None or action.get('target_binding') != engine.browser_erp.binding():
        raise ValueError("ERP target changed; stage and approve a fresh action")
    if action['status'] != 'approved':
        raise ValueError("Only approved actions may enter the browser queue")
    digest = fingerprint(intent(action))
    existing = engine.db.execute("SELECT intent_hash FROM execution_jobs WHERE action_id=?", (action['action_id'],)).fetchone()
    if existing and existing[0] != digest:
        raise ValueError("Approved action payload changed")
    engine.db.execute("INSERT OR IGNORE INTO execution_jobs(action_id,intent_hash,state) VALUES(?,?,'queued')", (action['action_id'], digest))


def job_state(engine, action_id):
    row = engine.db.execute("SELECT * FROM execution_jobs WHERE action_id=?", (action_id,)).fetchone()
    if not row:
        return None
    data = {k: row[k] for k in ('state', 'attempts', 'error', 'intent_hash')}
    data['receipt'] = json.loads(row['receipt_json']) if row['receipt_json'] else None
    return data


def run_browser(action, erp):
    if action.get('target') != 'browser_mock_erp' or action.get('status') not in {'approved', 'executed'}:
        raise ValueError('Browser execution requires an approved sandbox action')
    node = os.environ.get('OUTCOME_NODE') or shutil.which('node')
    if not node:
        raise ValueError("Node.js and Playwright are required for browser execution")
    # Expiring capability prevents a timed-out/orphan browser from writing later.
    job = {"action": intent(action), "password": erp.password, "signature": erp.sign(action)}
    process = subprocess.run([node, str(Path(__file__).with_name('browser_worker.mjs'))], input=canonical(job),
                             capture_output=True, text=True, encoding='utf-8', timeout=45, shell=False)
    if process.returncode or len(process.stdout) > 65536:
        raise ValueError("Browser execution failed; check Node/Playwright setup or changed ERP evidence")
    result = json.loads(process.stdout)
    receipt = result['receipt']
    expected = {'action_id': action['action_id'], 'entity_id': action['entity_id'], 'snapshot': action['snapshot'],
                'intent_hash': fingerprint(intent(action)), 'target_binding': action['target_binding'],
                'source_supplier_ids': sorted(action['payload']['source_supplier_ids']), 'payload_hash': fingerprint(action['payload']), 'status': 'verified'}
    if receipt != expected or result.get('adapter') != ADAPTER:
        raise ValueError("Browser returned an unexpected execution receipt")
    with erp.lock:
        erp.verify(receipt)
        if erp.receipt(action['action_id']) != receipt:
            raise ValueError("Destination receipt is missing")
    return result


def run_one(engine, browser=run_browser):
    """Claim transaction, then fence imports/writers until the bounded DOM run ends.

    This single-laptop adapter holds an IMMEDIATE transaction for <=45 seconds.
    A production connector must replace this fence with destination-side versions.
    No SQLite write is sent to the ERP by this consumer.
    """
    owner = uuid4().hex
    action_id = None
    with engine.lock:
        engine.db.execute('BEGIN IMMEDIATE')
        try:
            row = engine.db.execute("SELECT action_id FROM execution_jobs WHERE state='queued' OR (state='running' AND lease_until<?) ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                engine.db.commit()
                return None
            action_id = row[0]
            engine.db.execute("UPDATE execution_jobs SET state='running',attempts=attempts+1,owner=?,lease_until=?,error=NULL WHERE action_id=?", (owner, time.time()+90, action_id))
            engine.db.commit()
            engine.db.execute('BEGIN IMMEDIATE')
            action = engine._action(action_id)
            job = engine.db.execute('SELECT * FROM execution_jobs WHERE action_id=?', (action_id,)).fetchone()
            if job['owner'] != owner:
                raise ValueError('Worker lease changed')
            if action['snapshot'] != engine._meta('dataset', {}).get('snapshot') or action['status'] != 'approved':
                raise ValueError('Action is stale or no longer approved; stage and approve a fresh action')
            if engine.browser_erp is None or action.get('target_binding') != engine.browser_erp.binding() or fingerprint(intent(action)) != job['intent_hash']:
                raise ValueError('Approved target or payload changed; stage and approve a fresh action')
            result = browser(action, engine.browser_erp)
            receipt = result['receipt']
            # Validate even injected/test consumers; never accept a model's DONE.
            if receipt != engine.browser_erp.receipt(action_id) or receipt['intent_hash'] != job['intent_hash']:
                raise ValueError('Completion has no matching destination receipt')
            with engine.browser_erp.lock:
                engine.browser_erp.verify(receipt)
            engine.db.execute("UPDATE execution_jobs SET state='verified',receipt_json=?,lease_until=0 WHERE action_id=?", (canonical(receipt), action_id))
            engine.db.execute("UPDATE actions SET status='executed' WHERE action_id=?", (action_id,))
            engine._log('browser_erp_verified', {'action_id': action_id, 'receipt': receipt, 'dom_trace': result.get('trace', []), 'recovered': result.get('recovered', False)})
            engine.db.commit()
            return {'action_id': action_id, 'state': 'verified', 'receipt': receipt}
        except Exception:
            engine.db.rollback()
            if action_id is None:
                raise
            with engine.db:
                engine.db.execute("UPDATE execution_jobs SET state='failed',lease_until=0,error=? WHERE action_id=? AND owner=?", ('Completion not verified; ERP may have applied it. Retry checks its receipt, or restage changed evidence.', action_id, owner))
                engine._log('browser_erp_failed', {'action_id': action_id, 'completion_verified': False})
            return {'action_id': action_id, 'state': 'failed'}
        except BaseException:
            engine.db.rollback()
            raise


def retry(engine, action):
    if action['status'] != 'approved' or action['snapshot'] != engine._meta('dataset', {}).get('snapshot'):
        raise ValueError('Current, approved action required for retry')
    if engine.browser_erp is None or action.get('target_binding') != engine.browser_erp.binding():
        raise ValueError('ERP target changed; fresh approval required')
    enqueue(engine, action)
    engine.db.execute("UPDATE execution_jobs SET state='queued',error=NULL WHERE action_id=? AND state='failed'", (action['action_id'],))


class BrowserWorker:
    def __init__(self, engine):
        self.engine = engine
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.work, daemon=True)

    def work(self):
        while not self.stop.is_set():
            try:
                run_one(self.engine)
            except Exception:
                # A failed queue does not become a completed action.
                pass
            self.stop.wait(.5)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=50)
