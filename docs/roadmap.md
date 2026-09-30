# Development roadmap

This roadmap turns the enterprise execution concept into testable milestones. It is a plan, not a claim that later features already exist. The project starts with supplier identity and spend evidence, using synthetic data and local execution so development can proceed on a laptop without an API key or GPU.

## Milestone 1: Establish the baseline

Version 0.1's scope is CSV validation, explainable supplier matching, spend summaries, evidence outputs, simple request routing and staged updates to a local mock portal. The CLI and local web interface should expose the same core functions.

Release checks:

- A clean checkout can run the documented synthetic-data example.
- Input validation reports actionable errors rather than silently dropping malformed records.
- Match reasons and report totals can be traced to source records.
- Conflicting identity evidence is visible; fuzzy name similarity alone is not represented as verified identity.
- Currency totals remain separate, and invoice values reconcile to the input.
- A proposed mock action has an explicit review/execution lifecycle.
- Tests exercise meaningful failures, including repeated execution and invalid action states.
- The README labels the software as a deterministic prototype with no trained model and no live ERP connector.

The repository should contain code, synthetic examples, tests, documentation and generated synthetic demonstration artifacts only. Keep the private source brief, customer exports, credentials, local databases and runtime logs out of version control, even while the repository is private.

## Milestone 2: Validate the workflow with real reviewers

Conduct approximately five problem interviews and two or three assisted trials. These counts are suggested starting points, not statistical validation. Use data only with the owner's authorization and an agreed handling policy.

Ask about the last actual supplier cleanup: what triggered it, which systems were involved, what fields were available, which errors mattered most, how long review took, and who accepted the final output. Demonstrate a sample report before asking about payment.

For each trial, agree on a fixed input snapshot and label a held-out subset. Have a domain reviewer decide whether candidate pairs are the same supplier, different suppliers or unresolved. Capture disagreements rather than forcing every pair into a binary decision.

Measure:

| Metric | Purpose |
|---|---|
| Precision of accepted matches | Exposes false merges that can corrupt the supplier master |
| Recall of known matches | Shows how much useful duplication is missed |
| Cluster consistency | Finds groups containing contradictory identities |
| Review queue size and acceptance rate | Measures human workload, not just model output volume |
| Per-currency reconciliation | Verifies that aggregation preserves the source amounts |
| Evidence coverage | Shows whether a reviewer can reproduce the decision |
| Preparation, review and correction time | Measures the full workflow cost |
| Repeat usage or a paid follow-on run | Tests whether the result matters commercially |

A synthetic fixture passing its own labels is a regression test, not proof of performance on customer data. Publish measured sample sizes and failure cases. Agree risk tolerances with the pilot customer rather than inventing a universal acceptable false-merge rate.

## Milestone 3: Improve entity resolution selectively

Start by fixing observed input and normalization errors. Add candidate blocking for larger datasets when pair comparisons become a bottleneck. Evaluate additional fields only when they are reliable and available to the intended customer.

Compare three controlled alternatives on identical snapshots and labels:

1. Exact normalized identifiers and names.
2. The preceding baseline plus explicit fuzzy rules and conflict handling.
3. The strongest baseline plus graph relationships or a learned matcher.

Keep data preparation and evaluation splits fixed. Measure candidate-generation recall separately from match classification: a perfect classifier cannot recover a pair that candidate generation never presents. Include input parsing, index construction, refreshes and human corrections in runtime and cost accounting.

Add graph tooling when relationship queries earn its complexity. Useful questions might include which invoices belong to an accepted supplier cluster, which source systems disagree on identifiers, and which changed source record invalidates a previous decision. A graph database is an implementation option, not the product's success metric.

## Milestone 4: Add the first model only when it improves a task

Choose one task with a labeled, recurring failure: routing a supported user request, classifying a supplier pair, or explaining an already computed result. These are distinct tasks and should not share a vague “intelligence” score.

For routing, begin with explicit rules and an unsupported-request path. Compare a small supervised classifier before considering a language-model fine-tune. Training a 64M MiniMind-style language model from random initialization is an educational experiment with different data and compute requirements. It is not automatically the lowest-cost route to a routing classifier. [MiniMind training documentation](https://github.com/jingyaogong/minimind/blob/master/README_en.md)

Use reviewer-approved examples with clear provenance and permitted training rights. Split by customer/source and time where feasible, and keep near-duplicates in one split. Select model settings using validation data; evaluate the chosen candidate once on an untouched test set.

A promotion decision should consider task accuracy, appropriate abstention, latency, memory, operating cost and downstream errors. Routing by “simple versus complex” alone is insufficient: a short request may authorize an important write, while a long request may require only deterministic arithmetic.

Supervised classification is the first training baseline. RLHF is deferred until there is a specific preference or sequential-decision problem that simpler training fails to solve. Human labels by themselves do not make a process RLHF.

If a model is released, include its base-model attribution where applicable, versioned data recipe, training configuration, evaluation scripts, held-out results, hardware and limitations. A fine-tuned derivative must be described as such. Publish weights or adapters in a suitable model repository and link it from GitHub only once the artifact exists.

## Milestone 5: Integrate one real system

Select the destination from pilot demand. Prefer a documented import/API path when it provides stable record IDs and clear write semantics. Evaluate DOM automation when the authorized workflow has no suitable interface; the mock portal is the rehearsal environment, not proof of support for arbitrary ERPs.

Before production writes, implement scoped credentials, target restrictions, reviewer roles, stale-state protection, idempotency, verified postconditions and failure recovery. Recheck third-party licensing and pin reviewed dependency revisions. Browser UI changes and ambiguous execution outcomes need explicit failure paths rather than blind retries.

Jev is a candidate reference, not an existing dependency. Its published performance study is narrow and its documented DOM limitations matter for connector choice. Benchmark it on the actual target workflow if it is adopted. [Jev measurements](https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md)

Add Fabric, dbt, Power BI or Tableau adapters only when a pilot needs them and their incremental setup and support costs are understood. Keep domain logic independent of those adapters so it remains testable offline.

## Milestone 6: Offer a supported service

An initial service can deliver an analyst-reviewed reconciliation run for an agreed scope. Test willingness to pay after the buyer sees and uses the deliverable. Per-run pricing is easier to define initially than charging for an undefined “resolved supplier” or promising a successful compliance audit.

Track unit economics as:

`revenue - data preparation - review/support labour - inference - infrastructure - integration maintenance`

For any later LLM layer, compare cost per accepted outcome at a fixed quality level. Record tokens, failed requests, retries, indexing and training amortization. The rule-only prototype has zero model API usage; that is not evidence that it saves 50% against a task-equivalent production alternative.

Hosting, billing, authentication, tenant isolation, retention controls, monitoring and incident response become required product work before serving multiple customers. They should not be represented as implemented because the local demo runs.

## Sequencing and budget

Use milestone gates rather than promising the brief's twelve-week production schedule. An experienced developer and a student learning Python will progress at different rates, and customer data access may dominate elapsed time.

The first useful spending decision comes after observing a bottleneck. A GPU serves training; a lakehouse serves shared data infrastructure; paid inference serves an identified language task. None is needed merely to make the architecture diagram appear enterprise-ready.

Keep a chosen monthly cap and require each new dependency or paid service to answer: which measured failure does it solve, how will improvement be tested, and what is its ongoing support cost? Advance when the workflow becomes more useful, reproducible and affordable.
