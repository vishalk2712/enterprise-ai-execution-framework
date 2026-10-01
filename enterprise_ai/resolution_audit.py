"""Append-only evaluation and review history, independent of live supplier rows."""
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS resolution_runs (
      run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, config_id TEXT NOT NULL,
      config_json TEXT NOT NULL, statistics_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pair_evaluations (
      evaluation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES resolution_runs(run_id),
      left_id TEXT NOT NULL, right_id TEXT NOT NULL, algorithmic_outcome TEXT NOT NULL,
      similarity_score REAL NOT NULL CHECK(similarity_score BETWEEN 0 AND 1),
      payload_json TEXT NOT NULL, UNIQUE(run_id,left_id,right_id), CHECK(left_id < right_id));
    CREATE TABLE IF NOT EXISTS review_decisions (
      decision_id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES pair_evaluations(evaluation_id),
      human_label TEXT NOT NULL CHECK(human_label IN ('Match','NonMatch','Unsure')),
      reviewer TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
      supersedes TEXT UNIQUE REFERENCES review_decisions(decision_id));
    CREATE INDEX IF NOT EXISTS evaluations_by_run ON pair_evaluations(run_id);
    CREATE INDEX IF NOT EXISTS decisions_by_evaluation ON review_decisions(evaluation_id);
    CREATE UNIQUE INDEX IF NOT EXISTS initial_decision ON review_decisions(evaluation_id) WHERE supersedes IS NULL;
    PRAGMA user_version=2;
    """)
    for table in ("resolution_runs", "pair_evaluations", "review_decisions"):
        for verb in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'Resolution history is append-only'); END")


def persist_run(db, snapshot_id, config, evaluations, statistics):
    run_id = "run-" + uuid4().hex
    db.execute("INSERT INTO resolution_runs VALUES(?,?,?,?,?,?)", (run_id, snapshot_id, config.config_id, config.canonical(), json.dumps(statistics), timestamp()))
    for item in evaluations:
        item["evaluation_id"] = "eval-" + uuid4().hex
        item["run_id"] = run_id
        db.execute("INSERT INTO pair_evaluations VALUES(?,?,?,?,?,?,?)", (item["evaluation_id"], run_id, item["left_id"], item["right_id"], item["algorithmic_outcome"], item["similarity_score"], json.dumps(item, ensure_ascii=False)))
    return run_id


def list_evaluations(db, run_id, limit=200, offset=0):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000 or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("limit must be 1..1000 and offset must be non-negative")
    rows = db.execute("SELECT payload_json FROM pair_evaluations WHERE run_id=? ORDER BY similarity_score DESC,left_id,right_id LIMIT ? OFFSET ?", (run_id, limit, offset))
    results = []
    for row in rows:
        item = json.loads(row[0])
        label = db.execute("SELECT * FROM review_decisions WHERE evaluation_id=? ORDER BY rowid DESC LIMIT 1", (item["evaluation_id"],)).fetchone()
        item["human_label"] = label["human_label"] if label else None
        item["latest_decision"] = dict(label) if label else None
        results.append(item)
    return {"evaluations": results, "run_id": run_id, "total": db.execute("SELECT COUNT(*) FROM pair_evaluations WHERE run_id=?", (run_id,)).fetchone()[0], "offset": offset, "limit": limit}


def record_decision(db, evaluation_id, human_label, reviewer, reason, supersedes=None):
    if not isinstance(human_label, str) or human_label not in {"Match", "NonMatch", "Unsure"}:
        raise ValueError("human_label must be Match, NonMatch or Unsure")
    if not all(isinstance(s, str) and s.strip() for s in (evaluation_id, reviewer, reason)) or len(reviewer) > 100 or len(reason) > 2000:
        raise ValueError("A reviewer (1..100 characters) and reason (1..2000) are required")
    if not db.execute("SELECT 1 FROM pair_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
        raise ValueError("Evaluation not found")
    latest = db.execute("SELECT decision_id FROM review_decisions WHERE evaluation_id=? ORDER BY rowid DESC LIMIT 1", (evaluation_id,)).fetchone()
    if (latest[0] if latest else None) != supersedes:
        raise ValueError("Review changed; supply the latest decision_id as supersedes")
    item = {"decision_id": "decision-" + uuid4().hex, "evaluation_id": evaluation_id, "human_label": human_label,
            "reviewer": reviewer.strip(), "reason": reason.strip(), "created_at": timestamp(), "supersedes": supersedes}
    try:
        db.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?,?)", tuple(item.values()))
    except sqlite3.IntegrityError as exc:
        raise ValueError("Concurrent review changed; refresh and retry") from exc
    return item


def export_history(db):
    for table in ("resolution_runs", "pair_evaluations", "review_decisions"):
        for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            yield json.dumps({"table": table, **dict(row)}, ensure_ascii=False)
