"""Optional real dbt/DuckDB gate. A failing dbt build prevents matching."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def run_dbt_contracts(suppliers_csv, spend_csv, workdir):
    try:
        import duckdb
    except ImportError as exc:
        raise ValueError("Install requirements-dbt.txt in a virtual environment to use the dbt gate") from exc
    from .engine import read_csv, SUPPLIER_FIELDS, SPEND_FIELDS
    suppliers = read_csv(suppliers_csv, SUPPLIER_FIELDS, "suppliers.csv", 1000)
    spend = read_csv(spend_csv, SPEND_FIELDS, "spend.csv", 10000)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).parent / "dbt"
    project = workdir / "project"
    shutil.copytree(source, project)
    database = workdir / "contracts.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute("CREATE SCHEMA raw")
        for table, fields, rows in (("suppliers", (*SUPPLIER_FIELDS, "address", "aliases", "lei", "parent_lei", "bank_account_hash"), suppliers), ("spend", SPEND_FIELDS, spend)):
            db.execute(f"CREATE TABLE raw.{table} (" + ",".join(f'"{f}" VARCHAR' for f in fields) + ")")
            db.executemany(f"INSERT INTO raw.{table} VALUES (" + ",".join("?" for _ in fields) + ")", [[r.get(f, "") for f in fields] for r in rows])
    profile = {"outcome_contracts": {"target": "local", "outputs": {"local": {"type": "duckdb", "path": str(database), "schema": "main", "threads": 1}}}}
    (project / "profiles.yml").write_text(json.dumps(profile), encoding="utf-8")
    executable = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    if not executable.exists():
        raise ValueError("dbt executable missing from the current Python environment")
    environment = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "DO_NOT_TRACK": "1"}
    result = subprocess.run([str(executable), "build", "--project-dir", str(project), "--profiles-dir", str(project), "--no-use-colors"], capture_output=True, text=True, env=environment, timeout=300)
    (workdir / "dbt.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise ValueError(f"dbt data contracts failed; matching did not run. Inspect {workdir / 'dbt.log'}")
    with duckdb.connect(str(database), read_only=True) as db:
        cursor = db.execute("SELECT * FROM main.stg_suppliers ORDER BY supplier_id")
        normalized = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
    artifact = json.loads((project / "target" / "run_results.json").read_text(encoding="utf-8"))
    project_digest = hashlib.sha256(b"".join(p.relative_to(source).as_posix().encode() + p.read_bytes() for p in sorted(source.rglob("*")) if p.is_file())).hexdigest()
    return normalized, {"status": "passed", "adapter": "duckdb", "dbt_version": artifact["metadata"]["dbt_version"],
                        "project_sha256": project_digest, "input_sha256": hashlib.sha256((suppliers_csv+"\0"+spend_csv).encode()).hexdigest(),
                        "results": [{"id": r["unique_id"], "status": r["status"]} for r in artifact["results"]]}
