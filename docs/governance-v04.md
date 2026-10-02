# v0.4: corporate identity governance and reviewer learning

The v0.3 analysis correctly identified domain shift, transitive clustering risk and the need to learn from corporate reviewer decisions. This release implements that pipeline while keeping laptop inference dependency-free. It does not ship supplier weights learned from synthetic people, enable autonomous ledger writes or claim commercial readiness.

## Changes to the proposed blueprint

| Proposal | Implemented policy and reason |
|---|---|
| Corporate training instead of person coefficients | Supplier-domain review training is available. The frozen SPIDER artifact remains an archived experiment and is refused by the supplier engine. |
| P >= 0.99 auto-merge | This is an eligibility tier only for independently calibrated pair estimates plus a shared direct legal identity key. Probability alone does not merge records or approve execution. A 0.99 estimate is not a 99% precision guarantee. |
| Shared tax authority / banking alignment | Shared VAT groups, postal addresses and bank accounts can span separate entities. Only shared registration IDs or LEIs qualify as direct identity evidence. Conflicting identifiers and parent/child relationships block grouping. |
| Connected components | A connected path is insufficient. Every proposed cluster must form a consistent clique of direct identity evidence. |
| Reviewer approval creates verified ground truth | Labels retain self-declared reviewer provenance, source/configuration fingerprints and reasons. They are assertions requiring independent quality checks, not authenticated truth or action approval. |
| Weekly refits | An explicit local command exists. It never activates a model, creates a scheduler or uploads client evidence. Repeated test evaluations are marked as monitoring. |
| Graph lookup on 100k+ rows in milliseconds | SQLite indexes and capped one/two-hop queries are implemented. There is no measured 100k-row performance claim. App bounds remain 1,000 suppliers and 10,000 invoices. |
| Deterministic LLM audit prose without hallucinations | Default rationales use deterministic facts. The optional local model selects whitelisted fact IDs through JSON; software validates and renders the sentences. No generated prose or source instructions authorize actions. |

The application remains a local demonstration with a mock destination. Real ERP writes, authenticated reviewers and governed multi-tenant persistence are not implemented.

## Identity and operational decisions

Registration and LEI buckets are country-scoped for deterministic grouping. Before accepting a bucket union, all member pairs must share a registration ID or LEI and have no conflicting country, registration ID, tax ID, LEI, bank hash or explicit parent/child evidence. The same check runs after a previous bucket has already formed a group, preventing transitive bridges through missing identifiers.

Exact identity grouping is a separate deterministic rule, independent of classifier probability. Its audit tier is `Direct_Identity_Grouped`. It uses supplied assertions; LEI checksum validation is not registry verification. Requiring evidence for every pair favors precision and can leave true duplicates separate when identity keys are missing.

For remaining evaluated pairs with a four-cohort calibrated supplier artifact:

| Condition | Tier | Behavior |
|---|---|---|
| Any recorded identity conflict | `Conflict_Review` | Retain separate and show evidence. |
| P < 0.75 | `Separate_Diagnostic` | Retain evaluated pair in the audit; no negative human label is fabricated. |
| 0.75 <= P < 0.99 | `Human_Review` | Show candidate for a human label. |
| P >= 0.99 without a direct legal key | `Human_Review` | High score alone is insufficient. |
| P >= 0.99 with a direct legal key | `Exact_Identity_Eligible` | Eligibility only; whole-cluster validation remains required. |

Uncalibrated weighted scores, uncalibrated logistic outputs and historical one-dimensional weighted-score calibration never enter these probability tiers. Sampled blocking exclusions remain `Excluded_Diagnostic`. All generated evaluations are currently retained; review workloads and audit retention must be governed before production.

## Transactional feedback and stable cohorts

`training_feedback` is append-only and references the corresponding `review_decisions.decision_id`. A label and its numerical training evidence are written in one SQLite transaction: if either write fails, both roll back. Existing reviews are backfilled with their original provenance. Audit exports include this table. Parquet is an explicit derived export, avoiding inconsistent dual writes on each click.

Current samples use the latest decision for each evaluation and deduplicate identical record pairs across runs. Latest `Unsure` pairs and inconsistent definite labels across repeated evaluations are excluded. Corrections require the latest decision ID. A namespace isolates stable source supplier IDs across clients; it is a cohort key, not an authorization/tenant boundary.

The split manifest locks stable source IDs, including changed versions of records. Reviewed Match edges form entity components. New components are deterministically assigned 60% train / 15% validation / 10% calibration / 15% test. Endpoints assigned to different cohorts are excluded. A new Match linking two existing cohorts fails the refit rather than silently moving a known holdout into training. Contradictory NonMatch labels within a Match component also fail.

Every cohort needs at least ten usable pairs and both classes. This is only a software sanity floor, not statistical sufficiency. Hashing and cross-cohort exclusion can leave few negatives, so even many labels may be insufficient. Collect representative, independently reviewed matches and nonmatches across several entities, countries and variation types. Reviewer-selected candidates contain selection bias; fit quality does not establish population-wide accuracy.

Weights are fitted on train; validation chooses among three fixed L2 penalties and the original selection objective. A fixed monotone Platt fit uses only calibration. Test is evaluated after all choices are fixed. Artifacts include Brier score, reliability bins, diagnostics at 0.75 and 0.99, configuration/domain fingerprints and the count of test evaluations. A reused test is monitoring evidence, not a fresh promotion test. Establish a new independent evaluation campaign before production promotion; do not tune to repeatedly inspected holdouts.

## Commands

Run from the repository folder. On Windows, replace `python` with `py` if needed. Use a persistent database and stable namespace for real review collection:

```sh
python -m enterprise_ai --db .outcome/client.sqlite --dataset-namespace client_a serve
```

Import the client's CSVs and review pairs in the learning section. Do not launch this database with `--demo`, which replaces its current dataset. Synthetic demo labels do not support a production model.

```sh
python -m pip install -r requirements-ml.txt
python -m enterprise_ai --db .outcome/client.sqlite --dataset-namespace client_a train --from-reviews
python -m enterprise_ai --db .outcome/client.sqlite --dataset-namespace client_a feedback-export --output .outcome/labeled_feedback.jsonl
```

Training creates `.outcome/supplier-pair-model.json` and `.outcome/review-splits.json` only after sufficient evidence passes checks. Preserve the manifest across refits. Neither command changes grouping or activates weights. Optional Parquet export:

```sh
python -m pip install -r requirements-feedback.txt
python -m enterprise_ai --db .outcome/client.sqlite --dataset-namespace client_a feedback-export --output .outcome/labeled_feedback.parquet
```

Keep review evidence, manifests, Parquet, registry downloads and client models in ignored private directories. Names, reasons and pair fingerprints can remain sensitive even when raw addresses are omitted. Validate on a new representative corporate holdout before explicitly loading a reviewed artifact:

```sh
python -m enterprise_ai --db .outcome/client.sqlite --dataset-namespace client_a --pair-model .outcome/supplier-pair-model.json serve
```

The artifact must use the exact current matching configuration and supplier domain. `--match-config` is needed when trained with overrides. No automatic promotion gate accepts a model just because it meets an observed precision target.

## Indexed domain relationships

SQLite stores supplier, resolved-entity, registration, tax, LEI, postcode, bank-hash, invoice and invoice-category nodes. Composite primary/incoming indexes support traversal without an all-pairs table join. Identical attribute postings also contribute candidate generation, alongside Soundex and character N-grams. Aliases remain supplied strings, not inferred identity labels.

```sh
python -m enterprise_ai --db .outcome/client.sqlite graph supplier:S-001 --hops 2 --limit 100
python -m enterprise_ai --db .outcome/client.sqlite graph supplier:S-001 --relation HAS_TAX_ID
```

Relations include `HAS_REGISTRATION`, `HAS_TAX_ID`, `HAS_LEI`, `SHARES_POSTCODE`, `HAS_BANK_HASH`, `RESOLVED_TO`, `INVOICED_TO`, `IN_CATEGORY` and `ASSOCIATED_WITH`. Parent LEIs create association edges, never duplicate edges. Category nodes preserve imported categories; no UNSPSC mapping or forecasting is claimed.

The dashboard exposes a two-hop lookup by source supplier ID. Results disclose node-budget truncation and oversized hubs. Defaults skip postings over 150, cap neighbors at 40 per record and stop before the 50,000-pair evaluation budget is exceeded. Graph lookup caps output at 500 nodes and two hops; caps bound retained results, not a promise of constant query time. These bounds may reduce candidate recall. LEIs are globally indexed; other attributes are country-scoped. Conflicting countries still block deterministic grouping.

## Audit synthesis

```sh
python -m enterprise_ai --db .outcome/client.sqlite explain eval-REPLACE_WITH_ID
python -m enterprise_ai --db .outcome/client.sqlite explain eval-REPLACE_WITH_ID --ollama-model YOUR_INSTALLED_LOCAL_MODEL
```

The CLI and dashboard render two sentences from recorded features, outcomes and operational tiers. Reports include up to twenty current-run rationales and disclose the remainder. The optional Ollama adapter uses only `127.0.0.1:11434`, disables proxies and redirects, checks local GGUF metadata, refuses cloud-named/remote models, limits response sizes and requests a strict JSON schema. Source names, addresses and reviewer text are not sent to the model. Validated fact IDs determine the rendered sentences; malformed/injected outputs or an unavailable service fall back to deterministic evidence. No tools, weights download or paid calls are initiated.

The adapter is fixture-tested, including failure and evidence validation. No actual SLM was installed or measured for this release, and temperature zero is not a proof of deterministic inference. Configure a separately installed Ollama server for local-only mode (`OLLAMA_NO_CLOUD=1`, then restart Ollama) before opting in. See [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs), [generate API](https://docs.ollama.com/api/generate), [model details](https://docs.ollama.com/api-reference/show-model-details) and [local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).

For two mandatory facts, the template renderer is usually sufficient. The optional model is an integration surface, not demonstrated product value or a fabricated LLM.

## Corporate benchmark: evidence of the remaining gap

The [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api) exposes legal identity records. [Golden Copy](https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy) separates legal identity (Level 1) from direct/ultimate parent relationships (Level 2). Parent ownership labels must not be used as duplicate labels. GLEIF LEI data is available under [CC0 terms](https://www.gleif.org/en/meta/lei-data-terms-of-use); source data is not a guarantee of identity accuracy or endorsement.

This release fetched the first twenty pages of active records sorted by LEI: 2,000 entities, 108 distinct extra legal-name aliases, 2,108 benchmark rows and 155 positive alias pairs. Identifiers and the aliases field were masked from matching to avoid label leakage. No raw registry records or fitted corporate weights are published. The frozen aggregate [result](benchmarks/gleif-corporate-v04.json) includes an input hash and exact configuration.

| Diagnostic | Legacy first-word / SequenceMatcher | Indexed multi-feature baseline |
|---|---:|---:|
| Candidate pairs | 842 | 58,189 |
| Candidate recall | 45/155 (29.03%) | 134/155 (86.45%) |
| Pairs above review threshold that share an LEI | 9 | 15 |
| Above-threshold false positives in closed registry cohort | 49 | 36 |
| Pair precision in closed cohort | 15.52% | 29.41% |
| Pair recall at review threshold | 5.81% | 9.68% |

This is an alias-retrieval diagnostic, not a representative procurement benchmark. Addresses/postcodes are copied across an entity's aliases, which makes scoring optimistic. Different LEIs are assumed distinct in this closed cohort; historical multiple-LEI cases are not adjudicated. The roughly 22-second measured run is on this machine and this 2,108-row sample, with an explicit 200,000-pair benchmark budget; the interactive bounds are unchanged. The indexed system retrieves more difficult aliases, but fixed scoring performs poorly. Lowering thresholds after seeing these labels would require a new independent holdout.

Reproduce against saved ignored pages, or fetch a new sample (live registry changes will change the hash/results):

```sh
python -m enterprise_ai.corporate_benchmark --fetch --pages 20 --input datasets/gleif-sample --output .outcome/gleif-benchmark.json
```

The existing Amazon business-resolution adapter remains available for a manually obtained native TSV dataset. Its full dataset has not been downloaded or benchmarked. [Companies House data products](https://www.gov.uk/guidance/companies-house-data-products) provide company names, previous names and registered addresses, but a company registry is not an independent noisy procurement ground-truth dataset. No OpenCorporates access, massive registry download or unverified business data collection is implied.

## Migration, validation and next product milestone

v0.4 changes the matching configuration and feature schema. It creates new tables without overwriting old immutable history. Reanalyze CSVs to create current-policy groups and populate the current knowledge graph. Previous approved actions become stale when the effective policy/configuration snapshot changes. Historical reports remain frozen; reproduce them from their release commit. No incompatible weights are silently migrated.

Core tests cover probability boundaries, VAT groups, bank conflicts, explicit parent/child identity, transitive bridges, deterministic ordering, feedback rollback/deduplication, locked holdouts, graph bounds/index use, rationale injection resistance and HTTP endpoints. Optional jobs exercise NumPy calibration training, Parquet roundtrips and real passing/failing dbt gates. GitHub Actions and Azure templates run the same optional training/export checks. Fabric remains a notebook template with explicit source namespace/model/configuration parameters; no cloud deployment is claimed.

The next commercial milestone is a consented procurement pilot with adjudicated corporate identities and held-out entities. Measure candidate recall, precision/recall by variation type, calibration reliability, reviewer workload and end-to-end spend correctness. Preserve separate ownership links and approval controls. Add authenticated review, retention/tenant controls and a real destination contract only against a concrete pilot requirement. These gaps cannot be resolved by raising a confidence threshold or adding a larger LLM.
