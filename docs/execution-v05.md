# v0.5: factual rationales and verified browser execution

The resolution rules and calibrated classifier still determine supplier groups and review tiers. A rationale model cannot alter them. Approval now has an optional second destination: a separate mock ERP accessed through its HTML interface by a headless browser.

## Start the browser demonstration

Python 3.10+, Node.js 22+ and the optional Playwright library are required for this mode. The ordinary Python-only workflow remains available. From the repository folder:

```powershell
npm install --prefix .outcome/browser playwright@1.62.1
# Windows: use the installed Microsoft Edge browser in a fresh headless profile.
$env:OUTCOME_BROWSER_CHANNEL = 'msedge'
python -m enterprise_ai --db .outcome/browser-demo.sqlite serve --demo --browser-erp --port 8765 --erp-port 8770
```

For Linux or a machine without Edge, install Playwright's Chromium instead:

```sh
npm install --prefix .outcome/browser playwright@1.62.1
node .outcome/browser/node_modules/playwright/cli.js install --with-deps chromium
python -m enterprise_ai --db .outcome/browser-demo.sqlite serve --demo --browser-erp
```

Use `py` instead of `python` on Windows if that is your launcher. The dashboard is at `http://127.0.0.1:8765`; the ERP uses a **different origin and database** at `http://127.0.0.1:8770`. Its initial source records come from the imported dataset; an existing ERP database is never silently reseeded. Its transient operator password and approval secret stay in the server process. The browser receives them through stdin, not command arguments, logs or saved action payloads. It uses a fresh profile and never connects to your existing browser profile.

1. Open **Explain this group** for Northbridge in the supplier register.
2. Stage its sync and inspect the source membership, payload, snapshot and destination instance.
3. Click **Approve & run browser**. Approval commits a durable queue job.
4. The worker signs in, opens the approved sync form, fills the exact approved payload and submits it.
5. The action becomes **executed** only after an independently reloaded ERP receipt and destination postcondition pass verification. Inspect the receipt and the saved destination record in the dashboard.

Human Match/NonMatch labels remain training evidence. They do not approve or trigger an action. The default SQLite portal still requires separate approval and execution clicks.

## Rationale model

The dashboard now summarizes group-level legal evidence, source labels and the first pair's measured name/address features. The persistent cache binds the summary to the effective dataset snapshot, matching configuration, model tag and evidence hash. Single-record entities are explicitly described as unmerged.

An optional **already installed local Ollama model** selects `authority` plus one supporting fact through a strict JSON schema. Trusted Python renders those facts into two sentences. Supplier names never enter the prompt; invented facts, missing authority, additional prose, remote-model metadata, oversized output or an unavailable model trigger deterministic fallback. This is constrained summarization, not free-form reasoning or a merge classifier. Model tags are not immutable weight digests; the metadata hash is diagnostic provenance, not proof of exact weights.

For a modest laptop, an optional small model is a starting point; Ollama lists [Llama 3.2 1B at about 1.3 GB](https://ollama.com/library/llama3.2), excluding runtime memory. Install Ollama from its official source and explicitly download the model if desired:

```powershell
ollama pull llama3.2:1b
python -m enterprise_ai --db .outcome/browser-demo.sqlite serve --browser-erp --rationale-model llama3.2:1b
```

No model is bundled or automatically downloaded. Ollama must be running locally on `127.0.0.1:11434`. Its [structured-output API](https://docs.ollama.com/api/generate) supplies the JSON fact plan. The release validates the adapter with controlled model responses and offline fallback; it does not claim measured real-model inference, newly trained LLM weights or improved resolution accuracy.

## Execution contract and failure semantics

| Contract | Enforcement |
|---|---|
| Approved intent | Action ID, dataset snapshot, entity payload, original source records, destination origin and instance are hashed together |
| Destination scope | Only this process's loopback mock ERP is configured; browser requests are restricted to its known document paths |
| Browser targeting | Observe visible DOM controls, choose a unique named control from its index, retain its real node handle, check name/form/document freshness, then act |
| Legal identity | ERP independently checks a conflict-free direct-identity clique before grouping; source IDs and source content must match the staged evidence |
| Capability | HMAC-bound form capability expires after 40 seconds; sessions use same-origin forms, HttpOnly/SameSite cookies and CSRF checks |
| Completion | A reloaded receipt must match action ID, exact intent, source membership, payload hash, instance and actual saved ERP state |
| Retry | Failed jobs stay unexecuted locally; retry first checks an existing receipt, avoiding another submit after a lost acknowledgement |
| Recovery | A crashed worker's persisted lease expires after 90 seconds; a current approved job can then recover its receipt |
| Imports | A bounded IMMEDIATE SQLite transaction fences concurrent writers while this laptop worker executes; changed snapshots invalidate pending approvals |

`execution_jobs` is a durable outbox: `queued → running → verified` or `failed`. Actions remain `approved` after an unverified attempt. The ERP **may have applied** a write before communication failed; the UI says so and retry verifies its receipt. A stale snapshot or changed target needs a fresh staged payload and approval. There is no blind overwrite of a changed ERP group, reassignment of a supplier already owned by another group, or model-driven DONE shortcut.

The execution consumer never writes directly to the ERP database. The ERP web application's own HTML form handler persists the supplier group and receipt atomically. Test doubles call that handler's domain method only for queue-policy tests; the optional end-to-end tests run a real browser.

The DOM design is inspired by [Jev-ultrafast](https://github.com/browser-use/jev-ultrafast), whose demo requires external model API keys. This is an **original, fixed-workflow Playwright adapter**, not Jev's inference backend, speculative action/target heads, claimed speedups or a general-purpose agent swarm. [Playwright's browser documentation](https://playwright.dev/docs/browsers) describes supported Chromium and Edge execution. No Jev source code is copied.

## Audit and verification

The local audit export now includes action payloads, worker states, verified receipts, DOM control hashes and persisted rationales alongside evaluations, human labels and training evidence. Capabilities and passwords are excluded. Exports can contain client source evidence; keep them outside the public repository.

```powershell
python -m unittest discover -s tests -v
$env:OUTCOME_TEST_BROWSER = '1'
python -m unittest discover -s tests -p test_execution.py -v
python -m enterprise_ai --db .outcome/browser-demo.sqlite audit-export --output .outcome/browser-audit.jsonl
```

The browser suite checks actual login, observed-control form submission, independent receipt verification, recovery without another submit, and failure after a destination source change. Queue tests cover label/approval separation, duplicate approval, stale datasets, target/payload changes, expired capabilities, false completion, conflicting legal evidence, writer fencing and crash-after-destination-commit recovery. GitHub CI runs the browser suite in a separate optional-dependency job; core tests retain the Python-only environment.

## Enterprise runway

This is a local single-operator prototype. Its mock login and self-declared dashboard approval are not enterprise authorization. The one-worker, 45-second transaction fence deliberately serializes laptop writes; it is not a scalable distributed execution design. Browser work is capped at 64 KB of approved payload, fixed controls and a single supported workflow; iframes, shadow DOM, arbitrary sites and real ERP logins are unsupported.

Keep the batch/Fabric path independent of browser execution. A future destination adapter needs authenticated reviewer identities and role checks, destination-side version preconditions, transactional idempotency, durable encrypted credential handling, reconciliation and a governed deployment. Replace the laptop transaction fence before scaling worker concurrency. No real ERP or financial ledger is connected by v0.5.
