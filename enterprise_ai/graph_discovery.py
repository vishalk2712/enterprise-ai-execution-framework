"""Bounded association paths for review. Communities never establish identity."""
from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json

from .normalization import identifier, tokens
from .governance import degenerate_identifier


@dataclass(frozen=True)
class GraphPolicy:
    version: str = "association-paths-v1"
    max_posting: int = 40
    max_neighbors: int = 40
    max_paths: int = 2
    max_expansions: int = 20000
    max_pairs: int = 10000

    def __post_init__(self):
        for key in ("max_posting", "max_neighbors", "max_paths", "max_expansions", "max_pairs"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Graph bounds must be positive integers")
        if self.max_posting < 2 or self.max_paths > 5:
            raise ValueError("Graph postings require at least two rows; retain at most five paths")

    @property
    def policy_id(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def keys_for(row):
    country = row.get("country", "ZZ")
    keys = {}
    for field, relation in (("registration_id", "HAS_REGISTRATION"), ("lei", "HAS_LEI"),
                            ("parent_lei", "ASSOCIATED_WITH"), ("tax_id", "HAS_TAX_ID"),
                            ("bank_account_hash", "HAS_BANK_HASH"), ("postcode", "SHARES_POSTCODE")):
        value = identifier(row.get(field, ""))
        if not value or (field in {"registration_id", "tax_id"} and degenerate_identifier(value)):
            continue
        kind = "lei" if field == "parent_lei" else field
        scope = value if kind == "lei" else country + ":" + value
        node = kind + ":" + hashlib.sha256(scope.encode()).hexdigest()
        keys[node] = relation
    address = " ".join(tokens(row.get("address", "")))
    if len(address) >= 12 and len(address.split()) >= 3:
        node = "address:" + hashlib.sha256((country + ":" + address).encode()).hexdigest()
        keys[node] = "SHARES_ADDRESS"
    return keys


def discover(records, policy=None):
    policy = policy or GraphPolicy()
    rows = {r["supplier_id"]: r for r in records}
    if len(rows) != len(records):
        raise ValueError("Graph records require unique supplier IDs")
    keys = {rid: keys_for(row) for rid, row in sorted(rows.items())}
    postings = defaultdict(set)
    for rid, mapping in keys.items():
        for node in mapping:
            postings[node].add(rid)
    oversized = {node for node, ids in postings.items() if len(ids) > policy.max_posting}
    pairs, expansions, capped, exhausted = {}, 0, 0, False

    def edge(rid, node):
        return {"source": "supplier:" + rid, "source_label": rows[rid]['name'],
                "relation": keys[rid][node], "target": node, "target_kind": node.split(':', 1)[0]}

    # Four-hop paths must contain an asserted ownership relationship and an
    # address or legal key. No arbitrary postcode/bank/VAT transitive closure.
    for start in sorted(rows):
        found = defaultdict(list)
        for node in sorted(set(keys[start]) - oversized):
            for middle in sorted(postings[node] - {start}):
                expansions += 1
                if expansions > policy.max_expansions:
                    exhausted = True
                    break
                path = [edge(start, node), edge(middle, node)]
                if len(found[middle]) < policy.max_paths:
                    found[middle].append(path)
                for other_node in sorted(set(keys[middle]) - oversized - {node}):
                    relations = {keys[start][node], keys[middle][node], keys[middle][other_node]}
                    for end in sorted(postings[other_node] - {start, middle}):
                        expansions += 1
                        if expansions > policy.max_expansions:
                            exhausted = True
                            break
                        types = relations | {keys[end][other_node]}
                        if "ASSOCIATED_WITH" not in types or not types & {"SHARES_ADDRESS", "HAS_REGISTRATION", "HAS_LEI"}:
                            continue
                        if len(found[end]) < policy.max_paths:
                            found[end].append(path + [edge(middle, other_node), edge(end, other_node)])
                    if exhausted:
                        break
                if exhausted:
                    break
            if exhausted:
                break
        ranked = sorted(found, key=lambda rid: (min(len(p) for p in found[rid]), rid))
        capped += int(len(ranked) > policy.max_neighbors)
        for end in ranked[:policy.max_neighbors]:
            pair = tuple(sorted((start, end)))
            if pair not in pairs and len(pairs) >= policy.max_pairs:
                exhausted = True
                break
            bucket = pairs.setdefault(pair, [])
            for path in found[end]:
                if path not in bucket and len(bucket) < policy.max_paths:
                    bucket.append(path)
        if exhausted:
            break
    return pairs, {"policy": asdict(policy), "policy_id": policy.policy_id,
                   "candidate_pairs": len(pairs), "expansions": min(expansions, policy.max_expansions),
                   "oversized_nodes_skipped": len(oversized), "records_with_capped_neighbors": capped,
                   "truncated": bool(oversized or capped or exhausted), "expansion_budget_reached": exhausted,
                   "identity_authority": False, "storage": "in_memory_bipartite_index"}


def augment(evaluations, records, match_config, policy=None):
    """Union with existing retrieval: never remove baseline candidates."""
    from .matching import score_pair
    pairs, stats = discover(records, policy)
    by_id = {r["supplier_id"]: r for r in records}
    existing = {(e["left_id"], e["right_id"]): e for e in evaluations}
    added = 0
    for pair, paths in sorted(pairs.items()):
        item = existing.get(pair)
        if item is None:
            if len(evaluations) >= match_config.max_evaluations:
                stats["truncated"] = True
                stats["evaluation_budget_reached"] = True
                break
            left, right = (by_id[rid] for rid in pair)
            result = score_pair(left, right, match_config)
            item = {**result, "left_id": pair[0], "right_id": pair[1], "left_name": left["name"], "right_name": right["name"],
                    "left_record": left.get("_original", left), "right_record": right.get("_original", right),
                    "candidate_methods": [], "algorithmic_outcome": "Below_Threshold",
                    "near_miss": match_config.near_miss_floor <= result["similarity_score"] < match_config.review_threshold,
                    "sampling_probability": 1.0}
            evaluations.append(item)
            added += 1
        item["candidate_methods"] = sorted(set(item["candidate_methods"]) | {"graph_association_path"})
        item["graph_paths"] = paths
        item["graph_policy_id"] = stats["policy_id"]
    stats["additional_pairs"] = added
    return stats
