"""Regressions for incorrect query scope, source citations and action replay."""

import csv
import io
import json
import unittest

from enterprise_ai.engine import Engine, SPEND_FIELDS, SUPPLIER_FIELDS, ValidationError, read_csv


def csv_text(fields, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    writer.writerows(rows)
    return output.getvalue()


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.suppliers = [
            ("S1", "Alpha", "GB", "R1", "", "AA"),
            ("S10", "Beta", "GB", "R10", "", "BB"),
        ]
        self.invoices = [
            ("I1", "S1", "2026-01-01", "100", "GBP", "Hardware", "Synthetic A"),
            ("I2", "S10", "2026-01-01", "200", "GBP", "Services", "Synthetic B"),
        ]
        self.load()

    def tearDown(self):
        self.engine.close()

    def load(self, suppliers=None, invoices=None):
        return self.engine.analyze(
            csv_text(SUPPLIER_FIELDS, self.suppliers if suppliers is None else suppliers),
            csv_text(SPEND_FIELDS, self.invoices if invoices is None else invoices),
        )

    def assert_scope(self, question, amount, record_ids):
        result = self.engine.query(question)
        self.assertEqual(result["route"]["intent"], "spend_summary")
        self.assertIn(amount, result["answer"])
        self.assertEqual({r["record_id"] for r in result["evidence"]}, set(record_ids))
        return result

    def test_supplier_id_boundaries_do_not_match_prefixes(self):
        self.assert_scope("total spend for S10", "GBP 200.00", {"I2"})
        self.assert_scope("total spend for S1", "GBP 100.00", {"I1"})

    def test_dashboard_suggestions_and_all_are_not_currency_filters(self):
        self.assert_scope("Which suppliers have the highest spend?", "GBP 300.00", {"I1", "I2"})
        self.assert_scope("top suppliers", "GBP 300.00", {"I1", "I2"})
        self.assert_scope("total spend for all suppliers", "GBP 300.00", {"I1", "I2"})
        result = self.engine.query("Which supplier records are duplicates?")
        self.assertEqual(result["route"]["intent"], "supplier_resolution")
        self.assertIn("No matching invoices", self.engine.query("total spend in ALL")["answer"])

    def test_hyphenated_supplier_id_does_not_select_prefix(self):
        self.load([self.suppliers[0], ("S1-2", "Beta", "GB", "R10", "", "BB")],
                  [self.invoices[0], ("I2", "S1-2", *self.invoices[1][2:])])
        self.assert_scope("total spend for S1-2", "GBP 200.00", {"I2"})

    def test_non_latin_name_does_not_match_every_query(self):
        self.load([self.suppliers[0], ("S10", "供应商", "CN", "R10", "", "BB")])
        self.assert_scope("total spend for Alpha", "GBP 100.00", {"I1"})
        self.assert_scope("total spend for 供应商", "GBP 200.00", {"I2"})

    def test_punctuation_only_name_does_not_match_every_query(self):
        self.load([self.suppliers[0], ("S10", "!!!", "GB", "R10", "", "BB")])
        self.assert_scope("total spend for Alpha", "GBP 100.00", {"I1"})

    def test_entity_id_and_source_alias_select_the_whole_entity(self):
        state = self.load([self.suppliers[0], ("S10", "Alternate Trading", "GB", "R1", "", "BB")])
        self.assertEqual(len(state["entities"]), 1)
        entity_id = state["entities"][0]["entity_id"]
        self.assert_scope(f"total spend for {entity_id}", "GBP 300.00", {"I1", "I2"})
        self.assert_scope("total spend for Alternate Trading", "GBP 300.00", {"I1", "I2"})

    def test_missing_currency_never_falls_back_to_other_currencies(self):
        for currency in ("USD", "usd", "XYZ"):
            with self.subTest(currency=currency):
                result = self.engine.query(f"total spend in {currency}")
                self.assertEqual(result["route"]["intent"], "spend_summary")
                self.assertIn("No matching invoices", result["answer"])
                self.assertNotIn("GBP 300", result["answer"])
                self.assertEqual(result["evidence"], [])

    def test_partial_currency_selection_discloses_the_missing_part(self):
        result = self.assert_scope("total spend GBP and USD", "GBP 300.00", {"I1", "I2"})
        self.assertTrue(any("USD" in item for item in result["unknowns"]))

    def test_highest_spend_and_its_evidence_share_the_same_scope(self):
        result = self.assert_scope("highest spend for Alpha", "GBP 100.00", {"I1"})
        self.assertIn("Alpha 100.00", result["answer"])
        self.assertNotIn("Beta", result["answer"])

    def test_unknown_supplier_and_unsupported_conditions_refuse_totals(self):
        for question in (
            "total spend for Missing", "total spend for entity-doesnotexist",
            "total spend in 2025", "total spend last month", "total spend by category",
            "total spend Hardware", "forecast supplier spend next year",
            "total spend before 2026-01-01", "total spend excluding Alpha",
        ):
            with self.subTest(question=question):
                result = self.engine.query(question)
                self.assertEqual(result["route"]["intent"], "needs_review")
                self.assertEqual(result["evidence"], [])
                self.assertNotIn("GBP 300", result["answer"])
                self.assertTrue(result["unknowns"])

    def test_missing_authority_sentinels_cannot_merge_suppliers(self):
        for value in ("N/A", "n.a.", "NULL", "unknown", "not available", "---"):
            with self.subTest(value=value):
                state = self.load([
                    ("S1", "Alpha", "GB", value, value, "AA"),
                    ("S10", "Beta", "GB", value, value, "BB"),
                ])
                self.assertEqual(len(state["entities"]), 2)
                self.assertTrue(state["warnings"])
                self.assertTrue(all(not e["registration_ids"] and not e["tax_ids"] for e in state["entities"]))

    def test_source_lines_include_skipped_blank_and_multiline_records(self):
        text = ("supplier_id,name,country,registration_id,tax_id,postcode\n\n"
                'S1,"Alpha\nTrading",GB,R1,,AA\n\nS10,Beta,GB,R10,,BB\n')
        rows = read_csv(text, SUPPLIER_FIELDS, "suppliers.csv", 1000)
        self.assertEqual([r["_line"] for r in rows], [3, 6])
        state = self.engine.analyze(text, csv_text(SPEND_FIELDS, self.invoices))
        action = self.engine.stage_action(next(e["entity_id"] for e in state["entities"] if "S1" in e["source_supplier_ids"]))
        self.assertEqual(action["evidence"][0]["line"], 3)

    def test_evidence_preserves_supplied_values_before_normalization(self):
        state = self.load([("S1", "Alpha", "gb", "R-1", "N/A", "AA"), self.suppliers[1]])
        action = self.engine.stage_action(next(e["entity_id"] for e in state["entities"] if "S1" in e["source_supplier_ids"]))
        excerpt = action["evidence"][0]["excerpt"]
        self.assertIn("country=gb", excerpt)
        self.assertIn("registration_id=R-1", excerpt)
        self.assertIn("tax_id=N/A", excerpt)

    def stage_alpha(self):
        entity = next(e for e in self.engine.state()["entities"] if "S1" in e["source_supplier_ids"])
        return self.engine.stage_action(entity["entity_id"])

    def execute(self, action):
        self.engine.approve_action(action["action_id"])
        return self.engine.execute_action(action["action_id"])

    def test_restored_snapshot_needs_a_fresh_pending_action(self):
        old = self.stage_alpha()
        self.engine.approve_action(old["action_id"])
        self.load(invoices=[self.invoices[0], (*self.invoices[1][:3], "201", *self.invoices[1][4:])])
        self.load()
        fresh = self.stage_alpha()
        self.assertEqual(fresh["status"], "pending")
        self.assertNotEqual(fresh["action_id"], old["action_id"])
        with self.assertRaises(ValidationError):
            self.engine.execute_action(fresh["action_id"])
        self.execute(fresh)
        self.assertFalse(self.engine.portal()["suppliers"][0]["stale"])

    def test_executed_action_does_not_hide_overwritten_portal_state(self):
        old = self.execute(self.stage_alpha())
        self.load(suppliers=[("S1", "Changed Alpha", "GB", "R1", "", "AA"), self.suppliers[1]])
        self.execute(self.stage_alpha())
        self.load()
        with self.assertRaises(ValidationError):
            self.engine.execute_action(old["action_id"])
        fresh = self.stage_alpha()
        self.assertNotEqual(fresh["action_id"], old["action_id"])
        self.assertEqual(fresh["status"], "pending")
        self.execute(fresh)
        portal = self.engine.portal()["suppliers"][0]
        self.assertEqual(portal["display_name"], "Alpha")
        self.assertFalse(portal["stale"])

    def test_live_and_completed_action_reuse_remains_idempotent(self):
        action = self.stage_alpha()
        self.assertEqual(self.stage_alpha()["action_id"], action["action_id"])
        self.execute(action)
        before = self.engine.portal()
        self.assertEqual(self.stage_alpha()["action_id"], action["action_id"])
        self.engine.execute_action(action["action_id"])
        self.assertEqual(self.engine.portal(), before)

    def test_report_escapes_source_markdown_and_html(self):
        hostile = '![tracking](https://example.invalid/pixel) [click](javascript:bad) <img src=x> &amp;'
        self.load([("S1", hostile, "GB", "R1", "", "AA"), self.suppliers[1]])
        report = self.engine.export_report()
        self.assertNotIn("![tracking]", report)
        self.assertNotIn("[click]", report)
        self.assertNotIn("<img", report)
        self.assertNotIn("https://example", report)
        self.assertIn("&#91;tracking&#93;", report)
        self.assertIn("&lt;img", report)

    def test_context_budget_is_only_a_preview_and_no_savings_ratio_is_claimed(self):
        invoices = [(f"I-{n}", "S1", "2026-01-01", "0.10", "GBP", "Hardware", "x" * 200) for n in range(20)]
        self.load(invoices=invoices)
        result = self.engine.query("total spend", budget_tokens=256)
        self.assertIn("GBP 2.00", result["answer"])
        self.assertLess(len(result["evidence"]), 20)
        self.assertLessEqual(sum(len(json.dumps(e, ensure_ascii=False)) for e in result["evidence"]), 1024)
        self.assertNotIn("reduction_ratio", result["context"])
        self.assertTrue(any("calculation used every matching row" in s for s in result["unknowns"]))


if __name__ == "__main__":
    unittest.main()
