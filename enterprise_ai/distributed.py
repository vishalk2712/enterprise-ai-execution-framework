"""Short coordinator transactions; workers use HTTP, never a shared SQLite file."""
from contextlib import contextmanager
import json
import secrets
import threading
import time
from uuid import uuid4
from .mock_erp import fingerprint, intent
from .api_adapter import expected_receipt
from .runtime_clock import LeaseClock

INVESTIGATION = 'ERRORED_REQUIRES_INVESTIGATION'


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS dispatch_outbox(
        action_id TEXT PRIMARY KEY REFERENCES execution_jobs(action_id),
        epoch INTEGER NOT NULL DEFAULT 1,published_epoch INTEGER NOT NULL DEFAULT 0,last_published REAL NOT NULL DEFAULT 0);
    ''')
    if 'last_published' not in {row[1] for row in db.execute('PRAGMA table_info(dispatch_outbox)')}:
        db.execute('ALTER TABLE dispatch_outbox ADD COLUMN last_published REAL NOT NULL DEFAULT 0')
    db.executescript('''CREATE TABLE IF NOT EXISTS execution_dead_letters(
        action_id TEXT PRIMARY KEY REFERENCES actions(action_id),intent_hash TEXT NOT NULL,
        epoch INTEGER NOT NULL,attempts INTEGER NOT NULL,reason_code TEXT NOT NULL,
        created_at REAL NOT NULL,published_at REAL NOT NULL DEFAULT 0,resolution TEXT);
        CREATE TABLE IF NOT EXISTS execution_tombstones(
        action_id TEXT PRIMARY KEY REFERENCES actions(action_id),intent_hash TEXT NOT NULL,
        attempts INTEGER NOT NULL,archive_id TEXT NOT NULL,archived_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS maintenance_archives(
        archive_id TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL,job_count INTEGER NOT NULL,created_at REAL NOT NULL);
    ''')
    if 'completed_at' not in {r[1] for r in db.execute('PRAGMA table_info(execution_jobs)')}:
        db.execute('ALTER TABLE execution_jobs ADD COLUMN completed_at REAL NOT NULL DEFAULT 0')
        db.execute("UPDATE execution_jobs SET completed_at=? WHERE state='verified'", (time.time(),))
    db.execute('CREATE INDEX IF NOT EXISTS execution_retention ON execution_jobs(state,completed_at)')
    db.execute('CREATE INDEX IF NOT EXISTS dead_letter_dispatch ON execution_dead_letters(resolution,published_at)')


def queue(engine, action_id, retry=False):
    engine.db.execute('INSERT OR IGNORE INTO dispatch_outbox(action_id) VALUES(?)', (action_id,))
    if retry:
        engine.db.execute('UPDATE dispatch_outbox SET epoch=epoch+1 WHERE action_id=?', (action_id,))


class Coordinator:
    def __init__(self, engine, token, broker=None, max_attempts=3, archive_dir=None, retention_days=90):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('Worker token must contain at least 32 characters')
        self.engine, self.token, self.broker = engine, token, broker
        if not isinstance(max_attempts,int) or not 1<=max_attempts<=10: raise ValueError('Invalid attempt budget')
        if not 30<=retention_days<=3650: raise ValueError('Retention must be 30..3650 days')
        self.max_attempts, self.leases = max_attempts, LeaseClock()
        self.archive_dir, self.retention_days = archive_dir, retention_days
        self.maintenance_error = None
        self.stop = threading.Event()
        self.thread = None
        engine.coordinator = self
        engine.browser_erp.authorization_guard = self.guard

    def authorized(self, header):
        return isinstance(header, str) and secrets.compare_digest(header, 'Bearer '+self.token)

    def identity(self):
        return {'namespace': self.engine._meta('coordinator_id'), 'tenant_id': self.engine.tenant_id,
                'delivery': 'redis_streams' if self.broker else 'coordinator_poll', 'max_attempts': self.max_attempts}

    def publish(self):
        if not self.broker: return 0
        with self.engine.lock:
            rows = [dict(r) for r in self.engine.db.execute('''SELECT o.*,j.intent_hash,j.state,j.owner,j.lease_until FROM dispatch_outbox o
               JOIN execution_jobs j USING(action_id) WHERE (o.epoch>o.published_epoch OR o.last_published<?)
               AND j.state IN ('queued','running')
               ORDER BY o.last_published,o.rowid LIMIT 100''', (time.time()-30,))]
            rows = [r for r in rows if r.get('state')!='running' or not self.leases.live(r)][:8]
        delivered = 0
        for row in rows:
            if self.stop.is_set(): break
            # Broker failure leaves the transactional outbox unpublished.
            self.broker.publish(row['action_id'], row['intent_hash'], row['epoch'])
            with self.engine.lock, self.engine.db:
                self.engine.db.execute('UPDATE dispatch_outbox SET published_epoch=?,last_published=? WHERE action_id=? AND epoch=?', (row['epoch'], time.time(), row['action_id'], row['epoch']))
            delivered += 1
        with self.engine.lock:
            dead = [dict(r) for r in self.engine.db.execute('SELECT * FROM execution_dead_letters WHERE resolution IS NULL AND published_at<? LIMIT 8', (time.time()-30,))]
        for row in dead:
            self.broker.dead_letter(self.dead_reference(row))
            with self.engine.lock, self.engine.db:
                self.engine.db.execute('UPDATE execution_dead_letters SET published_at=? WHERE action_id=?', (time.time(),row['action_id']))
        return delivered

    def start(self):
        if self.broker: self.broker.initialize()
        if not self.broker and not self.archive_dir: return
        def pump():
            next_maintenance = time.monotonic()
            while not self.stop.is_set():
                try: self.publish()
                except (ValueError, OSError): pass  # Durable outbox retries after transient broker failure.
                if self.archive_dir and time.monotonic()>=next_maintenance:
                    try:
                        from .maintenance import compact
                        compact(self.engine, self.archive_dir, self.retention_days)
                        self.maintenance_error = None
                    except Exception: self.maintenance_error = 'Maintenance failed; source records retained'
                    next_maintenance = time.monotonic()+3600
                self.stop.wait(.5)
        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=40)

    @staticmethod
    def dead_reference(row):
        return {k:str(row[k]) for k in ('action_id','intent_hash','epoch','attempts','reason_code')}

    def dead_letter(self, row):
        e = self.engine
        epoch = e.db.execute('SELECT epoch FROM dispatch_outbox WHERE action_id=?', (row['action_id'],)).fetchone()
        e.db.execute("UPDATE execution_jobs SET state=?,lease_until=0,error='Attempt budget exhausted; investigate destination before replay.' WHERE action_id=?", (INVESTIGATION,row['action_id']))
        e.db.execute('INSERT OR IGNORE INTO execution_dead_letters(action_id,intent_hash,epoch,attempts,reason_code,created_at) VALUES(?,?,?,?,?,?)',
                     (row['action_id'],row['intent_hash'],epoch[0] if epoch else 1,row['attempts'],'ATTEMPT_BUDGET_EXHAUSTED',time.time()))
        self.leases.revoke(row['owner'])
        e._log(INVESTIGATION, {'action_id':row['action_id'],'attempts':row['attempts'],'reason_code':'ATTEMPT_BUDGET_EXHAUSTED'})
        dead = e.db.execute('SELECT * FROM execution_dead_letters WHERE action_id=?', (row['action_id'],)).fetchone()
        return {'state':INVESTIGATION,'reference':self.dead_reference(dead)}

    def investigations(self):
        with self.engine.lock:
            return {'jobs':[dict(r) for r in self.engine.db.execute('SELECT * FROM execution_dead_letters ORDER BY created_at DESC LIMIT 100')],
                    'maintenance_error':self.maintenance_error,'max_attempts':self.max_attempts}

    def investigate(self, action_id, actor, reason):
        if not isinstance(reason,str) or not 10<=len(reason)<=1000: raise ValueError('Investigation note must contain 10..1000 characters')
        e = self.engine
        with e.lock, e.db:
            row = e.db.execute('SELECT * FROM execution_dead_letters WHERE action_id=?', (action_id,)).fetchone()
            if not row: raise ValueError('Investigation not found')
            action = e._action(action_id)
            with e.browser_erp.lock:
                receipt = e.browser_erp.receipt(action_id)
                if receipt:
                    if receipt!=expected_receipt(action): raise ValueError('Destination receipt differs from approved intent')
                    e.browser_erp.verify(receipt)
                    e.db.execute("UPDATE execution_jobs SET state='verified',receipt_json=?,completed_at=? WHERE action_id=?", (json.dumps(receipt),time.time(),action_id))
                    e.db.execute("UPDATE actions SET status='executed' WHERE action_id=?", (action_id,))
            resolution = 'VERIFIED_EXISTING_RECEIPT' if receipt else 'INVESTIGATED_NO_RECEIPT'
            e.db.execute('UPDATE execution_dead_letters SET resolution=? WHERE action_id=?', (resolution,action_id))
            e._log('execution_investigated', {'action_id':action_id,'actor':actor,'reason':reason,'resolution':resolution})
            return {'resolution':resolution,'action_id':action_id}

    def claim(self, reference=None):
        e = self.engine
        if reference is not None and (not isinstance(reference, dict) or set(reference) != {'action_id', 'intent_hash', 'epoch'}
                                      or any(not isinstance(reference[k], (str, int)) for k in reference)):
            raise ValueError('Invalid broker job reference')
        with e.lock:
            e.db.execute('BEGIN IMMEDIATE')
            try:
                if reference is not None:
                    row = e.db.execute('SELECT j.*,o.epoch FROM execution_jobs j JOIN dispatch_outbox o USING(action_id) WHERE action_id=?', (reference['action_id'],)).fetchone()
                    if not row or row['intent_hash'] != reference['intent_hash'] or str(row['epoch']) != str(reference['epoch']):
                        e.db.commit()
                        return {'state': 'obsolete'}
                else:
                    row = next((r for r in e.db.execute("SELECT * FROM execution_jobs WHERE state IN ('queued','running') ORDER BY rowid") if r['state']=='queued' or not self.leases.live(r)), None)
                    if not row:
                        e.db.commit()
                        return {'state': 'empty'}
                action = e._action(row['action_id'])
                if row['state']==INVESTIGATION:
                    dead = e.db.execute('SELECT * FROM execution_dead_letters WHERE action_id=?',(row['action_id'],)).fetchone()
                    e.db.commit()
                    return {'state':INVESTIGATION,'reference':self.dead_reference(dead)}
                if row['state'] in {'verified', 'failed'}:
                    e.db.commit()
                    return {'state': 'terminal'}
                if row['state'] == 'running' and self.leases.live(row):
                    e.db.commit()
                    return {'state': 'busy'}
                if row['attempts']>=self.max_attempts:
                    result = self.dead_letter(row)
                    e.db.commit()
                    return result
                if (action['status'] != 'approved' or action['snapshot'] != e._meta('dataset', {}).get('snapshot')
                        or action.get('target_binding') != e.browser_erp.binding() or fingerprint(intent(action)) != row['intent_hash']):
                    e.db.execute("UPDATE execution_jobs SET state='failed',error='Stale approval or changed destination; restage.' WHERE action_id=?", (action['action_id'],))
                    e.db.commit()
                    return {'state': 'terminal'}
                lease = uuid4().hex
                self.leases.revoke(row['owner'])
                self.leases.issue(lease,90)
                e.db.execute("UPDATE execution_jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1,error=NULL WHERE action_id=?", (lease, time.time()+90, action['action_id']))
                e._log('execution_grant_issued', {'action_id': action['action_id'], 'adapter': action['target_binding']['adapter']})
                e.db.commit()
                return {'state': 'granted', 'lease': lease, 'action': {**intent(action), 'target': action['target'], 'status': 'approved'},
                        'password': e.browser_erp.password, 'signature': e.browser_erp.sign(action, lease=lease)}
            except BaseException:
                e.db.rollback()
                raise

    @contextmanager
    def guard(self, action, signature):
        """Mock destination commit fence: short engine→ERP lock ordering, no I/O.

        The co-located mock checks active leases and snapshots atomically with
        imports. Real adapters need an equivalent destination version contract.
        """
        e = self.engine
        with e.lock:
            e.db.execute('BEGIN IMMEDIATE')
            try:
                stored = e._action(action['action_id'])
                row = e.db.execute('SELECT * FROM execution_jobs WHERE action_id=?', (action['action_id'],)).fetchone()
                parts = signature.split(':')
                if (not row or len(parts) != 4 or parts[2] != row['owner'] or row['state'] != 'running' or not self.leases.live(row) or stored['status'] != 'approved'
                        or stored['snapshot'] != e._meta('dataset', {}).get('snapshot') or fingerprint(intent(action)) != row['intent_hash']):
                    raise ValueError('Destination refused a revoked, expired or changed execution grant')
                yield
                e.db.commit()
            except BaseException:
                e.db.rollback()
                raise

    def finish(self, data, failed=False):
        if not isinstance(data.get('action_id'), str) or not isinstance(data.get('lease'), str):
            raise ValueError('Action ID and lease required')
        e = self.engine
        with e.lock, e.db:
            row = e.db.execute('SELECT * FROM execution_jobs WHERE action_id=?', (data['action_id'],)).fetchone()
            if not row or row['owner'] != data['lease']:
                raise ValueError('Worker lease no longer owns this job')
            if row['state'] == 'verified': return {'state': 'verified'}
            if row['state'] != 'running' or not self.leases.live(row):
                raise ValueError('Expired execution lease')
            if failed:
                if row['attempts']>=self.max_attempts: return self.dead_letter(row)
                self.leases.revoke(row['owner'])
                e.db.execute("UPDATE execution_jobs SET state='failed',lease_until=0,error='Completion not verified; retry checks destination receipt.' WHERE action_id=?", (data['action_id'],))
                e._log('detached_execution_failed', {'action_id': data['action_id']})
                return {'state': 'failed'}
            action = e._action(data['action_id'])
            if action.get('target_binding') != e.browser_erp.binding() or fingerprint(intent(action)) != row['intent_hash']:
                raise ValueError('Approved destination or payload changed')
            receipt = data.get('receipt')
            with e.browser_erp.lock:
                if receipt != expected_receipt(action) or receipt != e.browser_erp.receipt(action['action_id']):
                    raise ValueError('Worker completion does not match an authoritative destination receipt')
                e.browser_erp.verify(receipt)
            self.leases.revoke(row['owner'])
            e.db.execute("UPDATE execution_jobs SET state='verified',receipt_json=?,lease_until=0,completed_at=? WHERE action_id=?", (json.dumps(receipt), time.time(), action['action_id']))
            e.db.execute("UPDATE actions SET status='executed' WHERE action_id=?", (action['action_id'],))
            e._log('detached_execution_verified', {'action_id': action['action_id'], 'receipt': receipt, 'adapter': action['target_binding']['adapter'],
                                                  'superseded_snapshot': action['snapshot'] != e._meta('dataset', {}).get('snapshot')})
            return {'state': 'verified'}
