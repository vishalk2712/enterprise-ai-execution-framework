# Enterprise deployment runway

The matching, calibration, audit and batch modules are independent of HTTP/UI code. The checked-in Azure pipeline and Fabric notebook are deployment templates; neither has been deployed into a cloud workspace.

v0.5 adds optional local fact-plan rationales and a bounded browser worker for a separate mock ERP. See the [execution guide](execution-v05.md). Browser execution remains outside the Fabric batch job; scaling requires destination-side concurrency controls and authenticated approvals instead of the laptop's SQLite transaction fence. GitHub CI includes genuine headless browser tests in its optional-dependency job.

v0.4 adds a stable dataset namespace, transactional reviewer training evidence, four-cohort calibrated supplier fitting and indexed relationship queries. The optional training job now checks review-based fitting and Parquet exports. See the [v0.4 governance guide](governance-v04.md) for cohort persistence and why refits never activate weights automatically. The notebook's namespace is a cohort key, not tenant authorization; its temporary engine database still does not provide a central review service.

## Azure DevOps

Create a pipeline pointing at `azure-pipelines.yml`. Core jobs run Python 3.10 and 3.12 tests. The Python 3.12 data-contract job installs pinned requirements, tests real passing/failing dbt builds, runs a synthetic batch and publishes its output artifact. GitHub Actions also runs core tests on Linux/Windows and a Linux dbt job. No production datasets or cloud secrets belong in either CI job.

The YAML uses hosted Ubuntu agents. An organization needs available hosted-agent capacity and repository access. See [Microsoft Python pipeline documentation](https://learn.microsoft.com/en-us/azure/devops/pipelines/ecosystems/customize-python?view=azure-devops) for agent/environment setup.

## Microsoft Fabric

Import `deployment/fabric_batch.ipynb` into a Fabric workspace and attach a Lakehouse. Build a wheel from the reviewed repository (`python -m pip wheel . --no-deps --wheel-dir dist`) and install it with the pinned dbt requirements in the Fabric Environment. Do not fetch a mutable branch or unreviewed notebook code at runtime. Configure the notebook's parameter cell with absolute Lakehouse CSV input paths and an output folder, then use a [Notebook activity](https://learn.microsoft.com/en-us/fabric/data-factory/notebook-activity) in a Data Factory pipeline. [Fabric notebook documentation](https://learn.microsoft.com/en-us/fabric/data-engineering/how-to-use-notebook) covers attachment and parameter behavior.

Each batch stages dbt/DuckDB and the engine database on notebook-local temporary disk. Only completed JSON/JSONL/Markdown artifacts and dbt evidence are copied to the Lakehouse; the completion manifest is copied last. Do not place a concurrently writable SQLite/DuckDB database directly on OneLake. The laptop input limits still apply; this template does not implement Spark-scale matching. Partition larger workloads with an explicit cross-partition recall strategy before raising limits.

Use a pipeline-owned identity and Lakehouse permissions for inputs/outputs. Keep reviewer labels in a durable governed store across notebook runs; the notebook currently produces a new audit export per run, not a central review service. An enterprise adapter should transactionally ingest exports keyed by run/evaluation IDs into a SQL/Warehouse audit store and retain human decisions separately. A failed notebook must fail the activity; downstream consumers process only directories with a valid completion manifest.

Before a production launch, implement Entra-authenticated reviewers, authorization, tenant boundaries, data retention and export controls, managed audit persistence, representative calibration and monitoring, and a destination-specific action/rollback contract. These are explicit remaining deployment requirements, not properties provided by the local dashboard.
