"""Bounded GLEIF corporate alias retrieval benchmark; never supplier training."""
import argparse
from dataclasses import asdict
import hashlib
from itertools import combinations
import json
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from .benchmarks import classification_metrics, old_candidates, record
from .matching import MatchConfig, generate_candidates, score_pair
from .normalization import name_key
from difflib import SequenceMatcher


def fetch_pages(directory, pages=20):
    if not 1 <= pages <= 50:
        raise ValueError("Use 1..50 pages of 100 registry records")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    url = "https://api.gleif.org/api/v1/lei-records?" + urlencode({"page[size]": 100, "filter[entity.status]": "ACTIVE", "sort": "lei"})
    paths = []
    for index in range(pages):
        if not url:
            break
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.gleif.org" or parsed.path != "/api/v1/lei-records":
            raise ValueError("Unexpected registry pagination URL")
        with urlopen(url, timeout=30) as response:
            body = response.read(5_000_001)
        if len(body) > 5_000_000:
            raise ValueError("Registry page exceeded size bound")
        content = json.loads(body)
        path = directory / f"page-{index+1:03}.json"
        path.write_bytes(body)
        paths.append(path)
        print(json.dumps({"page": index+1, "records": len(content["data"])}), flush=True)
        url = content["links"].get("next")
    return paths


def load_gleif(directory):
    paths = sorted(Path(directory).glob("page-*.json"))
    if not paths:
        raise ValueError("No GLEIF page files; fetch a bounded sample first")
    by_lei, digest = {}, hashlib.sha256()
    for path in paths:
        body = path.read_bytes()
        digest.update(path.name.encode() + body)
        for item in json.loads(body)["data"]:
            if item["id"] in by_lei:
                raise ValueError("Registry pagination contained a repeated LEI")
            by_lei[item["id"]] = item["attributes"]["entity"]
    rows, groups, positives, aliases = [], {}, set(), 0
    for lei, entity in sorted(by_lei.items()):
        names = [entity["legalName"]["name"]] + [r["name"] for r in entity.get("otherNames", []) if r.get("type") in {"PREVIOUS_LEGAL_NAME", "ALTERNATIVE_LEGAL_NAME"}]
        distinct, seen = [], set()
        for name in names:
            normalized = name_key(name)
            if normalized and normalized not in seen:
                distinct.append(name)
                seen.add(normalized)
        address = entity["legalAddress"]
        members = []
        for index, name in enumerate(distinct):
            rid = hashlib.sha256(lei.encode()).hexdigest()[:24] + f":{index}"
            # LEIs, registration IDs and the alias field are deliberately masked.
            rows.append(record(rid, name, " ".join(address.get("addressLines", []) + [address.get("city", "")]), address.get("country", ""), address.get("postalCode", "")))
            groups[rid] = lei
            members.append(rid)
        aliases += max(0, len(members)-1)
        positives.update(combinations(sorted(members), 2))
    return rows, groups, positives, {"registry_entities": len(by_lei), "alias_records": aliases, "input_sha256": digest.hexdigest(), "pages": len(paths)}


def run_benchmark(directory, output):
    rows, groups, positives, provenance = load_gleif(directory)
    # Benchmark-only explicit bound; the interactive importer stays at 1,000
    # suppliers and 50,000 evaluations. This is not a production scale promise.
    config = MatchConfig(max_evaluations=200000)
    started = time.perf_counter()
    candidates, stats = generate_candidates(rows, config)
    baseline = old_candidates(rows)
    by_id = {r["supplier_id"]: r for r in rows}
    labels = {pair: int(groups[pair[0]] == groups[pair[1]]) for pair in set(candidates) | baseline | positives}
    selected = {p for p in candidates if score_pair(by_id[p[0]], by_id[p[1]], config)["similarity_score"] >= config.review_threshold}
    old_selected = {p for p in baseline if SequenceMatcher(None, name_key(by_id[p[0]]["name"]), name_key(by_id[p[1]]["name"])).ratio() >= .88}
    def metrics(pool, predictions):
        return {"candidate_pairs": len(pool), "candidate_recall": len(positives & set(pool))/len(positives) if positives else None,
                **classification_metrics(predictions, labels)}
    result = {"version": "corporate-alias-benchmark-v1", "dataset": "GLEIF bounded active legal-name aliases", "source": "https://www.gleif.org/en/lei-data/gleif-api",
              "license": "CC0 (GLEIF LEI Data Terms of Use)", "provenance": provenance, "records": len(rows), "positive_reference_pairs": len(positives),
              "config": asdict(config), "config_id": config.config_id, "statistics": stats, "baseline": metrics(baseline, old_selected), "indexed": metrics(candidates, selected),
              "elapsed_seconds": round(time.perf_counter()-started, 3), "trained_model": False,
              "limitations": ["Convenience sample sorted by LEI, not representative supplier procurement data.", "Aliases are registry names attached to one entity, not independent source-system duplicate records.", "Addresses and postcodes are copied from the registry entity across its aliases, making scoring optimistic.", "Identifiers and alias fields are masked from matching; LEI equality labels this closed cohort only.", "Different LEIs are treated as different registry identities; historical multiple-LEI situations are not adjudicated.", "No weights fitted, no supplier model promoted, no 100k-row performance claim, no raw registry records published."]}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Local directory for bounded registry pages")
    parser.add_argument("--fetch", action="store_true", help="Create input directory and download public pages")
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--output", default=".outcome/gleif-benchmark.json")
    args = parser.parse_args()
    try:
        if args.fetch:
            fetch_pages(args.input, args.pages)
        result = run_benchmark(args.input, args.output)
        print(json.dumps({k: result[k] for k in ("records", "positive_reference_pairs", "baseline", "indexed", "elapsed_seconds")}, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Corporate benchmark failed: {exc}\n")


if __name__ == "__main__":
    main()
