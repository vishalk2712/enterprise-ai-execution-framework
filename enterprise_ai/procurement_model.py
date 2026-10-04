"""Experimental classifier from Contracts Finder supplier observations.

GB-COH equality supplies weak reference labels, never human reviews or registry
verification. No label-bearing authority key or alias field reaches the model.
Awards stay outside the payment ledger; their value is never split by supplier.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import gzip
import hashlib
from itertools import combinations
import json
from pathlib import Path
import re
import time

from .benchmarks import record
from .matching import MatchConfig, generate_candidates, score_pair
from .normalization import name_key
from .pair_model import FEATURE_SCHEMA, classification, feature_vector, fingerprint, fit_pair_model, predict, validate_artifact
from .open_data import ATTRIBUTION, LICENSE_URL

VERSION = "contracts-finder-weak-label-v1"
LIMITATIONS = [
    "Publisher-asserted GB-COH IDs are weak reference labels, not verified registry identities or human judgments. Incorrect reused IDs can corrupt either class.",
    "Different IDs are treated as NonMatch within this closed cohort. Missing/invalid IDs and uncertain collisions are excluded, not labeled NonMatch.",
    "One observed row per normalized name per company is retained. No generated typos, copied registry addresses or synthetic supplier records are added.",
    "Convenience cohort from 2025 award publications; identity variation groups are enriched deliberately. Metrics do not estimate all procurement/payment accuracy.",
    "Entity-disjoint splits use asserted company groups; undetected erroneous identities or families can still create leakage. Test observations within companies are correlated.",
    "The classifier estimates reference-label agreement for retrieved candidates, not legal identity probability. It is uncalibrated and cannot authorize merges or actions.",
    "Government payment names have no independent reference labels. Applying scores there measures workload, not accuracy, savings or production readiness."]


def load_observations(path, max_lines=100000, max_line_bytes=5_000_000):
    """Read to gzip EOF/CRC before accepting data. Bounded, no archive extraction."""
    path = Path(path)
    if path.stat().st_size > 60_000_000: raise ValueError("Source exceeds 60 MB compressed bound")
    groups = defaultdict(dict); counts = Counter(); seen_ocids = set(); names_to_ids = defaultdict(set)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        for number in range(max_lines + 1):
            line = handle.readline(max_line_bytes + 1)
            if not line: break
            if number == max_lines or len(line) > max_line_bytes:
                raise ValueError("OCDS source exceeds line or record-size bound")
            release = json.loads(line)
            ocid = release.get("ocid")
            if not isinstance(ocid, str) or not ocid or ocid in seen_ocids:
                raise ValueError("Expected unique compiled OCDS processes, not repeated incremental releases")
            seen_ocids.add(ocid); counts["compiled_processes"] += 1
            award_supplier_ids = {s.get("id") for a in release.get("awards", []) if a.get("status") == "active" for s in a.get("suppliers", [])}
            if not award_supplier_ids: continue
            # Conflicting duplicate party keys are a source-quality refusal for that process.
            parties = {}; ambiguous = set()
            for party in release.get("parties", []):
                key = party.get("id")
                if key in parties and parties[key] != party: ambiguous.add(key)
                parties[key] = party
            counts["ambiguous_party_ids"] += len(ambiguous)
            for pid in sorted(award_supplier_ids - ambiguous, key=str):
                party = parties.get(pid)
                if not party or "supplier" not in party.get("roles", []):
                    counts["unjoined_award_suppliers"] += 1; continue
                ident = party.get("identifier", {})
                if ident.get("scheme") != "GB-COH":
                    counts["non_coh_observations"] += 1; continue
                key = str(ident.get("id", "")).strip().upper()
                counts["coh_observations"] += 1
                if not re.fullmatch(r"(?:\d{8}|[A-Z]{2}\d{6})", key) or len(set(key)) == 1:
                    counts["invalid_coh_observations"] += 1; continue
                name = party.get("name", "").strip(); norm = name_key(name)
                if not norm or len(name) > 1000:
                    counts["invalid_name_observations"] += 1; continue
                addr = party.get("address", {})
                address = " ".join(str(addr.get(k, "")) for k in ("streetAddress", "locality", "region"))
                postcode = str(addr.get("postalCode", ""))
                if len(address) > 1000 or len(postcode) > 1000:
                    counts["oversized_address_observations"] += 1; continue
                # Country unknown stays unknown; ID jurisdiction is not address country.
                row = record(fingerprint([key, norm])[:32], name, address, "ZZ", postcode)
                evidence = {"ocid": ocid, "party_id": pid, "scheme": "GB-COH", "asserted_company_id": key,
                            "notice_urls": sorted({d.get("url") for a in release.get("awards", []) for d in a.get("documents", []) if d.get("documentType") == "awardNotice" and isinstance(d.get("url"), str)})}
                observation = {"record": row, "evidence": evidence}
                # Deterministic real observation choice, independent of input ordering.
                current = groups[key].get(norm)
                if current is None or fingerprint(observation) < fingerprint(current): groups[key][norm] = observation
                names_to_ids[norm].add(key)
    # Shared normalized names with different IDs could be subsidiaries, bad IDs
    # or generic names. Exclude the entire implicated company groups, don't guess.
    collisions = {key for values in names_to_ids.values() if len(values) > 1 for key in values}
    counts["name_collision_groups_excluded"] = len(collisions)
    oversized = {key for key, names in groups.items() if len(names) > 8}
    counts["oversized_variant_groups_excluded"] = len(oversized)
    clean = {k: v for k, v in groups.items() if k not in collisions | oversized}
    counts.update(usable_companies=len(clean), name_variation_groups=sum(len(v)>1 for v in clean.values()),
                  usable_observations=sum(map(len, clean.values())))
    return clean, dict(counts)


def select_cohorts(groups, max_groups=1400, seed=1729):
    if not 100 <= max_groups <= 3000: raise ValueError("Use 100..3000 company groups")
    key = lambda k: fingerprint([VERSION, seed, k])
    variations = sorted((k for k,v in groups.items() if len(v)>1), key=key)
    singles = sorted((k for k,v in groups.items() if len(v)==1), key=key)
    # Enrich naturally occurring variations to get enough positive training cases.
    selected = variations[:min(len(variations), max_groups//2)]
    selected += singles[:max_groups - len(selected)]
    ordered = sorted(selected, key=lambda k: fingerprint(["company-splits-v1", seed, k]))
    train, validation = int(.6*len(ordered)), int(.8*len(ordered))
    return {"train": ordered[:train], "validation": ordered[train:validation], "test": ordered[validation:]}


def build_pairs(groups, cohorts, config):
    rows = []; statistics = {}; provenance = []
    for split, companies in cohorts.items():
        records = []; membership = {}; positives = set()
        for company in companies:
            observations = list(groups[company].values())
            members = []
            for observation in observations:
                row = observation["record"]; rid = row["supplier_id"]
                records.append(row); members.append(rid); membership[rid] = company
                provenance.append({"split":split,"record_id":rid,**observation["evidence"]})
            positives.update(combinations(sorted(members), 2))
        candidates, stats = generate_candidates(records, config)
        by_id = {r["supplier_id"]:r for r in records}
        labels = []; scores = []
        for pair in candidates:
            evaluation = score_pair(by_id[pair[0]], by_id[pair[1]], config)
            features = feature_vector(evaluation, config.max_feature_chars)
            if any(features[n] for n in ("shared_registration_id", "shared_tax_id", "identifier_conflict")):
                raise ValueError("Reference identity leaked into model features")
            label = "Match" if membership[pair[0]] == membership[pair[1]] else "NonMatch"
            endpoints = [fingerprint(by_id[rid]) for rid in pair]
            rows.append({"evaluation_id":fingerprint([VERSION,split,pair]),"pair_key":fingerprint(sorted(endpoints)),
                         "record_keys":endpoints,"entity_group_ids":sorted({membership[rid] for rid in pair}),
                         "split":split,"config_id":config.config_id,"feature_schema":FEATURE_SCHEMA,"model_features":features,
                         "reference_label":label,"label_provenance":"publisher_asserted_GB-COH_reference_not_human",
                         "source_record_ids":list(pair)})
            labels.append(int(label=="Match")); scores.append(evaluation["similarity_score"])
        stats.update(companies=len(companies), records=len(records), known_positive_reference_pairs=len(positives),
                     retrieved_positive_reference_pairs=len(positives & candidates.keys()),
                     candidate_recall=len(positives & candidates.keys())/len(positives) if positives else None,
                     weighted_baseline=classification(labels,scores,config.review_threshold,len(positives)))
        statistics[split] = stats
    return rows, statistics, provenance


def run_experiment(path, output_dir, max_groups=1400):
    started = time.perf_counter(); out=Path(output_dir)
    if out.exists(): raise ValueError("Experiment output must be fresh; do not overwrite a locked test cohort")
    groups, source_counts = load_observations(path)
    cohorts = select_cohorts(groups, max_groups)
    config = MatchConfig(); rows, statistics, provenance = build_pairs(groups, cohorts, config)
    model = fit_pair_model(rows, config.config_id, "supplier", label_field="reference_label")
    model.pop("model_id")
    model.update(experiment=VERSION, intended_use="Experimental review ranking only; not a promoted supplier model",
                 training_label_provenance="publisher_asserted_GB-COH_reference_not_human",
                 input_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                 limitations=LIMITATIONS, activated=False, source_license=LICENSE_URL, attribution=ATTRIBUTION)
    model["model_id"] = fingerprint(model); validate_artifact(model,config.config_id,"supplier")
    for split in ("validation", "test"):
        measured = dict(model[split]); positives = statistics[split]["known_positive_reference_pairs"]
        measured["false_negatives"] = positives - measured["true_positives"]
        measured["recall"] = measured["true_positives"]/positives if positives else None
        statistics[split]["learned_end_to_end_reference_metrics"] = measured
        # Also expose difficult matches, separately from near-identical variations.
        hard = [r for r in rows if r["split"]==split and r["model_features"]["name_levenshtein"] < .88]
        predictions = [predict(r["model_features"],model) for r in hard]
        statistics[split]["difficult_retrieved_pairs"] = classification([int(r["reference_label"]=="Match") for r in hard],predictions,model["review_threshold"])
        statistics[split]["difficult_retrieved_pairs"]["rows"] = len(hard)
    result = {"experiment":VERSION,"source":"https://data.open-contracting.org/en/publication/128",
              "license_url":LICENSE_URL,"attribution":ATTRIBUTION,"source_counts":source_counts,
              "input_sha256":model["input_sha256"],"model_id":model["model_id"],"matching_config":asdict(config),"config_id":config.config_id,
              "seed":1729,"partition_company_hashes":{s:fingerprint(sorted(g)) for s,g in cohorts.items()},
              "partitions":statistics,"elapsed_seconds":round(time.perf_counter()-started,3),
              "protocol":"Whole asserted company identities split 60/20/20 before candidate indexing. No negative subsampling. Three regularizations and cutoff selected on validation only at target reference precision 0.95. Test evaluated after selection. All authority identifiers and alias fields masked. No independent probability calibration.",
              "limitations":LIMITATIONS,"activated":False}
    out.mkdir(parents=True)
    for name,content in (("model.json",model),("results.json",result),("split-manifest.json",cohorts)):
        (out/name).write_text(json.dumps(content,indent=2),encoding="utf-8")
    (out/"reference-pairs.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows),encoding="utf-8")
    (out/"record-provenance.jsonl").write_text("".join(json.dumps(r)+"\n" for r in provenance),encoding="utf-8")
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",required=True); parser.add_argument("--output-dir",required=True)
    parser.add_argument("--max-groups",type=int,default=1400)
    args=parser.parse_args(argv)
    try:
        result=run_experiment(args.input,args.output_dir,args.max_groups)
        print(json.dumps(result,indent=2))
    except (ValueError,OSError,EOFError) as exc: parser.exit(2,f"Procurement training failed: {exc}\n")


if __name__ == "__main__": main()
