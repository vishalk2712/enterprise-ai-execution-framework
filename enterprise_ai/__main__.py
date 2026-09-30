"""Run with python -m enterprise_ai; Python 3.10+ and no runtime packages."""
import argparse
import json
from pathlib import Path

from .engine import Engine, ValidationError
from .server import demo_csv, make_server


def main():
    parser = argparse.ArgumentParser(description="Outcome Engine: local supplier intelligence baseline")
    parser.add_argument("--db", default=".outcome/engine.sqlite", help="Local SQLite database path")
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
    args = parser.parse_args()
    engine = Engine(args.db)
    try:
        if args.command in {"demo", "analyze"}:
            data = demo_csv() if args.command == "demo" else (Path(args.suppliers).read_text(encoding="utf-8-sig"), Path(args.spend).read_text(encoding="utf-8-sig"))
            state = engine.analyze(*data)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(engine.export_report(), encoding="utf-8")
            print(json.dumps({"dataset": state["dataset"], "totals": state["totals"], "warnings": state["warnings"], "report": str(output)}, indent=2))
        elif args.command == "query":
            print(json.dumps(engine.query(args.question, args.budget_tokens), indent=2, ensure_ascii=False))
        elif args.command == "serve":
            if args.demo:
                engine.analyze(*demo_csv())
            server = make_server(engine, args.port)
            print(f"Outcome Engine: http://127.0.0.1:{server.server_port}", flush=True)
            print("Local demo only. Ctrl+C to stop. Data stays in your configured SQLite file.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ValidationError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
