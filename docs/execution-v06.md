# v0.6: REST execution and detached workers

This release adds an API-native **mock ERP contract** and separates worker processes from the dashboard. It does not integrate SAP Ariba or Coupa, move financial transactions, or provide enterprise authentication. Legal identity rules, matching thresholds and human approval remain unchanged.

## Start without Redis

The API adapter needs only Python 3.10+. From the repository directory, start the coordinator in one terminal:

```sh
python -m enterprise_ai --db .outcome/api-demo.sqlite serve --demo --api-erp --port 8765 --erp-port 8770 --worker-token-file .outcome/api-worker.token
```

Then start a separate worker in a second terminal:

```sh
python -m enterprise_ai worker --coordinator http://127.0.0.1:8765 --token-file .outcome/api-worker.token --consumer laptop-1
```

On Windows use `py` if that is your installed launcher. The first command creates a local worker token; keep the file private and under the ignored `.outcome` directory. The second process talks to authenticated coordinator endpoints and never opens the engine SQLite database. `--once` processes at most one delivery, useful for controlled testing.

Open the dashboard, stage a supplier group, inspect its exact payload and destination, then click **Approve & queue sync**. A queue remains pending until a worker is running. Successful completion displays **Worker: verified** and the destination receipt. Multiple workers can use distinct consumer names against the same coordinator.

For detached DOM execution use `serve --browser-erp --distributed` instead of `--api-erp`. Install the optional browser dependencies as described in [v0.5](execution-v05.md); each browser node needs Node and Playwright. The legacy `--browser-erp` mode alone retains its in-process worker.

## Redis Streams delivery

Redis 6.2+ supports the required pending-delivery recovery command; CI exercises Redis 7.2. Redis is optional and is not installed automatically. With an existing Docker installation, this sandbox template starts a loopback-only Redis service with append-only persistence:

```sh
docker compose -f deployment/redis-compose.yml up -d
```

Set the same URL environment variable in **each** coordinator/worker terminal before starting the commands above:

```powershell
$env:OUTCOME_REDIS_URL = 'redis://127.0.0.1:6379'
```

For POSIX shells use `export OUTCOME_REDIS_URL=redis://127.0.0.1:6379`. The coordinator advertises a stable namespace and delivery mode. Workers refuse a Redis configuration mismatch. Remote Redis URLs require `rediss://` with certificate verification; provide credentials through the environment, never a committed configuration file. The Docker service is a local development fixture, not a production broker deployment.

## Approval → outbox → worker → receipt

1. Approval atomically saves a durable execution job and outbox record in coordinator-owned SQLite.
2. Redis carries only `action_id`, approved `intent_hash` and retry `epoch`. Supplier records and credentials are absent from messages.
3. An authenticated worker claims the job through HTTP. A short transaction issues a unique 90-second lease, the exact approved intent and a 40-second destination capability bound to that lease.
4. The worker performs network/browser work outside the engine lock. The REST adapter sends an idempotent `PUT /api/supplier-sync` with `Idempotency-Key` and an approved source fingerprint in `If-Match`.
5. The mock destination checks the approved source records, legal identity clique, target instance, active lease and current snapshot before its atomic commit. A separate authenticated `GET /api/receipts/{action_id}` reloads and verifies saved destination state.
6. The coordinator accepts completion only when the worker owns the active lease and its receipt matches the authoritative ERP receipt and persisted membership. A worker's assertion alone cannot complete a job.

No long network call holds the coordinator transaction. Database writes remain serialized by one coordinator; detached workers are not permission to share SQLite over a network or start multiple coordinators against one file.

## Failure and retry semantics

Delivery is **at least once**. Duplicate messages cannot claim an active lease twice. Unacknowledged Redis messages can be reclaimed after 95 seconds. The outbox republishes uncompleted queued jobs and expired running jobs at 30-second intervals, so broker data loss does not erase durable approval. Retry increments the epoch; older messages become obsolete. Redis recovery uses consumer groups and [`XAUTOCLAIM`](https://redis.io/docs/latest/commands/xautoclaim/).

A crash after destination commit but before acknowledgement is recovered by reloading the existing receipt, without another form submission or PUT. An old worker cannot commit using a lease replaced by another worker. Failed jobs need explicit retry; the loop does not silently reapprove changed data.

An import before destination commit invalidates the approval and blocks that write. An import after destination commit but before completion acknowledgement retains the historical receipt and records `superseded_snapshot: true`; the dashboard marks earlier-dataset portal records. This preserves what actually happened rather than resubmitting against new data.

Stream entries are retained, including acknowledged entries. A production installation needs measured retention, pending-entry-aware trimming, broker backup/monitoring, token rotation, managed secret storage and reconciliation. The demo has no stream retention service or distributed coordinator failover.

## Verification and deployment limits

`python -m unittest discover -s tests -v` includes actual HTTP execution, receipt recovery, forged completion rejection, source changes, lease fencing, two separate CLI worker processes and an engine responsiveness test while execution waits on I/O. Optional tests use `OUTCOME_TEST_BROWSER=1` for a real detached browser and `OUTCOME_TEST_REDIS=1` with a real Redis service. GitHub Actions runs both environments; no fake broker is counted as real Redis verification.

The laptop demonstration still binds coordinator and mock ERP to loopback. HTTP worker clients accept configured HTTPS coordinator URLs, but this release does not provision TLS, remote ERP connectivity or cross-host deployment. The destination commit fence is implemented by the **co-located mock** checking coordinator state. Actual vendor adapters need their own OAuth/service identity, scoped operations, concurrency/version contracts, idempotency/receipt mapping, cancellation rules, reconciliation and destination-side enforcement. Do not infer those guarantees from this sandbox.

The API sync groups supplier source IDs into a canonical record. It does not delete supplier records, transfer funds, create invoices or merge financial ledgers. The next product step is one customer sandbox connector with its verified contract and failure tests, rather than a generic autonomous ERP swarm.
