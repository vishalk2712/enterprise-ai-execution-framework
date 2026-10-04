# v0.8: source preparation and identity correctness

This release adapts the supplied expert patch and source adapter to v0.7's
tenant, RBAC and execution boundaries. It is still a local prototype; the
fixtures are synthetic and there is no validated client matching model.

## Correctness fixes

- Placeholder authority identifiers (`0`, repeated characters, `TBC`, etc.)
  are ignored with a warning. The same guard protects direct governance calls.
  `min_authority_length=0` keeps length restrictions opt-in; a positive value
  is an operator policy. Short synthetic identifiers such as `R1` still work.
- A `(country, registration_id/LEI)` bucket larger than `max_authority_group=25`
  is treated as reused data and excluded from identity grouping. This is a
  conservative policy, not proof the identifier is false. Source excerpts
  remain available for review; tax IDs never independently establish identity.
- A failed clique reports the actual conflicting pair from `validate_cluster`.
  It still reports the first failure, not every possible conflict.
- Supplier names composed entirely of query words need explicit scoping:
  `total spend` covers all suppliers even if one is named Total Ltd;
  `total spend for Total` or `total spend for S-1` selects it. The answer
  discloses ambiguous names it skipped.
- HTTP state and import responses include at most 200 review candidates and
  300 graph nodes. True totals accompany the displayed subset, edges have both
  endpoints, and Previous/Next candidates uses an authenticated paging API.
  Query and report callers continue to receive complete data.

## Prepare a source, inspect the mappings, then import

Run from the repository directory. These commands work in PowerShell too.
CSV/TSV/semicolon/pipe extracts need only Python 3.10+. Excel requires
`python -m pip install -r requirements-ingest.txt` or the package's `ingest`
extra. Only the first worksheet is read. Export formulas to values and
zero-padded numeric IDs to text; they are refused rather than reinterpreted.

```powershell
python -m enterprise_ai.ingest profile --input examples/erp-vendors.csv
python -m enterprise_ai.ingest map --input examples/erp-vendors.csv --target suppliers --source-system SAPGB --output .outcome/sup-map.json
python -m enterprise_ai.ingest map --input examples/erp-invoices.csv --target spend --source-system SAPGB --output .outcome/inv-map.json
python -m enterprise_ai.ingest prepare --suppliers examples/erp-vendors.csv --spend examples/erp-invoices.csv --supplier-mapping .outcome/sup-map.json --spend-mapping .outcome/inv-map.json --output-dir .outcome/prepared
python -m enterprise_ai --db .outcome/v08.sqlite analyze-prepared --input-dir .outcome/prepared
```

Inspect both mapping JSON files before preparation. Re-run `map` with
`--column name=VendorName` (repeat for other fields) to confirm a different
column. Ambiguous dates require `--date-order dmy` or `mdy`; ambiguous decimal
conventions require `--decimal-style point` or `comma`. Conflicting conventions
usually require splitting/correcting the extract; a chosen policy must still
parse every row. Mapping inference proposes column semantics and does not
verify them. Preparation creates local artifacts and does not authorize an ERP
action. Importing replaces the selected database's current dataset.

Use `analyze-prepared`, **not** plain `analyze` on its generated CSVs. The new
command checks CSV and mapping fingerprints and carries the mapping artifact
into the effective approval snapshot. Add `--require-dbt` to run the existing
upstream contracts as well; both dbt and mapping evidence reach the snapshot.
Standalone Python callers use `prepare_dataset(...).analyze_with(engine)`.

The adapter handles banner rows, CP1252, date formats, decimal commas, signed
credits, per-vendor invoice numbering and source-prefixed identifiers. Leading
zeros are preserved by default. Delimiter/header inference can be overridden
through `profile_source(..., delimiter=..., header_row=...)`; sources with
ambiguous semantics should have explicit reusable mappings. Row-width and
duplicate-header errors refuse the source; extra columns are never truncated.
Input readers are bounded at 20 MB/20,000 rows, and the engine's smaller
1,000-supplier/10,000-invoice limits still apply.

## Corrections to the supplied adapter

1. **Tax fields are not automatic legal identity.** SAP describes `STCD1` as
   Tax Number 1; it is no longer a registration-ID synonym. See
   [SAP Control Data](https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/0d0bed912ebd477fa72a08a9441fd7ea/60be25528737f31ce10000000a423f68-125.html).
   The synthetic fixture now has a separate `REGISTRATION_ID` column. An
   explicit `--column registration_id=STCD1` is possible only when an operator
   asserts a verified source contract; inference alone must not promote tax
   associations into legal merges. `MWSKZ` (tax code) is not a tax-amount synonym.
2. **No approximate money.** Values requiring rounding to the current two-place
   ledger are refused. Exactly representable values such as KWD 1.230 are
   accepted as 1.23 with a limitation note; KWD 1.234 is refused. Zero-minor-unit
   currencies reject fractional values. The embedded currency table is a
   versioned adapter policy, not a live currency reference service.
3. **No silent partial source.** Invalid rows refuse preparation by default.
   A manually constructed, reviewed `SourceMapping(allow_rejected_rows=True)`
   explicitly permits partial preparation; its hash changes and every rejection
   is retained in the manifest. Key collisions and monetary precision errors
   remain fatal. Identical invoice rows are still deduplicated by the engine.
4. **No dropped tax evidence.** Auxiliary tax/gross/document-type/company-code
   columns remain in local extras. Net + tax versus gross mismatches produce
   counted notes; this is diagnostic, not tax accounting validation. The core
   still stores one amount, so operators must choose a consistent net basis.
5. **Private extras stay outside matching and engine storage.** `extras.json`
   can contain raw bank numbers from the source. Keep it and all source files
   private under `.outcome`/`local-data`, never upload them to GitHub. It is not
   encrypted by this adapter. Bank/credential columns are masked in local
   profile reports; all sample values are omitted from engine manifests.
   The dashboard shows column fill rates, placeholders and mapping evidence.
6. **The CLI keeps mapping evidence.** The supplied `analyze` example discarded
   its preparation manifest. `analyze-prepared` closes that approval-binding gap.

The manifest includes versioned mappings, original source hashes, canonical
CSV hashes, transformation notes and rejected rows. Fingerprints detect changes
relative to retained artifacts, not a malicious author's invented source.
One source system per run; JSON/Parquet/JDBC and cross-system preparation are
future work. Full net/tax/gross ledgers and new matching features need separate
schema/feature versions and real reviewed supplier data.

## Operational audit chain

Operational events now link canonical event JSON, timestamp, sequence, tenant,
chain identity and previous hash. Event and hash inserts share the operation's
transaction. Startup and audit export verify the entire chain. Rollback removes
both the event and link. Existing history is anchored as found and reported as
`legacy_events`; this does not authenticate its past.

```powershell
python -m enterprise_ai --db .outcome/v08.sqlite audit-verify --output .outcome/audit-checkpoint.json
python -m enterprise_ai --db .outcome/v08.sqlite audit-verify --checkpoint .outcome/audit-checkpoint.json
```

Retain checkpoints in a separate controlled location. An administrator who can
rewrite the database and every checkpoint can recompute hashes. The chain covers
the **operational `audit` table**, not the separate full resolution/label/source
tables. Audit export includes all existing evidence tables plus chain links and
the checkpoint. This is tamper evidence, not an externally notarized ledger.

## Migration and next evidence

`indexed-governance-v5`, `corporate-governance-v2`, adapter v2 and the package
version are explicit. Reanalysis changes snapshots and makes old pending or
approved actions stale. Models/calibrations trained against the old matching
configuration must be regenerated, not silently loaded. Prefer a fresh database
for this demo; preserve previous verified receipts and audit history.

The next product validation is a consenting procurement pilot with a private,
anonymized extract and analyst reviews. No client data, contacts, trained weights,
production ERP writes or commercial accuracy claims are created by this release.

## Local verification

The full local suite collected 209 tests: 203 passed and six optional service/
PyArrow checks skipped. Real local dbt validation passed separately. The latest
adapter and audit-chain checks passed after final read/checkpoint hardening.
Optional real Redis, Vault, Chromium, ML/Parquet and XLSX jobs run in GitHub CI.

On one synthetic input of 1,000 suppliers and 10,000 invoices, the JSON state
response decreased from 3,554,552 to 475,868 bytes (7.47 times smaller), with
5,031 candidate totals, 12,000 graph nodes, 11,000 graph edges and unchanged
currency totals. This fixture does not reproduce the review's 26-times claim;
response size depends on its candidates and metadata, and is not an API-token
savings measurement or general performance guarantee.
