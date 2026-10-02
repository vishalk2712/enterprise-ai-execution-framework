"""SPIDER experiment on entities unused by v0.2; raw records are not bundled."""
import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
from itertools import combinations
import json
from pathlib import Path
import time

from .benchmarks import read_rows, record, source_hash
from .matching import MatchConfig, generate_candidates, score_pair
from .pair_model import FEATURE_SCHEMA, classification, feature_vector, fingerprint, fit_pair_model


def partition_cohort(groups, exclude_groups=2000, max_groups=3000, seed=1729):
    if exclude_groups < 2000 or max_groups < 50 or exclude_groups + max_groups > len(groups):
        raise ValueError("Exclude at least the original 2,000 groups and select at least 50 available unused groups")
    ordered = sorted(groups, key=lambda g: hashlib.sha256(f"{seed}:{g}".encode()).hexdigest())
    excluded = ordered[:exclude_groups]
    chosen = ordered[exclude_groups:exclude_groups + max_groups]
    split_order = sorted(chosen, key=lambda g: hashlib.sha256(f"model-split-v1:{seed}:{g}".encode()).hexdigest())
    train_end, validation_end = int(.6 * len(chosen)), int(.8 * len(chosen))
    return {"train": split_order[:train_end], "validation": split_order[train_end:validation_end],
            "test": split_order[validation_end:]}, excluded


def run_experiment(path, output, model_output, max_groups=3000):
    started = time.perf_counter()
    groups = defaultdict(list)
    for row in read_rows(path):
        groups[row["cluster_id"]].append(row)
    partitions, excluded = partition_cohort(groups, max_groups=max_groups)
    config = MatchConfig(max_evaluations=200000)
    training_rows, statistics = [], {}
    for split, selected in partitions.items():
        records, memberships, positives = [], {}, set()
        for group in selected:
            ids = []
            for row in groups[group]:
                rid = row["record_id"]
                records.append(record(rid, " ".join(row.get(k, "") for k in ("first_name", "last_name")),
                                      " ".join(row.get(k, "") for k in ("street", "city", "state")), "US", row.get("postal_code", "")))
                memberships[rid] = group
                ids.append(rid)
            positives.update(combinations(sorted(ids), 2))
        by_id = {r["supplier_id"]: r for r in records}
        keys = {rid: fingerprint(row) for rid, row in by_id.items()}
        candidates, stats = generate_candidates(records, config)
        baseline_labels, baseline_scores = [], []
        for pair in candidates:
            result = score_pair(by_id[pair[0]], by_id[pair[1]], config)
            label = int(memberships[pair[0]] == memberships[pair[1]])
            endpoints = [keys[pair[0]], keys[pair[1]]]
            training_rows.append({"evaluation_id": fingerprint([split, pair]), "pair_key": fingerprint(sorted(endpoints)),
                "record_keys": endpoints, "entity_group_ids": sorted({memberships[rid] for rid in pair}), "split": split,
                "config_id": config.config_id, "feature_schema": FEATURE_SCHEMA,
                "model_features": feature_vector(result, config.max_feature_chars), "human_label": "Match" if label else "NonMatch"})
            baseline_labels.append(label)
            baseline_scores.append(result["similarity_score"])
        stats.update(groups=len(selected), known_positive_pairs=len(positives),
                     retrieved_positive_pairs=len(positives & candidates.keys()),
                     candidate_recall=len(positives & candidates.keys()) / len(positives) if positives else None,
                     weighted_baseline=classification(baseline_labels, baseline_scores, config.review_threshold, len(positives)))
        statistics[split] = stats
    artifact = fit_pair_model(training_rows, config.config_id, "person-spider-v2")
    for split in ("validation", "test"):
        measured = dict(artifact[split])
        positives = statistics[split]["known_positive_pairs"]
        measured["false_negatives"] = positives - measured["true_positives"]
        measured["recall"] = measured["true_positives"] / positives if positives else None
        statistics[split]["learned_classifier_end_to_end"] = measured
    result = {"experiment": "unused-spider-cohort-v1", "dataset": "SPIDER v2", "domain": artifact["domain"],
              "source_url": "https://figshare.com/articles/dataset/SPIDER_v2_Synthetic_Person_Information_Dataset_for_Entity_Resolution/30472712",
              "input_sha256": source_hash([path]), "seed": 1729, "excluded_original_groups": len(excluded),
              "excluded_groups_sha256": fingerprint(sorted(excluded)), "selected_groups": max_groups,
              "partition_group_hashes": {s: fingerprint(sorted(g)) for s, g in partitions.items()},
              "matching_config": asdict(config), "config_id": config.config_id, "model_id": artifact["model_id"],
              "partitions": statistics, "elapsed_seconds": round(time.perf_counter() - started, 3),
              "protocol": "Whole entities split 60/20/20 before independent candidate indexing. Three regularizations and review threshold selected on validation only at observed precision >=0.95; selected model evaluated on test once. End-to-end recall includes positive pairs missed by blocking. No negative subsampling.",
              "limitations": "Synthetic person cluster labels, including shared-account rule 7, are not verified supplier identities. Pair observations within entities are correlated. No production precision guarantee, independent probability calibration, domain transfer or universal performance claim. The old v0.2 benchmark files are unchanged."}
    for destination, value in ((output, result), (model_output, artifact)):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-output", required=True)
    parser.add_argument("--max-groups", type=int, default=3000)
    args = parser.parse_args()
    try:
        print(json.dumps(run_experiment(args.input, args.output, args.model_output, args.max_groups), indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
