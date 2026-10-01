"""Run with python -m enterprise_ai; Python 3.10+ and no runtime packages."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from .engine import Engine, ValidationError
from .server import demo_csv, make_server


def main():
    parser = argparse.ArgumentParser(description="Outcome Engine: local supplier intelligence baseline")
    parser.add_argument("--db", default=".outcome/engine.sqlite", help="Local SQLite database path")
    parser.add_argument("--match-config", help="JSON MatchConfig overrides")
    parser.add_argument("--calibration", help="Reviewed supplier-domain calibration artifact; never enables auto-merges")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Analyze synthetic data and export a report")
    demo.add_argument("--output", default=".outcome/demo-report.md")
    analyze = sub.add_parser("analyze", help="Import your two CSV files")
    analyze.add_argument("--suppliers", required=True)
    analyze.add_argument("--spend", required=True)
    analyze.add_argument("--output", default=".outcome/supplier-report.md")
    query = sub.add_parser("query", help="Ask a bounded question about imported data")
    query.add_argument("question")
    query.add_argument("--budget-tokens", type=int, default=2048)
    serve = sub.add_parser("serve", help="Start the local dashboard")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--demo", action="store_true", help="Replace the current dataset with the synthetic demo")
    serve.add_argument("--require-dbt", action="store_true", help="Gate every web import through dbt")
    audit = sub.add_parser("audit-export", help="Export immutable evaluations and separate review labels")
    audit.add_argument("--output", required=True)
    labels = sub.add_parser("labels-export", help="Export latest definite labels for explicit grouping and split assignment")
    labels.add_argument("--output", required=True)
    review = sub.add_parser("review", help="Append a human label; does not merge suppliers")
    review.add_argument("evaluation_id")
    review.add_argument("label", choices=["Match", "NonMatch", "Unsure"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--supersedes")
    batch = sub.add_parser("pipeline", help="Run a dbt-gated batch and export immutable artifacts")
    batch.add_argument("--suppliers", required=True)
    batch.add_argument("--spend", required=True)
    batch.add_argument("--output-dir", required=True)
    batch.add_argument("--without-dbt", action="store_true", help="Explicitly use only Python validation")
    calibrate = sub.add_parser("calibrate", help="Fit probability calibration from grouped, split labels")
    calibrate.add_argument("--labels", required=True)
    calibrate.add_argument("--domain", required=True)
    calibrate.add_argument("--output", required=True)
    args = parser.parse_args()
    from .matching import MatchConfig
    try:
        config = MatchConfig(**json.loads(Path(args.match_config).read_text())) if args.match_config else MatchConfig()
    except (ValueError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid matching configuration: {exc}\n")
    try:
        engine = Engine(args.db, config, json.loads(Path(args.calibration).read_text()) if args.calibration else None)
    except (ValueError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid engine configuration: {exc}\n")
    try:
        if args.command in {"demo", "analyze"}:
            data = demo_csv() if args.command == "demo" else (Path(args.suppliers).read_text(encoding="utf-8-sig"), Path(args.spend).read_text(encoding="utf-8-sig"))
            state = engine.analyze(*data)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(engine.export_report(), encoding="utf-8")
            print(json.dumps({"dataset": state["dataset"], "totals": state["totals"], "warnings": state["warnings"], "report": str(output)}, indent=2))
        elif args.command == "audit-export":
            Path(args.output).write_text(engine.export_audit(), encoding="utf-8")
        elif args.command == "labels-export":
            Path(args.output).write_text(engine.export_labels(), encoding="utf-8")
        elif args.command == "review":
            print(json.dumps(engine.review(args.evaluation_id, args.label, args.reviewer, args.reason, args.supersedes), indent=2))
        elif args.command == "pipeline":
            from .pipeline import run_pipeline
            print(json.dumps(run_pipeline(args.suppliers, args.spend, args.output_dir, not args.without_dbt, config, engine.calibration), indent=2))
        elif args.command == "calibrate":
            from .calibration import fit_calibration
            rows = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()]
            Path(args.output).write_text(json.dumps(fit_calibration(rows, config.config_id, args.domain), indent=2), encoding="utf-8")
        elif args.command == "query":
            print(json.dumps(engine.query(args.question, args.budget_tokens), indent=2, ensure_ascii=False))
        elif args.command == "serve":
            if args.demo:
                if args.require_dbt:
                    from .contracts import run_dbt_contracts
                    normalized, contract = run_dbt_contracts(*demo_csv(), Path(".outcome/contracts")/uuid4().hex)
                    engine.analyze(*demo_csv(), normalized, contract)
                else:
                    engine.analyze(*demo_csv())
            server = make_server(engine, args.port, ".outcome/contracts" if args.require_dbt else None)
            print(f"Outcome Engine: http://127.0.0.1:{server.server_port}", flush=True)
            print("Local demo only. Ctrl+C to stop. Data stays in your configured SQLite file.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
