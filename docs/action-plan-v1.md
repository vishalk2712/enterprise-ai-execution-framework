# Outcome Engine validation plan toward version 1

Prepared on 4 October 2026 against v0.10, commit `eda8c7ea0ca1ff5e19b8b313ca789e0be31a8e49`.

The next objective is to establish whether the engine helps a procurement analyst resolve supplier identities on independently reviewed data. Prioritize one scoring correction, human evidence and a bounded pilot. Reserve v1.0 for a clearly defined and validated workflow. The milestones below define that sequence; the status note distinguishes completed correction work from remaining human evidence.

**Correction status on 4 October 2026:** Milestone 1 is implemented in v0.10.1. The [correction guide](model-correction-v101.md) binds the archived model, derivative, equivalence checks and rerun results. All 45,318 saved reference decisions and graph selections are unchanged. The gated payment queue changes from 195 to 125 while all source payloads and totals remain unchanged. The full local suite passes with six optional service checks skipped. Human labels and the pilot remain pending; the next stage is Milestone 2.

## What was verified

- The existing model has seven constant features across its 29,831 training pairs. `address_present` and `same_country` are constantly one and each has weight -1.1404759251120244. Five constantly zero features already have zero weights. The training code does not exclude constant columns.
- A hypothetical pair with identical addresses and zero name similarity scores 0.0905305 with the same country and 0.2374522 with different countries. Folding the constant contributions into the intercept and zeroing their weights makes both score 0.0905305. This demonstrates an unsupported transfer effect; it does not establish cross-border identity accuracy.
- That hypothetical correction changes predictions on the 45,318 saved reference pairs by at most 5.551115123125783e-16. This check did not mutate the model, retrain it, rerun the graph ablation or validate any external patch.
- The existing private payment review pack contains 141 pairs and zero filled human labels. It has no `reference_label` column. It was sampled from the real government-payment run, not from the labeled Contracts Finder training cohort. Reviewing it adds payment-domain evidence; it cannot by itself measure errors in Companies House reference labels.
- The patch `v09-constant-features.patch` and test file `test_constant_features.py` referenced in the supplied brief were not found in the workspace or Downloads. The brief's asserted 254 passing tests and proposed new model fingerprint are unverified.
- Graph-run reviews are deliberately segregated from baseline calibration. The existing `train --from-reviews` path cannot train a graph-calibrated model from those reviews. Selecting a different CLI flag does not remove this data boundary.
- The v0.10 payment run preserves 2,772 lines and GBP 645,400,617.52, with 483 supplier observations and no human labels or actions. Its single month and absent bank tokens do not validate temporal risk detection.

## Milestone 1 Correct the constant feature behavior

**Start now. Proposed engineering allocation: one focused session, extended if regressions fail.**

Reproduce the defect in regression tests. During fitting, identify constant features using only the training partition, exclude them from coefficient updates and record the feature support in model metadata. Validation and test rows must not determine which features are retained.

For the existing research artifact, create a versioned derivative that folds each constant contribution `weight × training constant value` into the intercept and sets that feature's weight to zero. Retain the original artifact, provenance and cutoff. Generate the derivative's fingerprint from its complete contents; do not paste the brief's shortened model ID into an artifact.

Verify score equivalence and unchanged cutoff decisions on all saved reference pairs. Rerun the graph ablation and payment workload comparison with the derivative. Off-cohort review ordering is expected to change; equivalence on the original cohort does not imply equivalence everywhere. This repair still cannot make the research model calibrated or validated for payment suppliers.

Preserve historical benchmark files with the model IDs that generated their results. Publish a new correction report binding the original and derivative IDs, numerical equivalence checks and rerun results. Do not simply replace model IDs in old measurements.

Add `.coverage` to `.gitignore` and a factual `CHANGELOG.md`. Run the relevant model regressions, full suite and dedicated CI jobs. Publish as v0.10.1 once they pass. A chosen total test count is not an acceptance criterion.

**Gate:** reproducible defect and repair; training-only feature selection; traceable model migration; no change to merge/action authority; passing checks; published evidence of both original-cohort equivalence and changed transfer behavior.

## Milestone 2 Establish the human review protocol

**Begin alongside the correction. Suggested first batch: the existing 141 payment pairs.**

Define `Match` as the same legal supplier entity for this campaign. Parent/subsidiary, shared payment service and shared address relationships are associations, not sufficient identity evidence. Keep `NonMatch` and `Unsure` separate. A name-only impression without adequate supporting evidence may remain `Unsure`.

Prepare a private reviewer view with source records and authorized verification evidence, but hide model scores, algorithm outcomes and any reference labels during initial judgments. Retain the original diagnostic pack unchanged. For each pair record label, reviewer, date, reason, evidence type, verification source and snapshot/evaluation identifiers. Preserve disagreement and adjudication history rather than silently overwriting labels. The current dashboard is the authoritative append-only review path; an edited CSV does not automatically enter its training store.

The human reviewer must make the judgments. The assistant can assemble source evidence and review materials, but must not impersonate a reviewer or manufacture human labels. A second reviewer should independently label an overlapping 30–50 pairs before seeing the first reviewer's answers. This overlap is a practical starting point, not a guarantee of reliable agreement estimates. Report agreement, disagreements and the unsure rate with actual denominators.

The 141 pairs were selected diagnostically, including score-based strata and deterministic ID ordering. Their label proportions cannot be extrapolated to the whole supplier population. Use them to discover failure patterns and assess review effort, then choose additional sampling from the findings.

If the goal is to audit weak Companies House labels, prepare a separate Contracts Finder adjudication sample with those original references retained for comparison after blind review. Keep its results separate from payment-domain judgments.

**Gate:** a completed first batch with evidence and explicit unsure cases; reproducible reviewer provenance; independent overlap where available; a report that states the sampling limits. Missing evidence is a recorded finding, not a reason to invent a label.

## Milestone 3 Measure before fitting a new model

**Start when adequate definite labels and independent entity groups exist; not automatically when the row count reaches 141.**

Freeze a new evaluation campaign using companies/records outside the previous training, validation and test cohorts. Records belonging to the same verified entity must stay in one partition. Related or reused source records need leakage checks before splits are locked. Evaluate existing baselines on these human labels first; retraining is justified only when the comparison and available training evidence support it.

Separate the goals:

1. A diagnostic pair study measures agreement and failure patterns within reviewed pairs.
2. A population precision estimate requires an appropriate sample of predicted review selections, with sampling strata/weights disclosed when needed.
3. End-to-end duplicate recall requires independently established positive pairs or clusters, including positives missed by blocking. Labeling only retrieved candidates cannot establish that recall.
4. Probability calibration requires its own held-out partition and enough positive and negative examples. The code's minimum of ten rows and both classes in each partition is only an input guard, not statistical adequacy.

Keep the weak-label model as a frozen comparison. Allocate additional reviews if the available labels cannot support disjoint train/validation/calibration/test groups or contain too few matches. Report `Unsure`, cross-split exclusions and contradictions separately. Select settings on validation; calibrate separately if justified; evaluate the final chosen model on the untouched test campaign once. Repeated model selection requires a fresh final holdout.

Start with the supported baseline review-training path. Keep graph reviews in their own provenance cohort; graph-specific training/calibration support remains a separate, measured follow-on task. Do not remove the current rejection or mix graph evidence into baseline calibration to make a training run succeed.

Report sample sizes, pair and entity counts, candidate retrieval recall where ground truth permits it, selected-pair precision, end-to-end recall where measurable, review burden and uncertainty intervals appropriate to entity-correlated observations. No human-validated headline metric is published until its denominator and protocol are defensible.

**Gate:** fresh disjoint campaign; frozen baseline comparison; sufficient labels for the chosen analysis; reproducible model/report artifacts; explicit limits; no model-only merges or destination actions.

## Milestone 4 Run one assisted procurement pilot

**Suggested window: two to four weeks after a team and data access are agreed. Calendar time depends on reviewer and data availability.**

Choose one willing procurement/finance team and one fixed supplier-master extract. Agree the task, authorized fields, retention/deletion arrangements, where processing occurs, who reviews results and what acceptance means before data is transferred. Redaction/pseudonymization must preserve useful linkage through stable tokens where needed; do not treat the result as automatically anonymous. Begin with a report and analyst review. A write-back connector is outside this pilot unless separately requested and authorized.

Record the starting snapshot and the team's current cleanup workflow. Have its own analyst label a held-out subset independently. Measure observed false identity groups, unresolved cases, review and preparation minutes per 1,000 records, correction effort, per-currency reconciliation and whether the team used the output. Distinguish review suggestions from actual automatic grouping: false-positive suggestions are not automatically false merges. Keep numerator, denominator, sample selection and reviewer identity for each result.

Compare full workflow time with the team's actual baseline at the same scope and quality. Track preparation, review, correction and support cost; do not infer savings from inference speed alone. Avoid generalizing one team's results to all customers.

Risk dossiers need a separate evidence gate. At least four observed completed monthly periods are needed for the current spend rule, with consistent supplier identifiers and currency coverage. Bank churn needs real, authorized, stable pseudonymous bank-master observations over successive snapshots. Human investigators must adjudicate flagged and unflagged sampled cases before any risk-performance claim. These data requirements need not block a supplier-cleanup pilot that does not claim validated risk detection.

**Gate:** one completed bounded pilot, documented reviewer outcomes, reconciled amounts, workflow time and a useful handover. If the analyst cannot verify identities from the available fields, report that limitation and revise the service scope.

## Milestone 5 Define and release version 1

Release only a workflow whose evidence matches its claims. One pilot and 141 reviewed pairs are project milestones, not automatic proof of production readiness. Version 1 can mean a reproducible, assisted supplier-cleanup product with stated limits; production deployment remains subject to the actual customer's operational and security requirements.

Before tagging:

- Complete the scoring correction and its measured migration.
- Publish the review/evaluation protocol and aggregate human evidence; keep raw records, labels, private evidence and credentials private unless their specific publication is authorized.
- Include fresh held-out results only for metrics that can be measured from the available ground truth.
- Record the pilot's results, scope and limitations with permission to publish any customer detail.
- Update the model card, README, changelog and both version declarations consistently.
- Pass CI, artifact/snapshot verification and the clean-checkout documented workflow.
- Preserve safeguards: model scores, graph associations and risk dossiers cannot authorize merges or destination actions.

## Work to defer until a measured trigger

| Work | Trigger |
|---|---|
| Additional LLM agents or a new foundation model | A recurring task fails the existing baseline and a controlled trial demonstrates improvement |
| Postgres or larger graph infrastructure | Measured contention, memory/latency failure or a documented concurrency requirement; not an arbitrary 5,000-row cutoff |
| Shared multi-tenant hosting or SSO | A customer's actual deployment/access requirement; preserve existing per-tenant databases and RBAC meanwhile |
| A real ERP connector | An authorized pilot needs write-back and defines its semantics and verification |
| More suffixes or alternate string features | Human-reviewed failures identify normalization/name similarity as a bottleneck |
| Automated schema mapping | Timed onboarding shows mapping dominates effort and an evaluated adapter improves it |
| Tax/net/gross extensions | A pilot requires finance reconciliation beyond the currently preserved published payment amounts |

## Responsibility and immediate order

The assistant can implement and test the repair, prepare private review materials, assemble permitted evidence, enforce campaign provenance and run evaluations. You and an appropriate domain reviewer supply human judgments, recruit the pilot and agree its acceptance criteria. No outreach is sent without your explicit authorization.

The next task after the correction is the blinded review materials and human judgments in Milestone 2. No GPU, paid model API or new hosted service is required for those tasks. Time allocations above are planning estimates, not commitments or verified durations. The supplied brief's personal experience and suggested institutional contacts were not verified and are not assumed in this plan.
