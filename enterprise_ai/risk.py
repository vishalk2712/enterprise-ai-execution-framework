"""Read-only signal detectors; only derived observations/dossiers are written.

Source IDs must be stable inside a namespace. Missing observations are unknown,
not zero spend or bank changes. No detector can merge, approve or execute.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
from statistics import median

VERSION = "risk-signals-v1"
POLICY = {"version": VERSION, "history_snapshots": 8, "baseline_months": 3,
          "spike_ratio": "3", "minimum_increase": "1000.00", "bank_changes": 2,
          "audit_tail_events": 100, "max_dossiers_per_scan": 500}
POLICY_ID = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS risk_observations(
      snapshot TEXT PRIMARY KEY, namespace TEXT NOT NULL, captured_at TEXT NOT NULL,
      payload TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS risk_history ON risk_observations(namespace,captured_at);
    CREATE TABLE IF NOT EXISTS risk_dossiers(
      dossier_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, namespace TEXT NOT NULL,
      rule TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS risk_by_snapshot ON risk_dossiers(namespace,snapshot);
    """)
    for table in ("risk_observations", "risk_dossiers"):
        for verb in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'Risk evidence is append-only'); END")


def capture(db, meta, namespace, suppliers, invoices, memberships):
    monthly = {}
    for row in invoices:
        key = (row["supplier_id"], row["invoice_date"][:7], row["currency"])
        value = monthly.setdefault(key, {"positive": Decimal(0), "credits": Decimal(0), "lines": 0, "evidence": []})
        amount = Decimal(row["amount"])
        value["positive" if amount >= 0 else "credits"] += amount
        value["lines"] += 1
        if len(value["evidence"]) < 5:
            value["evidence"].append({"record_id": row["invoice_id"], "source": row["_source"], "line": row["_line"]})
    payload = {"measurement": meta["measurement"], "source_hashes": meta["source_hashes"],
               "suppliers": {r["supplier_id"]: {"entity": memberships[r["supplier_id"]], "bank": r.get("bank_account_hash", "")} for r in suppliers},
               "monthly": [{"supplier_id": k[0], "month": k[1], "currency": k[2], **{f: str(v) if isinstance(v, Decimal) else v for f, v in value.items()}}
                           for k, value in sorted(monthly.items())]}
    db.execute("INSERT OR IGNORE INTO risk_observations VALUES(?,?,?,?)", (meta["snapshot"], namespace, meta["analyzed_at"], canonical(payload)))


def month_before(period, offset):
    year, month = map(int, period.split("-"))
    index = year * 12 + month - 1 - offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def scan(db, meta, namespace, entities, today=None):
    """Pure reads of audit/data/history. Persisting output is a separate function."""
    today = today or datetime.now(timezone.utc).date()
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = db.execute("SELECT snapshot,payload FROM risk_observations WHERE namespace=? AND rowid <= (SELECT rowid FROM risk_observations WHERE snapshot=?) ORDER BY rowid DESC LIMIT ?",
                      (namespace, meta["snapshot"], POLICY["history_snapshots"])).fetchall()
    history = [{"snapshot": r["snapshot"], **json.loads(r["payload"])} for r in rows]
    history = [r for r in history if r["measurement"] == meta["measurement"]]
    current = next((r for r in history if r["snapshot"] == meta["snapshot"]), None)
    findings, incomplete = [], 0

    def finding(rule, key, title, evidence, recommendation):
        if len(findings) >= POLICY["max_dossiers_per_scan"]:
            return
        body = {"snapshot": meta["snapshot"], "namespace": namespace, "policy_id": POLICY_ID,
                "rule": rule, "subject": key, "title": title, "evidence": evidence,
                "recommendation": recommendation, "status": "Requires human investigation",
                "severity": "Review signal", "identity_or_fraud_conclusion": False,
                "read_only": True, "measurement": meta["measurement"],
                "limitations": "Observed extract coverage may be incomplete. Associations and detector signals do not prove legal identity, fraud or a causal effect of merging."}
        body["dossier_id"] = "risk-" + hashlib.sha256(canonical(body).encode()).hexdigest()
        findings.append(body)

    if current:
        # Replacement extracts overwrite a supplier/month observation; never add
        # overlapping snapshots or treat missing supplier/month rows as zero.
        periods = {}
        for observation in history:
            for item in observation["monthly"]:
                key = (item["supplier_id"], item["month"], item["currency"])
                periods.setdefault(key, {**item, "snapshot": observation["snapshot"]})
        current_keys = {(r['supplier_id'], r['month'], r['currency']) for r in current['monthly']}
        by_supplier = defaultdict(set)
        for rid, month, currency in current_keys:
            by_supplier[rid].add((month, currency))
        prior = next((r for r in history if r["snapshot"] != meta["snapshot"]), None)
        for entity in entities:
            members = sorted(entity["source_supplier_ids"])
            banks = []
            for rid in members:
                sequence = [(h["snapshot"], h["suppliers"].get(rid, {}).get("bank", "")) for h in reversed(history)]
                # Missing IDs/tokens break observation continuity, preventing a
                # removed/reintroduced row from implying a bank transition.
                changes, previous, trail = 0, None, []
                for snap, token in sequence:
                    if not token:
                        previous = None
                        continue
                    if previous is not None and token != previous:
                        changes += 1
                    previous = token
                    trail.append({"snapshot": snap, "bank_fingerprint": hashlib.sha256(token.encode()).hexdigest()[:12]})
                if changes >= POLICY["bank_changes"]:
                    banks.append({"supplier_id": rid, "observed_changes": changes, "observations": trail})
            if banks:
                finding("bank_link_churn", entity["entity_id"], f"Repeated bank-link changes: {entity['display_name']}",
                        {"sources": banks, "window": "Last eight observed import snapshots; no account numbers exposed"},
                        "Verify source-system vendor IDs, key stability and authorized bank-master amendments. Do not infer fraud from a hash change.")
            observed_keys = set().union(*(by_supplier[rid] for rid in members))
            currencies = sorted({currency for _, currency in observed_keys})
            for currency in currencies:
                observed = sorted({month for month, code in observed_keys if code == currency})
                if not observed:
                    continue
                period = observed[-1]
                windows = [period] + [month_before(period, i) for i in range(1, 4)]
                if period >= today.strftime("%Y-%m") or any((rid, period, currency) not in current_keys for rid in members) or any((rid, month, currency) not in periods for rid in members for month in windows):
                    incomplete += 1
                    continue
                monthly = []
                for month in windows:
                    items = [periods[(rid, month, currency)] for rid in members]
                    monthly.append({"month": month, "positive_outflow": str(sum((Decimal(r["positive"]) for r in items), Decimal(0))),
                                    "credits": str(sum((Decimal(r["credits"]) for r in items), Decimal(0))),
                                    "lines": sum(r["lines"] for r in items),
                                    "sources": [{"supplier_id": r["supplier_id"], "snapshot": r["snapshot"], "evidence": r["evidence"]} for r in items]})
                latest = Decimal(monthly[0]["positive_outflow"])
                baseline = median(Decimal(r["positive_outflow"]) for r in monthly[1:])
                if baseline <= 0 or latest < baseline * Decimal(POLICY["spike_ratio"]) or latest - baseline < Decimal(POLICY["minimum_increase"]):
                    continue
                changed_group = bool(prior and len(members) > 1 and all(rid in prior["suppliers"] for rid in members)
                                     and len({prior["suppliers"][rid]["entity"] for rid in members}) > 1)
                finding("monthly_outflow_spike", entity["entity_id"] + ":" + currency + ":" + period,
                        f"Observed monthly outflow spike: {entity['display_name']} ({currency})",
                        {"currency": currency, "periods": monthly, "baseline_median": str(baseline),
                         "ratio": str(latest / baseline), "newly_grouped_since_previous_import": changed_group,
                         "basis": "Positive amounts only; credits reported separately; same current source-member cohort in every month",
                         "minimum_increase_currency_units": POLICY["minimum_increase"]},
                        "Check extract coverage, large contracts, seasonality, duplicate line grain and supplier mappings. A newly grouped register does not establish that a merge caused increased payments.")

    failed = defaultdict(list)
    audit = db.execute("SELECT sequence,event,payload FROM audit ORDER BY sequence DESC LIMIT ?", (POLICY["audit_tail_events"],)).fetchall()
    for event in audit:
        if event["event"] in {"browser_erp_failed", "detached_execution_failed"}:
            action = json.loads(event["payload"]).get("action_id")
            scoped = db.execute("SELECT 1 FROM actions a JOIN risk_observations r ON a.snapshot=r.snapshot WHERE a.action_id=? AND r.namespace=?", (action, namespace)).fetchone() if action else None
            if scoped:
                failed[action].append(event["sequence"])
    for action, sequences in sorted(failed.items()):
        if len(sequences) >= 2:
            finding("repeated_execution_failure", action, "Repeated execution failures in the observed audit tail",
                    {"action_id": action, "audit_sequences": sequences, "tail_limit": POLICY["audit_tail_events"]},
                    "Inspect existing execution investigation and destination receipt. This dossier cannot retry or release a job.")
    return {"snapshot": meta["snapshot"], "namespace": namespace, "policy": POLICY, "policy_id": POLICY_ID,
            "scanned_at": timestamp, "dossiers": findings, "count": len(findings),
            "history_snapshots_read": len(history), "insufficient_or_partial_month_baselines": incomplete,
            "bank_observed_current_records": sum(bool(r['bank']) for r in current['suppliers'].values()) if current else 0,
            "audit_events_read": len(audit), "dossier_budget_reached": len(findings) >= POLICY["max_dossiers_per_scan"],
            "coverage": "Accepted replacement imports and latest 100 operational audit events; no external stream subscription or raw bank access"}


def persist(db, result):
    for item in result["dossiers"]:
        db.execute("INSERT OR IGNORE INTO risk_dossiers VALUES(?,?,?,?,?,?)", (item["dossier_id"], item["snapshot"], item["namespace"],
                   item["rule"], result["scanned_at"], canonical(item)))
    metadata = {k: v for k, v in result.items() if k != "dossiers"}
    metadata["dossier_ids"] = [d["dossier_id"] for d in result["dossiers"]]
    db.execute("INSERT OR REPLACE INTO metadata VALUES('risk_scan',?)", (canonical(metadata),))


def dossiers(db, namespace, limit=25, offset=0):
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (limit, offset)) or not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Risk paging requires limit 1..100 and non-negative offset")
    status = db.execute("SELECT value FROM metadata WHERE key='risk_scan'").fetchone()
    status = json.loads(status[0]) if status else {"count": 0, "dossier_ids": [], "coverage": "No dataset scanned yet"}
    current = set(status.get("dossier_ids", []))
    rows = db.execute("SELECT payload FROM risk_dossiers WHERE namespace=? ORDER BY rowid DESC LIMIT ? OFFSET ?", (namespace, limit, offset))
    items = [{**json.loads(r[0]), "active_signal": json.loads(r[0])["dossier_id"] in current} for r in rows]
    return {"dossiers": items, "scan": status, "limit": limit, "offset": offset,
            "total": db.execute("SELECT COUNT(*) FROM risk_dossiers WHERE namespace=?", (namespace,)).fetchone()[0]}


def export_markdown(result):
    lines = ["# Investigator risk dossiers", "", "Signals for human investigation; no fraud or legal-identity conclusion.", ""]
    for item in result["dossiers"]:
        title = html.escape(item["title"].replace("\n", " ").replace("\r", " "))
        lines.extend(["## " + title, "", f"Dossier: {item['dossier_id']}",
                      f"Snapshot: {item['snapshot']}", "", item["recommendation"], "", "```json", canonical(item["evidence"]).replace('`', '\\u0060'), "```", "", item["limitations"], ""])
    return "\n".join(lines)
