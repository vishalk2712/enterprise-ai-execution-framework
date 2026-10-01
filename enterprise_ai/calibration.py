"""Supervised probability calibration; never substitute algorithm outcomes for labels.

Platt-style logistic calibration of the fixed weighted score. Train/validation
and untouched test rows must be entity-group disjoint. No automatic deployment.
"""
import hashlib
import json
import math


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-40, min(40, x))))


def metrics(rows, slope, intercept):
    probabilities = [sigmoid(slope*r["similarity_score"]+intercept) for r in rows]
    labels = [int(r["human_label"] == "Match") for r in rows]
    brier = sum((p-y)**2 for p, y in zip(probabilities, labels)) / len(rows)
    bins = []
    for i in range(10):
        members = [(p, y) for p, y in zip(probabilities, labels) if min(int(p*10), 9) == i]
        if members:
            bins.append({"lower": i/10, "count": len(members), "predicted_mean": sum(p for p, _ in members)/len(members), "observed_match_rate": sum(y for _, y in members)/len(members)})
    return {"rows": len(rows), "positives": sum(labels), "brier_score": brier, "reliability_bins": bins,
            "ece": sum(b["count"]*abs(b["predicted_mean"]-b["observed_match_rate"]) for b in bins)/len(rows)}


def fit_calibration(rows, config_id, domain):
    if not isinstance(domain, str) or not domain.strip() or not config_id:
        raise ValueError("A domain and exact matching config ID are required")
    partitions = {s: [] for s in ("train", "validation", "test")}
    seen_pairs, group_split = set(), {}
    for row in rows:
        if row.get("human_label") not in {"Match", "NonMatch"}:
            raise ValueError("Use verified Match/NonMatch labels; Unsure and algorithm outcomes are not training labels")
        if row.get("config_id") != config_id or row.get("split") not in partitions:
            raise ValueError("All labels must share config_id and an explicit train/validation/test split")
        if not isinstance(row.get("similarity_score"), (int, float)) or not 0 <= row["similarity_score"] <= 1:
            raise ValueError("Invalid similarity_score")
        # Callers identify the connected entity component for BOTH endpoints.
        groups = row.get("entity_group_ids")
        if not isinstance(groups, list) or not groups or not all(isinstance(g, str) and g for g in groups):
            raise ValueError("entity_group_ids are required to check split leakage")
        for group in groups:
            if group in group_split and group_split[group] != row["split"]:
                raise ValueError("Entity group leakage across splits")
            group_split[group] = row["split"]
        pair = row.get("evaluation_id")
        if not isinstance(pair, str) or not pair or pair in seen_pairs:
            raise ValueError("Unique evaluation_id required")
        seen_pairs.add(pair)
        partitions[row["split"]].append(row)
    for split, subset in partitions.items():
        if len(subset) < 10 or {r["human_label"] for r in subset} != {"Match", "NonMatch"}:
            raise ValueError(f"{split} needs at least 10 labeled rows and both classes; this minimum is not evidence of sufficient sample size")
    candidates = []
    train = partitions["train"]
    for penalty in (.001, .01, .1):
        slope, intercept = 1.0, 0.0
        for _ in range(1500):
            errors = [(sigmoid(slope*r["similarity_score"]+intercept)-int(r["human_label"] == "Match"), r["similarity_score"]) for r in train]
            slope -= .5*(sum(e*x for e, x in errors)/len(train)+penalty*slope)
            intercept -= .5*sum(e for e, _ in errors)/len(train)
        validation = metrics(partitions["validation"], slope, intercept)
        candidates.append((validation["brier_score"], slope, intercept, penalty, validation))
    _, slope, intercept, penalty, validation = min(candidates, key=lambda c: c[0])
    artifact = {"version": "platt-v1", "config_id": config_id, "domain": domain, "slope": slope, "intercept": intercept,
                "regularization": penalty, "training_rows": len(train), "validation": validation,
                "test": metrics(partitions["test"], slope, intercept),
                "labels_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                "limitations": "Probability estimate for the labeled sampling distribution only. Diagnostic sampling is biased; do not deploy on suppliers based on person/product benchmarks. Labels never authorize writes."}
    artifact["model_id"] = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
    return artifact


def probability(score, artifact, config_id, domain):
    if not isinstance(artifact, dict) or artifact.get("version") != "platt-v1" or artifact.get("config_id") != config_id or artifact.get("domain") != domain:
        raise ValueError("Calibration domain or matching configuration mismatch")
    if not all(isinstance(artifact.get(k), (int, float)) and math.isfinite(artifact[k]) for k in ("slope", "intercept")):
        raise ValueError("Calibration coefficients must be finite numbers")
    return sigmoid(artifact["slope"]*score+artifact["intercept"])
