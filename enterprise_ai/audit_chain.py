"""Tamper evidence for operational audit events; checkpoint outside the database.

This detects edits against a retained chain/checkpoint. It cannot authenticate
pre-migration history or stop an administrator rewriting the entire database.
Resolution evaluations and labels remain separate, exported evidence tables.
"""
import hashlib
import json
from uuid import uuid4

VERSION = "operational-audit-chain-v1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _marker(db):
    row = db.execute("SELECT value FROM metadata WHERE key='audit_chain'").fetchone()
    return json.loads(row[0]) if row else None


def _save(db, marker):
    db.execute("INSERT OR REPLACE INTO metadata VALUES('audit_chain',?)", (json.dumps(marker),))


def _event_hash(row, previous, marker):
    return _hash({"version": VERSION, "chain_id": marker["chain_id"], "tenant_id": marker["tenant_id"],
                  "previous_hash": previous, "sequence": row["sequence"], "timestamp": row["timestamp"],
                  "event": row["event"], "payload": json.loads(row["payload"])})


def migrate(db, tenant_id):
    db.execute("""CREATE TABLE IF NOT EXISTS audit_chain(
        sequence INTEGER PRIMARY KEY REFERENCES audit(sequence), previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL)""")
    marker = _marker(db)
    if marker:
        if marker["tenant_id"] != tenant_id:
            raise ValueError("Audit chain belongs to another tenant")
        verify(db)
        return
    if db.execute("SELECT 1 FROM audit_chain LIMIT 1").fetchone():
        raise ValueError("Audit chain metadata is missing")
    marker = {"version": VERSION, "chain_id": uuid4().hex, "tenant_id": tenant_id,
              "legacy_events": 0, "sequence": 0, "event_count": 0}
    marker["genesis_hash"] = _hash({k: marker[k] for k in ("version", "chain_id", "tenant_id")})
    marker["head_hash"] = marker["genesis_hash"]
    for row in db.execute("SELECT * FROM audit ORDER BY sequence"):
        _link(db, row, marker)
        marker["legacy_events"] += 1
    _save(db, marker)


def _link(db, row, marker):
    previous = marker["head_hash"]
    head = _event_hash(row, previous, marker)
    db.execute("INSERT INTO audit_chain VALUES(?,?,?)", (row["sequence"], previous, head))
    marker.update(sequence=row["sequence"], head_hash=head, event_count=marker["event_count"]+1)


def append(db, timestamp, event, payload):
    marker = _marker(db)
    if not marker:
        raise ValueError("Audit chain is not initialized")
    # Validate the head and counts before appending; full history is verified
    # at startup, explicit verification and audit export.
    last = db.execute("SELECT sequence,event_hash FROM audit_chain ORDER BY sequence DESC LIMIT 1").fetchone()
    count = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    if count != marker["event_count"] or (last and (last["sequence"] != marker["sequence"] or last["event_hash"] != marker["head_hash"])):
        raise ValueError("Audit chain head changed")
    cursor = db.execute("INSERT INTO audit(timestamp,event,payload) VALUES(?,?,?)", (timestamp, event, json.dumps(payload)))
    row = {"sequence": cursor.lastrowid, "timestamp": timestamp, "event": event, "payload": json.dumps(payload)}
    _link(db, row, marker)
    _save(db, marker)


def verify(db, checkpoint=None):
    if checkpoint is not None and (not isinstance(checkpoint, dict)
            or not isinstance(checkpoint.get("sequence"), int) or isinstance(checkpoint.get("sequence"), bool)
            or checkpoint["sequence"] < 0 or not isinstance(checkpoint.get("chain_id"), str)
            or not isinstance(checkpoint.get("head_hash"), str)):
        raise ValueError("Retained checkpoint has an invalid structure")
    marker = _marker(db)
    if not marker or marker.get("version") != VERSION:
        raise ValueError("Audit chain metadata is invalid")
    previous, count, sequence = marker["genesis_hash"], 0, 0
    checkpoint_found = checkpoint is None or checkpoint.get("sequence") == 0
    for row in db.execute("""SELECT a.*,c.previous_hash,c.event_hash FROM audit a
                             LEFT JOIN audit_chain c USING(sequence) ORDER BY a.sequence"""):
        head = _event_hash(row, previous, marker)
        if row["previous_hash"] != previous or row["event_hash"] != head:
            raise ValueError(f"Audit chain verification failed at sequence {row['sequence']}")
        count += 1
        sequence, previous = row["sequence"], head
        if checkpoint and sequence == checkpoint.get("sequence"):
            checkpoint_found = True
            if checkpoint.get("chain_id") != marker["chain_id"] or checkpoint.get("head_hash") != head:
                raise ValueError("Retained audit checkpoint differs from the chain")
    if count != marker["event_count"] or sequence != marker["sequence"] or previous != marker["head_hash"]:
        raise ValueError("Audit chain tail or count changed")
    if db.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0] != count:
        raise ValueError("Audit chain contains orphan entries")
    if checkpoint and (not checkpoint_found or checkpoint.get("chain_id") != marker["chain_id"] or checkpoint.get("sequence", -1) > sequence
                       or checkpoint.get("sequence", -1) < 0
                       or (checkpoint.get("sequence") == 0 and checkpoint.get("head_hash") != marker["genesis_hash"])):
        raise ValueError("Retained audit checkpoint is not part of this chain")
    return {**marker, "status": "verified", "scope": "operational audit events only",
            "limitation": "Legacy events are anchored as found, not historically authenticated. Retain checkpoints outside this database to detect wholesale rewrites."}
