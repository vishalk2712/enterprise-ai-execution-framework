"""Separate, loopback-only ERP sandbox with HTML forms and idempotent receipts.

HTML execution is the default. The opt-in REST sandbox uses the same approved
intent checks, atomic destination commit and independently verified receipt.
"""
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from contextlib import nullcontext
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ADAPTER = "mock-erp-dom-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_record(record):
    return {k: record.get(k, "") for k in ("supplier_id", "name", "country", "registration_id", "tax_id", "postcode", "address", "lei", "parent_lei", "bank_account_hash")}


def intent(action):
    return {k: action[k] for k in ("action_id", "snapshot", "entity_id", "operation", "target_binding", "payload", "source_records")}


class MockERP:
    def __init__(self, db_path, records, api_enabled=False, tenant_id='local', password=None):
        from .security import validate_tenant
        validate_tenant(tenant_id)
        self.tenant_id=tenant_id
        if password is not None and (not isinstance(password,str) or not 24<=len(password)<=256): raise ValueError('Invalid ERP credential')
        self.api_enabled = api_enabled
        self.authorization_guard = lambda action, signature: nullcontext()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.secret = secrets.token_bytes(32)
        self.password = password or secrets.token_urlsafe(24)
        self.sessions = {}
        self.capabilities = {}
        self.clock = time.monotonic
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS source_suppliers(supplier_id TEXT PRIMARY KEY,payload TEXT NOT NULL,entity_id TEXT);
          CREATE TABLE IF NOT EXISTS supplier_groups(entity_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts(action_id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,payload TEXT NOT NULL);
        """)
        stored_tenant = self.db.execute("SELECT value FROM settings WHERE key='tenant_id'").fetchone()
        if stored_tenant and stored_tenant[0] != tenant_id:
            self.db.close()
            raise ValueError('Mock ERP database belongs to another tenant')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('tenant_id',?)", (tenant_id,))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('instance_id',?)", (secrets.token_hex(16),))
            # Never reset an existing ERP after an import or server restart.
            if not self.db.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
                for row in records:
                    record = source_record(row)
                    self.db.execute("INSERT INTO source_suppliers VALUES(?,?,NULL)", (record["supplier_id"], canonical(record)))
                self.db.execute("INSERT INTO settings VALUES('seeded','true')")
        self.instance_id = self.db.execute("SELECT value FROM settings WHERE key='instance_id'").fetchone()[0]
        self.origin = None

    def binding(self):
        return {"origin": self.origin, "instance_id": self.instance_id, "tenant_id":self.tenant_id, "adapter": 'mock-erp-rest-v1' if self.api_enabled else ADAPTER}

    def sign(self, action, expires_at=None, lease=None):
        # Only the issuer interprets the optional legacy absolute-time argument.
        ttl = 40 if expires_at is None else min(40, expires_at-time.time())
        with self.lock:
            self.capabilities = {k:v for k,v in self.capabilities.items() if v > self.clock()}
            if len(self.capabilities)>=500: raise ValueError('Capability capacity reached')
            capability = secrets.token_hex(16)
            self.capabilities[capability] = self.clock()+ttl
        prefix = 'v2:'+capability+':'+(lease or '-')
        message = canonical(intent(action)) + ':' + prefix
        return prefix + ':' + hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()

    def capability_live(self, signature):
        parts = signature.split(':')
        return len(parts)==4 and parts[0]=='v2' and self.capabilities.get(parts[1],0)>self.clock()

    def receipt(self, action_id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM receipts WHERE action_id=?", (action_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def apply(self, action, signature):
        if not isinstance(signature, str) or ':' not in signature:
            raise ValueError("This form has no valid approved execution capability")
        parts = signature.split(':')
        expected = ':'.join(parts[:-1])+':'+hmac.new(self.secret, (canonical(intent(action))+':'+':'.join(parts[:-1])).encode(), hashlib.sha256).hexdigest()
        if not self.capability_live(signature) or not hmac.compare_digest(expected, signature):
            raise ValueError("This form has no valid approved execution capability")
        if action["target_binding"] != self.binding() or action["operation"] != "sync_supplier":
            raise ValueError("ERP instance or operation changed; approve a fresh action")
        records = action["source_records"]
        ids = sorted(r["supplier_id"] for r in records)
        entity = action["payload"]
        if not records or len(ids) != len(set(ids)) or ids != sorted(entity["source_supplier_ids"]) or entity["entity_id"] != action["entity_id"]:
            raise ValueError("Supplier membership differs from the approved payload")
        from .governance import validate_cluster
        if not validate_cluster(records)["valid"]:
            raise ValueError("ERP refused inconsistent legal identity evidence")
        digest = fingerprint(intent(action))
        with self.authorization_guard(action, signature), self.lock, self.db:
            if not self.capability_live(signature):
                raise ValueError('Execution capability expired before destination write')
            old = self.receipt(action["action_id"])
            if old:
                if old["intent_hash"] != digest:
                    raise ValueError("Action ID was already used for different data")
                self.verify(old)
                return old
            for record in records:
                row = self.db.execute("SELECT * FROM source_suppliers WHERE supplier_id=?", (record["supplier_id"],)).fetchone()
                if not row or fingerprint(json.loads(row["payload"])) != fingerprint(record):
                    raise ValueError("ERP source record changed or is missing; stage and review new evidence")
                if row["entity_id"] not in (None, action["entity_id"]):
                    raise ValueError("ERP source supplier already belongs to another entity")
            saved = self.db.execute("SELECT payload FROM supplier_groups WHERE entity_id=?", (action["entity_id"],)).fetchone()
            if saved and json.loads(saved[0]) != entity:
                raise ValueError("ERP group changed; automatic overwrites are disabled")
            if not self.capability_live(signature):
                raise ValueError('Execution capability expired before destination write')
            self.db.execute("INSERT OR IGNORE INTO supplier_groups VALUES(?,?)", (action["entity_id"], canonical(entity)))
            for record in records:
                self.db.execute("UPDATE source_suppliers SET entity_id=? WHERE supplier_id=?", (action["entity_id"], record["supplier_id"]))
            if not self.capability_live(signature):
                raise ValueError('Execution capability expired before destination commit')
            receipt = {"action_id": action["action_id"], "entity_id": action["entity_id"], "snapshot": action["snapshot"], "intent_hash": digest,
                       "target_binding": self.binding(), "source_supplier_ids": ids, "payload_hash": fingerprint(entity), "status": "verified"}
            self.db.execute("INSERT INTO receipts VALUES(?,?,?)", (action["action_id"], digest, canonical(receipt)))
            return receipt

    def verify(self, receipt):
        row = self.db.execute("SELECT payload FROM supplier_groups WHERE entity_id=?", (receipt["entity_id"],)).fetchone()
        actual_ids = sorted(r[0] for r in self.db.execute("SELECT supplier_id FROM source_suppliers WHERE entity_id=?", (receipt["entity_id"],)))
        if not row or fingerprint(json.loads(row[0])) != receipt["payload_hash"] or actual_ids != receipt["source_supplier_ids"]:
            raise ValueError("ERP postcondition differs from its receipt")

    def close(self):
        self.db.close()


def make_erp_server(erp, port=8770):
    esc = lambda s: html.escape(str(s), quote=True)
    class Handler(BaseHTTPRequestHandler):
        def json_response(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            if not erp.api_enabled or urlsplit(self.path).path != '/api/supplier-sync':
                return self.json_response(404, {'error': 'API adapter disabled or unknown operation'})
            if not self.host_ok() or self.headers.get('Origin') not in (None, erp.origin) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return self.json_response(403, {'error': 'Origin refused'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 100000 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Bounded JSON request required')
                action = json.loads(self.rfile.read(length))
                if self.headers.get('Idempotency-Key') != action['action_id'] or self.headers.get('If-Match') != '"'+fingerprint(action['source_records'])+'"':
                    raise ValueError('Idempotency or source precondition missing')
                auth = self.headers.get('Authorization', '')
                if not auth.startswith('Bearer '):
                    return self.json_response(403, {'error': 'Approved execution capability required'})
                receipt = erp.apply(action, auth[7:])
                return self.json_response(200, receipt)
            except (ValueError, KeyError, TypeError):
                self.json_response(409, {'error': 'Approved intent, lease or destination precondition failed'})
        def respond(self, status, content, redirect=None):
            body = content.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            if redirect:
                self.send_header("Location", redirect)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get("Host") == urlsplit(erp.origin).netloc

        def session(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                with erp.lock:
                    return erp.sessions.get(cookie["erp_session"].value)
            except (KeyError, ValueError):
                return None

        def page(self, content):
            return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Outcome Mock ERP</title><style>body{font:16px system-ui;background:#101b2b;color:#e8eff8;max-width:850px;margin:50px auto;padding:24px}label,input,textarea{display:block;margin:12px 0}textarea{width:95%;height:190px}button,a{color:#13ddbd}button{background:#233951;padding:12px;border:1px solid #13ddbd}pre{white-space:pre-wrap;overflow-wrap:anywhere}input{padding:10px}section{padding:20px;background:#192b41;margin:15px 0}</style><h1>Outcome Mock ERP</h1><p>Separate local sandbox · no financial ledger access</p>' + content + '</html>'

        def do_GET(self):
            if not self.host_ok():
                return self.respond(403, "Loopback host required")
            path = urlsplit(self.path).path
            if erp.api_enabled and path.startswith('/api/receipts/'):
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+erp.password):
                    return self.json_response(403, {'error': 'ERP read credential required'})
                with erp.lock:
                    receipt = erp.receipt(path.removeprefix('/api/receipts/'))
                    if not receipt: return self.json_response(404, {'error': 'Receipt not found'})
                    try: erp.verify(receipt)
                    except ValueError: return self.json_response(409, {'error': 'ERP postcondition changed'})
                return self.json_response(200, receipt)
            session = self.session()
            if path == "/" or path == "/login":
                return self.respond(200, self.page('<h2>Operator sign in</h2><form method="post" action="/login"><label>Username<input name="username" aria-label="Username" autocomplete="off"></label><label>Password<input name="password" type="password" aria-label="Password" autocomplete="off"></label><button type="submit">Sign in</button></form>'))
            if not session:
                return self.respond(303, "Sign in required", "/login")
            if path == "/suppliers":
                with erp.lock:
                    total = erp.db.execute("SELECT COUNT(*) FROM source_suppliers").fetchone()[0]
                    groups = [json.loads(r[0]) for r in erp.db.execute("SELECT payload FROM supplier_groups")]
                content = f'<p>{total} source suppliers · {len(groups)} resolved groups</p><a href="/merge">Open approved supplier sync</a>'
                for group in groups:
                    content += '<section><h2>' + esc(group['display_name']) + '</h2><pre>' + esc(canonical(group)) + '</pre></section>'
                return self.respond(200, self.page(content))
            if path == "/merge":
                return self.respond(200, self.page('<h2>Approved supplier sync</h2><form method="post" action="/merge"><input type="hidden" name="csrf" value="' + session['csrf'] + '"><label>Approved payload<textarea name="action" aria-label="Approved payload" required></textarea></label><label>Approval capability<input name="signature" aria-label="Approval capability" required></label><button type="submit">Apply approved supplier sync</button></form>'))
            if path.startswith("/receipts/"):
                action_id = path.removeprefix("/receipts/")
                with erp.lock:
                    receipt = erp.receipt(action_id)
                    if not receipt:
                        return self.respond(404, self.page('<h2>Receipt not found</h2>'))
                    try:
                        erp.verify(receipt)
                    except ValueError:
                        return self.respond(409, self.page('<h2>ERP postcondition failed</h2>'))
                return self.respond(200, self.page('<h2>Verified execution receipt</h2><pre id="receipt" aria-label="Verified receipt">' + esc(canonical(receipt)) + '</pre><a href="/suppliers">View suppliers</a>'))
            self.respond(404, "Not found")

        def do_POST(self):
            if not self.host_ok() or self.headers.get("Origin") != erp.origin or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.respond(403, "Same-origin form required")
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.respond(415, "Use an HTML form")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 150000:
                    raise ValueError("Form exceeds bounds")
                values = parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
                if any(len(v) != 1 for v in values.values()):
                    raise ValueError("Repeated form field")
                data = {k: v[0] for k, v in values.items()}
                if self.path == "/login":
                    if data.get("username") != "operator" or not secrets.compare_digest(data.get("password", ""), erp.password):
                        return self.respond(403, "Invalid sandbox credentials")
                    token = secrets.token_urlsafe(32)
                    with erp.lock:
                        if len(erp.sessions) > 500:
                            erp.sessions.clear()
                        erp.sessions[token] = {"csrf": secrets.token_urlsafe(32)}
                    self.send_response(303)
                    self.send_header("Location", "/suppliers")
                    self.send_header("Set-Cookie", f"erp_session={token}; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                session = self.session()
                if not session or not secrets.compare_digest(data.get("csrf", ""), session['csrf']):
                    return self.respond(403, "Valid session and CSRF token required")
                if self.path == "/merge":
                    receipt = erp.apply(json.loads(data["action"]), data["signature"])
                    return self.respond(303, "Applied", "/receipts/" + receipt["action_id"])
                self.respond(404, "Not found")
            except (ValueError, KeyError, TypeError, UnicodeError):
                self.respond(409, self.page('<h2>Sync rejected</h2><p>Invalid approval, changed source evidence or conflicting membership; inspect the local audit and stage a fresh action.</p>'))

        def log_message(self, *args):
            pass  # Never write capability tokens, credentials or source data to logs.

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    erp.origin = f"http://127.0.0.1:{server.server_port}"
    return server
