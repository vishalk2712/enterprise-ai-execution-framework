"""Transactional reviewer evidence and stable, entity-disjoint learning cohorts."""
from collections import defaultdict
import json
from pathlib import Path
import os
from tempfile import NamedTemporaryFile

from .pair_model import FEATURE_SCHEMA, feature_vector, fingerprint

RECORD_FIELDS = ("supplier_id", "name", "country", "registration_id", "tax_id", "postcode", "address", "aliases", "lei", "parent_lei", "bank_account_hash")
SPLITS = ("train", "validation", "calibration", "test")


def sample(evaluation, config, config_id, namespace="local"):
    records = [evaluation["left_record"], evaluation["right_record"]]
    keys = [fingerprint({k: row.get(k, "") for k in RECORD_FIELDS}) for row in records]
    return {"evaluation_id": evaluation["evaluation_id"], "config_id": config_id, "namespace": namespace,
            "left_id": evaluation["left_id"], "right_id": evaluation["right_id"],
            "record_keys": keys, "pair_key": fingerprint(sorted(keys)),
            "source_keys": [fingerprint([namespace, row["supplier_id"]]) for row in records],
            "feature_schema": FEATURE_SCHEMA, "model_features": feature_vector(evaluation, config["max_feature_chars"]),
            "algorithmic_outcome": evaluation["algorithmic_outcome"], "sampling_probability": evaluation["sampling_probability"]}


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS training_feedback (
      decision_id TEXT PRIMARY KEY REFERENCES review_decisions(decision_id),
      pair_key TEXT NOT NULL, config_id TEXT NOT NULL, namespace TEXT NOT NULL, payload_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS feedback_by_config ON training_feedback(config_id,namespace,pair_key);
    CREATE TRIGGER IF NOT EXISTS feedback_no_update BEFORE UPDATE ON training_feedback
      BEGIN SELECT RAISE(ABORT,'Training feedback is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS feedback_no_delete BEFORE DELETE ON training_feedback
      BEGIN SELECT RAISE(ABORT,'Training feedback is append-only'); END;
    """)
    # Existing decisions retain their provenance, without fabricating new labels.
    with db:
        old = [dict(row) for row in db.execute("SELECT d.* FROM review_decisions d LEFT JOIN training_feedback f USING(decision_id) WHERE f.decision_id IS NULL ORDER BY d.rowid")]
        for decision in old:
            record_feedback(db, decision)


def record_feedback(db, decision):
    row = db.execute("""SELECT e.payload_json,r.config_json,r.config_id,r.snapshot_id,r.statistics_json
      FROM pair_evaluations e JOIN resolution_runs r USING(run_id) WHERE e.evaluation_id=?""", (decision["evaluation_id"],)).fetchone()
    stats = json.loads(row["statistics_json"])
    item = sample(json.loads(row["payload_json"]), json.loads(row["config_json"]), row["config_id"], stats.get("dataset_namespace", "local"))
    item.update(decision_id=decision["decision_id"], human_label=decision["human_label"], reviewer=decision["reviewer"],
                reason=decision["reason"], snapshot_id=row["snapshot_id"], created_at=decision["created_at"], label_provenance="self_declared_local_reviewer")
    db.execute("INSERT INTO training_feedback VALUES(?,?,?,?,?)", (decision["decision_id"], item["pair_key"], item["config_id"], item["namespace"], json.dumps(item)))
    return item


def current_samples(db, config_id, namespace):
    rows = [json.loads(row[0]) for row in db.execute("""SELECT f.payload_json FROM training_feedback f JOIN review_decisions d USING(decision_id)
      WHERE f.config_id=? AND f.namespace=? AND d.rowid=(SELECT MAX(d2.rowid) FROM review_decisions d2 WHERE d2.evaluation_id=d.evaluation_id)
      ORDER BY d.rowid""", (config_id, namespace))]
    pairs = defaultdict(list)
    for row in rows:
        pairs[row["pair_key"]].append(row)
    selected, conflicting, unsure = [], [], 0
    for pair, history in sorted(pairs.items()):
        if history[-1]["human_label"] == "Unsure":
            unsure += 1
            continue
        if len({r["human_label"] for r in history if r["human_label"] != "Unsure"}) > 1:
            conflicting.append(pair)
            continue
        selected.append(history[-1])
    return selected, {"review_events": db.execute("SELECT COUNT(*) FROM training_feedback WHERE config_id=? AND namespace=?", (config_id, namespace)).fetchone()[0],
                      "eligible_pairs": len(selected), "unsure_pairs": unsure, "conflicting_pairs": conflicting}


def prepare_splits(rows, config_id, namespace, manifest=None):
    """Match components plus stable source IDs keep edited record versions together.

    Known monitoring holdouts are never reassigned to training on a later refit.
    Linking two existing splits is rejected instead of silently leaking entities.
    """
    if manifest is not None and (manifest.get("version") != "review-splits-v1" or manifest.get("config_id") != config_id or manifest.get("namespace") != namespace):
        raise ValueError("Review split manifest configuration or namespace mismatch")
    assignments = dict((manifest or {}).get("source_assignments", {}))
    if any(s not in SPLITS for s in assignments.values()):
        raise ValueError("Invalid existing review split")
    parent = {key: key for row in rows for key in row["source_keys"]}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for row in rows:
        if row["human_label"] == "Match":
            a, b = map(root, row["source_keys"])
            parent[max(a, b)] = min(a, b)
    components = defaultdict(list)
    for key in parent:
        components[root(key)].append(key)
    for members in components.values():
        known = {assignments[k] for k in members if k in assignments}
        if len(known) > 1:
            raise ValueError("A reviewed Match links existing holdout splits; establish a new disjoint evaluation campaign")
        bucket = int(fingerprint(sorted(members))[:8], 16) / 0x100000000
        split = next(iter(known)) if known else ("train" if bucket < .6 else "validation" if bucket < .75 else "calibration" if bucket < .85 else "test")
        for key in members:
            assignments[key] = split
    prepared, cross_split = [], 0
    for row in rows:
        a, b = row["source_keys"]
        if row["human_label"] == "NonMatch" and root(a) == root(b):
            raise ValueError("Contradictory labels: NonMatch inside a reviewed Match component")
        if assignments[a] != assignments[b]:
            cross_split += 1
            continue
        prepared.append({**row, "split": assignments[a], "entity_group_ids": sorted({root(a), root(b)})})
    counts = {s: {label: sum(r["split"] == s and r["human_label"] == label for r in prepared) for label in ("Match", "NonMatch")} for s in SPLITS}
    return prepared, {"version": "review-splits-v1", "config_id": config_id, "namespace": namespace, "source_assignments": assignments,
                      "test_evaluations": (manifest or {}).get("test_evaluations", 0)}, {"partition_counts": counts, "cross_split_pairs_excluded": cross_split}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, allow_nan=False)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_feedback(db, config_id, namespace, path):
    rows, stats = current_samples(db, config_id, namespace)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".parquet":
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ValueError("Parquet export requires requirements-feedback.txt; JSONL and SQLite need no extra packages") from exc
        with NamedTemporaryFile(dir=path.parent, suffix=".parquet", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            pq.write_table(pa.Table.from_pylist(rows) if rows else pa.table({"decision_id": pa.array([], type=pa.string())}), temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".jsonl", delete=False) as handle:
            temporary = Path(handle.name)
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**stats, "output": str(path)}


def train_from_reviews(db, config_id, namespace, output, split_manifest):
    from .pair_model import fit_pair_model
    output, split_manifest = Path(output), Path(split_manifest)
    if output.resolve() == split_manifest.resolve():
        raise ValueError("Model and split manifest need separate paths")
    rows, feedback_stats = current_samples(db, config_id, namespace)
    manifest = json.loads(split_manifest.read_text(encoding="utf-8")) if split_manifest.exists() else None
    prepared, pending, split_stats = prepare_splits(rows, config_id, namespace, manifest)
    artifact = fit_pair_model(prepared, config_id, "supplier", calibrate=True)
    pending["test_evaluations"] += 1
    artifact.update(feedback_provenance={**feedback_stats, **split_stats, "namespace": namespace,
                                       "test_evaluations": pending["test_evaluations"], "split_manifest_sha256": fingerprint(pending)})
    artifact["model_id"] = fingerprint({k: v for k, v in artifact.items() if k != "model_id"})
    # Persist the cohort lock before writing weights. If the latter fails, a retry
    # keeps the same holdouts; it cannot accidentally train on an evaluated test.
    atomic_json(split_manifest, pending)
    atomic_json(output, artifact)
    return {"model_id": artifact["model_id"], "output": str(output), "activated": False,
            "feedback": artifact["feedback_provenance"], "calibration": artifact["calibration"], "test": artifact["test"]}
