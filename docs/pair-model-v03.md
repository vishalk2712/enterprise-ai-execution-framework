# v0.3: an optional learned pair classifier

The engine can now train and execute a small logistic classifier using twelve pair features. The default app still uses deterministic grouping and weighted similarity. This is a supervised entity-resolution model, not an LLM, RLHF system or enterprise deployment.

## What changed

The classifier uses name Levenshtein, name token Jaccard, character trigram Dice, token-sorted name Levenshtein, address Jaccard, postcode agreement, explicit address/postcode availability, country agreement, shared tax/registration identifiers and identifier conflict. Missing evidence is represented separately from disagreement. Features use the matching configuration's character bound.

NumPy is optional and used only for training. Inference, the dashboard and batch execution use the standard library. A model's feature order, finite coefficients, content fingerprint, exact matching configuration and declared domain are checked before use. The fingerprint detects accidental modification; it is not a signature or proof of trusted authorship.

Use Python 3.12 for the optional training environment. The default app and model inference retain Python 3.10+ support; the pinned training dependency has newer Python requirements.

Three fixed L2 penalties (0.0001, 0.001, 0.01) use 1,800 full-batch gradient steps each. Validation alone selects the model and review threshold: maximize recall subject to observed precision of at least 95%, then prefer lower Brier score. Tied scores are selected together. An infeasible target produces zero review selections. The selected artifact is evaluated on test data after selection. Validation and test use the same scalar prediction arithmetic as deployed inference.

The output is labeled `model_estimate`: it is an **uncalibrated classifier estimate**, not an established probability of legal identity. Its review cutoff is a routing policy, not a confidence guarantee. The previous `--calibration` option calibrates the weighted similarity score; it cannot be combined with `--pair-model` because these are different quantities.

Model routing changes only `Review_Candidate` and `Below_Threshold`. Conflict reviews, excluded diagnostic samples and exact authority grouping remain governed by their original policies. Original similarity, heuristic outcome, model features, model ID and review cutoff are retained in audit payloads. No learned score or human match label authorizes a merge or portal write. Effective snapshots include the artifact, so switching models invalidates pending/approved actions on the next analysis.

## Measured experiment and promotion decision

We excluded **all 2,000 entity groups** inspected by the v0.2 SPIDER benchmark. A separate 3,000-group cohort contains 3,747 records. Whole groups are assigned 60/20/20 to train, validation and test **before** building independent candidate indexes. This avoids entities crossing splits and avoids building a transductive index over held-out records. Training uses all 55,571 generated pairs; there is no negative subsampling.

| Untouched test: 600 groups, 742 records | Weighted score at 0.88 | Learned classifier |
|---|---:|---:|
| Labeled matches recovered | 97 / 142 | 142 / 142 |
| False positive review suggestions | 0 | 8 |
| Missed labeled matches, including blocking misses | 45 | 0 |
| Precision | 100.00% | 94.67% |
| End-to-end recall | 68.31% | 100.00% |
| Pairs sent for review | 97 | 150 |

Candidate generation retrieved all 142 test positives among 13,045 pairs. Validation precision was 97.69% with 169 matches and four false positives. **Test precision fell below the 95% target.** The classifier recovers more labeled matches but adds 53 review suggestions, including eight errors. It does not qualify for supplier deployment or a claim of guaranteed 95% precision. The old Magellan recall regression is also still unresolved; this person-domain experiment does not fix or re-evaluate it.

The learned cutoff is about 0.04024 on the uncalibrated output. It must not be read as “4% confidence” or compared directly with the baseline's 0.88 similarity. Brier scores describe the highly imbalanced candidate population; low Brier alone does not establish calibration or useful precision. Correlated pairs within entities limit uncertainty estimates, and changing candidate caps or corpus size changes the population.

Results and complete matching configuration are in [the experiment JSON](benchmarks/spider-pair-model-v03.json). We ship only [numeric model coefficients](../models/spider-pair-model.json), aggregate results and a [model card](../models/README.md). No benchmark person records are included. The artifact's `person-spider-v2` domain deliberately fails the supplier application's deployment check, even if its configuration is supplied.

The experiment was rerun after aligning evaluation arithmetic with standard-library inference to prevent floating-point differences at a threshold tie. Features, cohort, regularization choices, iteration count, validation policy and test labels were not adjusted in response to test performance. v0.2 reports remain unchanged.

## Reproduce the experiment

Download the full SPIDER CSV from its [primary Figshare source](https://figshare.com/articles/dataset/SPIDER_v2_Synthetic_Person_Information_Dataset_for_Entity_Resolution/30472712) into the ignored `datasets/` folder, naming it `spider.csv` to reproduce the filename-inclusive source hash. See [v0.2 provenance](benchmark-results.md) for the verified source-file MD5.

```sh
python -m pip install -r requirements-ml.txt
python -m enterprise_ai.model_experiment --input datasets/spider.csv --output .outcome/spider-pair-results.json --model-output .outcome/spider-pair-model.json
```

No GPU, API key or paid inference is used. Run time in the report is one Windows laptop execution, including indexing, feature extraction and fitting, not a portable speed guarantee. NumPy/BLAS implementations can cause minor training coefficient differences; cohort hashes and counts support reproducibility, not bit-identical training across platforms.

## Build a supplier model from review evidence

```sh
python -m enterprise_ai labels-export --output local-data/review-labels.jsonl
```

The export includes the latest definite human decision, snapshot/config identifiers, numeric features and content-derived `record_keys`/`pair_key`. It does not include raw names or addresses, but IDs and feature exports should still be handled as private customer data. Repeated pairs across runs retain the same pair key when their source records are unchanged.

Before training, deduplicate pair keys, adjudicate contradictory labels, and assign `entity_group_ids` for both endpoints. All aliases, known duplicates and versions of the same real entity must stay in one connected group. Assign whole groups to explicit `train`, `validation` and `test` partitions. Exact endpoint keys are checked in addition to declared groups, but the software cannot prove that your group assignments are correct or catch unrecognized duplicates. Record keys reflect supplied record content; edited record versions still need the same entity group.

Use consenting, representative supplier examples and source/time holdouts where possible. Near-miss and shared-tax sampling is deliberately biased; training on it does not estimate the population's match prevalence. Include representative negatives and positive blocking misses when designing an evaluation. The CLI requires both classes and at least ten labels per split as an input check, not a recommendation that such a small sample is adequate.

```sh
python -m enterprise_ai train-pair-model --labels local-data/supplier-splits.jsonl --domain supplier --output local-data/supplier-pair-model.json
# After independent evaluation and a review of the artifact:
python -m enterprise_ai --pair-model local-data/supplier-pair-model.json analyze --suppliers local-data/suppliers.csv --spend local-data/spend.csv
```

Supply the same `--match-config` used to produce the labels if it differs from the default. Merely declaring `supplier` does not establish training provenance or accuracy; operators must review the data rights and evidence. The CLI does not automatically deploy its output. Batch pipelines accept the same explicit artifact and export its ID in the completion manifest. Fabric and Azure files are templates; no cloud service has been deployed.

The next release gate is an untouched, permissioned supplier test set that measures precision, blocking recall, review workload and false merges. The current implementation already supports collecting that evidence without changing authoritative grouping.
