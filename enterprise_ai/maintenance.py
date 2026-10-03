"""Archive before compacting verified operational rows; retain approval/idempotency evidence."""
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4


def compact(engine, directory, retention_days=90, clock=None):
    if not isinstance(retention_days,int) or not 30<=retention_days<=3650: raise ValueError('Retention must be 30..3650 days')
    current = time.time() if clock is None else clock
    cutoff = current-retention_days*86400
    with engine.lock:
        jobs = [dict(r) for r in engine.db.execute("SELECT * FROM execution_jobs WHERE state='verified' AND completed_at>0 AND completed_at<? ORDER BY completed_at LIMIT 200",(cutoff,))]
        rows,outboxes = [],{}
        for job in jobs:
            rows.append({'table':'execution_jobs',**job})
            outbox = engine.db.execute('SELECT * FROM dispatch_outbox WHERE action_id=?',(job['action_id'],)).fetchone()
            outboxes[job['action_id']]=dict(outbox) if outbox else None
            if outbox: rows.append({'table':'dispatch_outbox',**dict(outbox)})
    if not jobs: return {'archived_jobs':0,'retention_days':retention_days}
    # File I/O is outside the coordinator lock. Never delete before durable export.
    data = ''.join(json.dumps(row,sort_keys=True)+'\n' for row in rows).encode()
    digest = hashlib.sha256(data).hexdigest()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    archive = directory/(digest+'.jsonl')
    if archive.exists():
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest: raise ValueError('Existing archive failed integrity check')
    else:
        pending = directory/(uuid4().hex+'.tmp')
        try:
            with pending.open('xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending,archive)
        finally:
            if pending.exists(): pending.unlink()
    archived = 0
    with engine.lock:
        engine.db.execute('BEGIN IMMEDIATE')
        try:
            # A changed row during export is retained for the next run.
            for job in jobs:
                present = engine.db.execute('SELECT * FROM execution_jobs WHERE action_id=?',(job['action_id'],)).fetchone()
                if not present or dict(present)!=job: continue
                outbox=engine.db.execute('SELECT * FROM dispatch_outbox WHERE action_id=?',(job['action_id'],)).fetchone()
                if (dict(outbox) if outbox else None)!=outboxes[job['action_id']]: continue
                engine.db.execute('INSERT OR IGNORE INTO execution_tombstones VALUES(?,?,?,?,?)',
                                  (job['action_id'],job['intent_hash'],job['attempts'],digest,current))
                engine.db.execute('DELETE FROM dispatch_outbox WHERE action_id=?',(job['action_id'],))
                engine.db.execute('DELETE FROM execution_jobs WHERE action_id=?',(job['action_id'],))
                archived += 1
            if archived:
                engine.db.execute('INSERT OR IGNORE INTO maintenance_archives VALUES(?,?,?,?,?)',(digest,str(archive),digest,archived,current))
                engine._log('operational_records_archived',{'archive_id':digest,'jobs':archived,'retention_days':retention_days})
            engine.db.commit()
        except BaseException:
            engine.db.rollback()
            raise
    return {'archived_jobs':archived,'archive_id':digest,'sha256':digest,'retention_days':retention_days}
