"""Transactional approved intents; transport and reconciliation stay in adapters.

The caller owns the SQLite transaction. An interrupted DISPATCHING action must
be reconciled at its destination, never automatically returned to QUEUED.
This gives durable intent deduplication, NOT exactly-once remote effects.
"""
from .canonical import canonical, fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_actions (
  idempotency_key TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  target_action TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK(state IN ('QUEUED','DISPATCHING','SUCCEEDED','UNKNOWN','FAILED')),
  receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json))
);
CREATE TRIGGER IF NOT EXISTS outbox_intent_immutable
BEFORE UPDATE OF idempotency_key,subject_id,target_action,payload_json,
payload_sha256,approved_by,approved_at ON outbox_actions
BEGIN SELECT RAISE(ABORT, 'approved intent is immutable'); END;
"""


def enqueue(db, subject_id, target_action, payload, approved_digest, approved_by, approved_at):
    if not all(isinstance(x, str) and x.strip() for x in (subject_id, target_action, approved_by, approved_at)):
        raise ValueError('Subject, target and human approval are required')
    digest = fingerprint(payload)
    if approved_digest != digest:
        raise ValueError('Payload changed since review')
    key = fingerprint({'version': 1, 'subject_id': subject_id,
                       'target_action': target_action, 'payload_sha256': digest})
    db.execute('''INSERT OR IGNORE INTO outbox_actions
        (idempotency_key,subject_id,target_action,payload_json,payload_sha256,approved_by,approved_at)
        VALUES(?,?,?,?,?,?,?)''',
        (key, subject_id, target_action, canonical(payload), digest, approved_by, approved_at))
    return key


def claim(db, key):
    return db.execute("UPDATE outbox_actions SET state='DISPATCHING' WHERE idempotency_key=? AND state='QUEUED'", (key,)).rowcount == 1


def finish(db, key, state, receipt=None):
    if state not in {'SUCCEEDED', 'UNKNOWN', 'FAILED'}:
        raise ValueError('Invalid dispatch outcome')
    if state == 'SUCCEEDED' and not receipt:
        raise ValueError('Success requires destination evidence')
    changed = db.execute('''UPDATE outbox_actions SET state=?,receipt_json=?
        WHERE idempotency_key=? AND state='DISPATCHING' ''',
        (state, canonical(receipt) if receipt is not None else None, key)).rowcount
    if not changed:
        raise ValueError('Only a claimed action can finish; reconcile interrupted actions explicitly')
