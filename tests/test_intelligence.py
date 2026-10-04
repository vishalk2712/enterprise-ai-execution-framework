import csv
import io
import json
import http.client
import threading
import unittest
from datetime import date

from enterprise_ai.engine import Engine, SUPPLIER_FIELDS, SPEND_FIELDS
from enterprise_ai.graph_discovery import discover, augment, GraphPolicy
from enterprise_ai.matching import MatchConfig, evaluate_records
from enterprise_ai.risk import scan, persist, export_markdown
from enterprise_ai.server import make_server
from enterprise_ai.security import AccessControl, password_hash


def csv_text(fields, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, '') for field in fields})
    return output.getvalue()


def supplier(rid, name, **extra):
    return dict(supplier_id=rid, name=name, country='GB', registration_id='', tax_id='', postcode='', **extra)


def sources(rows, amounts=None):
    fields = list(SUPPLIER_FIELDS) + ['address', 'lei', 'parent_lei', 'bank_account_hash']
    spend = amounts if amounts is not None else [('2025-01-01', '10.00', 'GBP')]
    invoices = [dict(invoice_id=f'I-{rid}-{i}', supplier_id=rid, invoice_date=d, amount=a, currency=c,
                     category='Synthetic test', description='Synthetic evidence') for rid in [r['supplier_id'] for r in rows] for i, (d, a, c) in enumerate(spend)]
    return csv_text(fields, rows), csv_text(SPEND_FIELDS, invoices)


class AssociationGraphTests(unittest.TestCase):
    def test_dissimilar_names_retrieved_by_full_address_without_identity_merge(self):
        rows = [supplier('A', 'Zebra Orchard', address='10 Shared Street York'), supplier('B', 'Quantum Parts', address='10 shared street, York')]
        pairs, stats = discover(rows)
        self.assertIn(('A', 'B'), pairs)
        self.assertEqual(len(pairs[('A', 'B')][0]), 2)
        engine = Engine(graph_discovery=True)
        self.addCleanup(engine.close)
        state = engine.analyze(*sources(rows))
        self.assertEqual(state['dataset']['entity_count'], 2)
        self.assertTrue(state['review_candidates'])
        item = engine.evaluations()['evaluations'][0]
        self.assertEqual(item['algorithmic_outcome'], 'Graph_Context_Review')
        self.assertFalse(item['operational_decision']['auto_merge_eligible'])
        graph = engine.graph_neighbors('supplier:A', relation='SHARES_ADDRESS')
        self.assertEqual(len(graph['nodes']), 3)

    def test_four_hop_ownership_path_never_transitively_groups_siblings(self):
        parent = 'PARENT-ASSERTION'
        rows = [supplier('A', 'Zebra'), supplier('B', 'Anchor'), supplier('C', 'Quantum')]
        rows[0]['address'] = rows[1]['address'] = '10 Shared Street York'
        rows[1]['parent_lei'] = parent
        rows[2]['lei'] = parent
        pairs, _ = discover(rows)
        paths = pairs[('A', 'C')]
        self.assertEqual(len(paths[0]), 4)
        self.assertIn('ASSOCIATED_WITH', {e['relation'] for e in paths[0]})

    def test_shared_postcode_chain_and_hubs_do_not_make_unbounded_communities(self):
        rows = [supplier(str(i), f'Supplier {i}') for i in range(8)]
        for row in rows: row['postcode'] = 'ZZ1 1ZZ'
        pairs, stats = discover(rows, GraphPolicy(max_posting=3))
        self.assertEqual(pairs, {})
        self.assertTrue(stats['truncated'])
        self.assertEqual(stats['oversized_nodes_skipped'], 1)
        rows[0]['postcode'] = rows[1]['postcode'] = 'AB1'
        rows[1]['tax_id'] = rows[2]['tax_id'] = 'SYNTH-TAX'
        pairs, _ = discover(rows)
        self.assertNotIn(('0', '2'), pairs)

    def test_graph_union_preserves_baseline_and_reports_budget(self):
        rows = [supplier(str(i), 'Alpha Supply', address='10 Shared Street York') for i in range(8)]
        original, _ = evaluate_records(rows, MatchConfig())
        before = {(r['left_id'], r['right_id']) for r in original}
        stats = augment(original, rows, MatchConfig(), GraphPolicy(max_expansions=2))
        self.assertTrue(stats['truncated'])
        self.assertTrue(before <= {(r['left_id'], r['right_id']) for r in original})
        self.assertEqual(discover(rows), discover(list(reversed(rows))))

    def test_parentage_and_conflicting_legal_ids_remain_separate(self):
        rows = [supplier('A', 'Acme UK', address='10 Shared Street York'), supplier('B', 'Acme Global', address='10 Shared Street York')]
        rows[0]['registration_id'], rows[1]['registration_id'] = 'SC000123', 'SC000456'
        engine = Engine(graph_discovery=True)
        self.addCleanup(engine.close)
        state = engine.analyze(*sources(rows))
        self.assertEqual(state['dataset']['entity_count'], 2)
        self.assertEqual(engine.evaluations()['evaluations'][0]['algorithmic_outcome'], 'Conflict_Review')
        self.assertEqual(state['recent_actions'], [])

    def test_graph_policy_is_snapshot_bound_without_changing_frozen_model_config(self):
        data = sources([supplier('A', 'Acme')])
        baseline, graph = Engine(), Engine(graph_discovery=True)
        self.addCleanup(baseline.close); self.addCleanup(graph.close)
        b, g = baseline.analyze(*data), graph.analyze(*data)
        self.assertNotEqual(b['dataset']['snapshot'], g['dataset']['snapshot'])
        self.assertEqual(baseline.match_config.config_id, graph.match_config.config_id)
        with self.assertRaisesRegex(ValueError, 'candidate prevalence'):
            Engine(graph_discovery=True, calibration={})

    def test_graph_reviews_are_retained_but_excluded_from_baseline_calibration(self):
        engine = Engine(graph_discovery=True); self.addCleanup(engine.close)
        engine.analyze(*sources([supplier('A', 'Alpha'), supplier('B', 'Alpha')]))
        evaluation = engine.evaluations()['evaluations'][0]
        engine.review(evaluation['evaluation_id'], 'Unsure', 'Synthetic QA', 'Synthetic cohort boundary check')
        evidence = json.loads(engine.db.execute('SELECT payload_json FROM training_feedback').fetchone()[0])
        self.assertNotEqual(evidence['config_id'], engine.match_config.config_id)
        self.assertEqual(evidence['base_matching_config_id'], engine.match_config.config_id)
        self.assertEqual(engine.feedback_summary()['review_events'], 0)
        self.assertEqual(engine.feedback_summary()['graph_review_events'], 1)
        self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM review_decisions').fetchone()[0], 1)


class RiskSignalTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.addCleanup(self.engine.close)

    def scan(self):
        e = self.engine
        result = scan(e.db, e._meta('dataset'), e.dataset_namespace, e._records('entities'), date(2026, 10, 4))
        with e.db: persist(e.db, result)
        return result

    def test_monthly_spike_has_exact_same_cohort_baseline_and_credit_evidence(self):
        months = [(f'2025-0{i}-01', '100.00' if i < 4 else '2000.00', 'GBP') for i in range(1, 5)]
        months += [('2025-04-02', '-1900.00', 'GBP')]
        self.engine.analyze(*sources([supplier('A', 'Acme')], months))
        result = self.scan()
        item = result['dossiers'][0]
        self.assertEqual(item['rule'], 'monthly_outflow_spike')
        self.assertEqual(item['evidence']['baseline_median'], '100.00')
        self.assertEqual(item['evidence']['periods'][0]['credits'], '-1900.00')
        self.assertEqual(item['evidence']['ratio'], '20')
        self.assertTrue(item['evidence']['periods'][0]['sources'][0]['evidence'])

    def test_sparse_partial_or_mixed_currency_periods_do_not_invent_baselines(self):
        amounts = [('2025-01-01', '100.00', 'GBP'), ('2025-02-01', '100.00', 'EUR'), ('2025-03-01', '100.00', 'GBP'), ('2025-04-01', '5000.00', 'GBP')]
        self.engine.analyze(*sources([supplier('A', 'Acme')], amounts))
        self.assertEqual(self.scan()['dossiers'], [])
        complete = [(f'2026-0{i}-01', '100.00' if i < 4 else '2000.00', 'GBP') for i in range(1, 5)]
        self.engine.analyze(*sources([supplier('A', 'Acme')], complete))
        result = scan(self.engine.db, self.engine._meta('dataset'), 'local', self.engine._records('entities'), date(2026, 4, 15))
        self.assertEqual(result['dossiers'], [])
        self.assertGreater(result['insufficient_or_partial_month_baselines'], 0)

    def test_monthly_replacement_imports_and_revisions_do_not_double_count(self):
        for i in range(1, 5):
            self.engine.analyze(*sources([supplier('A', 'Acme')], [(f'2025-0{i}-01', '100.00' if i < 4 else '2000.00', 'GBP')]))
        result = self.scan()
        self.assertEqual(result['dossiers'][0]['evidence']['ratio'], '20')
        count = self.engine.db.execute('SELECT COUNT(*) FROM risk_dossiers').fetchone()[0]
        self.scan(); self.scan()
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM risk_dossiers').fetchone()[0], count)
        data = sources([supplier('A', 'Acme')], [('2025-04-01', '2000.00', 'GBP')])
        self.engine.analyze(*data)
        self.assertEqual(self.scan()['dossiers'][0]['evidence']['ratio'], '20')
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM risk_observations').fetchone()[0], 4)

    def test_bank_churn_is_temporal_per_source_not_shared_bank_or_removed_value(self):
        for token in ('a', 'b', 'c'):
            self.engine.analyze(*sources([supplier('A', 'Acme', bank_account_hash=token * 64)]))
        item = next(d for d in self.scan()['dossiers'] if d['rule'] == 'bank_link_churn')
        self.assertEqual(item['evidence']['sources'][0]['observed_changes'], 2)
        self.assertNotIn('a' * 64, json.dumps(item))
        self.assertNotIn('b' * 64, json.dumps(item))
        self.engine.analyze(*sources([supplier('A', 'Acme', bank_account_hash='c' * 64)]))
        self.assertEqual(next(d for d in self.scan()['dossiers'] if d['rule'] == 'bank_link_churn')['evidence']['sources'][0]['observed_changes'], 2)
        other = Engine(); self.addCleanup(other.close)
        for token in ('a', '', 'b'):
            other.analyze(*sources([supplier('A', 'Acme', bank_account_hash=token * 64)]))
        self.assertEqual(other.risk_dossiers()['dossiers'], [])

    def test_derived_scans_do_not_write_reviews_groups_approvals_or_actions(self):
        self.engine.analyze(*sources([supplier('A', 'Acme')]))
        before = {table: list(self.engine.db.execute(f'SELECT * FROM {table}')) for table in ('entities', 'suppliers', 'invoices', 'review_decisions', 'training_feedback', 'actions', 'audit')}
        for _ in range(3): self.engine.scan_risks()
        for table, rows in before.items():
            self.assertEqual(list(self.engine.db.execute(f'SELECT * FROM {table}')), rows)

    def test_failures_have_audit_sequences_and_no_retry_capability(self):
        self.engine.analyze(*sources([supplier('A', 'Acme')]))
        action = self.engine.stage_action(self.engine.state()['entities'][0]['entity_id'])['action_id']
        with self.engine.db:
            for _ in range(2): self.engine._log('detached_execution_failed', {'action_id': action})
        result = self.scan()
        self.assertEqual(result['dossiers'][0]['rule'], 'repeated_execution_failure')
        self.assertEqual(len(result['dossiers'][0]['evidence']['audit_sequences']), 2)
        self.assertIn('cannot retry', export_markdown(result))
        self.assertEqual(self.engine.state()['recent_actions'][0]['status'], 'pending')

    def test_namespace_and_missing_stable_ids_prevent_borrowing_history(self):
        for i in range(1, 4):
            self.engine.analyze(*sources([supplier('A', 'Acme')], [(f'2025-0{i}-01', '100.00', 'GBP')]))
        self.engine.dataset_namespace = 'another-client'
        self.engine.analyze(*sources([supplier('A', 'Acme')], [('2025-04-01', '5000.00', 'GBP')]))
        self.assertEqual(self.scan()['dossiers'], [])

    def test_invalid_import_does_not_advance_history_and_risk_evidence_is_append_only(self):
        self.engine.analyze(*sources([supplier('A', 'Acme')]))
        with self.assertRaises(ValueError): self.engine.analyze('bad', 'bad')
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM risk_observations').fetchone()[0], 1)
        with self.assertRaisesRegex(Exception, 'append-only'):
            self.engine.db.execute('DELETE FROM risk_observations')

    def test_legal_regrouping_compares_same_members_not_one_premerge_supplier(self):
        months = [(f'2025-0{i}-01', '100.00', 'GBP') for i in range(1, 5)]
        rows = [supplier('A', 'Alpha'), supplier('B', 'Beta')]
        self.engine.analyze(*sources(rows, months))
        for row in rows: row['registration_id'] = 'SC000123'
        current = [(f'2025-0{i}-01', '100.00' if i < 4 else '2000.00', 'GBP') for i in range(1, 5)]
        self.engine.analyze(*sources(rows, current))
        item = next(d for d in self.scan()['dossiers'] if d['rule'] == 'monthly_outflow_spike')
        self.assertEqual(item['evidence']['baseline_median'], '200.00')
        self.assertTrue(item['evidence']['newly_grouped_since_previous_import'])
        self.assertEqual(item['evidence']['ratio'], '20')

    def test_reopening_older_snapshot_does_not_use_future_bank_history(self):
        old = sources([supplier('A', 'Acme', bank_account_hash='a' * 64)])
        for data in (old, sources([supplier('A', 'Acme', bank_account_hash='b' * 64)]), sources([supplier('A', 'Acme', bank_account_hash='c' * 64)])):
            self.engine.analyze(*data)
        self.assertTrue(self.scan()['dossiers'])
        self.engine.analyze(*old)
        self.assertEqual(self.scan()['dossiers'], [])


class RiskHTTPTests(unittest.TestCase):
    def test_investigator_role_required_and_monitor_closes_with_server(self):
        engine = Engine(tenant_id='qa')
        engine.analyze(*sources([supplier('A', 'Acme')]))
        access = AccessControl({'tenant_id': 'qa', 'principals': [{'id': r, 'roles': [r], 'password_hash': password_hash('synthetic-pass-123')} for r in ('viewer', 'steward', 'approver', 'investigator')]}, 'qa')
        server = make_server(engine, 0, security=access, risk_interval=5)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for role in ('viewer', 'steward', 'approver', 'investigator'):
                token = access.login(role, 'synthetic-pass-123')
                for path in ('/api/risk-dossiers', '/api/risk-export'):
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    conn.request('GET', path, headers={'Cookie': 'outcome_session=' + token})
                    response = conn.getresponse(); response.read(); conn.close()
                    self.assertEqual(response.status, 200 if role == 'investigator' else 403)
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            conn.request('GET', '/api/risk-dossiers?limit=0', headers={'Cookie': 'outcome_session=' + token})
            response = conn.getresponse(); response.read(); conn.close()
            self.assertEqual(response.status, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join(2); engine.close()
        self.assertFalse(server.risk_thread.is_alive())
