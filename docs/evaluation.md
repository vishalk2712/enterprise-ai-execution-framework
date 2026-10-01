> v0.2 update: See [current resolution implementation](resolution-v02.md) and [measured benchmark results](benchmark-results.md). The initial v0.1 plan below is retained as project history.

# Evaluation plan and limits

## What this first version can demonstrate

The product ingests supplier and invoice CSV files, resolves conservative identity groups, computes currency-separated spend, returns source evidence, and stages approved updates to a local mock portal. It has no trained model, verified external registry, live ERP connector, or autonomous browser worker. The demonstrations use invented records.

Passing the acceptance suite establishes that the tested contracts hold on these cases. It does not establish production accuracy, business demand, legal compliance, model quality, or readiness to update customer systems.

## Reproducible correctness checks

Run from the repository root:

```console
python -m unittest discover -s tests -p "test*.py" -v
```

The demo truth is recorded in `examples/README.md`. Independently authored cases in `tests/test_acceptance.py` check the following:

| Concern | Expected outcome |
| --- | --- |
| Exact supported identity links | Merge only compatible nonempty authority IDs within one country. |
| Similar names and empty IDs | No automatic merge. |
| Conflicting or transitive authority IDs | No entity contains incompatible nonempty registration IDs or tax IDs. |
| Different countries | Matching ID text does not merge suppliers across countries. |
| Money | Decimal-exact sums; currency totals remain separate. |
| Identical invoice repeats | Count once and disclose the skipped duplicate. |
| Conflicting invoice repeats | Reject input; do not silently choose a row. |
| Broken schema, bad amounts, invalid dates/currencies, unknown suppliers | Reject invalid input before it affects results. |
| Injection-shaped data | A supplier name cannot authorize an action or bypass approval. |
| Context budget | Respect the supported bound; expose retained evidence. |
| Actions | Approval required; execution is local and idempotent; changed data invalidates an earlier unexecuted approval. |

These checks target user-visible outcomes. They deliberately avoid requiring a particular generated entity ID, text template, or internal graph representation.

`tests/test_http.py` additionally starts the real standard-library server on a random loopback port for each case. It exercises demo ingestion, state retrieval, queries, approval and execution, exported reports, failed-import preservation, rejected cross-origin mutations, the Host header guard, malformed request bodies, and traversal-like paths. Each test shuts down its server thread and closes its in-memory database. These integration checks cover the local demonstration boundary; they do not establish production authentication or deployment security.

On 30 September 2026, all 47 checks passed locally: 21 acceptance checks, 8 HTTP integration checks and 18 regression checks. Regression coverage includes ID boundaries, Unicode names, missing currencies, query scope, missing-ID sentinels, source-line accuracy, restored dataset approvals, portal postconditions and escaped report text. The dashboard's question → evidence → stage → approve → execute → portal flow was also checked in a browser. Re-run the command above after changes; these checks do not establish production readiness or customer-data accuracy.

## How to evaluate on real customer data later

Obtain permission to use an anonymized export, preserve invoice currencies, and have a domain reviewer label identity pairs and incompatible clusters before tuning rules. Keep a held-out set from different organizations to reduce leakage through repeated names and IDs.

Report separate measures:

- **Auto-merge precision:** correctly merged labeled pairs divided by all automatically merged labeled pairs. Inspect cluster consistency as well, because one bad bridge can join many records.
- **Auto-merge recall:** correctly merged labeled pairs divided by all pairs labeled as the same entity. Optimize conservatively; a review queue is preferable to silently combining unrelated suppliers.
- **Review workload:** candidate pairs and reviewer minutes per imported source record; track false alarms as well as missed duplicates.
- **Spend correctness:** exact reconciliation of unique invoices and per-currency totals against a trusted ledger export.
- **Answer support:** the proportion of factual answer statements directly supported by retained source evidence, assessed by reviewers.
- **Action correctness:** approved payloads applied exactly once to the intended entity; no stale approvals accepted.

Publish counts, labeling protocol, and examples of failures beside percentages. A small hand-built fixture cannot substantiate a general accuracy percentage.

## Testing graph retrieval and routing claims

Do not start with a required savings claim. Compare a simple lexical/SQL baseline against any future graph method using the same question set, source snapshot, model, prompt, answer quality criteria, and context limit. Record ingestion cost, index freshness, retrieval latency, selected-context size, answer correctness, and failures.

If a future version calls a model, use that provider's actual usage counters to measure billed input and output tokens. Character-based estimates in a local prototype are diagnostics, not API billing measurements. Compare total successful-workflow cost, including retries and index construction, rather than one prompt alone. There is currently no evidence from this prototype for a 70× token reduction or 50% cost reduction.

A learned router should be compared against deterministic routing and a simple classifier on held-out questions. Collect reviewed examples before training; measure misrouting, fallback frequency, quality, latency, and cost. A small parameter count alone does not establish useful routing ability.

## First user validation

Ask a procurement operator or accountant to compare one generated report against their existing reconciliation workflow. Record whether it finds a real problem, whether every proposed merge can be checked, time spent reviewing it, and willingness to pay for a repeatable deliverable. Keep these observations separate from synthetic test results.
