"""Regressions for identity hardening and payload bounds (v0.7 fixes).

Each test encodes a defect found by review: a placeholder authority ID that
merged unrelated suppliers, a reused real-looking ID, a supplier name that
captured a query's scope, a conflict attributed to the wrong pair, and an
unbounded state payload.
"""

import unittest

from enterprise_ai.engine import Engine
from enterprise_ai.matching import MatchConfig


SUPPLIER_HEADER = "supplier_id,name,country,registration_id,tax_id,postcode"
SPEND_HEADER = "invoice_id,supplier_id,invoice_date,amount,currency,category,description"


def suppliers(rows):
    return "\n".join([SUPPLIER_HEADER, *rows]) + "\n"


def spend(rows):
    return "\n".join([SPEND_HEADER, *rows]) + "\n"


ONE_INVOICE = spend(["I-1,S-0,2026-01-01,10.00,GBP,Parts,synthetic"])


class PlaceholderAuthorityTests(unittest.TestCase):
    def test_direct_governance_call_cannot_bypass_placeholder_guard(self):
        from enterprise_ai.governance import direct_identity, validate_cluster
        left = {"supplier_id": "A", "country": "GB", "registration_id": "00000"}
        right = {**left, "supplier_id": "B"}
        self.assertEqual(direct_identity(left, right), [])
        self.assertFalse(validate_cluster([left, right])["valid"])

    def test_repeated_character_registration_id_never_groups_suppliers(self):
        for placeholder in ("0", "X", "----", "999999", "00000000"):
            with self.subTest(placeholder=placeholder):
                rows = [f"S-{i},Company {i} Ltd,GB,{placeholder},,AB{i + 10} 1CD" for i in range(5)]
                state = Engine().analyze(suppliers(rows), ONE_INVOICE)
                self.assertEqual(state["dataset"]["entity_count"], 5)
                # "----" normalizes to empty and takes the existing
                # missing-value branch; the rest take the degenerate branch.
                self.assertTrue(any("ignored for" in w for w in state["warnings"]))

    def test_alphabetic_placeholder_registration_id_never_groups_suppliers(self):
        rows = [f"S-{i},Company {i} Ltd,GB,TBC,,AB{i + 10} 1CD" for i in range(4)]
        state = Engine().analyze(suppliers(rows), ONE_INVOICE)
        self.assertEqual(state["dataset"]["entity_count"], 4)

    def test_reused_plausible_registration_id_above_cap_does_not_group(self):
        config = MatchConfig(max_authority_group=25)
        rows = [f"S-{i:03d},Company {i} Ltd,GB,SC123456,,AB{i % 40 + 10} 1CD" for i in range(50)]
        state = Engine(match_config=config).analyze(
            suppliers(rows), spend(["I-1,S-000,2026-01-01,10.00,GBP,Parts,synthetic"]))
        self.assertEqual(state["dataset"]["entity_count"], 50)
        self.assertTrue(any("above the 25-record limit" in w for w in state["warnings"]))

    def test_registration_id_shared_by_a_few_records_still_groups(self):
        rows = ["S-0,Alpha Trading Ltd,GB,SC123456,,AB10 1CD",
                "S-1,Alpha Trading Limited,GB,SC123456,,AB10 1CD"]
        state = Engine().analyze(suppliers(rows), ONE_INVOICE)
        self.assertEqual(state["dataset"]["entity_count"], 1)

    def test_length_rule_is_opt_in_and_rejects_short_identifiers(self):
        rows = ["S-0,Alpha Ltd,GB,R1,,AB10 1CD", "S-1,Beta Ltd,GB,R1,,BB10 1CD"]
        self.assertEqual(Engine().analyze(suppliers(rows), ONE_INVOICE)["dataset"]["entity_count"], 1)
        strict = Engine(match_config=MatchConfig(min_authority_length=5))
        self.assertEqual(strict.analyze(suppliers(rows), ONE_INVOICE)["dataset"]["entity_count"], 2)


class ConflictAttributionTests(unittest.TestCase):
    def test_failed_clique_names_the_pair_that_actually_conflicts(self):
        rows = ["S-A,Alpha Ltd,GB,SC100001,TAX-1111,AB1 2CD",
                "S-B,Bravo Ltd,GB,SC100001,,AB1 2CD",
                "S-C,Charlie Ltd,GB,SC100001,TAX-9999,AB1 2CD"]
        state = Engine().analyze(suppliers(rows), spend(["I-1,S-A,2026-01-01,10.00,GBP,Parts,x"]))
        reported = {(r["left_id"], r["right_id"]) for r in state["review_candidates"]
                    if "Cluster consistency" in r["reason"]}
        self.assertEqual(reported, {("S-A", "S-C")})


class AmbiguousSupplierNameTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.engine.analyze(
            suppliers(["S-1,Total Ltd,GB,SC111111,,AB1 2CD",
                       "S-2,Zeta Works Ltd,GB,SC222222,,ZZ9 9ZZ"]),
            spend(["I-1,S-1,2026-01-01,100.00,GBP,Parts,x",
                   "I-2,S-2,2026-01-02,900.00,GBP,Parts,y"]))

    def test_keyword_named_supplier_does_not_silently_narrow_the_total(self):
        result = self.engine.query("What is our total spend by currency?")
        self.assertIn("GBP 1,000.00", result["answer"])
        self.assertIn("all imported suppliers", result["answer"])
        self.assertTrue(any("ordinary query words" in u for u in result["unknowns"]))

    def test_explicit_scoping_word_still_selects_that_supplier(self):
        result = self.engine.query("total spend for Total")
        self.assertIn("GBP 100.00", result["answer"])
        self.assertIn("Total Ltd", result["answer"])

    def test_source_supplier_id_still_selects_that_supplier(self):
        self.assertIn("GBP 100.00", self.engine.query("total spend for S-1")["answer"])


class StatePayloadBoundTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        rows = [f"S-{i:03d},Trading Company {i} Ltd,GB,SC{i:06d},,AB{i % 40 + 10} 1CD" for i in range(60)]
        self.engine.analyze(
            suppliers(rows), spend([f"I-{i:03d},S-{i:03d},2026-01-01,10.00,GBP,Parts,x" for i in range(60)]))

    def test_internal_callers_still_see_every_row(self):
        full = self.engine.state()
        self.assertEqual(len(full["review_candidates"]), full["review_candidate_total"])
        self.assertEqual(len(full["graph"]["nodes"]), full["graph"]["node_total"])

    def test_bounded_state_truncates_rows_but_reports_true_totals(self):
        full = self.engine.state()
        bounded = self.engine.state(review_limit=5, graph_limit=10)
        self.assertLessEqual(len(bounded["review_candidates"]), 5)
        self.assertLessEqual(len(bounded["graph"]["nodes"]), 10)
        self.assertEqual(bounded["review_candidate_total"], full["review_candidate_total"])
        self.assertEqual(bounded["graph"]["node_total"], full["graph"]["node_total"])
        self.assertEqual(bounded["graph"]["edge_total"], full["graph"]["edge_total"])

    def test_bounded_graph_never_returns_an_edge_without_both_endpoints(self):
        bounded = self.engine.state(graph_limit=10)
        shown = {node["id"] for node in bounded["graph"]["nodes"]}
        for edge in bounded["graph"]["edges"]:
            self.assertIn(edge["source"], shown)
            self.assertIn(edge["target"], shown)

    def test_totals_are_unaffected_by_the_display_bound(self):
        self.assertEqual(self.engine.state()["totals"],
                         self.engine.state(review_limit=1, graph_limit=1)["totals"])


if __name__ == "__main__":
    unittest.main()
