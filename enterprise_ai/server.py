"""Loopback-only demonstration server. Not a production multi-tenant service."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from .engine import Engine, ValidationError


def demo_csv():
    root = Path(__file__).resolve().parent / "data"
    return ((root / "suppliers.csv").read_text(encoding="utf-8"),
            (root / "spend.csv").read_text(encoding="utf-8"))


def make_server(engine: Engine, port: int = 8765, contract_workdir=None):
    def import_dataset(suppliers, spend):
        if contract_workdir:
            from .contracts import run_dbt_contracts
            normalized, contract = run_dbt_contracts(suppliers, spend, Path(contract_workdir)/uuid4().hex)
            return engine.analyze(suppliers, spend, normalized, contract)
        return engine.analyze(suppliers, spend)
    class Handler(BaseHTTPRequestHandler):
        server_version = "OutcomeEngine/0.2"

        def _host_ok(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def respond(self, status, data, content_type="application/json; charset=utf-8", download=False):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="supplier-handover.md"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self.respond(403, {"error": "This application only accepts loopback hostnames."})
            path = urlsplit(self.path).path
            if path == "/api/evaluations":
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    return self.respond(200, engine.evaluations(params.get("run_id", [None])[0], int(params.get("limit", [100])[0]), int(params.get("offset", [0])[0])))
                except ValueError as exc:
                    return self.respond(400, {"error": str(exc)})
            if path == "/api/resolution-runs":
                with engine.lock:
                    runs = [dict(r) for r in engine.db.execute("SELECT * FROM resolution_runs ORDER BY rowid DESC LIMIT 100")]
                return self.respond(200, {"runs": runs})
            if path == "/api/audit-export":
                return self.respond(200, engine.export_audit().encode("utf-8"), "application/x-ndjson; charset=utf-8")
            if path == "/api/state":
                return self.respond(200, engine.state())
            if path == "/api/portal":
                return self.respond(200, engine.portal())
            if path == "/api/health":
                return self.respond(200, {"status": "ok", "mode": "local-deterministic", "version": "0.2.0"})
            if path == "/api/export":
                return self.respond(200, engine.export_report().encode("utf-8"), "text/markdown; charset=utf-8", True)
            assets = {"/": ("index.html", "text/html"), "/static/app.js": ("app.js", "text/javascript"), "/static/reviews.js": ("reviews.js", "text/javascript"), "/static/style.css": ("style.css", "text/css")}
            if path in assets:
                name, mime = assets[path]
                return self.respond(200, (Path(__file__).parent / "static" / name).read_bytes(), mime + "; charset=utf-8")
            self.respond(404, {"error": "Not found."})

        def do_POST(self):
            if not self._host_ok():
                return self.respond(403, {"error": "This application only accepts loopback hostnames."})
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin is not None and origin not in allowed:
                return self.respond(403, {"error": "Cross-origin mutations are blocked."})
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.respond(403, {"error": "Cross-site mutations are blocked."})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "Use application/json."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 12_000_000:
                    return self.respond(413, {"error": "Request body must be between 1 byte and 12 MB."})
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValidationError("JSON object required.")
                path = urlsplit(self.path).path
                if path == "/api/demo":
                    return self.respond(200, import_dataset(*demo_csv()))
                if path == "/api/analyze":
                    return self.respond(200, import_dataset(data.get("suppliers_csv"), data.get("spend_csv")))
                if path == "/api/query":
                    return self.respond(200, engine.query(data.get("question"), data.get("budget_tokens", 2048)))
                if path == "/api/reviews":
                    return self.respond(200, engine.review(data.get("evaluation_id"), data.get("human_label"), data.get("reviewer"), data.get("reason"), data.get("supersedes")))
                if path == "/api/actions":
                    if data.get("operation", "sync_supplier") != "sync_supplier" or not isinstance(data.get("entity_id"), str):
                        raise ValidationError("Specify entity_id and operation sync_supplier.")
                    return self.respond(200, engine.stage_action(data["entity_id"]))
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "actions"]:
                    if parts[3] == "approve":
                        return self.respond(200, engine.approve_action(parts[2]))
                    if parts[3] == "execute":
                        return self.respond(200, engine.execute_action(parts[2]))
                self.respond(404, {"error": "Not found."})
            except (ValidationError, ValueError, UnicodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                # Never expose local paths, stack traces or source records to a client.
                self.respond(500, {"error": "An internal operation failed. Your last valid dataset is retained."})

        def log_message(self, fmt, *args):
            # Access logs exclude bodies and uploaded source data.
            super().log_message(fmt, *args)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
