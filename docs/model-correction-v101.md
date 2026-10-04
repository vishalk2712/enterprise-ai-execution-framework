# Constant feature correction in version 0 10 1

v0.10.1 corrects unsupported feature effects without refitting the published weak-reference model. Future training detects constant columns using only training rows and holds those coefficients at zero. Validation, calibration and test observations cannot unmask a feature that never varied during fitting. Models record training ranges; validation rejects contradictory ranges or nonzero declared constant coefficients. Legacy artifacts without this metadata remain readable so historical experiments can be reproduced.

## Observed defect and exact migration

The original Contracts Finder model trained on 29,831 pairs. Seven of twelve features were constant: address presence and same country were one; postcode equality/presence, shared tax/registration ID and identifier conflict were zero. The two constantly one features each acquired weight -1.1404759251120244. Their effect was not identifiable separately from the intercept. This did not teach the model how missing addresses or differing countries affect legal identity.

The derivative adds each old constant contribution to the intercept and sets its coefficient to zero. The intercept changes from -5.4902309431464245 to -7.771182793370474. All variable coefficients and the review cutoff 0.10782381324458289 are retained. There is no refit, threshold selection, new label, probability calibration or automatic activation.

The original model is retained byte-for-byte at [archived artifact](../models/archive/contracts-finder-pair-model-v09.json). Its fingerprint is `61c3fc29844cdf4c14a138ecae4a46131b073282df68e1f48c5f4c9fa39771ac`. The [current derivative](../models/contracts-finder-pair-model.json) is `c3058988da0cdce96e3dd0f45e752636c1de6b71fa851fafd90b682aa3f139cb`. Its correction metadata binds that source and the exact original label fingerprint. Model fingerprints bind content; they are not digital signatures.

## What the measurements establish

The [aggregate correction report](benchmarks/constant-features-v101.json) checks all 45,318 saved reference pairs: 29,831 training, 7,820 validation and 7,667 test rows. Maximum prediction change is 5.551115123125783e-16; zero review-cutoff decisions change. Brier differences are floating-point noise. Preserved artifact diagnostics retain their original weak-label meaning. This equivalence does not validate transfer to other supplier data.

The [paired graph rerun](benchmarks/graph-discovery-v101.json) uses the same 410 records, 165 known positive reference pairs, candidate pool and graph policy as v0.10. Both artifacts select 135 reference matches and three reference false positives on the union: 97.83% reference precision and 81.82% end-to-end reference recall. Graph/path selections are unchanged. This repeated frozen-cohort ablation is a regression check, not a fresh held-out customer test.

The [gated real-payment rerun](benchmarks/uk-payments-v101.json) passed dbt and audit verification. It retains identical supplier and payment payloads, 483 separate entities, 2,772 payment lines and GBP 645,400,617.52. All 10,809 evaluated pairs and feature vectors are unchanged. Seventy algorithmic outcomes change and the graph-enabled review queue falls from 195 to 125. Human labels and actions remain zero. Removing a spurious feature effect can remove correct as well as incorrect suggestions; a smaller queue is not an accuracy, recall or savings claim.

A synthetic numeric counterfactual with identical addresses and no name similarity scored 0.0905305 for the same country and 0.2374522 for different countries under the original weights. Both score 0.0905305 under the derivative. The example demonstrates unsupported asymmetry, not cross-border supplier identity truth.

Historical v0.9/v0.10 benchmark JSON files retain the fingerprints that generated them. New results bind both artifacts explicitly. No historical measurement is relabeled as if it originally used the derivative.

## Reproduce the correction

Use the retained local experiment pairs, which are private and excluded from the repository. A fresh checkout must first reproduce that experiment from the authorized original source using the [v0.9 data recipe](open-data-v09.md). Outputs must use new paths; the command refuses overwrites, mixed input/output paths, a different label fingerprint, repeated correction and calibrated legacy migrations. It writes no output if original-cohort numerical or cutoff equivalence fails.

```powershell
python -m enterprise_ai.model_correction --model models/archive/contracts-finder-pair-model-v09.json --labels .outcome/open-data-v09/experiment/reference-pairs.jsonl --output .outcome/constant-feature-derivative.json --report .outcome/constant-feature-equivalence.json
```

The reproduction requires the original ordered pair JSONL. Re-fitting with the corrected trainer deliberately produces a different training trajectory and artifact fingerprint; the label fingerprint remains the same if the input rows are unchanged. A refit is not a recreation of this no-refit migration. NumPy is needed for new fitting, not the migration or inference.

Use a fresh database to preserve older runs. The dbt gate requires the existing dbt environment:

```powershell
python -m enterprise_ai --db .outcome/v101-payments.sqlite --tenant-id student-demo --dataset-namespace uk-open-payments-2025-03 --graph-discovery --pair-model models/contracts-finder-pair-model.json analyze-prepared --input-dir .outcome/open-data-v09/prepared --require-dbt --output .outcome/v101-payment-report.md
```

Loading a derivative and reanalyzing changes the operational snapshot. Old model-bound action approvals become stale and cannot execute; source-input hashes and legal grouping remain unchanged. The migration never rewrites prior approvals, labels or audit rows.

## Validation and remaining evidence

Sixteen regression tests cover nonbinary constants, transfer counterfactuals, provenance, immutable inputs, overwrite refusal, double correction, calibration refusal, metadata tampering, training-only masks, variable columns, intercept-only fitting and stale approvals. The full local suite ran 255 tests: 249 passed and six optional service checks skipped. Dedicated CI environments exercise those services and model fitting.

This repair does not establish that the model is a legal-identity probability. It remains an experimental, uncalibrated reference estimate with weak publisher-ID labels. Graph review evidence remains excluded from baseline calibration. Human adjudication, a fresh evaluation campaign and an assisted pilot remain required; the [validation plan](action-plan-v1.md) sets out those gates. No v1.0, independently verified accuracy or production readiness is claimed.
