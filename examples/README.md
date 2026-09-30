# Synthetic demonstration data

All supplier names, authority IDs, invoice entries, and commercial relationships in these fixtures are invented for testing. Any resemblance to an existing business is coincidental. The fixtures do not establish identity or tax status for a real supplier.

`suppliers.csv` has ten source records. `spend.csv` has twelve invoice rows, including one deliberately repeated, identical row for `INV-002`. There are eleven unique invoices.

## Expected identity groups

The engine should produce six entities. Each set below is one entity; generated entity IDs are implementation details.

| Source supplier IDs | Evidence for grouping |
| --- | --- |
| SUP-001, SUP-002, SUP-003 | SUP-001 and SUP-002 share their GB registration ID; SUP-001 and SUP-003 share their GB tax ID. The cluster has no conflicting nonempty authority IDs. |
| SUP-004 | Separate authority IDs; a similar name is insufficient to merge. |
| SUP-005, SUP-006 | The same nonempty US registration ID; no conflicting tax ID. |
| SUP-007 | No authority IDs. A similar name and nearby postcode are insufficient to merge. |
| SUP-008 | No authority IDs. Must remain separate from SUP-007. |
| SUP-009, SUP-010 | The same nonempty DE tax ID; no conflicting registration ID. |

Similarity review candidates are suggestions for investigation and must not change these groups. A source identifier match is only a deterministic demo rule, not verification against a corporate register.

## Expected amounts

Amounts remain in their invoice currencies. There is no currency conversion or combined cross-currency total.

| Entity source IDs | GBP | EUR | USD |
| --- | ---: | ---: | ---: |
| SUP-001, SUP-002, SUP-003 | 1650.00 | 250.00 | — |
| SUP-004 | 800.00 | — | — |
| SUP-005, SUP-006 | — | — | 1500.00 |
| SUP-007 | 200.00 | — | — |
| SUP-008 | 300.00 | — | — |
| SUP-009, SUP-010 | — | 1000.00 | — |
| **Currency totals** | **2950.00** | **1250.00** | **1500.00** |

The GBP total covers six unique invoices, EUR three, and USD two. The repeated `INV-002` must be skipped with a visible warning. In this MVP an `invoice_id` is a globally unique document key for one imported dataset; integrations must construct one if external invoice numbers are unique only within a supplier.

## Demo prompts

- What is the total spend by currency?
- Which suppliers appear to be duplicates?
- Which suppliers need manual review?
- Show the evidence for supplier identity resolution.
- What will supplier prices be next year? (The system must disclose the missing evidence.)

Use the approval flow on a matched entity and verify that execution changes only the local mock portal. Repeating execution must not apply a second change. Importing changed data must invalidate an unexecuted approval.

## Independent acceptance cases

`tests/test_acceptance.py` constructs a separate set of synthetic records with different names, identifiers, and amounts. These cases exercise conflicting authority IDs, empty IDs, cross-country matching, injection-shaped names, invalid inputs, and action freshness. They are authored cases, not a representative customer sample or a statistical benchmark.
