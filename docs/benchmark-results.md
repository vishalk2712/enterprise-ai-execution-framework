# v0.2 benchmark results

Measured locally on 1 October 2026 using the committed configuration and seed 1729. Results are dataset-specific, not enterprise supplier accuracy claims. Full machine-readable inputs/configuration fingerprints and counts are in [SPIDER results](benchmarks/spider-v2.json) and [Magellan results](benchmarks/magellan-fodors-zagats.json).

| Dataset | Records | Old / new candidate recall | Old / new precision at 0.88 | Old / new recall at 0.88 |
|---|---:|---:|---:|---:|
| SPIDER v2, 2,000 complete sampled clusters | 2,485 | 69.48% / 99.38% | 75.95% / 100.00% | 57.94% / 63.71% |
| Magellan Fodors-Zagats, official labeled test pairs | 864 | 95.45% / 100.00% | 94.74% / 100.00% | 81.82% / 68.18% |

The baseline uses first-word/country blocking and SequenceMatcher scoring. The new pipeline uses indexed candidate generation and weighted features. This is a comparison of both pipeline changes together, not an ablation isolating either contribution. No threshold was adjusted after inspecting these test results. Precision is restricted to labeled pairs and is not a guarantee of zero false merges in deployment.

SPIDER candidate pairs increased from 14,309 to 62,202 (versus 3,086,370 possible pairs). The new retrieval recovered 482 of 485 positive pairs; scoring retained 309. Magellan candidate pairs increased from 985 to 12,668; all 22 labeled positives were retrieved, but scoring retained 15 versus the baseline's 18. **The Magellan scoring recall regression remains unresolved.** The next model experiment should use separate training/validation data and reserve a new untouched supplier test set.

## Dataset provenance and limits

- [SPIDER v2 on Figshare](https://figshare.com/articles/dataset/SPIDER_v2_Synthetic_Person_Information_Dataset_for_Entity_Resolution/30472712): 50,000 records / 40,000 clusters in the downloaded full file. CSV file ID 59138009, verified MD5 `dcd1de9eaf87f0dcc0e8ca14b476023a`. Authors Praveen Chinnappa, Rose Mary Arokiya Dass and Yash Mathur; CC BY 4.0. The run selects 2,000 whole clusters by stable hash before scoring. Published `cluster_id` labels are used; rule 7 may include distinct people sharing an account, so these labels are not legal-person identity truth. Names/addresses are mapped to the matcher; no tax IDs are invented. A grouped person-domain calibration experiment is recorded in the result but is not installed in the supplier engine.
- [Magellan repository](https://sites.google.com/site/anhaidgroup/useful-stuff/the-magellan-data-repository), [official DeepMatcher dataset index](https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md): structured Fodors-Zagats tables plus the official `test.csv`. Only published test labels count; unlabeled pairs are unknown. Pair splits can share entities, so no calibrator is trained on these splits. Check each dataset's source terms before redistribution.
- [Amazon ML Challenge 2026 on Kaggle](https://www.kaggle.com/datasets/raghavdharwal/amazon-ml-challenge-2026): native streaming TSV adapter implemented and fixture-tested. Official downloaded README confirms three sources, `entity_id`, `business_name`, `business_address`, `country`, and training labels mapping `source1_entity_id` to comma-separated `matched_entity_ids`. Complete selected Source 1 groups are retained. Country strings remain source values. **Full dataset benchmark not run**; no Amazon performance is claimed. Training files total about 1.3 GB uncompressed, and challenge test labels are not published. The uploader declares MIT; verify underlying data rights before commercial redistribution.

Raw benchmark datasets and person records are not bundled in this repository. Fetch from the linked sources into an ignored `datasets/` directory. Full file hashes in reports include filenames as well as file bytes; they are separate from Figshare's raw-file MD5.

## Reproduce

```sh
python -m enterprise_ai.benchmarks --dataset spider --input datasets/spider.csv --max-groups 2000 --output .outcome/spider.json
python -m enterprise_ai.benchmarks --dataset magellan --input datasets/fodors-zagats --output .outcome/magellan.json
python -m enterprise_ai.benchmarks --dataset amazon --input datasets/amazon/train --max-groups 1000 --output .outcome/amazon.json
```

For Amazon, point at the directory containing `train_source1.tsv`, `train_source2.tsv`, `train_source3.tsv` and `train_ground_truth.tsv`. For Magellan, use the directory containing `tableA.csv`, `tableB.csv`, `test.csv`. Offline benchmarking permits 200,000 candidates; the interactive engine retains its lower 50,000 bound. Hash sampling is reproducible; elapsed times depend on hardware. The calibration experiment describes only the labeled candidate sampling distribution and does not estimate missed-candidate probability.
