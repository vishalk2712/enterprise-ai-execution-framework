"""Local bootstrap RBAC. One tenant per process/database; no implicit admin rights."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time

PERMISSIONS = {'viewer': {'read'}, 'steward': {'read', 'import', 'review', 'stage'},
               'approver': {'read', 'approve', 'execute'}, 'investigator': {'read', 'investigate'}}


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12..256 characters')
    salt = salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300000).hex()


def bootstrap(path, tenant):
    validate_tenant(tenant)
    path = Path(path)
    logins = path.with_name(path.stem+'-initial-logins.json')
    if path.exists() or logins.exists(): raise ValueError('Identity bootstrap files already exist')
    credentials = {role: secrets.token_urlsafe(24) for role in PERMISSIONS}
    config = {'tenant_id': tenant, 'principals': [{'id': role, 'roles': [role], 'password_hash': password_hash(password)} for role, password in credentials.items()]}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite identities or silently rotate existing credentials.
    with path.open('x', encoding='utf-8') as handle: json.dump(config, handle, indent=2)
    with logins.open('x', encoding='utf-8') as handle: json.dump(credentials, handle, indent=2)
    return {'identity_config': str(path), 'private_initial_logins': str(logins), 'tenant_id': tenant}


def validate_tenant(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Invalid tenant ID')
    return value


class AccessControl:
    def __init__(self, config, tenant):
        if not isinstance(config,dict) or config.get('tenant_id') != validate_tenant(tenant): raise ValueError('Identity configuration belongs to another tenant')
        self.tenant = tenant
        self.principals = {}
        for entry in config.get('principals', []):
            if not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', entry['id']) or entry['id'] in self.principals:
                raise ValueError('Invalid or duplicate principal')
            roles = entry.get('roles')
            encoded = entry.get('password_hash', '')
            if not isinstance(roles, list) or not roles or any(not isinstance(r,str) or r not in PERMISSIONS for r in roles) or not isinstance(encoded,str) or not re.fullmatch(r'[a-f0-9]{32}:[a-f0-9]{64}', encoded):
                raise ValueError('Invalid principal roles or password hash')
            self.principals[entry['id']] = entry
        if not self.principals: raise ValueError('At least one principal required')
        self.sessions, self.failures = {}, {}
        self.lock = threading.RLock()

    def login(self, name, password):
        if not isinstance(name, str) or not isinstance(password, str) or len(name)>80 or len(password)>256: raise ValueError('Invalid login')
        with self.lock:
            now = time.monotonic()
            self.failures = {k:v for k,v in self.failures.items() if v[1]>now}
            if len(self.failures)>1000 or self.failures.get(name, (0, 0))[0]>=5: raise ValueError('Login temporarily unavailable')
            entry = self.principals.get(name)
            encoded = entry['password_hash'] if entry else '0'*32+':'+'0'*64
            salt, expected = encoded.split(':')
            actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300000).hex()
            if not entry or not hmac.compare_digest(expected, actual):
                count = self.failures.get(name, (0,0))[0]+1
                self.failures[name] = count, now+60
                raise ValueError('Invalid login')
            self.sessions = {k:v for k,v in self.sessions.items() if v['expires']>now}
            if len(self.sessions)>=500: raise ValueError('Session capacity reached')
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {'id': name, 'roles': entry['roles'], 'tenant_id': self.tenant, 'expires': now+1800}
            self.failures.pop(name, None)
            return token

    def principal(self, cookie):
        from http.cookies import SimpleCookie
        try: token = SimpleCookie(cookie or '')['outcome_session'].value
        except (KeyError, ValueError): return None
        with self.lock:
            entry = self.sessions.get(token)
            if not entry or entry['expires']<=time.monotonic():
                self.sessions.pop(token, None)
                return None
            return {k:entry[k] for k in ('id','roles','tenant_id')}

    def logout(self, cookie):
        from http.cookies import SimpleCookie
        try: token = SimpleCookie(cookie or '')['outcome_session'].value
        except (KeyError, ValueError): return
        with self.lock: self.sessions.pop(token, None)

    @staticmethod
    def allows(principal, permission):
        return principal is not None and any(permission in PERMISSIONS[r] for r in principal['roles'])
