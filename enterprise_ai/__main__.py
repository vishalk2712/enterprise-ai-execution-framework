"""Run with python -m enterprise_ai; Python 3.10+ and no runtime packages."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from .engine import Engine, ValidationError
from .server import demo_csv, make_server


def main():
    parser = argparse.ArgumentParser(description="Outcome Engine: local supplier intelligence baseline")
    parser.add_argument("--db", default=".outcome/engine.sqlite", help="Local SQLite database path")
    parser.add_argument("--match-config", help="JSON MatchConfig overrides")
    parser.add_argument("--calibration", help="Reviewed supplier-domain calibration artifact; never enables auto-merges")
    parser.add_argument("--pair-model", help="Explicit supplier-domain pair classifier; routes human review only")
    parser.add_argument("--dataset-namespace", default="local", help="Stable client/source namespace for review cohorts")
    parser.add_argument('--tenant-id', default='local', help='One tenant per database, ERP instance and server process')
    parser.add_argument('--vault-config', help='Non-secret JSON vault connection settings')
    parser.add_argument('--bank-key-ref', help='Vault reference containing a 64-character hex bank-linkage HMAC key; new database only')
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
    serve.add_argument('--rationale-model', help='Already installed local Ollama model for factual group summaries')
    serve.add_argument('--browser-erp', action='store_true', help='Run a separate mock ERP and execute approved payloads using a headless DOM worker')
    serve.add_argument('--erp-port', type=int, default=8770)
    serve.add_argument('--erp-db', help='Separate persistent mock ERP database; never use the engine database')
    serve.add_argument('--api-erp', action='store_true', help='Use the API-native mock ERP and detached workers')
    serve.add_argument('--distributed', action='store_true', help='Detach browser workers; optional Redis Streams broker')
    serve.add_argument('--worker-token-file', help='Local bootstrap credential; default is scoped to --tenant-id')
    serve.add_argument('--redis-url-env', default='OUTCOME_REDIS_URL', help='Environment variable containing redis/rediss URL; never put credentials in CLI arguments')
    serve.add_argument('--auth-config', help='Tenant identity JSON with password hashes; enables dashboard RBAC')
    serve.add_argument('--worker-token-ref', help='Read detached worker credential from configured vault; no file fallback')
    serve.add_argument('--erp-password-ref', help='Read mock ERP credential from configured vault')
    serve.add_argument('--max-attempts', type=int, default=3)
    serve.add_argument('--archive-dir', help='Defaults to a directory beside the selected engine database')
    serve.add_argument('--retention-days', type=int, default=90)
    worker_cmd = sub.add_parser('worker', help='Run an independent API/DOM consumer; no access to engine SQLite')
    worker_cmd.add_argument('--coordinator', required=True)
    worker_cmd.add_argument('--token-file', help='Defaults to the tenant-specific bootstrap credential')
    worker_cmd.add_argument('--token-ref', help='Read worker credential from configured vault; no file fallback')
    worker_cmd.add_argument('--redis-url-env', default='OUTCOME_REDIS_URL')
    worker_cmd.add_argument('--consumer', default='worker-1')
    worker_cmd.add_argument('--once', action='store_true')
    identities = sub.add_parser('auth-init', help='Create tenant identities and private initial passwords without overwriting files')
    identities.add_argument('--output', default='.outcome/auth.json')
    maintenance = sub.add_parser('maintenance', help='Archive verified operational rows; retain approval and destination evidence')
    maintenance.add_argument('--archive-dir', help='Defaults to a directory beside the selected engine database')
    maintenance.add_argument('--retention-days', type=int, default=90)
    maintenance.add_argument('--vacuum', action='store_true', help='Reclaim SQLite file space; stop the server and workers before running')
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
    train = sub.add_parser("train-pair-model", help="Train an optional multivariate classifier from verified, grouped labels")
    train.add_argument("--labels", required=True)
    train.add_argument("--domain", required=True)
    train.add_argument("--output", required=True)
    train.add_argument("--target-precision", type=float, default=.95, help="Observed validation precision target; not a guarantee")
    live = sub.add_parser("train", help="Fit a corporate model from persistent reviewer labels; does not activate weights")
    live.add_argument("--from-reviews", action="store_true", required=True)
    live.add_argument("--output", default=".outcome/supplier-pair-model.json")
    live.add_argument("--split-manifest", default=".outcome/review-splits.json")
    feedback = sub.add_parser("feedback-export", help="Export current deduplicated reviewer evidence")
    feedback.add_argument("--output", default=".outcome/labeled_feedback.jsonl")
    graph = sub.add_parser("graph", help="Bounded indexed relationship lookup")
    graph.add_argument("node_id", help="For example supplier:SUP-001")
    graph.add_argument("--hops", type=int, default=2)
    graph.add_argument("--limit", type=int, default=100)
    graph.add_argument("--relation")
    explanation = sub.add_parser("explain", help="Render a two-sentence evidence rationale")
    explanation.add_argument("evaluation_id")
    explanation.add_argument("--ollama-model", help="Optional already installed model; fixed local endpoint")
    args = parser.parse_args()
    try:
        from .security import validate_tenant
        validate_tenant(args.tenant_id)
        default_token = '.outcome/'+('worker.token' if args.tenant_id=='local' else args.tenant_id+'-worker.token')
        archive_dir = getattr(args,'archive_dir',None) or str(Path(args.db).with_name(Path(args.db).stem+'-archives'))
        from .secret_store import load_store
        store = load_store(args.vault_config) if args.vault_config else None
        def secret(reference):
            if not store: raise ValueError('Secret references require --vault-config')
            return store.get(reference)
        bank_key = bytes.fromhex(secret(args.bank_key_ref)) if args.bank_key_ref else None
        if bank_key is not None and len(bank_key)!=32: raise ValueError('Bank-linkage key must contain 32 bytes')
        if args.command=='auth-init':
            from .security import bootstrap
            print(json.dumps(bootstrap(args.output,args.tenant_id),indent=2))
            return
    except (ValueError, TypeError, OSError):
        parser.exit(2,'Invalid tenant or vault configuration; secret values are never printed.\n')
    if args.command == 'worker':
        from .worker import run_worker
        try: run_worker(args.coordinator, args.token_file or default_token, os.environ.get(args.redis_url_env), args.consumer, args.once,
                        token=secret(args.token_ref) if args.token_ref else None, tenant=args.tenant_id)
        except KeyboardInterrupt: pass
        except (ValueError, OSError): parser.exit(2, 'Worker could not connect or verify completion; inspect configuration.\n')
        return
    from .matching import MatchConfig
    try:
        config = MatchConfig(**json.loads(Path(args.match_config).read_text())) if args.match_config else MatchConfig()
    except (ValueError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid matching configuration: {exc}\n")
    try:
        engine = Engine(args.db, config, json.loads(Path(args.calibration).read_text()) if args.calibration else None,
                        json.loads(Path(args.pair_model).read_text()) if args.pair_model else None, dataset_namespace=args.dataset_namespace,
                        tenant_id=args.tenant_id, bank_link_key=bank_key)
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
        elif args.command=='maintenance':
            from .maintenance import compact
            result = compact(engine,archive_dir,args.retention_days)
            if args.vacuum:
                engine.db.execute('VACUUM')
                result['vacuumed'] = True
            print(json.dumps(result,indent=2))
        elif args.command == "audit-export":
            Path(args.output).write_text(engine.export_audit(), encoding="utf-8")
        elif args.command == "labels-export":
            Path(args.output).write_text(engine.export_labels(), encoding="utf-8")
        elif args.command == "review":
            print(json.dumps(engine.review(args.evaluation_id, args.label, args.reviewer, args.reason, args.supersedes), indent=2))
        elif args.command == "pipeline":
            from .pipeline import run_pipeline
            print(json.dumps(run_pipeline(args.suppliers, args.spend, args.output_dir, not args.without_dbt, config, engine.calibration, engine.pair_model, args.dataset_namespace,args.tenant_id,bank_key), indent=2))
        elif args.command == "train":
            from .feedback import train_from_reviews
            print(json.dumps(train_from_reviews(engine.db, config.config_id, args.dataset_namespace, args.output, args.split_manifest), indent=2))
        elif args.command == "feedback-export":
            from .feedback import export_feedback
            print(json.dumps(export_feedback(engine.db, config.config_id, args.dataset_namespace, args.output), indent=2))
        elif args.command == "graph":
            print(json.dumps(engine.graph_neighbors(args.node_id, args.hops, args.limit, args.relation), indent=2))
        elif args.command == "explain":
            print(json.dumps(engine.explain(args.evaluation_id, args.ollama_model), indent=2))
        elif args.command == "train-pair-model":
            from .pair_model import fit_pair_model
            rows = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()]
            artifact = fit_pair_model(rows, config.config_id, args.domain, args.target_precision)
            Path(args.output).write_text(json.dumps(artifact, indent=2), encoding="utf-8")
            print(json.dumps({"model_id": artifact["model_id"], "validation": artifact["validation"], "test": artifact["test"]}, indent=2))
        elif args.command == "calibrate":
            from .calibration import fit_calibration
            rows = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()]
            Path(args.output).write_text(json.dumps(fit_calibration(rows, config.config_id, args.domain), indent=2), encoding="utf-8")
        elif args.command == "query":
            print(json.dumps(engine.query(args.question, args.budget_tokens), indent=2, ensure_ascii=False))
        elif args.command == "serve":
            if args.api_erp and args.browser_erp:
                raise ValueError('Choose API or browser ERP mode, not both')
            if args.distributed and not (args.api_erp or args.browser_erp):
                raise ValueError('Detached workers require an ERP adapter')
            if args.auth_config and args.browser_erp and not args.distributed:
                raise ValueError('Protected browser execution requires --distributed')
            security = None
            if args.auth_config:
                from .security import AccessControl
                security = AccessControl(json.loads(Path(args.auth_config).read_text()),args.tenant_id)
            if args.demo:
                if args.require_dbt:
                    from .contracts import run_dbt_contracts
                    normalized, contract = run_dbt_contracts(*demo_csv(), Path(".outcome/contracts")/uuid4().hex)
                    engine.analyze(*demo_csv(), normalized, contract)
                else:
                    engine.analyze(*demo_csv())
            erp_server = worker = None
            if args.browser_erp or args.api_erp:
                import threading
                from .mock_erp import MockERP, make_erp_server
                from .execution import BrowserWorker
                records = engine._records('suppliers')
                if not records:
                    raise ValueError('Import a dataset first or use --demo to seed the mock ERP')
                erp_path = Path(args.erp_db) if args.erp_db else Path(args.db).with_name(Path(args.db).stem+'-mock-erp.sqlite')
                if erp_path.resolve() == Path(args.db).resolve():
                    raise ValueError('The mock ERP must use a separate database')
                engine.browser_erp = MockERP(erp_path, records, api_enabled=args.api_erp, tenant_id=args.tenant_id,
                                            password=secret(args.erp_password_ref) if args.erp_password_ref else None)
                erp_server = make_erp_server(engine.browser_erp, args.erp_port)
                if args.api_erp or args.distributed:
                    import secrets
                    from .distributed import Coordinator
                    if args.worker_token_ref:
                        worker_token = secret(args.worker_token_ref)
                    else:
                        token_path = Path(args.worker_token_file or default_token)
                        token_path.parent.mkdir(parents=True, exist_ok=True)
                        if not token_path.exists():
                            descriptor = os.open(token_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
                            with os.fdopen(descriptor, 'w') as handle: handle.write(secrets.token_urlsafe(32))
                        worker_token = token_path.read_text().strip()
                    broker = None
                    if os.environ.get(args.redis_url_env):
                        from .broker import RedisBroker
                        broker = RedisBroker(os.environ[args.redis_url_env], engine._meta('coordinator_id'))
                    Coordinator(engine, worker_token, broker, args.max_attempts, archive_dir, args.retention_days)
                else:
                    worker = BrowserWorker(engine)
            try:
                server = make_server(engine, args.port, ".outcome/contracts" if args.require_dbt else None, args.rationale_model, security)
            except OSError:
                if erp_server:
                    erp_server.server_close()
                    engine.browser_erp.close()
                raise
            if erp_server:
                threading.Thread(target=erp_server.serve_forever, daemon=True).start()
            print(f"Outcome Engine: http://127.0.0.1:{server.server_port}", flush=True)
            print("Local demo only. Ctrl+C to stop. Data stays in your configured SQLite file.", flush=True)
            print('Tenant: '+args.tenant_id+'; dashboard '+('RBAC enabled.' if security else 'unsecured loopback demo.'),flush=True)
            if engine.browser_erp:
                print(f'Mock ERP: {engine.browser_erp.origin} (separate sandbox database). Approval queues the configured worker.', flush=True)
                if engine.coordinator: print('Detached execution enabled. Start python -m enterprise_ai worker with this coordinator URL and worker token file.', flush=True)
            try:
                if engine.coordinator: engine.coordinator.start()
                if worker: worker.start()
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
                if worker:
                    worker.close()
                if engine.coordinator: engine.coordinator.close()
                if erp_server:
                    erp_server.shutdown()
                    erp_server.server_close()
                    engine.browser_erp.close()
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
