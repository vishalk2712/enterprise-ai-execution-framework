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


def make_server(engine: Engine, port: int = 8765, contract_workdir=None, rationale_model=None, security=None):
    if security and security.tenant!=engine.tenant_id: raise ValueError('Dashboard identities belong to another tenant')
    with engine.lock,engine.db:
        access_mode=engine._meta('dashboard_access_mode')
        if not security and access_mode=='rbac': raise ValueError('This database requires its tenant identity configuration')
        if security and access_mode!='rbac':
            # A pre-RBAC approval cannot acquire authority during migration.
            count=engine.db.execute("UPDATE actions SET status='stale' WHERE status IN ('pending','approved')").rowcount
            engine._log('rbac_enabled_legacy_approvals_revoked',{'actions':count,'tenant_id':engine.tenant_id})
        engine.db.execute("INSERT OR REPLACE INTO metadata VALUES('dashboard_access_mode',?)",(json.dumps('rbac' if security else 'unsecured'),))
    def import_dataset(suppliers, spend):
        if contract_workdir:
            from .contracts import run_dbt_contracts
            normalized, contract = run_dbt_contracts(suppliers, spend, Path(contract_workdir)/uuid4().hex)
            return engine.analyze(suppliers, spend, normalized, contract)
        return engine.analyze(suppliers, spend)
    class Handler(BaseHTTPRequestHandler):
        server_version = "OutcomeEngine/0.7"

        def _host_ok(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def respond(self, status, data, content_type="application/json; charset=utf-8", download=False, cookie=None):
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
            if cookie: self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(data)

        def permitted(self, permission):
            if not security:
                if engine._meta('dashboard_access_mode')=='rbac':
                    self.respond(403,{'error':'This database requires its protected dashboard'})
                    return False
                return True
            principal = security.principal(self.headers.get('Cookie'))
            if not principal:
                self.respond(401,{'error':'Sign in required'})
                return False
            if self.headers.get('X-Tenant-Id',engine.tenant_id)!=engine.tenant_id or not security.allows(principal,permission):
                self.respond(403,{'error':'Role or tenant does not authorize this operation'})
                return False
            self.actor = principal['id']
            return True

        def do_GET(self):
            if not self._host_ok():
                return self.respond(403, {"error": "This application only accepts loopback hostnames."})
            path = urlsplit(self.path).path
            if path=='/api/session':
                if not security and engine._meta('dashboard_access_mode')=='rbac': return self.respond(503,{'error':'Use the protected dashboard for this database'})
                principal = security.principal(self.headers.get('Cookie')) if security else None
                return self.respond(200,{'secured':bool(security),'principal':principal,'tenant_id':engine.tenant_id,
                    'permissions': [p for p in ('read','import','review','stage','approve','execute','investigate') if not security or security.allows(principal,p)]})
            if path == '/api/worker/identity':
                if not engine.coordinator or not engine.coordinator.authorized(self.headers.get('Authorization')):
                    return self.respond(403, {'error': 'Worker authentication required'})
                return self.respond(200, engine.coordinator.identity())
            if path.startswith('/api/') and path!='/api/health' and not self.permitted('read'): return
            if path=='/api/investigations':
                return self.respond(200,engine.coordinator.investigations() if engine.coordinator else {'jobs':[],'max_attempts':3})
            if path in {"/api/graph", "/api/explanation", "/api/entity-rationale", "/api/feedback-summary"}:
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    if path == "/api/feedback-summary":
                        return self.respond(200, engine.feedback_summary())
                    if path == "/api/explanation":
                        return self.respond(200, engine.explain(params.get("evaluation_id", [""])[0]))
                    if path == "/api/entity-rationale":
                        return self.respond(200, engine.explain_entity(params.get('entity_id', [''])[0], rationale_model))
                    return self.respond(200, engine.graph_neighbors(params.get("node_id", [""])[0], int(params.get("hops", [2])[0]), int(params.get("limit", [100])[0]), params.get("relation", [None])[0]))
                except ValueError as exc:
                    return self.respond(400, {"error": str(exc)})
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
                from . import __version__
                return self.respond(200, {"status": "ok", "mode": "local-governed", "version": __version__, "rationale_model": rationale_model,
                                           "browser_execution": engine.browser_erp is not None, 'detached_workers': engine.coordinator is not None})
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
                if path=='/api/session/login':
                    if not security: return self.respond(400,{'error':'RBAC is not configured'})
                    if self.headers.get('Origin')!=f'http://{self.headers.get("Host")}': return self.respond(403,{'error':'Same-origin login required'})
                    token = security.login(data.get('username'),data.get('password'))
                    return self.respond(200,{'signed_in':True},cookie='outcome_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800')
                if path=='/api/session/logout':
                    if security: security.logout(self.headers.get('Cookie'))
                    return self.respond(200,{'signed_in':False},cookie='outcome_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if path.startswith('/api/worker/'):
                    if not engine.coordinator or not engine.coordinator.authorized(self.headers.get('Authorization')):
                        return self.respond(403, {'error': 'Worker authentication required'})
                    if path == '/api/worker/claim': return self.respond(200, engine.coordinator.claim(data.get('reference')))
                    if path == '/api/worker/complete': return self.respond(200, engine.coordinator.finish(data))
                    if path == '/api/worker/fail': return self.respond(200, engine.coordinator.finish(data, failed=True))
                    return self.respond(404, {'error': 'Unknown worker operation'})
                permission = 'import' if path in {'/api/demo','/api/analyze'} else 'review' if path=='/api/reviews' else 'stage' if path=='/api/actions' else 'investigate' if path=='/api/investigations' else 'approve' if path.endswith(('/approve','/retry')) else 'execute' if path.endswith('/execute') else 'read'
                if not self.permitted(permission): return
                if security and data.get('tenant_id',engine.tenant_id)!=engine.tenant_id: return self.respond(403,{'error':'Tenant mismatch'})
                actor = getattr(self,'actor','local_operator')
                if path=='/api/investigations':
                    if not engine.coordinator: raise ValueError('Detached coordinator required')
                    return self.respond(200,engine.coordinator.investigate(data.get('action_id'),actor,data.get('reason')))
                if path == "/api/demo":
                    return self.respond(200, import_dataset(*demo_csv()))
                if path == "/api/analyze":
                    return self.respond(200, import_dataset(data.get("suppliers_csv"), data.get("spend_csv")))
                if path == "/api/query":
                    return self.respond(200, engine.query(data.get("question"), data.get("budget_tokens", 2048)))
                if path == "/api/reviews":
                    return self.respond(200, engine.review(data.get("evaluation_id"), data.get("human_label"), actor if security else data.get("reviewer"), data.get("reason"), data.get("supersedes")))
                if path == "/api/actions":
                    if data.get("operation", "sync_supplier") != "sync_supplier" or not isinstance(data.get("entity_id"), str):
                        raise ValidationError("Specify entity_id and operation sync_supplier.")
                    return self.respond(200, engine.stage_action(data["entity_id"],actor))
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "actions"]:
                    if parts[3] == "approve":
                        return self.respond(200, engine.approve_action(parts[2],actor,separate_duties=bool(security)))
                    if parts[3] == "execute":
                        return self.respond(200, engine.execute_action(parts[2]))
                    if parts[3] == "retry":
                        return self.respond(200, engine.retry_browser_action(parts[2]))
                self.respond(404, {"error": "Not found."})
            except (ValidationError, ValueError, UnicodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                # Never expose local paths, stack traces or source records to a client.
                self.respond(500, {"error": "An internal operation failed. Your last valid dataset is retained."})

        def log_message(self, fmt, *args):
            # Access logs exclude bodies and uploaded source data.
            if urlsplit(self.path).path.startswith('/api/worker/') or urlsplit(self.path).path.startswith('/api/session/'): return
            super().log_message(fmt, *args)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
