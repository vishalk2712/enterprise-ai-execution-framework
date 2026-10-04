# Graph discovery and Investigator risk dossiers

v0.10 adds an opt-in graph retrieval lane and persistent, read-only risk detection. It extends the existing indexed SQLite relationship graph and uses a bounded in-memory bipartite index during ingestion. No Neo4j service, GPU, paid API, new third-party dependency or autonomous ERP authority is required.

## Identity graphs are not corporate communities

Records connect to typed registration, LEI, asserted parent LEI, full-address, postcode, tax and bank-link nodes. Addresses use normalized tokens without legal-name suffix stripping. Address keys require at least three tokens and twelve characters, and are scoped by supplied country. `ZZ` remains unknown; same-country scoping is a retrieval convention, not domicile verification.

Two-hop supplier–attribute–supplier paths generate review suggestions. A four-hop path must contain an asserted ownership relationship and address or legal-key evidence. This supports `source → shared address → anchor → parent LEI → related source` without treating the anchor or parent relationship as registry-verified. Arbitrary postcode/bank/VAT transitive closure and community-based identity merges are forbidden. Shared registered offices, payment services and corporate parents often link distinct legal entities.

Fixed policy `association-paths-v1` bounds postings to 40 suppliers, neighbors to 40 per starting record, retained paths to two per pair, expansions to 20,000 and graph pairs to 10,000. Oversized nodes and exhausted budgets are explicitly reported. Graph discovery unions its pairs with existing candidates and never discards the baseline candidate set. The overall matching evaluation budget still applies.

Pairs carry source names, typed edges, node fingerprints and the graph policy ID in their persistent evaluation. Below-cutoff association paths become `Graph_Context_Review`; identifier/country conflicts remain conflict reviews. The existing direct-identity clique policy remains the sole grouping mechanism. No graph path, classifier score or human pair label authorizes a destination action.

The graph policy ID changes the operational snapshot. The twelve model features and frozen v0.9 model are unchanged, so the uncalibrated model can be used for an explicit retrieval ablation. Existing probability calibration is rejected in graph mode because retrieval changes candidate prevalence. Graph-run reviewer evidence receives a separate sampling-configuration fingerprint, including for pairs also retrieved by the baseline. It is retained but excluded from baseline review calibration and exported with its graph provenance. Graph-specific calibration and a fresh independent holdout remain future work.

## Measured graph ablation

The [aggregate benchmark](benchmarks/graph-discovery-v10.json) uses the frozen v0.9 test companies and cutoff. No model fitting, threshold changes, generated typos or new labels were used. The 410 observed records have 165 positive weak reference pairs.

| Measure | v0.9 baseline | Graph union |
|---|---:|---:|
| Retrieved reference matches | 153 / 165 | 155 / 165 |
| Candidate retrieval recall | 92.73% | 93.94% |
| Classifier-selected reference matches | 133 | 135 |
| Classifier-selected reference false positives | 3 | 3 |
| Classifier-selected reference precision | 97.79% | 97.83% |
| End-to-end reference recall | 80.61% | 81.82% |

The classifier-cutoff plus graph-path lane selects 138 pairs on this cohort. This excludes the separate conflict-only review policy. Candidate pools are 7,667 versus 7,669. The graph added two retrieved reference matches; it did not produce a dramatic recall increase. Address coverage and network evidence constrain the gain. Publisher-asserted Companies House IDs remain weak labels, not human or registry-verified truth. Reusing the same holdout for this declared ablation does not constitute a new independent customer evaluation. Neither precision nor recall is guaranteed on payment records.

## Read-only Investigator

Three evidence detectors run immediately after an accepted import and every 60 seconds while the local server runs. The background monitor uses an interruptible event wait, shares the engine lock for consistent snapshots and stops with the HTTP server. It inspects accepted replacement imports and the latest 100 operational audit events; there is no external streaming connector or independently deployed swarm. Detection is deterministic and auditable, not an LLM's financial judgment.

1. **Monthly positive-outflow spike:** Compare a completed UTC calendar month with the median of the immediately preceding three observed months. Require every current group member in all four months, retain currency separation and require both a 3× ratio and a 1,000-unit increase in that currency. Credits are reported separately. Revised extracts replace the previous supplier/month observation; overlapping snapshots are not summed. Current members missing from the current month, gaps, partial current months and zero baselines cannot produce a spike dossier. These are extract observations, not proof of complete financial coverage or bank settlement.
2. **Bank-link churn:** Flag at least two observed bank-token changes for the same source supplier across the last eight snapshots. Missing supplier IDs or tokens break continuity. A shared bank across different suppliers is not temporal churn. Dossiers contain fingerprints of the existing pseudonymous tokens, not account numbers or full bank hashes. Stable source IDs, source namespaces and bank-link keys are required; changing an ID or key is not silently inferred as a supplier change.
3. **Repeated execution failures:** Attach audit sequence references when an action has two failure events in the latest 100 events. Its approval snapshot must be attributable to the current source namespace. This observation complements the existing execution recovery panel and cannot retry or release the action. It is not full audit-chain verification; use `audit-verify` for that check.

Dossiers bind the rule, policy, namespace, snapshot and evidence to a deterministic ID. Repeated scans deduplicate identical observations. Evidence and dossiers are append-only; inactive observations remain historical, not automatically adjudicated or cleared. Only derived risk tables and scan status are written. Supplier, invoice, entity, analyst-label, approval, action and operational audit tables remain unchanged during scans. Failed imports roll back without advancing risk history.

With RBAC, only an **Investigator** can access `/api/risk-dossiers` or download a paged Markdown dossier at `/api/risk-export`. Stewards, approvers and viewers receive 403; unauthenticated callers receive 401. The same tenant database boundary applies. In the explicitly unsecured loopback demo all local permissions remain available. Source values render through `textContent`, never HTML. Paging is bounded to 100 dossiers; the dashboard displays 25 and refreshes every 30 seconds.

The [real payment run](benchmarks/uk-payments-v10.json) passed dbt and retained 2,772 lines, 483 separate observations and GBP 645,400,617.52 with zero human labels or actions. Its review queue increased from 190 to 195: seven association paths were already retrieved by the baseline, and five were added to contextual review below the classifier cutoff. This is workload evidence, not verified payment accuracy.

The panel discloses insufficient monthly baselines and the number of current suppliers with bank evidence. No dossier does not imply safety. March 2025 government publications contain only one month and no bank tokens: the run reports 483 insufficient monthly baselines and zero bank-observed suppliers. It cannot validate spend-spike detection or bank-churn performance. Separate synthetic fixtures exercise those mechanisms and are labeled as synthetic throughout. Production risk precision, seasonality adjustment, complete payment coverage, independently verified bank-master events, SSO, external streaming and retention/archival of these new evidence tables remain unimplemented.

## Run on your laptop

Use a separate database to preserve v0.9 evidence:

```powershell
python -m enterprise_ai --db .outcome/payment-graph.sqlite --tenant-id student-demo --dataset-namespace uk-open-payments-2025-03 --graph-discovery --pair-model models/contracts-finder-pair-model.json analyze-prepared --input-dir .outcome/open-data-v09/prepared --require-dbt
python -m enterprise_ai --db .outcome/payment-graph.sqlite --tenant-id student-demo --dataset-namespace uk-open-payments-2025-03 --graph-discovery --pair-model models/contracts-finder-pair-model.json serve --port 8779 --auth-config .outcome/v07-auth.json
```

The first command requires the existing dbt environment; omit `--require-dbt` only for an explicitly ungated experiment. The second uses this workspace's existing private identity configuration. A fresh clone must create its own identities with `auth-init`; never publish initial credentials or databases. No ERP worker is started by these commands.

Run `python -m unittest discover -s tests -p test_intelligence.py -v` for graph, temporal-data, calibration-boundary and HTTP-role checks. The full test suite and dedicated intelligence CI job provide regression coverage. Commercial validation still requires independently labeled supplier pairs and analyst review of risk cases; no premium pricing, market differentiation or business outcome has been established by this release.
