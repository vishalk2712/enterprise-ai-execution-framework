# v0.7: bounded recovery and tenant access

This release hardens the existing **local mock ERP**. It adds a three-attempt execution budget, persistent investigations, archive-before-delete maintenance, owner-controlled expiry, opt-in dashboard roles, tenant pins and read-only vault adapters. It is not a shared multi-tenant SaaS, enterprise SSO deployment or real SAP/Coupa connector.

## Protected laptop demonstration

Run from the repository directory; on Windows use `py` if that is your installed launcher:

```sh
python -m enterprise_ai --tenant-id student-demo auth-init --output .outcome/student-auth.json
python -m enterprise_ai --tenant-id student-demo --db .outcome/student.sqlite serve --demo --auth-config .outcome/student-auth.json --api-erp --port 8765 --erp-port 8770 --worker-token-file .outcome/student-worker.token
```

In another terminal:

```sh
python -m enterprise_ai --tenant-id student-demo worker --coordinator http://127.0.0.1:8765 --token-file .outcome/student-worker.token --consumer laptop-1
```

The first command creates password hashes and a separate **private** `.outcome/student-auth-initial-logins.json` containing initial passwords. Neither file is overwritten. Read that file locally, sign in as `steward`, stage a supplier, sign out, then sign in as `approver` to release it. Passwords, worker tokens, databases and archives must stay outside GitHub; `.outcome/` is ignored. Bootstrap files depend on the operating system's file permissions; this command does not configure Windows ACLs or enterprise password policy. Delete the initial-password file after secure handover and use protected identity configuration/backups.

| Role | Dashboard permissions |
|---|---|
| Viewer | Read evidence, results, audit and destination records |
| Steward | Viewer access, import data, append review labels and stage actions |
| Approver | Viewer access, approve staged payloads, authorize bounded retries and execute the basic local portal demo |
| Investigator | Viewer access, check authoritative destination receipts and record investigation findings |

The server enforces these permissions, including exports. Hiding a button is not the authorization boundary. Reviewer/approval identities come from the session, not submitted names. Even a principal with both steward and approver roles cannot approve their own staged action. A worker bearer token grants worker endpoints only; a dashboard cookie cannot claim worker jobs. Sessions are random, HttpOnly, SameSite=Strict, expire after 30 minutes of process-monotonic time, and are revoked by logout/restart. Login is same-origin, passwords use salted PBKDF2-SHA256, and five failed attempts temporarily fence a username. This local bootstrap is not Entra ID, MFA, a complete intrusion defense or a public internet login service.

Protected DOM execution requires `--browser-erp --distributed` and the optional browser dependencies from [v0.5](execution-v05.md). Redis setup remains in [v0.6](execution-v06.md). `serve --demo` without `--auth-config` remains an explicitly **unsecured loopback demonstration**.

## Tenant isolation and migration

Each tenant has its own coordinator process, engine database, ERP database/instance, identity configuration, worker credential, Redis namespace and archive directory. Set distinct ports and paths when running two tenants. A persisted tenant ID prevents reopening either database as another tenant; a configured bank-key fingerprint also prevents silent key changes. Workers verify the coordinator's advertised tenant. Foreign session cookies and tenant headers cannot select another tenant's data.

Use **one coordinator per database**. SQLite writes remain serialized; do not share SQLite across hosts. Separate-process isolation is an intentional boundary, not a shared-table row-level tenancy implementation. OS administrators and direct CLI/database access remain trusted; RBAC governs dashboard HTTP operations. TLS termination, managed identities/SSO, per-user lifecycle management, backups and a vendor sandbox connector are deployment work still required.

Existing v0.6 databases open as tenant `local`. Export and re-import into a fresh database to adopt a named tenant or bank protection; do not edit the tenant metadata to bypass the guard. Enabling RBAC invalidates prior pending/approved actions, requiring signed-in staging and fresh approval. A database that has enabled RBAC refuses an unsecured dashboard restart. Approved action/dataset snapshots include the tenant and bank-key policy on subsequent imports. Existing evidence and human labels are not silently relabeled or rewritten.

## Retry budget and dead letters

The coordinator counts **issued execution grants**, not Redis redeliveries. Default `--max-attempts 3` accepts 1..10. Duplicate references do not spend a new attempt while a lease is active. An explicit failure requires an approver's retry; a crashed running lease can be reclaimed. After the budget, both paths persist `ERRORED_REQUIRES_INVESTIGATION`. No fourth grant, automatic reset or replay is available. The legacy in-process DOM worker also fences at three attempts.

The database is authoritative even when Redis is offline. Its dead-letter record is republished when the broker recovers. Redis dead letters contain only `action_id`, `intent_hash`, retry `epoch`, `attempts` and `reason_code`; no supplier payload or credentials. The stream is `outcome:dlq:<coordinator namespace>`. An atomic Lua operation deduplicates that reference and acknowledges/deletes the normal stream entry. Normal successful/failed acknowledgements likewise use atomic `XACK` + `XDEL`, compatible with tested Redis 7.2. The dedicated stream supports one configured consumer group; do not add unrelated groups and expect this deletion policy to preserve their deliveries.

An investigator's **Check receipt & record finding** verifies the exact approved intent and actual destination membership. A previously committed receipt marks the historical action executed without a new PUT/form submission. A missing receipt records the finding but leaves the attempt budget exhausted. Correct the underlying evidence/connector and obtain a fresh payload-bound approval for a changed action; investigation itself never authorizes another write. Investigations and append-only audit evidence are retained, including closed findings. The release does not automatically purge DLQ history or reset poison jobs.

## Expiry without distributed-clock comparisons

Workers receive opaque capabilities and lease IDs; they never decide expiry using their own clock. The coordinator owns its 90-second lease using **its process's monotonic clock**. The co-located mock destination owns its 40-second capability using **its process's monotonic clock** and checks the coordinator's active lease/snapshot before commit. Persisted wall timestamps are diagnostic/retention metadata, not execution authority. Restart invalidates in-memory capabilities and leases; durable jobs/receipts permit recovery.

Monotonic counter values are never compared between machines or persisted as reusable authority. The sandbox therefore survives local wall-clock jumps, but it does not prove arbitrary distributed vendor expiry: a real connector needs a destination-enforced expiry/version contract and trusted coordinator authorization. Retention and outbox scheduling still use wall dates where appropriate; clock jumps can affect maintenance timing, without reviving execution authority.

## Operational retention

Detached coordinators run maintenance at startup and hourly. Defaults: 90 days, with archives in `<database-stem>-archives` beside the selected database; override with `--retention-days` and `--archive-dir`. File bootstrap credentials default to `.outcome/<tenant-id>-worker.token` for named tenants (`.outcome/worker.token` for the legacy local demo). Each pass selects at most 200 **verified** jobs older than retention, writes a content-addressed JSONL archive with SHA-256 and fsync/atomic rename, rechecks unchanged rows, and then removes their live execution/outbox rows in a transaction. Export failure retains the source rows. JSONL keeps this path dependency-free; Parquet conversion can be added to an external archival pipeline.

Approval/actions, audit, human labels, tombstones, archive manifests, investigations and authoritative ERP receipts remain for traceability and idempotency. Queued, failed and uninvestigated jobs are never aged out. This is operational compaction, not a legal data-erasure policy or a guarantee that all tables have bounded growth. Monitor backlog and run additional batches if completion volume exceeds 200 jobs/hour. Protect archives with the same access and backup controls as the source database.

For an additional batch:

```sh
python -m enterprise_ai --tenant-id student-demo --db .outcome/student.sqlite maintenance --archive-dir .outcome/student-archives --retention-days 90
```

Deletion makes SQLite pages reusable. To shrink the physical file, **stop the server/workers** and add `--vacuum`; do not vacuum an active coordinator. Use the same vault/key settings for a protected database. Archive hashes establish content integrity, not encrypted storage or independently signed audit authenticity.

## Vault-managed credentials and bank linkage

The read-only adapters resolve named secrets. There is **no file fallback** when a configured reference fails. Values are absent from CLI arguments, audit events, broker messages and dashboard state. A worker's authenticated grant still necessarily contains the bounded mock ERP credential/capability in memory. Loopback HTTP is a development boundary; remote service deployment requires verified HTTPS.

HashiCorp [KV v2 read-data](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2/cookbook/read-data) configuration:

```json
{"provider":"hashicorp","url":"https://vault.example.com","mount":"secret","token_env":"OUTCOME_VAULT_TOKEN"}
```

Store each referenced KV v2 document as `{"value":"<secret value>"}`. Use scoped vault authentication; the bootstrap Vault token is still supplied by the runtime environment/Vault Agent, not committed in the JSON. Local HTTP is allowed only for a loopback development Vault. Other vault connections require certificate-verified HTTPS; redirects are rejected, reads are bounded, and failure messages omit server responses/secret values.

Azure configuration:

```json
{"provider":"azure","url":"https://YOUR-VAULT.vault.azure.net"}
```

Install `requirements-vault-azure.txt`. The adapter uses Microsoft's [`DefaultAzureCredential` and `SecretClient`](https://learn.microsoft.com/en-us/azure/developer/python/walkthrough-tutorial-authentication-06); deployed Azure environments should use a narrowly scoped managed identity. It reads an existing secret, never creates a vault or changes access policies. The Azure adapter has controlled-response tests; no live Azure account or managed identity deployment has been validated here.

Example configuration with references only:

```sh
python -m enterprise_ai --tenant-id customer-a --db .outcome/customer-a.sqlite --vault-config .outcome/vault.json --bank-key-ref customer-a/bank-link-key serve --demo --auth-config .outcome/customer-a-auth.json --api-erp --worker-token-ref customer-a/worker-token --erp-password-ref customer-a/erp-password --archive-dir .outcome/customer-a-archives
python -m enterprise_ai --tenant-id customer-a --vault-config .outcome/vault.json worker --coordinator http://127.0.0.1:8765 --token-ref customer-a/worker-token
```

For Azure use hyphenated secret names instead of slash paths. Bank-linkage key value: 64 hex characters representing 32 random bytes. Worker credential: at least 32 characters. Mock ERP password: 24..256 characters. Missing SDK/authentication/secret data fails closed. Secrets are read at process startup; rotation requires coordinated restart. Do not rotate the bank key against an existing database: use an explicit re-ingestion migration with new snapshots and approvals.

Bank hashes are **data**, not login secrets. `--bank-key-ref` uses the vault-managed key to replace incoming hashes with tenant-specific HMAC pseudonyms before storing supplier records, original-row evidence, graph features or mock ERP source data. This preserves equality matching within a tenant and prevents direct correlation of imported hashes between tenants. It is not encryption of the whole SQLite database or source CSV files. The caller's input files, optional dbt contract artifacts containing imported data, and other supplier fields remain outside this protection; protect those paths separately. Hashes still cannot establish legal identity.

## Verification

The core suite checks role denials, export protection, trusted reviewer attribution, separation of duties, foreign cookies/tenant pins, no dashboard downgrade, bank pseudonymization, clock jumps/restart, three failures/crashes, receipt reconciliation and archive failure safety. GitHub Actions adds a genuine HashiCorp Vault KV v2 service and genuine Redis DLQ tests alongside real Chromium, dbt and learning jobs. Optional environment flags fail loudly when their required service is missing. All sample data/CI credentials are synthetic.
