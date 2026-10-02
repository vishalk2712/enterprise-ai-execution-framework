# Experimental SPIDER pair model

`spider-pair-model.json` contains the twelve coefficients, intercept, feature order, configuration fingerprint, validation-selected cutoff and held-out diagnostics of a logistic pair classifier trained from zero initialization. There is no base LLM, adapter or external inference service.

**Purpose:** reproduce a synthetic person entity-resolution experiment and demonstrate a laptop-friendly training path. **Domain:** `person-spider-v2`. **Supplier deployment:** rejected by the engine. Do not change the domain string to bypass that check.

Training uses the next 3,000 complete clusters after excluding the 2,000 groups used in v0.2. Groups are split before candidate retrieval. Training has 55,571 pairs, including 431 matches; validation has 14,039 pairs, including 169 matches; test has 13,045 pairs, including 142 matches. Labels are published `cluster_id` equality, not new human adjudications. All generated cross-cluster pairs are negatives under this closed-cohort labeling assumption; real procurement data does not generally provide that guarantee.

Test results: 142 matches recovered, eight false positives, 94.67% precision, 100% recall. This misses the 95% precision target. Independent calibration, supplier-domain evaluation, drift monitoring, reviewer authentication and production deployment are not provided. Country is constant and tax/registration evidence is absent in this benchmark, so it cannot teach the model those supplier-specific behaviors.

Data attribution: **Praveen Chinnappa, Rose Mary Arokiya Dass and Yash Mathur**, [SPIDER v2, Figshare](https://figshare.com/articles/dataset/SPIDER_v2_Synthetic_Person_Information_Dataset_for_Entity_Resolution/30472712), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The source contains synthetic person information; rule 7 can place different people sharing an account in the same cluster. This repository transforms the names/addresses into numeric pair features and learns coefficients. It distributes no source records. Source-data terms remain applicable; this attribution does not relicense the dataset. Original training/inference code is under the repository's MIT license.

See the [implementation and reproduction guide](../docs/pair-model-v03.md) and [machine-readable aggregate results](../docs/benchmarks/spider-pair-model-v03.json). The artifact is content-fingerprinted, not cryptographically signed; verify provenance before using external artifacts.
