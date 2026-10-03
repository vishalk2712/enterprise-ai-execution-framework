"""API-native mock ERP contract: idempotency, source preconditions and reload."""
import json
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from .explanations import NoRedirect
from .mock_erp import fingerprint, intent

API_ADAPTER = 'mock-erp-rest-v1'


def loopback_origin(origin):
    parsed = urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('', '/') or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('This sandbox adapter accepts only a fixed loopback ERP origin')
    return origin.rstrip('/')


def expected_receipt(action):
    return {'action_id': action['action_id'], 'entity_id': action['entity_id'], 'snapshot': action['snapshot'], 'intent_hash': fingerprint(intent(action)),
            'target_binding': action['target_binding'], 'source_supplier_ids': sorted(action['payload']['source_supplier_ids']),
            'payload_hash': fingerprint(action['payload']), 'status': 'verified'}


def run_api(action, password, signature):
    origin = loopback_origin(action['target_binding']['origin'])
    if action['target_binding']['adapter'] != API_ADAPTER:
        raise ValueError('API adapter differs from the approved destination')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def request(path, headers, body=None, method=None):
        with opener.open(Request(origin+path, body, headers, method=method), timeout=12) as response:
            data = response.read(65537)
            if len(data) > 65536: raise ValueError('ERP response exceeded bound')
            return json.loads(data)
    receipt_path = '/api/receipts/' + action['action_id']
    auth = {'Authorization': 'Bearer '+password}
    recovered = False
    try:
        request(receipt_path, auth)
        recovered = True
    except HTTPError as exc:
        if exc.code != 404: raise ValueError('Existing ERP receipt did not verify') from None
        body = json.dumps(intent(action)).encode()
        request('/api/supplier-sync', {'Authorization': 'Bearer '+signature, 'Content-Type': 'application/json',
                'Idempotency-Key': action['action_id'], 'If-Match': '"'+fingerprint(action['source_records'])+'"'}, body, 'PUT')
    # Independent authenticated GET checks the persisted destination postcondition.
    receipt = request(receipt_path, auth)
    if receipt != expected_receipt(action):
        raise ValueError('API receipt does not match the exact approved intent')
    return {'receipt': receipt, 'recovered': recovered, 'adapter': API_ADAPTER, 'trace': [{'kind': 'rest', 'method': 'GET', 'postcondition': 'verified'}]}
