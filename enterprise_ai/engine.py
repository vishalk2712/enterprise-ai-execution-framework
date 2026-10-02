"""Deterministic baseline: identity resolution, exact spend and staged local actions.

No API calls or model inference occur here. Source values are data, never commands.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from .normalization import identifier, name_key
from .matching import MatchConfig, evaluate_records
from . import resolution_audit


class ValidationError(ValueError):
    """Input or action cannot be accepted safely."""


SUPPLIER_FIELDS = ("supplier_id", "name", "country", "registration_id", "tax_id", "postcode")
SPEND_FIELDS = ("invoice_id", "supplier_id", "invoice_date", "amount", "currency", "category", "description")
MAX_BYTES = 5_000_000
QUERY_WORDS = set("""what is the total totals spend spent net by currency currencies show me please all imported
    suppliers supplier for of on from and or to in our we have has how much money did do does a an are
    this these those which highest top invoice invoices duplicate duplicates identity identities resolve
    resolved matched matching merge evidence amount amounts summary give tell list entity entities with
    review candidate candidates find identify display explain i want can you record records""".split())
# Recognize currency filters independently of whether that currency has rows.
# Other uppercase three-letter codes are also accepted as explicit filters.
CURRENCY_CODES = set("""AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR
    FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES
    KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR
    MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB
    RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD
    TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG""".split())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def phrase_in(phrase: str, text: str) -> bool:
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def read_csv(text: str, fields: tuple[str, ...], source: str, limit: int) -> list[dict]:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError(f"{source}: CSV content is required.")
    if len(text.encode("utf-8")) > MAX_BYTES or "\x00" in text:
        raise ValidationError(f"{source}: file too large or contains null bytes.")
    # csv.reader exposes each blank record, unlike DictReader which silently
    # skips it. Track physical line starts, including quoted multiline records.
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
    try:
        header = next(reader, None)
        optional = {"address", "aliases"} if fields == SUPPLIER_FIELDS else set()
        if not header or len(header) != len(set(header)) or not set(fields) <= set(header) or set(header) - set(fields) - optional:
            raise ValidationError(f"{source}: expected exactly these columns: {', '.join(fields)}.")
        result = []
        previous = reader.line_num
        for values in reader:
            start = previous + 1
            previous = reader.line_num
            if not values:
                continue
            if len(values) != len(header):
                raise ValidationError(f"{source}:{start}: row does not match the header.")
            row = dict(zip(header, values))
            clean = {k: v.strip() for k, v in row.items()}
            if any(len(v) > 1000 for v in clean.values()):
                raise ValidationError(f"{source}:{start}: field exceeds 1000 characters.")
            clean["_original"] = row
            clean["_line"] = start
            clean["_source"] = source
            result.append(clean)
            if len(result) > limit:
                raise ValidationError(f"{source}: this prototype accepts at most {limit} rows.")
        if not result:
            raise ValidationError(f"{source}: at least one data row is required.")
        return result
    except csv.Error as exc:
        raise ValidationError(f"{source}: malformed CSV ({exc}).") from exc


def evidence(row: dict) -> dict:
    return {"source": row["_source"], "line": row["_line"],
            "record_id": row.get("invoice_id", row.get("supplier_id", "")),
            "excerpt": "; ".join(f"{k}={v}" for k, v in row.get("_original", row).items() if not k.startswith("_"))}


class Engine:
    def __init__(self, db_path: str = ":memory:", match_config: MatchConfig | None = None, calibration=None, pair_model=None):
        self.match_config = match_config or MatchConfig()
        self.calibration = calibration
        self.pair_model = pair_model
        if pair_model is not None:
            from .pair_model import validate_artifact
            validate_artifact(pair_model, self.match_config.config_id, "supplier")
            if calibration is not None:
                raise ValueError("Choose pair_model or weighted-score calibration; they estimate different quantities")
        if calibration is not None:
            from .calibration import probability
            probability(.5, calibration, self.match_config.config_id, "supplier")
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suppliers (
                supplier_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(entity_id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS portal (
                entity_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        resolution_audit.migrate(self.db)

    def close(self):
        self.db.close()

    def _log(self, event: str, payload: dict):
        self.db.execute("INSERT INTO audit(timestamp,event,payload) VALUES(?,?,?)", (now(), event, json.dumps(payload)))

    def _meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _records(self, table: str) -> list[dict]:
        # Table is always a code-owned constant, never request input.
        if table not in {"suppliers", "invoices", "entities"}:
            raise ValueError("Unknown internal table")
        return [json.loads(r[0]) for r in self.db.execute(f"SELECT payload FROM {table} ORDER BY 1")]

    def analyze(self, suppliers_csv: str, spend_csv: str, normalized_suppliers=None, contract_manifest=None) -> dict:
        suppliers = read_csv(suppliers_csv, SUPPLIER_FIELDS, "suppliers.csv", 1000)
        invoices = read_csv(spend_csv, SPEND_FIELDS, "spend.csv", 10000)
        if normalized_suppliers is not None:
            prepared = {r["supplier_id"]: r for r in normalized_suppliers}
            if len(prepared) != len(suppliers) or set(prepared) != {r["supplier_id"] for r in suppliers}:
                raise ValidationError("Upstream contract changed supplier identity keys")
            for row in suppliers:
                row.update({k: v for k, v in prepared[row["supplier_id"]].items() if k in {*SUPPLIER_FIELDS, "address", "aliases"}})
        ids, warnings = set(), []
        for row in suppliers:
            if not row["supplier_id"] or not row["name"]:
                raise ValidationError(f"suppliers.csv:{row['_line']}: supplier_id and name are required.")
            if row["supplier_id"] in ids:
                raise ValidationError("Duplicate supplier_id; provide one record for each source supplier.")
            ids.add(row["supplier_id"])
            row["country"] = row["country"].upper()
            if not re.fullmatch(r"[A-Z]{2}", row["country"]):
                raise ValidationError(f"suppliers.csv:{row['_line']}: country must contain two letters.")
            for field in ("registration_id", "tax_id"):
                supplied = row[field]
                row[field] = identifier(supplied)
                if supplied and not row[field]:
                    warnings.append(f"suppliers.csv:{row['_line']}: {field} missing-value placeholder ignored for matching.")
        unique = {}
        for row in invoices:
            if not row["invoice_id"] or row["supplier_id"] not in ids:
                raise ValidationError(f"spend.csv:{row['_line']}: invoice_id required and supplier_id must exist.")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["invoice_date"]):
                raise ValidationError(f"spend.csv:{row['_line']}: use an ISO date YYYY-MM-DD.")
            try:
                date.fromisoformat(row["invoice_date"])
            except ValueError as exc:
                raise ValidationError(f"spend.csv:{row['_line']}: invalid date.") from exc
            row["currency"] = row["currency"].upper()
            if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
                raise ValidationError(f"spend.csv:{row['_line']}: currency must contain three letters.")
            if not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,2})?", row["amount"]):
                raise ValidationError(f"spend.csv:{row['_line']}: amount must be a finite decimal with at most two places.")
            row["amount"] = f"{Decimal(row['amount']):.2f}"
            old = unique.get(row["invoice_id"])
            if old:
                if any(old[k] != row[k] for k in SPEND_FIELDS):
                    raise ValidationError(f"Conflicting duplicate invoice_id: {row['invoice_id']}.")
                warnings.append(f"Skipped exact duplicate invoice {row['invoice_id']} at spend.csv:{row['_line']}.")
            else:
                unique[row["invoice_id"]] = row
        invoices = list(unique.values())
        entities, reviews, memberships = self._resolve(suppliers)
        evaluations, statistics = evaluate_records(suppliers, self.match_config)
        if self.pair_model is not None:
            from .pair_model import apply_model
            apply_model(evaluations, self.pair_model, self.match_config)
            statistics["pair_model"] = self.pair_model
        if self.calibration is not None:
            from .calibration import probability
            statistics["calibration"] = self.calibration
            for item in evaluations:
                item["match_probability"] = probability(item["similarity_score"], self.calibration, self.match_config.config_id, "supplier")
                item["probability_status"] = "calibrated_estimate"
                item["calibration_model_id"] = self.calibration["model_id"]
        if contract_manifest:
            statistics["upstream_contract"] = contract_manifest
        review_map = {(r["left_id"], r["right_id"]): r for r in reviews}
        for item in evaluations:
            pair = item["left_id"], item["right_id"]
            if memberships[pair[0]] == memberships[pair[1]]:
                item["authority_grouped"] = True
                if item["algorithmic_outcome"] != "Excluded_Sampled":
                    item["algorithmic_outcome"] = "Authority_Grouped"
            elif item["algorithmic_outcome"] in {"Review_Candidate", "Conflict_Review"}:
                review_map.setdefault(pair, {"left_id": pair[0], "right_id": pair[1], "left_name": item["left_name"], "right_name": item["right_name"],
                    "reason": "Multi-feature similarity candidate; no automatic merge. Review identifiers and source evidence."})
        reviews = list(review_map.values())
        snapshot_id = digest(suppliers_csv + "\0" + spend_csv)
        snapshot = digest(snapshot_id + self.match_config.canonical() + json.dumps(contract_manifest, sort_keys=True) + json.dumps(self.calibration, sort_keys=True))
        if self.pair_model is not None:
            snapshot = digest(snapshot + json.dumps(self.pair_model, sort_keys=True))
        meta = {"supplier_count": len(suppliers), "invoice_count": len(invoices),
                "entity_count": len(entities), "currencies": sorted({r["currency"] for r in invoices}),
                "snapshot": snapshot, "source_hashes": {"suppliers.csv": digest(suppliers_csv), "spend.csv": digest(spend_csv)},
                "full_characters": len(suppliers_csv) + len(spend_csv), "analyzed_at": now(),
                "snapshot_id": snapshot_id, "matching_config_id": self.match_config.config_id, "matching_statistics": statistics}
        with self.lock, self.db:
            meta["resolution_run_id"] = resolution_audit.persist_run(self.db, snapshot_id, self.match_config, evaluations, statistics)
            evaluation_ids = {(e["left_id"], e["right_id"]): e["evaluation_id"] for e in evaluations}
            for review in reviews:
                review["evaluation_id"] = evaluation_ids.get((review["left_id"], review["right_id"]))
            self.db.execute("DELETE FROM invoices")
            self.db.execute("DELETE FROM suppliers")
            self.db.execute("DELETE FROM entities")
            for entity in entities:
                self.db.execute("INSERT INTO entities VALUES(?,?)", (entity["entity_id"], json.dumps(entity)))
            for row in suppliers:
                self.db.execute("INSERT INTO suppliers VALUES(?,?,?)", (row["supplier_id"], memberships[row["supplier_id"]], json.dumps(row)))
            for row in invoices:
                self.db.execute("INSERT INTO invoices VALUES(?,?,?)", (row["invoice_id"], row["supplier_id"], json.dumps(row)))
            for key, value in (("dataset", meta), ("reviews", reviews), ("warnings", warnings)):
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))
            self.db.execute("UPDATE actions SET status='stale' WHERE snapshot<>? AND status IN ('pending','approved')", (snapshot,))
            self._log("dataset_analyzed", {"snapshot": snapshot, "suppliers": len(suppliers), "invoices": len(invoices)})
        return self.state()

    @staticmethod
    def _resolve(suppliers: list[dict]):
        by_id = {r["supplier_id"]: r for r in suppliers}
        parent = {s: s for s in by_id}

        def root(s):
            while parent[s] != s:
                parent[s] = parent[parent[s]]
                s = parent[s]
            return s

        reasons = defaultdict(set)
        conflicts = []
        # Process authoritative buckets as a whole to avoid order-dependent partial
        # merges when a shared tax ID bridges contradictory registration IDs.
        for field in ("registration_id", "tax_id"):
            buckets = defaultdict(list)
            for row in suppliers:
                if row[field]:
                    buckets[(row["country"], row[field])].append(row["supplier_id"])
            for key, members in sorted(buckets.items()):
                if len(members) < 2:
                    continue
                roots = {root(m) for m in members}
                group = [s for s in by_id if root(s) in roots]
                conflict = any(len({by_id[s][f] for s in group if by_id[s][f]}) > 1 for f in ("registration_id", "tax_id"))
                if conflict:
                    for left, right in zip(sorted(members), sorted(members)[1:]):
                        conflicts.append((left, right, "Shared identifier conflicts with other authority IDs; review required."))
                    continue
                representative = min(group)
                for s in group:
                    parent[root(s)] = representative
                    reasons[s].add(f"Exact {field} within {key[0]}; no conflicting authority IDs")
        groups = defaultdict(list)
        for s in by_id:
            groups[root(s)].append(s)
        entities, memberships = [], {}
        for group in groups.values():
            group.sort()
            entity_id = "entity-" + digest("\0".join(group))[:12]
            for s in group:
                memberships[s] = entity_id
            entities.append({"entity_id": entity_id, "display_name": by_id[group[0]]["name"],
                             "country": by_id[group[0]]["country"], "source_supplier_ids": group,
                             "match_basis": sorted({r for s in group for r in reasons[s]}) or ["Single source record; no automatic match"],
                             "registration_ids": sorted({by_id[s]["registration_id"] for s in group if by_id[s]["registration_id"]}),
                             "tax_ids": sorted({by_id[s]["tax_id"] for s in group if by_id[s]["tax_id"]})})
        review_map = {}
        def add_review(left, right, reason):
            if memberships[left] == memberships[right]:
                return
            pair = tuple(sorted((left, right)))
            review_map[pair] = {"left_id": pair[0], "right_id": pair[1], "left_name": by_id[pair[0]]["name"],
                                "right_name": by_id[pair[1]]["name"], "reason": reason}
        for left, right, reason in conflicts:
            add_review(left, right, reason)
        return sorted(entities, key=lambda e: e["display_name"]), list(review_map.values()), memberships

    def evaluations(self, run_id=None, limit=200, offset=0):
        with self.lock:
            selected = run_id or self._meta("dataset", {}).get("resolution_run_id")
            return resolution_audit.list_evaluations(self.db, selected, limit, offset)

    def review(self, evaluation_id, human_label, reviewer, reason, supersedes=None):
        with self.lock, self.db:
            return resolution_audit.record_decision(self.db, evaluation_id, human_label, reviewer, reason, supersedes)

    def export_audit(self):
        with self.lock:
            return "\n".join(resolution_audit.export_history(self.db)) + "\n"

    def export_labels(self):
        with self.lock:
            rows = self.db.execute("""SELECT e.evaluation_id,e.left_id,e.right_id,e.similarity_score,e.payload_json,
              r.config_id,r.config_json,r.snapshot_id,d.human_label
              FROM pair_evaluations e JOIN resolution_runs r ON r.run_id=e.run_id
              JOIN review_decisions d ON d.evaluation_id=e.evaluation_id
              WHERE d.rowid=(SELECT MAX(d2.rowid) FROM review_decisions d2 WHERE d2.evaluation_id=e.evaluation_id)
                AND d.human_label IN ('Match','NonMatch')""")
            from .pair_model import FEATURE_SCHEMA, feature_vector, fingerprint
            exported = []
            for row in rows:
                item = dict(row)
                evaluation = json.loads(item.pop("payload_json"))
                config = json.loads(item.pop("config_json"))
                record_keys = [fingerprint({k: record.get(k, "") for k in
                    ("supplier_id", "name", "country", "registration_id", "tax_id", "postcode", "address", "aliases")})
                    for record in (evaluation["left_record"], evaluation["right_record"])]
                item.update(feature_schema=FEATURE_SCHEMA, model_features=feature_vector(evaluation, config["max_feature_chars"]),
                            record_keys=record_keys, pair_key=fingerprint(sorted(record_keys)), split=None, entity_group_ids=[],
                            instructions="Deduplicate pair_key across runs; assign connected groups for BOTH endpoints and disjoint train/validation/test splits. Reviewers' labels do not authorize actions.")
                exported.append(json.dumps(item))
            return "\n".join(exported) + ("\n" if exported else "")

    def state(self) -> dict:
        with self.lock:
            entities = self._records("entities")
            totals, grouped, counts, group_counts = defaultdict(Decimal), defaultdict(Decimal), defaultdict(int), defaultdict(int)
            nodes = [{"id": e["entity_id"], "label": e["display_name"], "type": "entity"} for e in entities]
            edges = []
            for row in self.db.execute("SELECT supplier_id,entity_id,payload FROM suppliers"):
                nodes.append({"id": "supplier:" + row["supplier_id"], "label": json.loads(row["payload"])["name"], "type": "source_supplier"})
                edges.append({"source": "supplier:" + row["supplier_id"], "target": row["entity_id"], "relation": "resolved_to"})
            for row in self.db.execute("SELECT i.payload,s.entity_id FROM invoices i JOIN suppliers s ON i.supplier_id=s.supplier_id"):
                invoice = json.loads(row["payload"])
                currency, amount = invoice["currency"], Decimal(invoice["amount"])
                totals[currency] += amount
                counts[currency] += 1
                key = (row["entity_id"], currency)
                grouped[key] += amount
                group_counts[key] += 1
                nodes.append({"id": "invoice:" + invoice["invoice_id"], "label": invoice["invoice_id"], "type": "invoice"})
                edges.append({"source": "invoice:" + invoice["invoice_id"], "target": "supplier:" + invoice["supplier_id"], "relation": "billed_by"})
            names = {e["entity_id"]: e["display_name"] for e in entities}
            spend = [{"entity_id": entity, "name": names[entity], "currency": curr, "amount": f"{amount:.2f}", "invoice_count": group_counts[(entity, curr)]}
                     for (entity, curr), amount in sorted(grouped.items(), key=lambda x: (x[0][1], -x[1]))]
            return {"dataset": self._meta("dataset", {"supplier_count": 0, "invoice_count": 0, "currencies": [], "source_hashes": {}}),
                    "entities": entities, "totals": [{"currency": c, "amount": f"{v:.2f}", "invoice_count": counts[c]} for c, v in sorted(totals.items())],
                    "supplier_spend": spend, "review_candidates": self._meta("reviews", []), "warnings": self._meta("warnings", []),
                    "graph": {"nodes": nodes, "edges": edges}, "recent_actions": self._actions()}

    def query(self, question: str, budget_tokens: int = 2048) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValidationError("Provide a question between 1 and 2000 characters.")
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or not 256 <= budget_tokens <= 32768:
            raise ValidationError("budget_tokens must be an integer between 256 and 32768.")
        with self.lock:
            state = self.state()
            if not state["entities"]:
                raise ValidationError("Import a dataset or load the demo first.")
            q = question.casefold()
            suppliers, invoices = self._records("suppliers"), self._records("invoices")
            normalized_query = name_key(q)
            source_names = {s["supplier_id"]: name_key(s["name"]) for s in suppliers}
            named, recognized = [], set()
            for entity in state["entities"]:
                identifiers = [entity["entity_id"], *entity["source_supplier_ids"]]
                id_hits = [s for s in identifiers if re.search(r"(?<![\w.-])" + re.escape(s.casefold()) + r"(?![\w.-])", q)]
                name_hits = [source_names[s] for s in entity["source_supplier_ids"]
                             if phrase_in(source_names[s], normalized_query)]
                if id_hits or name_hits:
                    named.append(entity)
                    recognized.update(name_key(s) for s in id_hits)
                    recognized.update(name_hits)
            residual = normalized_query
            for phrase in sorted(recognized, key=len, reverse=True):
                residual = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", residual)
            remaining = set(residual.split())
            possible_codes = CURRENCY_CODES | set(state["dataset"]["currencies"])
            possible_codes.update(token for token in re.findall(r"\b[A-Z]{3}\b", question)
                                  if token.casefold() not in QUERY_WORDS)
            # ALL and TOP are both currency codes and ordinary query words.
            # Treat ambiguous words as currency only in an explicit filter.
            requested_currencies = {word.upper() for word in remaining if word.upper() in possible_codes
                                    and (word not in QUERY_WORDS or re.search(r"\b(?:in|currency|currencies)\s+" + re.escape(word) + r"\b", q))}
            unknown_terms = remaining - QUERY_WORDS - {c.casefold() for c in requested_currencies}
            unknowns = []
            if unknown_terms:
                intent = "needs_review"
                records = []
                answer = "This query includes an unsupported condition or an unrecognized supplier. No spend total was calculated."
                unknowns.append("Use an exact source supplier ID, entity ID or full supplier name, optionally with a currency code. Date/category filters, forecasts and other conditions are unsupported.")
                unknowns.append("Unrecognized query terms: " + ", ".join(sorted(unknown_terms)))
            elif re.search(r"\b(duplicate|duplicates|identity|identities|resolve|matched|matching|merge)\b", q):
                intent = "supplier_resolution"
                selected_entities = named or state["entities"]
                wanted = {s for e in selected_entities for s in e["source_supplier_ids"]}
                records = [s for s in suppliers if s["supplier_id"] in wanted]
                matches = [e for e in selected_entities if len(e["source_supplier_ids"]) > 1]
                reviews = [r for r in state["review_candidates"] if r["left_id"] in wanted or r["right_id"] in wanted]
                answer = f"{len(records)} source suppliers resolve to {len(selected_entities)} proposed entities using exact authority IDs. "
                answer += "Matched groups: " + ("; ".join(f"{e['display_name']} ({', '.join(e['source_supplier_ids'])})" for e in matches) or "none") + ". "
                answer += f"{len(reviews)} candidate pair(s) require review. Source identifiers have not been independently verified."
                if requested_currencies:
                    unknowns.append("Currency filters apply only to spend queries; supplier identity does not depend on invoice currency.")
            elif re.search(r"\b(spend|spent|total|totals|invoice|invoices|supplier|suppliers|top|highest)\b", q):
                intent = "spend_summary"
                wanted = {s for e in named for s in e["source_supplier_ids"]}
                records = [r for r in invoices if not wanted or r["supplier_id"] in wanted]
                if requested_currencies:
                    records = [r for r in records if r["currency"] in requested_currencies]
                by_currency = defaultdict(Decimal)
                for r in records:
                    by_currency[r["currency"]] += Decimal(r["amount"])
                scope = ", ".join(e["display_name"] for e in named) if named else "all imported suppliers"
                if by_currency:
                    answer = f"Net spend for {scope}: " + "; ".join(f"{c} {v:,.2f}" for c, v in sorted(by_currency.items())) + f" across {len(records)} unique invoices. Currencies are kept separate."
                else:
                    answer = f"No matching invoices for {scope}" + (" in " + ", ".join(sorted(requested_currencies)) if requested_currencies else "") + "."
                missing_currencies = requested_currencies - set(by_currency)
                if missing_currencies:
                    unknowns.append("No invoices in this scope for: " + ", ".join(sorted(missing_currencies)) + ". No currency conversion was performed.")
                unknowns.append("Supported spend scope covers all imported dates. Date/category filters and forecasts require analyst review.")
                if re.search(r"\b(top|highest)\b", q):
                    wanted_entities = {e["entity_id"] for e in named}
                    selected = [r for r in state["supplier_spend"]
                                if (not requested_currencies or r["currency"] in requested_currencies)
                                and (not wanted_entities or r["entity_id"] in wanted_entities)]
                    leaders = {}
                    for r in selected:
                        if r["currency"] not in leaders:
                            leaders[r["currency"]] = r
                    if leaders:
                        answer += " Highest net supplier spend per currency in this scope: " + "; ".join(f"{c}: {r['name']} {r['amount']}" for c, r in leaders.items()) + "."
            else:
                intent = "needs_review"
                records = []
                answer = "This question needs a supported query or analyst review. Ask about total spend, top suppliers, or duplicate supplier identities."
                unknowns.append("No general-purpose LLM is connected. No external model was called and no action was executed.")
            all_evidence = [evidence(r) for r in records]
            selected, size = [], 0
            for item in all_evidence:
                item_size = len(json.dumps(item, ensure_ascii=False))
                if size + item_size <= budget_tokens * 4:
                    selected.append(item)
                    size += item_size
            if len(selected) < len(all_evidence):
                unknowns.append(f"Evidence preview contains {len(selected)} of {len(all_evidence)} contributing rows; calculation used every matching row. Increase the evidence budget or export the report.")
            full = state["dataset"].get("full_characters", 0)
            result = {"answer": answer, "route": {"intent": intent, "provider": "deterministic", "reason": "Versioned keyword router with exact arithmetic; unsupported tasks stop for review.", "estimated_tokens": 0},
                      "evidence": selected, "unknowns": unknowns,
                      "context": {"selected_characters": size, "full_characters": full,
                                  "token_estimate_note": "Evidence budget uses 4 characters/token as a sizing heuristic. No tokenizer, LLM call or billed-token saving is measured."}}
            with self.db:
                self._log("query_routed", {"intent": intent, "snapshot": state["dataset"]["snapshot"], "evidence_rows": len(selected)})
            return result

    def _actions(self):
        actions = []
        for row in self.db.execute("SELECT * FROM actions ORDER BY rowid DESC LIMIT 50"):
            data = json.loads(row["payload"])
            data.update(action_id=row["action_id"], status=row["status"], snapshot=row["snapshot"])
            actions.append(data)
        return actions

    def _action(self, action_id):
        row = self.db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if not row:
            raise ValidationError("Action not found.")
        data = json.loads(row["payload"])
        data.update(action_id=row["action_id"], status=row["status"], snapshot=row["snapshot"])
        return data

    def _portal_matches(self, action: dict) -> bool:
        row = self.db.execute("SELECT payload FROM portal WHERE entity_id=?", (action["entity_id"],)).fetchone()
        if not row:
            return False
        actual = json.loads(row[0])
        entity = {k: v for k, v in actual.items() if k not in {"snapshot", "synced_at", "action_id"}}
        return (actual.get("snapshot") == action["snapshot"]
                and actual.get("action_id") == action["action_id"]
                and entity == action["payload"])

    def stage_action(self, entity_id: str) -> dict:
        with self.lock, self.db:
            row = self.db.execute("SELECT payload FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            if not row:
                raise ValidationError("Entity not found in the current dataset.")
            entity = json.loads(row[0])
            snapshot = self._meta("dataset")["snapshot"]
            # Reuse a live attempt or a verified completed write. A restored old
            # snapshot needs a fresh review when its previous attempt is stale
            # or its portal postcondition has since been overwritten.
            for old_row in self.db.execute("SELECT action_id,payload FROM actions WHERE snapshot=? ORDER BY rowid DESC", (snapshot,)):
                if json.loads(old_row["payload"])["entity_id"] != entity_id:
                    continue
                old = self._action(old_row["action_id"])
                if old["status"] in {"pending", "approved"} or (old["status"] == "executed" and self._portal_matches(old)):
                    return old
                break
            action_id = "action-" + uuid4().hex[:20]
            payload = {"entity_id": entity_id, "operation": "sync_supplier", "target": "local_mock_portal", "created_at": now(),
                       "payload": entity,
                       "evidence": [evidence(json.loads(r[0])) for r in self.db.execute("SELECT payload FROM suppliers WHERE entity_id=?", (entity_id,))]}
            self.db.execute("INSERT INTO actions VALUES(?,?,?,?)", (action_id, snapshot, "pending", json.dumps(payload)))
            self._log("action_staged", {"action_id": action_id, "target": "local_mock_portal"})
            return self._action(action_id)

    def approve_action(self, action_id: str) -> dict:
        with self.lock, self.db:
            action = self._action(action_id)
            if action["snapshot"] != self._meta("dataset", {}).get("snapshot") or action["status"] == "stale":
                raise ValidationError("Stale action; stage a new action from the current dataset.")
            if action["status"] == "pending":
                self.db.execute("UPDATE actions SET status='approved' WHERE action_id=?", (action_id,))
                self._log("action_approved", {"action_id": action_id, "actor": "local_operator"})
            return self._action(action_id)

    def execute_action(self, action_id: str) -> dict:
        with self.lock, self.db:
            action = self._action(action_id)
            if action["snapshot"] != self._meta("dataset", {}).get("snapshot"):
                raise ValidationError("Stale action; the dataset changed after this action was staged.")
            if action["status"] == "executed":
                if not self._portal_matches(action):
                    raise ValidationError("The portal changed after execution; stage and approve a fresh action.")
                return action
            if action["status"] != "approved":
                raise ValidationError("Action requires approval before execution.")
            self.db.execute("INSERT OR REPLACE INTO portal VALUES(?,?)", (action["entity_id"], json.dumps({**action["payload"], "snapshot": action["snapshot"], "synced_at": now(), "action_id": action_id})))
            self.db.execute("UPDATE actions SET status='executed' WHERE action_id=?", (action_id,))
            self._log("mock_portal_updated", {"action_id": action_id, "entity_id": action["entity_id"]})
            return self._action(action_id)

    def portal(self) -> dict:
        with self.lock:
            snapshot = self._meta("dataset", {}).get("snapshot")
            rows = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM portal ORDER BY entity_id")]
            for row in rows:
                row["stale"] = row["snapshot"] != snapshot
            return {"suppliers": rows, "target": "local_mock_portal"}

    def export_report(self) -> str:
        with self.lock:
            state = self.state()
            def safe(text):
                text = html.escape(str(text), quote=True).replace("\r", " ").replace("\n", " ")
                # Encode Markdown syntax as literal entities, preventing data
                # from becoming links, images, code, headings or list markers.
                return "".join(f"&#{ord(c)};" if c in "\\`*_{}[]()#+:!|" else c for c in text)
            parts = ["# Supplier intelligence report", "", "Local deterministic baseline. No external system was updated by this report.", "",
                     "Snapshot: " + state["dataset"].get("snapshot", "No dataset"), "", "## Net spend by currency", ""]
            for row in state["totals"]:
                parts.append(f"- {row['currency']} {row['amount']} ({row['invoice_count']} unique invoices)")
            parts += ["", "## Proposed supplier entities", ""]
            for row in state["entities"]:
                parts.append(f"- {safe(row['display_name'])}: {safe(', '.join(row['source_supplier_ids']))}. Basis: {safe('; '.join(row['match_basis']))}.")
            parts += ["", "## Review candidates", ""]
            for row in state["review_candidates"]:
                parts.append(f"- {safe(row['left_id'])} / {safe(row['right_id'])}: {safe(row['reason'])}")
            parts += ["", "## Import warnings", ""] + ["- " + safe(w) for w in state["warnings"]]
            parts += ["", "## Source evidence", ""]
            for row in self._records("suppliers") + self._records("invoices"):
                item = evidence(row)
                parts.append(f"- {item['source']}:{item['line']} — {safe(item['excerpt'])}")
            parts += ["", "## Limits", "", "Identifiers are supplied data, not independently verified legal identity. Similar names require review. Currency totals are not converted or combined. The schema accepts amounts with at most two decimal places. Action execution targets only the local mock portal. General reasoning, trained routing, live ERP connectors and DOM automation are future work.", ""]
            return "\n".join(parts)
