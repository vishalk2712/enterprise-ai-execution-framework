"""Real loopback HTTP checks for the local demo's externally visible contracts."""

import http.client
import json
import threading
import unittest

from enterprise_ai.engine import Engine
from enterprise_ai.server import make_server


class LocalHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(db_path=":memory:")
        self.server = make_server(self.engine, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="enterprise-ai-test-http",
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_port
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.engine.close()
        self.assertFalse(self.thread.is_alive(), "HTTP server thread did not stop.")

    def request(self, method, path, data=None, headers=None, raw_body=None):
        request_headers = {"Host": self.host}
        body = raw_body
        if method == "POST":
            request_headers.update({"Content-Type": "application/json", "Origin": self.origin})
            if body is None:
                body = json.dumps({} if data is None else data).encode("utf-8")
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            text = response.read().decode("utf-8")
            payload = json.loads(text) if "application/json" in response_headers.get("content-type", "") else text
            return response.status, response_headers, payload
        finally:
            connection.close()

    def load_demo(self):
        status, _, state = self.request("POST", "/api/demo")
        self.assertEqual(status, 200, state)
        return state

    def test_demo_state_and_query_use_the_real_http_endpoints(self):
        status, _, empty = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(empty["entities"], [])
        demo = self.load_demo()
        self.assertEqual(len(demo["entities"]), 8)
        status, headers, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["entities"], demo["entities"])
        self.assertEqual(state["totals"], demo["totals"])
        self.assertEqual(headers["cache-control"], "no-store")
        status, _, answer = self.request("POST", "/api/query", {
            "question": "What is the total spend by currency?", "budget_tokens": 512,
        })
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer["answer"])
        self.assertTrue(answer["evidence"])
        self.assertIn("route", answer)

    def test_review_history_api_separates_labels_and_survives_import(self):
        self.load_demo()
        status, _, result = self.request('GET', '/api/evaluations?limit=1')
        self.assertEqual(status, 200)
        row = result['evaluations'][0]
        payload = {'evaluation_id': row['evaluation_id'], 'human_label': 'Unsure', 'reviewer': 'QA', 'reason': 'Synthetic workflow check'}
        status, _, decision = self.request('POST', '/api/reviews', payload)
        self.assertEqual(status, 200, decision)
        self.assertEqual(self.request('POST', '/api/reviews', payload)[0], 400)
        self.assertEqual(self.request('POST', '/api/reviews', dict(payload, human_label=[]))[0], 400)
        self.assertEqual(self.request('GET', '/api/evaluations?limit=-1')[0], 400)
        self.load_demo()
        status, _, old = self.request('GET', '/api/evaluations?run_id='+result['run_id'])
        self.assertEqual(status, 200)
        restored = next(e for e in old['evaluations'] if e['evaluation_id'] == row['evaluation_id'])
        self.assertEqual(restored['human_label'], 'Unsure')
        self.assertEqual(restored['algorithmic_outcome'], row['algorithmic_outcome'])
        status, _, audit = self.request('GET', '/api/audit-export')
        self.assertEqual(status, 200)
        self.assertIn('review_decisions', audit)

    def test_governance_evidence_endpoints_and_input_bounds(self):
        self.load_demo()
        self.assertEqual(self.request('GET', '/api/health')[2]['version'], '0.4.0')
        status, _, graph = self.request('GET', '/api/graph?node_id=supplier:SUP-001&hops=2&limit=20')
        self.assertEqual(status, 200)
        self.assertLessEqual(len(graph['nodes']), 20)
        self.assertEqual(self.request('GET', '/api/graph?node_id=supplier:SUP-001&hops=3')[0], 400)
        self.assertEqual(self.request('GET', '/api/graph?node_id=unknown')[0], 400)
        self.assertEqual(self.request('GET', '/api/feedback-summary')[2]['eligible_pairs'], 0)
        item = self.request('GET', '/api/evaluations?limit=1')[2]['evaluations'][0]
        status, _, explanation = self.request('GET', '/api/explanation?evaluation_id='+item['evaluation_id'])
        self.assertEqual(status, 200)
        self.assertEqual(explanation['backend'], 'deterministic_evidence')
        self.assertFalse(explanation['model_used'])
        self.assertEqual(self.request('GET', '/api/explanation?evaluation_id=unknown')[0], 400)

    def test_approval_and_execution_update_only_the_local_portal_once(self):
        state = self.load_demo()
        entity = next(item for item in state["entities"] if len(item["source_supplier_ids"]) > 1)
        status, _, staged = self.request("POST", "/api/actions", {"entity_id": entity["entity_id"]})
        self.assertEqual(status, 200, staged)
        self.assertEqual(staged["status"], "pending")
        self.assertEqual(staged["target"], "local_mock_portal")
        path = f"/api/actions/{staged['action_id']}"
        status, _, refused = self.request("POST", path + "/execute")
        self.assertEqual(status, 400, refused)
        self.assertEqual(self.request("GET", "/api/portal")[2]["suppliers"], [])
        status, _, approved = self.request("POST", path + "/approve")
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["status"], "approved")
        status, _, executed = self.request("POST", path + "/execute")
        self.assertEqual(status, 200, executed)
        self.assertEqual(executed["status"], "executed")
        status, _, portal = self.request("GET", "/api/portal")
        self.assertEqual(status, 200)
        self.assertEqual(portal["target"], "local_mock_portal")
        self.assertEqual(len(portal["suppliers"]), 1)
        self.assertEqual(portal["suppliers"][0]["entity_id"], entity["entity_id"])
        self.assertEqual(portal["suppliers"][0]["source_supplier_ids"], entity["source_supplier_ids"])
        status, _, replayed = self.request("POST", path + "/execute")
        self.assertEqual(status, 200, replayed)
        self.assertEqual(self.request("GET", "/api/portal")[2], portal)

    def test_malformed_import_keeps_the_previous_valid_state(self):
        before = self.load_demo()
        status, _, error = self.request("POST", "/api/analyze", {
            "suppliers_csv": "supplier_id,name\nBROKEN,Synthetic record\n",
            "spend_csv": "invoice_id,supplier_id\nBROKEN,MISSING\n",
        })
        self.assertEqual(status, 400, error)
        self.assertTrue(error["error"])
        status, _, after = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(after["entities"], before["entities"])
        self.assertEqual(after["totals"], before["totals"])
        self.assertEqual(after["dataset"]["snapshot"], before["dataset"]["snapshot"])

    def test_cross_origin_requests_cannot_import_or_approve(self):
        rejected_headers = (
            {"Origin": "https://untrusted.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        )
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                status, _, error = self.request("POST", "/api/demo", headers=headers)
                self.assertEqual(status, 403, error)
                self.assertEqual(self.request("GET", "/api/state")[2]["entities"], [])
        state = self.load_demo()
        status, _, staged = self.request("POST", "/api/actions", {
            "entity_id": state["entities"][0]["entity_id"],
        })
        self.assertEqual(status, 200, staged)
        path = f"/api/actions/{staged['action_id']}"
        status, _, error = self.request("POST", path + "/approve", headers={
            "Origin": "https://untrusted.example",
        })
        self.assertEqual(status, 403, error)
        status, _, error = self.request("POST", path + "/execute")
        self.assertEqual(status, 400, error)
        self.assertEqual(self.request("GET", "/api/portal")[2]["suppliers"], [])

    def test_host_guard_blocks_rebound_hostname_on_reads_and_writes(self):
        bad_host = {"Host": f"untrusted.example:{self.port}"}
        for method, path in (("GET", "/api/state"), ("POST", "/api/demo")):
            with self.subTest(method=method):
                status, _, error = self.request(method, path, headers=bad_host)
                self.assertEqual(status, 403, error)
        self.assertEqual(self.request("GET", "/api/state")[2]["entities"], [])
        status, _, health = self.request("GET", "/api/health", headers={
            "Host": f"localhost:{self.port}",
        })
        self.assertEqual(status, 200, health)
        self.assertEqual(health["status"], "ok")

    def test_path_traversal_never_serves_repository_files(self):
        for path in (
            "/static/../engine.py", "/static/%2e%2e/engine.py",
            "/../examples/suppliers.csv", "/static/..%5cengine.py",
        ):
            with self.subTest(path=path):
                status, headers, payload = self.request("GET", path)
                self.assertEqual(status, 404, payload)
                self.assertIn("application/json", headers["content-type"])
                self.assertEqual(payload, {"error": "Not found."})

    def test_export_download_reconciles_totals_and_cites_sources(self):
        self.load_demo()
        status, headers, report = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["content-type"])
        self.assertIn("attachment", headers["content-disposition"])
        for expected in ("GBP 2950.00", "EUR 1250.00", "USD 1500.00", "suppliers.csv:2", "spend.csv:2"):
            self.assertIn(expected, report)

    def test_malformed_http_payloads_are_rejected_before_mutation(self):
        cases = (
            (b"{invalid", {"Content-Type": "application/json"}, 400),
            (b"[]", {"Content-Type": "application/json"}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
        )
        for body, headers, expected_status in cases:
            with self.subTest(body=body, headers=headers):
                status, _, error = self.request("POST", "/api/demo", raw_body=body, headers=headers)
                self.assertEqual(status, expected_status, error)
                self.assertTrue(error["error"])
                self.assertEqual(self.request("GET", "/api/state")[2]["entities"], [])


if __name__ == "__main__":
    unittest.main()
