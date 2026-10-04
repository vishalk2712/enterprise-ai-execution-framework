# Real procurement data and an experimental matching model

v0.9 concentrates on ingestion and supervised supplier-name matching. No execution infrastructure was added.

## What actually ran

Three complete March 2025 transparency publications were downloaded and profiled. Original files and per-file download receipts stay in ignored local storage. Both baseline and research imports passed the real dbt gate before analysis.

| Publisher | Published rows | Original format | Repeated transaction lines |
|---|---:|---|---:|
| Cabinet Office group | 1,216 | CP1252 CSV | 160 |
| HMRC | 1,430 | CP1252 CSV | 109 |
| HM Treasury group | 126 | XLSX, first worksheet | 18 |
| Combined | **2,772** | | **287** |

The cohort contains **483 source supplier observations**. Exact publisher/entity/name/postcode observations define source keys. This does not assert 483 distinct legal companies. All 2,772 payment lines survive, including identical-looking lines, credits and repeated transaction numbers. Amounts sum to **GBP 645,400,617.52**, with an exact **0.00** source-to-canonical difference.

These are **published payments**, with unknown tax basis. They are not net invoices, all departmental expenditure, contract award values or savings. GBP is a recorded recipe assertion for these sterling transparency publications. `ZZ` explicitly means supplier country unknown; a UK publisher does not establish supplier domicile. The database retains its legacy invoice table/field names, but queries, reports and the dashboard display payment semantics for this cohort.

Original rows, transaction references and physical line numbers remain in the local extras file. Line keys contain the publisher, source-hash prefix and source line. Revised source files produce a new snapshot; this is replacement ingestion, not an incremental payment-deduplication service.

The adapter now supports `03-Mar-25`. Its mapping artifact records a two-digit-year pivot of **70**: `00..69` means `2000..2069`, `70..99` means `1970..1999`. Numeric two-digit dates still require the correct day/month policy. Changing the pivot changes the mapping fingerprint. The adapter version is `source-adapter-v3`; regenerate and review historical v2 prepared artifacts rather than relabeling them manually.

## Training that was performed

The separate [2025 Contracts Finder OCDS archive](https://data.open-contracting.org/en/publication/128) is 27,992,402 compressed bytes and contains 46,122 unique compiled contracting processes. Its complete gzip stream/CRC was verified. Input SHA256:

`0b207365a72e01db2f123c6b7cff1749f95f756ec31f271a5373146e2864aec0`

Active-award suppliers are joined to parties by their process-scoped party key. Only well-formed, publisher-asserted `GB-COH` identifiers define research reference groups. Missing or invalid identifiers remain unknown, not negatives. The reader excluded 2,544 invalid-ID observations and 240 company groups implicated in names shared by different IDs. It retained one genuine observed row per normalized supplier name within each company; it never generated misspellings or copied registry addresses between variants.

The eligible pool contains 7,205 asserted companies, including 498 with name variations. The experiment selects 1,400 companies, deliberately including those 498 variation groups and 902 singleton groups. This enrichment is a research sampling choice, not representative deployment prevalence.

All registration IDs, tax IDs, LEIs and label-derived aliases are masked from matching features. Actual published names and addresses are used. Entire asserted company groups are split **60/20/20 before candidate indexing**. The trainer fits twelve-feature logistic coefficients from zero initialization on the laptop using NumPy. It is a trained entity-resolution classifier, not an LLM or foundation model.

| Partition | Companies | Observed records | Candidate pairs | Retrieved reference matches |
|---|---:|---:|---:|---:|
| Train | 840 | 1,202 | 29,831 | 438 |
| Validation | 280 | 417 | 7,820 | 181 |
| Test | 280 | 410 | 7,667 | 153 |

Three regularization settings and a review cutoff are selected on validation at an observed reference precision target of 0.95. No negative subsampling is used. Test companies do not select coefficients or cutoff. The original experiment took **46.31 seconds**; a subsequent deterministic fit check reproduced its coefficients, cutoff and metrics exactly. That check is not a new test cohort or promotion claim.

| Held-out test metric | Weighted baseline | Learned classifier |
|---|---:|---:|
| Reference true positives | 13 | 133 |
| Reference false positives | 0 | 3 |
| Selected review pairs | 13 | 136 |
| Reference precision | 100% on 13 selections | 97.79% on 136 selections |
| End-to-end reference recall | 7.88% | 80.61% |

There are 165 known positive reference pairs in the test cohort. Blocking retrieved 153, so its recall ceiling is **92.73%**. The learned classifier found 133; its remaining 32 misses include the 12 missed by blocking. Artifact-level candidate recall is 86.93% (133/153); **80.61% (133/165)** is the end-to-end figure. The difficult retrieved subset with name Levenshtein below 0.88 contains 105 selected reference matches and 3 selected reference false positives.

**Labels are not verified ground truth.** Equality/difference of publisher-asserted Companies House numbers supplies weak `reference_label` values. The registry itself documents identifier-quality problems. A malformed/reused ID can corrupt labels, and same normalized names under different IDs may be subsidiaries or errors. The supplied classifier is uncalibrated, experimental, opt-in and not promoted. Reference-label training is forbidden from producing reviewer probability calibration. It never populates `human_label`, review decisions or the append-only analyst training evidence.

The [model artifact](../models/contracts-finder-pair-model.json) and [aggregate results](benchmarks/contracts-finder-v09.json) are published; raw source records, company IDs, review packs and databases are not bundled. Code is MIT. Source-derived material retains its source attribution and applicable terms: **Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).** This is an original research classifier, not a government-endorsed model.

## Applying the model to real payment records

Two separate databases were analysed against the same dbt-validated payment snapshot. The baseline produced 61 review candidates; the opt-in research model produced 190. Both retained **483 separate supplier observations**, **2,772 payment lines**, identical exact totals, zero human labels and zero actions. This is review-workload evidence. Payment records have no independent labels, so no payment accuracy, verified merges or savings are claimed.

The local evaluation created a 141-row review pack: 70 pairs above the model cutoff, one heuristic near-miss below it and 70 below-threshold pairs selected by deterministic evaluation-ID order. Human label, reviewer, reason and verification-source columns are blank. This is a diagnostic sample, not a representative audit sample or an importable set of approvals. The dashboard remains the authoritative route for appending real reviews; the pack is not ingested automatically.

## Reproduce on a laptop

From the repository directory, using Python 3.10+:

```powershell
python -m pip install -r requirements-ingest.txt -r requirements-ml.txt
python -m enterprise_ai.open_data --fetch --fetch-contracts
python -m enterprise_ai.procurement_model --input .outcome/open-data-v09/raw/contracts-2025.jsonl.gz --output-dir .outcome/open-data-v09/my-experiment
python -m enterprise_ai --db .outcome/my-payments.sqlite --dataset-namespace uk-open-payments-2025-03 analyze-prepared --input-dir .outcome/open-data-v09/prepared
```

`--fetch` downloads only the three pinned payment sources. `--fetch-contracts` downloads the separate research archive. Downloads have HTTPS host, size and Content-Length checks plus SHA256 receipts. Cached files must match their receipt; partial downloads are retained for inspection and never accepted silently. Output and experiment directories must be fresh to avoid overwriting frozen cohorts. The OCDS reader accepts compiled JSONL or gzip, refuses repeated process IDs and verifies the full compressed stream before returning a cohort.

To require upstream dbt, install `requirements-dbt.txt` in an appropriate virtual environment and append `--require-dbt` to `analyze-prepared`. NumPy is needed only for fitting. XLSX ingestion needs openpyxl. Neither fitting nor inference needs a GPU, paid API, Ollama or cloud service.

For a separate, explicitly experimental payment run:

```powershell
python -m enterprise_ai --db .outcome/my-payment-research.sqlite --dataset-namespace uk-open-payments-2025-03 --pair-model models/contracts-finder-pair-model.json analyze-prepared --input-dir .outcome/open-data-v09/prepared
python -m enterprise_ai --db .outcome/my-payment-research.sqlite --dataset-namespace uk-open-payments-2025-03 --pair-model models/contracts-finder-pair-model.json serve --port 8778
```

The last command is an unsecured, loopback-only personal demo. Use the existing documented tenant/RBAC options for protected workspaces. It does not launch an ERP/browser worker. Dataset and matching fingerprints remain bound to the exact data, mappings, recipe and explicitly selected model. Changing the source recipe requires inspection and a new snapshot.

## Assessment of the supplied source search

| Source | Decision for this milestone |
|---|---|
| [Cabinet Office](https://www.gov.uk/government/publications/cabinet-office-spend-data), [HMRC](https://www.gov.uk/government/publications/hmrc-spending-over-25000-march-2025), [HM Treasury](https://www.gov.uk/government/publications/hmt-spend-greater-than-25000-march-2025) | Used complete March 2025 publications. Treasury supplies XLSX, not CSV. Their missing authority IDs make them valuable review targets but not labeled training data. |
| [MOD](https://www.gov.uk/government/publications/mod-spending-over-25000-january-to-december-2025) and councils | Useful additional publishers. MOD's March 2025 publication is ODS; that format is outside the current adapter. Deferred while one complete cohort is established. |
| [Contracts Finder](https://data.open-contracting.org/en/publication/128) | Used its manageable annual JSONL archive. Awards are kept outside the payment ledger; no full award value is allocated to each supplier. Published quality notes mean identifiers are not automatically trusted. |
| [Companies House snapshot](https://download.companieshouse.gov.uk/en_output.html) | Free monthly company reference data. The listed October 2026 archive is 471 MB, with seven smaller parts. Not downloaded for this milestone; no claim of Companies House registry verification. Fuzzy retrieval would generate review suggestions, never ground truth. Current register data also needs historical-name/date checks against 2025 payments. |
| [GLEIF](https://www.gleif.org/en/meta/lei-data-terms-of-use) | Useful legal-entity reference data under CC0 terms. The previous alias benchmark is supplementary and does not label these payment suppliers. |
| Leipzig / academic ER collections and [ALASKA paper](https://arxiv.org/abs/2101.11259) | Secondary regression benchmarks, not substitutes for verified corporate-payment identity labels. The old Leipzig download page was unavailable during verification; no blanket CC-license assertion is made. Check each chosen dataset's domain, labels and terms separately. |
| [OpenSanctions licensing](https://www.opensanctions.org/licensing/) | Separate diligence/sanctions use case. Do not treat all distributed data as unrestricted business training data; terms depend on the dataset and licence. No data incorporated here. |
| USAspending / [SAM Entity API](https://open.gsa.gov/api/entity-api/) | Potential US expansion after UK validation. SAM requires an account/API key and has role-dependent limits. No account was created or credentials requested. |
| Kaggle suggestions | The assertion that all Kaggle procurement data is synthetic is too broad. Judge individual sources by provenance, real variation, label validity and licence; none was needed for this cohort. |

## Next evidence milestone

Verify a stratified set of real payment pairs using authoritative company records and analyst judgment. Record source URLs, effective dates, ambiguity and `Match`/`NonMatch`/`Unsure` separately from candidate scores. Keep subsidiary, parent, trading-name and shared-VAT relationships distinct. Then use the existing reviewer feedback pipeline with locked company cohorts for a supplier-domain calibration experiment. A blinded, separately verified holdout is required before making customer accuracy claims or promoting a production model.
