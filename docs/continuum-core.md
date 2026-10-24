# Shared core for Continuum — 2026-10-24

Continuum is a separate repository. This change extracts the existing operational
audit-chain v1 implementation without changing persisted hashes, the read-only
vault implementation, and the generic Levenshtein primitive. Supplier identity
policies, normalization, model calibration and ERP execution remain here.

The root distribution includes the shared source for compatibility and installs
with `pip install -e .`. The core is independently buildable from
`packages/outcome-core`; it has no runtime dependencies, UI or native binaries.
Compatibility facades preserve `enterprise_ai.audit_chain` and
`enterprise_ai.secret_store`. Root tests and CI now install the package first.

The new generic outbox binds human approval to a canonical payload digest and
deduplicates structured `(subject,target,payload hash)` intents. The caller owns
the transaction. It provides queue/claim/result boundaries, not a generic ERP
worker or exactly-once remote execution. An ambiguous remote outcome must be
reconciled, not automatically replayed. Keep the legacy worker's destination
receipt checks and fencing intact.

## Version/release contract

Core package version: `0.1.0`. Review and merge this extraction before declaring
its first stable release. Use a dedicated `outcome-core-v0.1.0` tag rather than
pretending the existing Outcome Engine (0.10.1) is version 0.1.0.

The eventual dependency must include the package subdirectory:

```
outcome-core @ git+https://github.com/vishalk2712/enterprise-ai-execution-framework@outcome-core-v0.1.0#subdirectory=packages/outcome-core
```

Do not use this future tag until it exists. Until release, install a wheel built
from this reviewed commit or pin the feature commit plus the same subdirectory.
A Git dependency pointing at the repository root installs the wrong project.

No Natively source or assets are used. No consumer transcripts, API keys, audio
or private Continuum source are included in this public framework change.
