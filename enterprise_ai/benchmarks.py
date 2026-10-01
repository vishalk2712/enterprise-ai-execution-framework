"""Offline benchmarks against source labels; raw datasets are never bundled."""
import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, replace
from difflib import SequenceMatcher
import hashlib
from itertools import combinations
import json
from pathlib import Path
import random
import time

from .matching import MatchConfig, generate_candidates, score_pair
from .normalization import name_key
from .calibration import fit_calibration


def read_rows(path, delimiter=","):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader((line for line in handle if not line.startswith("#")), delimiter=delimiter)


def record(rid, name, address="", country="", postcode=""):
    return {"supplier_id": rid, "name": name or "", "address": address or "", "country": country or "", "postcode": postcode or "", "registration_id": "", "tax_id": ""}


def source_hash(paths):
    digest = hashlib.sha256()
    for path in sorted(map(Path, paths)):
        digest.update(path.name.encode())
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024*1024), b""):
                digest.update(block)
    return digest.hexdigest()


def load_spider(path, max_groups=2000, seed=1729):
    groups = defaultdict(list)
    for row in read_rows(path):
        groups[row["cluster_id"]].append(row)
    selected = sorted(groups, key=lambda c: hashlib.sha256(f"{seed}:{c}".encode()).hexdigest())[:max_groups]
    rows, labels, memberships, rules = [], {}, {}, {}
    for group in selected:
        members = []
        for row in groups[group]:
            rid = row["record_id"]
            rows.append(record(rid, row["first_name"]+" "+row["last_name"], " ".join(row.get(k, "") for k in ("street", "city", "state")), "US", row.get("postal_code", "")))
            memberships[rid] = group
            rules[rid] = row.get("rule_id", "")
            members.append(rid)
        for pair in combinations(sorted(members), 2):
            labels[pair] = 1
    return rows, labels, memberships, {"dataset": "SPIDER v2", "selected_groups": len(selected), "available_groups": len(groups),
        "input_sha256": source_hash([path]), "label_semantics": "Published cluster_id equality. Rule 7 can represent different people sharing an account; these are dataset labels, not verified legal identity.", "rules": rules}


def load_magellan(directory):
    directory = Path(directory)
    rows = []
    for side in ("A", "B"):
        for row in read_rows(directory/f"table{side}.csv"):
            rows.append(record(side+":"+row["id"], row.get("name", row.get("title", "")), " ".join(row.get(k, "") for k in ("addr", "city"))))
    labels = {tuple(sorted(("A:"+r["ltable_id"], "B:"+r["rtable_id"]))): int(r["label"]) for r in read_rows(directory/"test.csv")}
    return rows, labels, None, {"dataset": "Magellan Fodors-Zagats official test split", "input_sha256": source_hash([directory/n for n in ("tableA.csv", "tableB.csv", "test.csv")]),
        "label_semantics": "Metrics restricted to published test pairs. Unlabeled pairs are unknown. Published pair splits can share entities; no calibration trained on these splits."}


def load_amazon(directory, max_groups=1000, seed=1729):
    """Stream locally downloaded native challenge TSVs; keep complete selected S1 groups."""
    directory = Path(directory)
    truth = list(read_rows(directory/"train_ground_truth.tsv", "\t"))
    truth.sort(key=lambda r: hashlib.sha256(f"{seed}:{r['source1_entity_id']}".encode()).hexdigest())
    truth = truth[:max_groups]
    memberships, labels = {}, {}
    for row in truth:
        root = row["source1_entity_id"]
        memberships[root] = root
        for rid in filter(None, (x.strip() for x in row["matched_entity_ids"].split(","))):
            if rid in memberships and memberships[rid] != root:
                raise ValueError("A source record belongs to conflicting ground-truth groups")
            memberships[rid] = root
            labels[tuple(sorted((root, rid)))] = 1
    rows = []
    for source in (1, 2, 3):
        for row in read_rows(directory/f"train_source{source}.tsv", "\t"):
            if row["entity_id"] in memberships:
                rows.append(record(row["entity_id"], row["business_name"], row["business_address"], row["country"]))
    if {r["supplier_id"] for r in rows} != set(memberships):
        raise ValueError("Source files are incomplete for selected ground-truth groups")
    return rows, labels, memberships, {"dataset": "Amazon ML Challenge 2026 training holdout", "selected_groups": len(truth),
        "input_sha256": source_hash([directory/n for n in ("train_ground_truth.tsv", "train_source1.tsv", "train_source2.tsv", "train_source3.tsv")]),
        "label_semantics": "Only Source 1 versus Source 2/3 pairs; selected complete training groups. Challenge test data has no published labels."}


def old_candidates(rows):
    buckets = defaultdict(list)
    for row in rows:
        name = name_key(row["name"])
        if name:
            buckets[(row.get("country", ""), name.split()[0])].append(row["supplier_id"])
    return {p for ids in buckets.values() for p in combinations(sorted(ids), 2)}


def classification_metrics(predictions, labels):
    tp = sum(bool(labels.get(p)) for p in predictions if p in labels)
    fp = sum(labels[p] == 0 for p in predictions if p in labels)
    positives = sum(labels.values())
    return {"true_positives": tp, "false_positives": fp, "false_negatives": positives-tp,
            "precision_on_labeled_pairs": tp/(tp+fp) if tp+fp else None,
            "recall_on_labeled_pairs": tp/positives if positives else None}


def benchmark(rows, labels, memberships, metadata, config, seed=1729):
    started = time.perf_counter()
    candidates, statistics = generate_candidates(rows, config)
    baseline = old_candidates(rows)
    if metadata["dataset"].startswith("Amazon"):
        valid = lambda p: p[0].startswith("S1-") != p[1].startswith("S1-")
        candidates = {p:m for p,m in candidates.items() if valid(p)}
        baseline = {p for p in baseline if valid(p)}
    by_id = {r["supplier_id"]: r for r in rows}
    true_pairs = {p for p, y in labels.items() if y}
    feature_rows = {p: score_pair(by_id[p[0]], by_id[p[1]], config) for p in candidates}
    if memberships is not None:
        # Source labels define all negatives in this closed selected cohort.
        labels = {**{p: int(memberships[p[0]] == memberships[p[1]]) for p in set(candidates)|baseline}, **labels}
    predicted = {p for p, f in feature_rows.items() if f["similarity_score"] >= config.review_threshold}
    old_predicted = {p for p in baseline if SequenceMatcher(None,name_key(by_id[p[0]]["name"]),name_key(by_id[p[1]]["name"])).ratio() >= .88}
    result = {"metadata": {k:v for k,v in metadata.items() if k != "rules"}, "config": asdict(config), "config_id": config.config_id,
        "seed": seed, "records": len(rows), "positive_reference_pairs": len(true_pairs), "statistics": statistics,
        "baseline": {"candidate_pairs": len(baseline), "candidate_recall": len(true_pairs & baseline)/len(true_pairs) if true_pairs else None, **classification_metrics(old_predicted,labels)},
        "indexed": {"candidate_pairs": len(candidates), "candidate_recall": len(true_pairs & set(candidates))/len(true_pairs) if true_pairs else None, **classification_metrics(predicted,labels)},
        "notes": ["Threshold predictions are hypothetical pair classifications, not automatic supplier merges.", "Candidate recall is separate from scoring recall. No ungenerated near-miss guarantee is made.", "Benchmarks measure this configuration and dataset; they do not establish supplier accuracy."]}
    if memberships is not None:
        unique_groups = sorted(set(memberships.values()), key=lambda g: hashlib.sha256(f"split:{seed}:{g}".encode()).hexdigest())
        split = {g: ("train" if i < .6*len(unique_groups) else "validation" if i < .8*len(unique_groups) else "test") for i,g in enumerate(unique_groups)}
        calibration_rows = []
        for pair, features in feature_rows.items():
            ga, gb = (memberships[s] for s in pair)
            if split[ga] == split[gb]:
                calibration_rows.append({"evaluation_id": "|".join(pair), "config_id": config.config_id, "split": split[ga], "entity_group_ids": [ga,gb],
                    "similarity_score": features["similarity_score"], "human_label": "Match" if labels[pair] else "NonMatch"})
        try:
            result["calibration_experiment"] = fit_calibration(calibration_rows, config.config_id, metadata["dataset"])
        except ValueError as exc:
            result["calibration_experiment"] = {"status": "insufficient_labeled_split", "reason": str(exc)}
    if metadata.get("rules"):
        rules = defaultdict(lambda: [0,0,0])
        for pair in true_pairs:
            key = "+".join(sorted({metadata["rules"][s] for s in pair if metadata["rules"][s]})) or "base"
            rules[key][0] += 1; rules[key][1] += int(pair in baseline); rules[key][2] += int(pair in candidates)
        result["candidate_recovery_by_source_rule"] = dict(rules)
    result["elapsed_seconds"] = round(time.perf_counter()-started,3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["spider", "magellan", "amazon"], required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--max-groups", type=int, default=2000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.max_groups < 1: parser.error("max-groups must be positive")
    config = MatchConfig(max_evaluations=200000)
    if args.dataset == "spider": data=load_spider(args.input,args.max_groups)
    elif args.dataset == "amazon": data=load_amazon(args.input,args.max_groups)
    else: data=load_magellan(args.input)
    result=benchmark(*data,config)
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({"records":result["records"],"baseline":result["baseline"],"indexed":result["indexed"],"elapsed_seconds":result["elapsed_seconds"]},indent=2))


if __name__ == "__main__": main()
