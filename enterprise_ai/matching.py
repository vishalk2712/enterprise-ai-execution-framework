"""Bounded candidate generation, independent feature scoring and diagnostics."""
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations
import hashlib
import json
import random

from .normalization import VERSION, identifier, name_key, ngrams, soundex, tokens


@dataclass(frozen=True)
class MatchConfig:
    version: str = "indexed-features-v2"
    normalization_version: str = VERSION
    ngram_size: int = 3
    min_shared_grams: int = 2
    max_posting: int = 150
    max_candidates_per_record: int = 40
    max_evaluations: int = 50000
    max_feature_chars: int = 256
    review_threshold: float = .88
    near_miss_floor: float = .80
    excluded_tax_sample_size: int = 100
    random_seed: int = 1729
    name_weight: float = .70
    address_weight: float = .20
    postcode_weight: float = .10

    def __post_init__(self):
        for field in ("ngram_size", "min_shared_grams", "max_posting", "max_candidates_per_record", "max_evaluations", "max_feature_chars", "excluded_tax_sample_size", "random_seed"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if not 2 <= self.ngram_size <= 5 or self.min_shared_grams < 1 or self.max_posting < 2 or self.max_candidates_per_record < 1 or self.max_evaluations < 1 or not 16 <= self.max_feature_chars <= 1000:
            raise ValueError("Invalid candidate or feature bounds")
        if not 0 <= self.near_miss_floor < self.review_threshold <= 1:
            raise ValueError("Expected 0 <= near_miss_floor < review_threshold <= 1")
        weights = (self.name_weight, self.address_weight, self.postcode_weight)
        if not all(isinstance(w, (int, float)) and 0 <= w <= 1 for w in weights) or self.name_weight <= 0:
            raise ValueError("Weights must be finite values in [0,1], with positive name weight")

    def canonical(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @property
    def config_id(self):
        return hashlib.sha256(self.canonical().encode()).hexdigest()


def levenshtein_similarity(left, right):
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left)+1))
    for j, b in enumerate(right, 1):
        current = [j]
        for i, a in enumerate(left, 1):
            current.append(min(current[-1]+1, previous[i]+1, previous[i-1]+(a != b)))
        previous = current
    return 1 - previous[-1] / max(len(left), len(right))


def jaccard(left, right):
    a, b = set(tokens(left)), set(tokens(right))
    return len(a & b) / len(a | b) if a and b else None


def score_pair(left, right, config):
    limit = config.max_feature_chars
    ln, rn = name_key(left["name"]), name_key(right["name"])
    features = {
        "name_levenshtein": levenshtein_similarity(ln[:limit], rn[:limit]),
        "address_jaccard": jaccard(left.get("address", "")[:limit], right.get("address", "")[:limit]),
        "postcode_exact": float(identifier(left.get("postcode", "")) == identifier(right.get("postcode", ""))) if left.get("postcode") and right.get("postcode") else None,
        "same_country": float(left.get("country") == right.get("country")),
        "shared_tax_id": float(bool(left.get("tax_id") and left.get("tax_id") == right.get("tax_id"))),
        "shared_registration_id": float(bool(left.get("registration_id") and left.get("registration_id") == right.get("registration_id"))),
        "identifier_conflict": float(any(left.get(f) and right.get(f) and left[f] != right[f] for f in ("registration_id", "tax_id"))),
    }
    weighted = [(features[k], w) for k, w in (("name_levenshtein", config.name_weight), ("address_jaccard", config.address_weight), ("postcode_exact", config.postcode_weight)) if features[k] is not None]
    score = sum(v*w for v, w in weighted) / sum(w for _, w in weighted)
    return {"features": features, "similarity_score": score, "match_probability": None,
            "probability_status": "uncalibrated", "normalized_left": ln, "normalized_right": rn,
            "feature_truncated": any(len(v) > limit for v in (ln, rn, left.get("address", ""), right.get("address", "")))}


def generate_candidates(records, config):
    """Inverted indexes; skip oversized postings explicitly, cap ranked neighbors.

    Country is a feature, not a blocker. Exact identity grouping remains a
    separate, conservative policy in Engine. Aliases are supplied data.
    """
    by_id = {r["supplier_id"]: r for r in records}
    if len(by_id) != len(records):
        raise ValueError("Candidate records require unique supplier_id")
    postings, keys = defaultdict(set), {}
    for rid, row in sorted(by_id.items()):
        variants = [row["name"], *row.get("aliases", "").split("|")]
        record_keys = set()
        for variant in variants:
            variant = name_key(variant)[:config.max_feature_chars]
            if not variant:
                continue
            record_keys.update(("ngram", gram) for gram in ngrams(variant, config.ngram_size))
            code = soundex(variant)
            if code:
                record_keys.add(("soundex", code))
        keys[rid] = record_keys
        for key in record_keys:
            postings[key].add(rid)
    oversized = {k for k, ids in postings.items() if len(ids) > config.max_posting}
    pairs, capped_records = {}, 0
    for rid in sorted(by_id):
        hits, methods = defaultdict(int), defaultdict(set)
        for key in sorted(keys[rid] - oversized):
            for other in sorted(postings[key]):
                if other == rid:
                    continue
                hits[other] += 1
                methods[other].add(key[0])
        ranked = [other for other in sorted(hits, key=lambda o: (-hits[o], o))
                  if "soundex" in methods[other] or hits[other] >= config.min_shared_grams]
        capped_records += int(len(ranked) > config.max_candidates_per_record)
        for other in ranked[:config.max_candidates_per_record]:
            pair = tuple(sorted((rid, other)))
            pairs.setdefault(pair, set()).update(methods[other])
            if len(pairs) > config.max_evaluations:
                raise ValueError("Candidate evaluation budget exceeded; partition input or raise the explicit bound")
    stats = {"records": len(records), "candidate_pairs": len(pairs), "oversized_postings_skipped": len(oversized),
             "records_with_capped_neighbors": capped_records, "all_possible_pairs": len(records)*(len(records)-1)//2}
    return {pair: sorted(methods) for pair, methods in sorted(pairs.items())}, stats


def sample_excluded_tax_pairs(records, candidates, config):
    """Uniform reservoir sample across shared-tax pairs absent from name blocking.

    This is a biased diagnostic stratum, never a negative ground-truth label.
    Indexing tax groups avoids scanning unrelated row pairs.
    """
    groups = defaultdict(list)
    for row in records:
        if row.get("tax_id"):
            groups[row["tax_id"]].append(row["supplier_id"])
    rng, sample, population = random.Random(config.random_seed), [], 0
    for tax in sorted(groups):
        for pair in combinations(sorted(groups[tax]), 2):
            if pair in candidates:
                continue
            population += 1
            if len(sample) < config.excluded_tax_sample_size:
                sample.append(pair)
            else:
                index = rng.randrange(population)
                if index < len(sample):
                    sample[index] = pair
    return sorted(sample), population


def evaluate_records(records, config):
    candidates, stats = generate_candidates(records, config)
    sample, population = sample_excluded_tax_pairs(records, candidates, config)
    by_id = {r["supplier_id"]: r for r in records}
    evaluations = []
    for pair, methods in list(candidates.items()) + [(p, ["excluded_shared_tax_sample"]) for p in sample]:
        left, right = (by_id[s] for s in pair)
        result = score_pair(left, right, config)
        sampled = pair not in candidates
        outcome = "Excluded_Sampled" if sampled else ("Review_Candidate" if result["similarity_score"] >= config.review_threshold else "Below_Threshold")
        if not sampled and (result["features"]["identifier_conflict"] or not result["features"]["same_country"]):
            outcome = "Conflict_Review"
        evaluations.append({**result, "left_id": pair[0], "right_id": pair[1], "left_name": left["name"], "right_name": right["name"],
                            "left_record": left.get("_original", left), "right_record": right.get("_original", right),
                            "candidate_methods": methods, "algorithmic_outcome": outcome,
                            "near_miss": config.near_miss_floor <= result["similarity_score"] < config.review_threshold,
                            "sampling_probability": len(sample)/population if sampled else 1.0})
    stats.update(excluded_tax_population=population, excluded_tax_sampled=len(sample), evaluations=len(evaluations),
                 near_misses=sum(e["near_miss"] for e in evaluations),
                 retention="All evaluated candidates retained; 100% of evaluated near-misses, not all ungenerated pairs")
    return evaluations, stats
