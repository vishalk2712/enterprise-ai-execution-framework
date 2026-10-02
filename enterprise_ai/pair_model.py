"""Optional supervised pair classifier. NumPy is needed for fitting, not inference.

The output is an uncalibrated estimate for the labeled candidate distribution.
It can route a pair to human review; it cannot authorize a merge or an action.
"""
import hashlib
import json
import math

from .calibration import sigmoid
from .matching import jaccard, levenshtein_similarity
from .normalization import ngrams

VERSION = "pair-logistic-v1"
FEATURE_SCHEMA = "pair-features-v2"
FEATURE_NAMES = (
    "name_levenshtein", "name_token_jaccard", "name_trigram_dice",
    "name_sorted_levenshtein", "address_jaccard", "address_present",
    "postcode_exact", "postcode_present", "same_country", "shared_tax_id",
    "shared_registration_id", "identifier_conflict",
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def feature_vector(evaluation, limit=256):
    """Derive bounded numeric features from recorded, normalized pair evidence."""
    features = evaluation["features"]
    left, right = evaluation["normalized_left"][:limit], evaluation["normalized_right"][:limit]
    a, b = set(ngrams(left, 3)), set(ngrams(right, 3))
    extra = {
        "name_token_jaccard": jaccard(left, right) or 0.0,
        "name_trigram_dice": 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0,
        "name_sorted_levenshtein": levenshtein_similarity(" ".join(sorted(left.split())), " ".join(sorted(right.split()))),
        "address_present": float(features.get("address_jaccard") is not None),
        "postcode_present": float(features.get("postcode_exact") is not None),
    }
    return validate_features({name: extra.get(name, features.get(name)) or 0.0 for name in FEATURE_NAMES})


def validate_features(features):
    if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
        raise ValueError("Exact pair feature schema required")
    if not all(finite_number(v) and 0 <= v <= 1 for v in features.values()):
        raise ValueError("Pair features must be finite numbers in [0,1]")
    return features


def validate_artifact(artifact, config_id, domain):
    if not isinstance(artifact, dict) or artifact.get("version") != VERSION or artifact.get("feature_schema") != FEATURE_SCHEMA:
        raise ValueError("Unsupported pair model or feature schema")
    if artifact.get("config_id") != config_id or artifact.get("domain") != domain:
        raise ValueError("Pair model domain or matching configuration mismatch")
    if artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("Pair model feature order mismatch")
    weights = artifact.get("weights")
    if not isinstance(weights, list) or len(weights) != len(FEATURE_NAMES) or not all(finite_number(w) for w in weights) or not finite_number(artifact.get("intercept")):
        raise ValueError("Pair model coefficients must be finite numbers")
    threshold = artifact.get("review_threshold")
    if not finite_number(threshold) or not 0 <= threshold <= 1:
        raise ValueError("Invalid pair model review threshold")
    calibration = artifact.get("calibration")
    if calibration is not None:
        if (not isinstance(calibration, dict) or calibration.get("method") != "heldout-platt-v1"
                or not finite_number(calibration.get("slope")) or calibration["slope"] < 0
                or not finite_number(calibration.get("intercept")) or calibration.get("rows", 0) < 10
                or artifact.get("probability_status") != "calibrated_pair_estimate"):
            raise ValueError("Invalid held-out pair calibration")
    elif artifact.get("probability_status") == "calibrated_pair_estimate":
        raise ValueError("Calibrated status requires a separate calibration artifact")
    try:
        expected = fingerprint({k: v for k, v in artifact.items() if k != "model_id"})
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid pair model JSON") from exc
    if artifact.get("model_id") != expected:
        raise ValueError("Pair model fingerprint mismatch")


def predict(features, artifact):
    """Use an artifact already checked by validate_artifact at the boundary."""
    validate_features(features)
    logit = artifact["intercept"] + sum(features[n] * w for n, w in zip(FEATURE_NAMES, artifact["weights"]))
    calibration = artifact.get("calibration")
    if calibration:
        logit = calibration["intercept"] + calibration["slope"] * max(-20, min(20, logit))
    return sigmoid(logit)


def partitions_for(rows, config_id, calibrate=False):
    partitions = {s: [] for s in (("train", "validation", "calibration", "test") if calibrate else ("train", "validation", "test"))}
    seen_evaluations, seen_pairs, group_split, record_split = set(), set(), {}, {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each label must be a JSON object")
        if row.get("human_label") not in {"Match", "NonMatch"}:
            raise ValueError("Verified Match/NonMatch labels required; algorithm outcomes are not labels")
        if row.get("config_id") != config_id or row.get("split") not in partitions or row.get("feature_schema") != FEATURE_SCHEMA:
            raise ValueError("Exact config, feature schema and explicit split required")
        validate_features(row.get("model_features"))
        groups = row.get("entity_group_ids")
        if not isinstance(groups, list) or not groups or not all(isinstance(g, str) and g.strip() for g in groups):
            raise ValueError("Connected entity_group_ids for both endpoints are required")
        for group in groups:
            if group in group_split and group_split[group] != row["split"]:
                raise ValueError("Entity group leakage across splits")
            group_split[group] = row["split"]
        record_keys = row.get("record_keys")
        if not isinstance(record_keys, list) or len(record_keys) != 2 or not all(isinstance(k, str) and k for k in record_keys) or len(set(record_keys)) != 2:
            raise ValueError("Two distinct endpoint record_keys required")
        if row.get("pair_key") != fingerprint(sorted(record_keys)):
            raise ValueError("pair_key must fingerprint its endpoint record_keys")
        for key in record_keys:
            if key in record_split and record_split[key] != row["split"]:
                raise ValueError("Endpoint record leakage across splits")
            record_split[key] = row["split"]
        for key, seen in (("evaluation_id", seen_evaluations), ("pair_key", seen_pairs)):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip() or value in seen:
                raise ValueError(f"Unique {key} required; deduplicate repeated pairs across runs")
            seen.add(value)
        partitions[row["split"]].append(row)
    for split, subset in partitions.items():
        if len(subset) < 10 or {r["human_label"] for r in subset} != {"Match", "NonMatch"}:
            raise ValueError(f"{split} needs at least 10 rows and both classes; this minimum does not establish statistical adequacy")
    return partitions


def classification(labels, probabilities, threshold, total_positives=None):
    tp = sum(y == 1 and p >= threshold for y, p in zip(labels, probabilities))
    fp = sum(y == 0 and p >= threshold for y, p in zip(labels, probabilities))
    positives = sum(labels) if total_positives is None else total_positives
    return {"true_positives": tp, "false_positives": fp, "false_negatives": positives - tp,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / positives if positives else None,
            "review_pairs": tp + fp}


def select_threshold(labels, probabilities, target_precision):
    """Sweep tied scores together; choose maximum validation recall at target."""
    tp, fp, best = 0, 0, None
    ordered = sorted(zip(probabilities, labels), reverse=True)
    index = 0
    while index < len(ordered):
        score = ordered[index][0]
        while index < len(ordered) and ordered[index][0] == score:
            tp += ordered[index][1]
            fp += 1 - ordered[index][1]
            index += 1
        precision = tp / (tp + fp)
        if precision >= target_precision and (best is None or (tp, precision, score) > best[:3]):
            best = (tp, precision, score)
    # A score of 1 cannot be reached by our clipped sigmoid. Explicitly report
    # zero selections when no validation threshold meets the policy.
    return best[2] if best else 1.0


def diagnostics(rows, probabilities, threshold):
    labels = [int(r["human_label"] == "Match") for r in rows]
    return {"rows": len(rows), "positives": sum(labels),
            "brier_score": sum((p-y)**2 for p, y in zip(probabilities, labels)) / len(rows),
            **classification(labels, probabilities, threshold)}


def reliability(rows, probabilities):
    labels = [int(r["human_label"] == "Match") for r in rows]
    bins = []
    for index in range(10):
        selected = [(p, y) for p, y in zip(probabilities, labels) if min(9, int(p * 10)) == index]
        bins.append({"lower": index / 10, "upper": (index + 1) / 10, "rows": len(selected),
                     "mean_probability": sum(p for p, _ in selected) / len(selected) if selected else None,
                     "observed_match_fraction": sum(y for _, y in selected) / len(selected) if selected else None})
    return bins


def fit_pair_model(rows, config_id, domain, target_precision=.95, calibrate=False):
    if not isinstance(domain, str) or not domain.strip() or not isinstance(config_id, str) or not config_id:
        raise ValueError("Domain and exact matching config ID required")
    if not finite_number(target_precision) or not 0 < target_precision <= 1:
        raise ValueError("Target validation precision must be in (0,1]")
    partitions = partitions_for(rows, config_id, calibrate)
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError("Training requires NumPy: python -m pip install -r requirements-ml.txt") from exc
    matrices = {s: np.asarray([[r["model_features"][n] for n in FEATURE_NAMES] for r in subset], dtype=float) for s, subset in partitions.items()}
    labels = {s: np.asarray([int(r["human_label"] == "Match") for r in subset], dtype=float) for s, subset in partitions.items()}
    x, y = matrices["train"], labels["train"]
    candidates = []
    for penalty in (.0001, .001, .01):
        weights = np.zeros(len(FEATURE_NAMES))
        intercept = math.log(float(y.mean()) / (1 - float(y.mean())))
        for _ in range(1800):
            errors = 1 / (1 + np.exp(-np.clip(x @ weights + intercept, -40, 40))) - y
            weights -= (x.T @ errors) / len(y) + penalty * weights
            intercept -= float(errors.mean())
        # Evaluate with the same scalar arithmetic as dependency-free inference.
        # BLAS rounding at an exactly tied cutoff must not change review routing.
        scalar_model = {"weights": weights.tolist(), "intercept": intercept}
        predictions = [predict(row["model_features"], scalar_model) for row in partitions["validation"]]
        threshold = select_threshold(labels["validation"].tolist(), predictions, target_precision)
        validation = diagnostics(partitions["validation"], predictions, threshold)
        # Validation alone selects weights and threshold. Test is never used.
        key = (validation["recall"] if validation["review_pairs"] else -1, -validation["brier_score"])
        candidates.append((key, weights, intercept, penalty, threshold, validation))
    _, weights, intercept, penalty, threshold, validation = max(candidates, key=lambda item: item[0])
    scalar_model = {"weights": weights.tolist(), "intercept": intercept}
    test_predictions = [predict(row["model_features"], scalar_model) for row in partitions["test"]]
    artifact = {"version": VERSION, "feature_schema": FEATURE_SCHEMA, "feature_names": list(FEATURE_NAMES),
                "config_id": config_id, "domain": domain.strip(), "weights": weights.tolist(), "intercept": intercept,
                "regularization": penalty, "iterations": 1800, "review_threshold": threshold,
                "target_validation_precision": target_precision, "training_rows": len(y),
                "validation": validation, "test": diagnostics(partitions["test"], test_predictions, threshold),
                "labels_sha256": fingerprint(rows), "probability_status": "model_estimate",
                "limitations": "Uncalibrated estimate for the labeled candidate distribution. Validation precision is observed, not guaranteed. Entity groups must be correct. No auto-merges or action authorization; person benchmarks do not establish supplier accuracy."}
    if calibrate:
        # Fixed monotone Platt fit on its own partition, after weight selection.
        # Neither calibration parameters nor thresholds are selected on test.
        subset = partitions["calibration"]
        z = np.asarray([max(-20, min(20, scalar_model["intercept"] + sum(r["model_features"][n] * w for n, w in zip(FEATURE_NAMES, scalar_model["weights"])))) for r in subset])
        cy = labels["calibration"]
        slope, offset = 1.0, 0.0
        for _ in range(3000):
            errors = 1 / (1 + np.exp(-np.clip(offset + slope * z, -40, 40))) - cy
            slope = max(0.0, slope - .01 * float((errors * z).mean()))
            offset -= .01 * float(errors.mean())
        artifact.update(calibration={"method": "heldout-platt-v1", "slope": slope, "intercept": offset,
                                     "rows": len(subset), "labels_sha256": fingerprint(subset)},
                        probability_status="calibrated_pair_estimate", review_threshold=.75,
                        policy={"review_floor": .75, "auto_floor": .99, "requires_direct_identity": True},
                        limitations="Calibrated on reviewer-selected candidate pairs, not the full supplier population. Selection bias and domain drift remain. A .99 estimate does not guarantee 99% precision. No model-only merges or action authorization. Repeated test evaluations are monitoring, not fresh promotion evidence.")
        for split in ("validation", "calibration", "test"):
            probabilities = [predict(r["model_features"], artifact) for r in partitions[split]]
            artifact["calibration_evaluation" if split == "calibration" else split] = {**diagnostics(partitions[split], probabilities, .75),
                               "at_auto_floor": diagnostics(partitions[split], probabilities, .99),
                               "reliability_bins": reliability(partitions[split], probabilities)}
    artifact["model_id"] = fingerprint(artifact)
    validate_artifact(artifact, config_id, domain.strip())
    return artifact


def apply_model(evaluations, artifact, config):
    validate_artifact(artifact, config.config_id, "supplier")
    for item in evaluations:
        features = feature_vector(item, config.max_feature_chars)
        estimate = predict(features, artifact)
        item.update(model_features=features, feature_schema=FEATURE_SCHEMA, match_probability=estimate,
                    probability_status=artifact.get("probability_status", "model_estimate"), matching_model_id=artifact["model_id"],
                    heuristic_outcome=item["algorithmic_outcome"], model_review_threshold=artifact["review_threshold"])
        if item["algorithmic_outcome"] in {"Review_Candidate", "Below_Threshold"}:
            item["algorithmic_outcome"] = "Review_Candidate" if estimate >= artifact["review_threshold"] else "Below_Threshold"
