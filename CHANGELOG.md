# Changelog

This project remains pre-1.0. Version headings describe the implemented code and linked evidence; they do not imply a production deployment or a GitHub release tag.

## 0.10.1 2026-10-04

- Detect constant features from training rows only and hold their coefficients at zero, including intercept-only cases. Record and validate complete training ranges.
- Provide a provenance-bound, dependency-free legacy correction command. It preserves input files, refuses overwrites and double correction, checks exact original label provenance, and verifies original-cohort score/cutoff equivalence before writing outputs.
- Replace the opt-in Contracts Finder artifact with its uncalibrated derivative; archive the original byte-for-byte. Preserve historical benchmarks and add separately bound correction, graph and payment reports.
- Keep all 45,318 saved reference cutoff decisions and frozen graph selections unchanged. The corrected real payment queue is 125 versus 195 previously; source records, entity groups, payment totals and action authority are unchanged. Payment accuracy remains unmeasured.
- Add 16 constant-feature regression tests, consistent runtime/batch/UI versions and coverage-file ignores.
- Document the human-label and pilot gates toward v1.0.

## 0.10

Bounded graph association discovery, separated graph-review evidence, read-only temporal risk detectors and Investigator-only persistent dossiers. See [intelligence guide](docs/intelligence-v10.md).

## 0.9

Real UK government-payment preparation and exact reconciliation, weak-reference Contracts Finder training, frozen company splits and a private unlabeled payment review pack. See [real-data guide](docs/open-data-v09.md).

## 0.8

Source mapping, placeholder identity checks, improved conflict/query handling, paged candidates and verifiable operational audit chains. See [source adapter guide](docs/source-adapter-v08.md).

## 0.7

Retry fencing and investigation, operational retention, coordinator-owned expiry, per-tenant RBAC and vault references. See [security guide](docs/security-v07.md).

## 0.6

API-native mock ERP adapters, detached HTTP workers, Redis Streams and a transactional outbox. See [execution guide](docs/execution-v06.md).

## 0.5

Bounded DOM execution, verified destination receipts, payload-bound approvals and factual group rationales. See [execution guide](docs/execution-v05.md).

## 0.4

Corporate identity clique governance, persistent reviewer evidence, disjoint learning cohorts and review-only probability policies. See [governance guide](docs/governance-v04.md).

## 0.3

Optional multivariate pair training and a historical synthetic person experiment; no supplier deployment of that person model. See [pair model guide](docs/pair-model-v03.md).

## 0.2

Phonetic/N-gram blocking, independent feature scoring, persistent pair audit, bounded sampling, dbt contracts and deployment templates. See [resolution guide](docs/resolution-v02.md).

## 0.1

Initial CSV validation, deterministic supplier/spend evidence and staged local mock actions. The original plan is retained in [roadmap](docs/roadmap.md).
