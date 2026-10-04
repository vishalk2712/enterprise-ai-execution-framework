"""Reproducible, bounded UK transparency-payment ingestion. No identity labels.

Published payment lines are not invoice IDs or net amounts. Keep line grain,
negative amounts and source receipts; never infer legal identities from names.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .ingest import IngestError, read_table, _load, parse_amount, prepare_dataset, _fingerprint, profile_source

VERSION = "uk-payments-v1"
LICENSE_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
ATTRIBUTION = "Contains public sector information licensed under the Open Government Licence v3.0."
SOURCES = (
    {"publisher": "cabinet", "file": "cabinet-2025-03.csv",
     "page": "https://www.gov.uk/government/publications/cabinet-office-spend-data",
     "url": "https://assets.publishing.service.gov.uk/media/682212f3ced319d02c906137/Expenditure_Over__25_000_-_March_2025__1_.csv",
     "date": "Date", "transaction": "Transaction number", "category": "Expense Type", "description": "Expense Area"},
    {"publisher": "hmrc", "file": "hmrc-2025-03.csv",
     "page": "https://www.gov.uk/government/publications/hmrc-spending-over-25000-march-2025",
     "url": "https://assets.publishing.service.gov.uk/media/680fdb56b0d43971b07f5c76/HMRC_spending_over_25000_for_March_2025.csv",
     "date": "Date", "transaction": "Transaction number", "category": "Expense type", "description": "Description", "postcode": "Supplier Postcode"},
    {"publisher": "treasury", "file": "treasury-2025-03.xlsx",
     "page": "https://www.gov.uk/government/publications/hmt-spend-greater-than-25000-march-2025",
     "url": "https://assets.publishing.service.gov.uk/media/68a888e8969253904d1557d0/Inv_over__25k_-_Mar_25.xlsx",
     "date": "Payment Date", "transaction": "Voucher Number", "category": "Expense Type", "description": "Publication Description"},
)
CONTRACTS_SOURCE = {
    "file": "contracts-2025.jsonl.gz", "url": "https://data.open-contracting.org/en/publication/128/download?name=2025.jsonl.gz",
    "page": "https://data.open-contracting.org/en/publication/128"}


def download_source(source, directory, max_bytes=60_000_000):
    """Public HTTPS allowlist, streamed size/length checks, source receipt.

    Never trust a truncated download, overwrite a cache or unpack archives here.
    JSONL gzip CRC is also checked by the OCDS reader before it returns records.
    """
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    path = root / source["file"]; receipt = root / (source["file"] + ".source.json")
    if path.exists():
        if not receipt.exists():
            raise IngestError("Cached source has no receipt")
        meta = json.loads(receipt.read_text(encoding="utf-8"))
        if path.stat().st_size > max_bytes or (meta.get("bytes") is not None and meta["bytes"] != path.stat().st_size):
            raise IngestError("Cached source has an invalid size")
        if meta.get("url") != source["url"] or meta.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            raise IngestError("Cached source does not match its receipt")
        return path, meta
    allowed = {"assets.publishing.service.gov.uk", "data.open-contracting.org", "fastly.data.open-contracting.org"}
    def check_url(url):
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in allowed or p.username or p.password:
            raise IngestError("Source URL is outside the public-data allowlist")
    check_url(source["url"])
    temporary = path.with_suffix(path.suffix + ".partial")
    if temporary.exists():
        raise IngestError("Incomplete download exists; inspect it before retrying")
    digest = hashlib.sha256(); size = 0
    try:
        with urlopen(Request(source["url"], headers={"User-Agent": "OutcomeEngine/0.9 public data research"}), timeout=30) as response:
            check_url(response.url)
            expected = response.headers.get("Content-Length")
            with temporary.open("xb") as out:
                while True:
                    chunk = response.read(1_000_000)
                    if not chunk: break
                    size += len(chunk)
                    if size > max_bytes: raise IngestError("Public source exceeded download bound")
                    out.write(chunk); digest.update(chunk)
            if expected is not None and size != int(expected):
                raise IngestError("Incomplete download: Content-Length does not match bytes received")
            resolved_url = response.url
        meta = {"file": source["file"], "url": source["url"], "page": source.get("page"),
                "resolved_url": resolved_url, "bytes": size, "sha256": digest.hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "license_url": LICENSE_URL, "attribution": ATTRIBUTION}
        temporary.replace(path)
        receipt.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return path, meta
    except Exception:
        # Preserve partial bytes for diagnosis; no incomplete receipt is issued.
        raise


def csv_text(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    return out.getvalue()


def prepare_payments(directory, sources=SOURCES):
    """Consume complete files, preserve every payment line, reconcile exactly.

    Missing supplier country is explicitly ZZ (unknown), not assumed UK.
    Exact publisher/entity/name/postcode observations define source keys only.
    Repeated transaction references never collapse published expense lines.
    """
    root = Path(directory); suppliers = {}; payments = []; evidence = []; extras = {}
    for source in sources:
        path = root / source["file"]
        receipt = json.loads((root/(source["file"] + ".source.json")).read_text(encoding="utf-8"))
        if receipt.get("url") != source["url"] or receipt.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            raise IngestError("Payment bytes do not match the source receipt")
        header, rows, profile = read_table(_load(path), path.name)
        if profile.content_sha256 != receipt["sha256"]:
            raise IngestError("Payment source changed during preparation")
        source_profile = profile_source(_load(path), path.name)
        if source_profile.content_sha256 != receipt["sha256"]:
            raise IngestError("Payment source changed during profiling")
        required = {"Supplier", "Entity", "Amount", source["date"], source["transaction"], source["category"], source["description"]}
        if source.get("postcode"): required.add(source["postcode"])
        if not required <= set(header): raise IngestError("Published payment schema changed: " + path.name)
        total = Decimal(0); references = set(); duplicates = 0
        for offset, values in enumerate(rows, profile.header_row + 2):
            row = dict(zip(header, values)); name = row["Supplier"].strip(); entity = row["Entity"].strip()
            if not name or not entity: raise IngestError("Payment has no named supplier/entity")
            postcode = row.get(source.get("postcode", ""), "").strip()
            sid = source["publisher"] + "-" + _fingerprint([entity, name, postcode])[:24]
            supplier = {"supplier_id": sid, "name": name, "country": "ZZ", "registration_id": "", "tax_id": "", "postcode": postcode}
            if sid in suppliers and suppliers[sid] != supplier: raise IngestError("Source supplier key collision")
            suppliers[sid] = supplier
            value = parse_amount(row["Amount"], "point")
            if value is None: raise IngestError(f"Invalid published amount: {path.name}:{offset}")
            total += value
            # A line key is intentionally snapshot-bound. It is not an ERP invoice number.
            pid = source["publisher"] + "-" + receipt["sha256"][:12] + f"-line{offset}"
            payments.append({"invoice_id": pid, "supplier_id": sid, "invoice_date": row[source["date"]],
                             "amount": row["Amount"], "currency": "GBP", "category": row[source["category"]],
                             "description": row[source["description"]]})
            ref = row[source["transaction"]].strip()
            duplicates += int(ref in references); references.add(ref)
            extras[pid] = {"source_file": path.name, "source_line": offset, "publisher_entity": entity,
                           "transaction_reference": ref, "original": row}
        evidence.append({**receipt, "rows": len(rows), "published_amount_total": str(total),
                         "repeated_transaction_lines": duplicates, "profile": source_profile.report(include_samples=False)})
    if len(suppliers) > 1000 or len(payments) > 10000:
        raise IngestError("Combined payment cohort exceeds interactive importer bounds; split sources explicitly")
    from .engine import SUPPLIER_FIELDS, SPEND_FIELDS
    prepared = prepare_dataset(csv_text(list(suppliers.values()), SUPPLIER_FIELDS), csv_text(payments, SPEND_FIELDS),
                               source_system="UKPAY", spend={"date_order": "dmy", "decimal_style": "point", "two_digit_year_pivot": 70})
    canonical_total = sum((Decimal(r["amount"]) for r in csv.DictReader(io.StringIO(prepared.spend_csv))), Decimal(0))
    original_total = sum((Decimal(e["published_amount_total"]) for e in evidence), Decimal(0))
    if canonical_total != original_total or prepared.manifest["spend"]["rows"] != len(payments):
        raise IngestError("Payment reconciliation failed")
    prepared.extras["published_payment_lines"] = extras
    prepared.manifest["public_data"] = {
        "version": VERSION, "sources": evidence, "source_recipe_id": _fingerprint(list(sources)),
        "measurement": "published_payment", "amount_basis": "as_published_tax_basis_unknown",
        "currency_policy": "GBP explicitly supplied for these UK sterling transparency publications; not inferred from Departmental Family",
        "supplier_country_policy": "ZZ means unknown; no supplier country asserted",
        "supplier_key_policy": "Exact publisher/entity/name/postcode observation; no fuzzy coalescing or registry enrichment",
        "line_key_policy": "Publisher + source SHA256 prefix + physical source line; repeated references and identical payment lines retained",
        "two_digit_year_policy": "YY >= 70 becomes 19YY, otherwise 20YY",
        "reconciliation": {"rows": len(payments), "source_total": str(original_total), "canonical_total": str(canonical_total), "difference": "0.00"},
        "identity_labels": 0, "limitations": "Published payments only, not all departmental spend, net invoices, savings or verified supplier identities."}
    prepared.manifest["manifest_id"] = _fingerprint({k:v for k,v in prepared.manifest.items() if k not in {"profiles", "manifest_id"}})
    return prepared


def save_prepared(prepared, directory):
    root = Path(directory); root.mkdir(parents=True, exist_ok=False)
    for name, content in (("suppliers.csv", prepared.suppliers_csv), ("spend.csv", prepared.spend_csv)):
        (root/name).write_text(content, encoding="utf-8")
    (root/"extras.json").write_text(json.dumps(prepared.extras, indent=2), encoding="utf-8")
    for target, mapping in prepared.manifest["mappings"].items():
        (root/(target + "-mapping.json")).write_text(json.dumps(mapping,indent=2),encoding="utf-8")
    (root/"manifest.json").write_text(json.dumps(prepared.manifest, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=".outcome/open-data-v09/raw")
    parser.add_argument("--output-dir", default=".outcome/open-data-v09/prepared")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--fetch-contracts", action="store_true", help="Download the separate source-asserted research dataset, not payments")
    args = parser.parse_args(argv)
    try:
        if args.fetch:
            for source in SOURCES: download_source(source, args.raw_dir)
        if args.fetch_contracts: download_source(CONTRACTS_SOURCE, args.raw_dir)
        prepared = prepare_payments(args.raw_dir)
        save_prepared(prepared, args.output_dir)
        print(json.dumps({"output_dir":args.output_dir,"manifest_id":prepared.manifest["manifest_id"],
                          "suppliers":prepared.manifest["suppliers"]["rows"],"payments":prepared.manifest["public_data"]["reconciliation"]},indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Public-data ingestion failed: {exc}\n")


if __name__ == "__main__": main()
