"""Detached HTTP worker; broker messages carry references, never credentials."""
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from .explanations import NoRedirect
from .api_adapter import run_api, API_ADAPTER
from .execution import run_browser_grant


class WorkerClient:
    def __init__(self, origin, token):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost'})):
            raise ValueError('Use a loopback HTTP coordinator or an explicitly configured HTTPS coordinator')
        self.origin, self.token = origin.rstrip('/'), token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.origin+'/api/worker/'+path, body, {'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=8) as response:
            body = response.read(150001)
            if len(body) > 150000: raise ValueError('Coordinator response exceeded bound')
            return json.loads(body)


def consume_one(client, broker=None, consumer='worker-1'):
    message = broker.pop(consumer) if broker else None
    if broker and not message: return {'state': 'empty'}
    grant = client.request('claim', {'reference': message[1] if message else None})
    state = grant['state']
    if state != 'granted':
        if message and state in {'terminal', 'obsolete'}: broker.ack(message[0])
        return {'state': state}
    report = {'action_id': grant['action']['action_id'], 'lease': grant['lease']}
    try:
        action = grant['action']
        adapter = action['target_binding']['adapter']
        if adapter == API_ADAPTER:
            result = run_api(action, grant['password'], grant['signature'])
        elif adapter == 'mock-erp-dom-v1':
            result = run_browser_grant(action, grant['password'], grant['signature'])
        else:
            raise ValueError('Unsupported approved adapter')
    except Exception:
        # Failed acknowledgement is left pending and recovered after lease expiry.
        response = client.request('fail', report)
    else:
        response = client.request('complete', {**report, 'receipt': result['receipt']})
    if message and response['state'] in {'verified', 'failed'}:
        broker.ack(message[0])
    return {'action_id': report['action_id'], 'state': response['state']}


def run_worker(origin, token_file, redis_url=None, consumer='worker-1', once=False):
    token = Path(token_file).read_text(encoding='utf-8').strip()
    if len(token) < 32: raise ValueError('Invalid worker token file')
    client = WorkerClient(origin, token)
    identity = client.request('identity')
    broker = None
    if identity['delivery'] == 'redis_streams':
        if not redis_url: raise ValueError('This coordinator requires the Redis URL environment variable')
        from .broker import RedisBroker
        broker = RedisBroker(redis_url, identity['namespace'])
        broker.initialize()
    elif redis_url:
        raise ValueError('Redis workers require Redis delivery configured on the coordinator')
    while True:
        try:
            result = consume_one(client, broker, consumer)
            if once or result['state'] not in {'empty', 'busy'}: print(json.dumps(result), flush=True)
            if once: return result
        except (ValueError, OSError):
            if once: raise
            if broker:
                try: broker.initialize()
                except (ValueError, OSError): pass
            print(json.dumps({'state': 'unavailable', 'detail': 'Coordinator/broker completion unavailable; grants and receipts remain durable.'}), flush=True)
        time.sleep(.5)
