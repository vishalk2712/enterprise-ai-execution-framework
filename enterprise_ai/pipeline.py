"""Batch entry point for local jobs and Fabric notebooks. No cloud SDK needed."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from .engine import Engine
from .contracts import run_dbt_contracts
from .matching import MatchConfig


def run_pipeline(suppliers_path, spend_path, output_dir, use_dbt=True, config=None, calibration=None, pair_model=None, dataset_namespace="local", tenant_id='local', bank_link_key=None, graph_discovery=False):
    suppliers = Path(suppliers_path).read_text(encoding="utf-8-sig")
    spend = Path(spend_path).read_text(encoding="utf-8-sig")
    destination = Path(output_dir) / ("run-" + uuid4().hex)
    destination.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="outcome-") as scratch:
        # Keep contract evidence, including failure logs, after temporary engine cleanup.
        normalized, contract = run_dbt_contracts(suppliers, spend, destination/"contracts") if use_dbt else (None, None)
        engine = Engine(str(Path(scratch)/"engine.sqlite"), config or MatchConfig(), calibration, pair_model, dataset_namespace=dataset_namespace,tenant_id=tenant_id,bank_link_key=bank_link_key,graph_discovery=graph_discovery)
        try:
            state = engine.analyze(suppliers, spend, normalized, contract)
            (destination/"state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            (destination/"resolution-audit.jsonl").write_text(engine.export_audit(), encoding="utf-8")
            (destination/"report.md").write_text(engine.export_report(), encoding="utf-8")
            manifest = {"version": "0.10.0", "tenant_id":tenant_id,"bank_link_key_id":engine._meta('bank_link_key_id'),"dataset_namespace": dataset_namespace, "run_id": state["dataset"]["resolution_run_id"], "snapshot_id": state["dataset"]["snapshot_id"],
                        "pair_model_id": pair_model["model_id"] if pair_model else None,
                        "graph_discovery": state['dataset']['matching_statistics'].get('graph_discovery'),
                        "config_id": engine.match_config.config_id, "contracts": contract or {"status": "python_validation_only"},
                        "output_dir": str(destination), "status": "complete"}
            # Completion manifest is written last; consumers ignore incomplete directories.
            (destination/"manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return manifest
        finally:
            engine.close()
