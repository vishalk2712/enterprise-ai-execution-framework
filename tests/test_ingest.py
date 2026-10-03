"""Source adapter checks against extracts shaped like real ERP exports.

Fixtures are synthetic but deliberately hostile: banner rows above the header,
CP1252 bytes, semicolon delimiters, zero-padded keys, compact and ambiguous
dates, decimal commas, parenthesised credits, placeholder identifiers, invoice
numbers unique only per vendor, and columns the engine does not model.
"""

import json
import unittest
from decimal import Decimal

from enterprise_ai.engine import Engine
from enterprise_ai.ingest import (
    IngestError, SourceMapping, apply_mapping, detect_date_order,
    detect_decimal_style, infer_mapping, parse_amount, parse_date,
    prepare_dataset, profile_source,
)


VENDORS = (
    "Vendor Master Extract;Run 2026-09-30;Client 100\n"
    "Selection: BUKRS = GB01\n"
    "LIFNR;NAME1;LAND1;REGISTRATION_ID;STCEG;PSTLZ;STRAS;ORT01;ZTERM;BANKN\n"
    "0000010001;Müller Kabel GmbH;DE;HRB12345;DE811234567;80331;Leopoldstr 1;München;NT30;12345678\n"
    "0000010002;MULLER KABEL GMBH;DE;HRB12345;DE811234567;80331;Leopoldstr 1;Munchen;NT30;12345678\n"
    "0000010003;Northern Steel Ltd;GB;SC456789;GB123456789;AB10 1AA;12 Dock Rd;Aberdeen;NT60;87654321\n"
    "0000010004;Northern Steel Limited;GB;SC456789;GB123456789;AB10 1AA;12 Dock Road;Aberdeen;NT60;87654321\n"
    "0000010005;Acme Parts & Co;GB;0;N/A;M1 2AB;5 Mill St;Manchester;NT30;11112222\n"
    "0000010006;Beta Tooling;GB;0;TBC;LS1 4XY;9 Kirk Ln;Leeds;NT30;33334444\n"
).encode("cp1252")

INVOICES = (
    "Invoice Register;GB01\n"
    "BELNR;LIFNR;BLDAT;WRBTR;MWSKZ_AMT;GROSS;WAERS;MATKL;SGTXT\n"
    "1001;0000010001;15032026;1.234,56;246,91;1.481,47;EUR;CABLE;Fibre drum\n"
    "1002;0000010003;28022026;2.000,00;400,00;2.400,00;GBP;STEEL;Plate order\n"
    "1001;0000010005;01042026;500,00;100,00;600,00;GBP;PARTS;Repeated number\n"
    "1003;0000010006;12042026;(250,00);(50,00);(300,00);GBP;TOOL;Credit note\n"
).encode("cp1252")


class ValueParsingTests(unittest.TestCase):
    def test_dates_in_every_shape_an_erp_emits(self):
        for value, order, expected in (
                ("2026-03-15", "dmy", "2026-03-15"), ("20260315", "dmy", "2026-03-15"),
                ("15032026", "dmy", "2026-03-15"), ("03152026", "mdy", "2026-03-15"),
                ("15/03/2026", "dmy", "2026-03-15"), ("03/15/2026", "mdy", "2026-03-15"),
                ("15.03.2026", "dmy", "2026-03-15"), ("15-Mar-2026", "dmy", "2026-03-15")):
            with self.subTest(value=value):
                self.assertEqual(parse_date(value, order), expected)

    def test_impossible_and_empty_dates_return_none(self):
        for value in ("", "not a date", "2026-13-01", "32/01/2026", "abcdefgh"):
            self.assertIsNone(parse_date(value, "dmy"))

    def test_date_order_is_decided_on_evidence_or_refused(self):
        self.assertEqual(detect_date_order(["20260315", "20260228"]), "ymd")
        self.assertEqual(detect_date_order(["15/03/2026", "01/02/2026"]), "dmy")
        self.assertEqual(detect_date_order(["03/15/2026", "01/02/2026"]), "mdy")
        self.assertEqual(detect_date_order(["01/02/2026", "03/04/2026"]), "ambiguous")
        self.assertEqual(detect_date_order(["15/03/2026", "03/15/2026"]), "conflicting")

    def test_amounts_in_both_decimal_conventions(self):
        self.assertEqual(parse_amount("1.234,56", "comma"), Decimal("1234.56"))
        self.assertEqual(parse_amount("1,234.56", "point"), Decimal("1234.56"))
        self.assertEqual(parse_amount("€ 1.234,56", "comma"), Decimal("1234.56"))

    def test_credits_are_recognised_in_every_notation(self):
        self.assertEqual(parse_amount("(250,00)", "comma"), Decimal("-250.00"))
        self.assertEqual(parse_amount("-250.00", "point"), Decimal("-250.00"))
        self.assertEqual(parse_amount("250.00 CR", "point"), Decimal("-250.00"))
        self.assertEqual(parse_amount("250.00 DR", "point"), Decimal("250.00"))

    def test_decimal_style_detection(self):
        self.assertEqual(detect_decimal_style(["1.234,56", "2.000,00"]), "comma")
        self.assertEqual(detect_decimal_style(["1,234.56", "2,000.00"]), "point")


class ProfilingTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_source(VENDORS, "vendors.csv")

    def test_encoding_delimiter_and_banner_rows_are_detected(self):
        self.assertEqual(self.profile.encoding, "cp1252")
        self.assertEqual(self.profile.delimiter, ";")
        self.assertEqual(self.profile.header_row, 2)
        self.assertEqual(self.profile.row_count, 6)

    def test_real_header_names_survive_the_banner(self):
        self.assertEqual([c.name for c in self.profile.columns][:4],
                         ["LIFNR", "NAME1", "LAND1", "REGISTRATION_ID"])

    def test_placeholder_identifiers_are_reported_before_import(self):
        self.assertEqual(self.profile.column("REGISTRATION_ID").placeholder_count, 2)
        self.assertTrue(any("not identity" in note for note in self.profile.notes))

    def test_column_types_are_inferred(self):
        self.assertEqual(self.profile.column("LAND1").inferred_type, "country_code")
        spend = profile_source(INVOICES, "invoices.csv")
        self.assertEqual(spend.column("WAERS").inferred_type, "currency_code")

    def test_an_empty_source_is_refused(self):
        with self.assertRaises(IngestError):
            profile_source(b"   ", "empty.csv")


class MappingTests(unittest.TestCase):
    def test_sap_field_names_map_without_help(self):
        mapping = infer_mapping(profile_source(VENDORS, "v"), "suppliers", "SAPGB", VENDORS)
        self.assertEqual(mapping.columns["supplier_id"], "LIFNR")
        self.assertEqual(mapping.columns["name"], "NAME1")
        self.assertEqual(mapping.columns["registration_id"], "REGISTRATION_ID")
        self.assertEqual(mapping.columns["tax_id"], "STCEG")

    def test_per_vendor_invoice_numbering_is_detected(self):
        mapping = infer_mapping(profile_source(INVOICES, "i"), "spend", "SAPGB", INVOICES)
        self.assertEqual(mapping.invoice_key_scope, "supplier")
        self.assertEqual(mapping.decimal_style, "comma")

    def test_auxiliary_tax_columns_are_recognised_not_discarded(self):
        mapping = infer_mapping(profile_source(INVOICES, "i"), "spend", "SAPGB", INVOICES)
        self.assertEqual(mapping.auxiliary["tax_amount"], "MWSKZ_AMT")
        self.assertEqual(mapping.auxiliary["gross_amount"], "GROSS")

    def test_ambiguous_dates_are_refused_rather_than_guessed(self):
        source = ("INVOICE_NO;VENDOR_NO;INVOICE_DATE;NET_AMOUNT;CURRENCY\n"
                  "1;V1;01/02/2026;10.00;GBP\n"
                  "2;V2;03/04/2026;20.00;GBP\n").encode()
        with self.assertRaises(IngestError) as caught:
            infer_mapping(profile_source(source, "s"), "spend", "SRC", source)
        self.assertIn("ambiguous", str(caught.exception))

    def test_an_explicit_date_order_resolves_the_refusal(self):
        source = ("INVOICE_NO;VENDOR_NO;INVOICE_DATE;NET_AMOUNT;CURRENCY\n"
                  "1;V1;01/02/2026;10.00;GBP\n"
                  "2;V2;03/04/2026;20.00;GBP\n").encode()
        mapping = infer_mapping(profile_source(source, "s"), "spend", "SRC", source, date_order="dmy")
        prepared = apply_mapping(source, mapping, "s")
        self.assertIn("2026-02-01", prepared.csv_text)

    def test_mapping_is_fingerprinted_and_tamper_evident(self):
        mapping = infer_mapping(profile_source(VENDORS, "v"), "suppliers", "SAPGB", VENDORS)
        self.assertEqual(SourceMapping.from_json(mapping.to_json()).mapping_id, mapping.mapping_id)
        tampered = json.loads(mapping.to_json())
        tampered["columns"]["name"] = "ORT01"
        with self.assertRaises(IngestError):
            SourceMapping.from_json(json.dumps(tampered))

    def test_mapping_rejects_an_unusable_configuration(self):
        with self.assertRaises(IngestError):
            SourceMapping(source_system="X", target="nonsense", columns={})
        with self.assertRaises(IngestError):
            SourceMapping(source_system="bad system!", target="suppliers",
                          columns={"supplier_id": "a", "name": "b", "country": "c"})


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.prepared = prepare_dataset(VENDORS, INVOICES, source_system="SAPGB",
                                        supplier_name="vendors.csv", spend_name="invoices.csv")

    def test_repeated_invoice_numbers_become_unique_instead_of_rejected(self):
        ids = [line.split(",")[0] for line in self.prepared.spend_csv.splitlines()[1:]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("SAPGB:0000010001:1001", ids)
        self.assertIn("SAPGB:0000010005:1001", ids)

    def test_unmapped_columns_are_retained_as_extras_not_dropped(self):
        extras = self.prepared.extras["suppliers"]["SAPGB:0000010001"]
        self.assertEqual(extras["ZTERM"], "NT30")
        self.assertEqual(extras["BANKN"], "12345678")
        self.assertNotIn("ZTERM", self.prepared.suppliers_csv)

    def test_values_are_normalised_for_the_engine_schema(self):
        rows = self.prepared.spend_csv.splitlines()
        self.assertIn("2026-03-15,1234.56,EUR", rows[1])
        self.assertIn("-250.00", self.prepared.spend_csv)

    def test_placeholder_identifiers_are_blanked_before_the_engine_sees_them(self):
        for line in self.prepared.suppliers_csv.splitlines():
            if "Acme" in line or "Beta" in line:
                self.assertIn(",,,", line)

    def test_manifest_fingerprints_both_mappings_and_both_sources(self):
        manifest = self.prepared.manifest
        self.assertNotEqual(manifest["suppliers"]["mapping_id"], manifest["spend"]["mapping_id"])
        self.assertEqual(len(manifest["manifest_id"]), 64)
        self.assertTrue(manifest["suppliers"]["source_sha256"])

    def test_mismatched_key_settings_are_refused(self):
        suppliers = infer_mapping(profile_source(VENDORS, "v"), "suppliers", "SAPGB", VENDORS)
        spend = infer_mapping(profile_source(INVOICES, "i"), "spend", "OTHER", INVOICES)
        with self.assertRaises(IngestError):
            prepare_dataset(VENDORS, INVOICES, suppliers, spend)


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.prepared = prepare_dataset(VENDORS, INVOICES, source_system="SAPGB")
        self.state = self.prepared.analyze_with(self.engine)

    def test_the_unmodified_engine_accepts_the_adapter_output(self):
        self.assertEqual(self.state["dataset"]["supplier_count"], 6)
        self.assertEqual(self.state["dataset"]["invoice_count"], 4)

    def test_registration_ids_group_duplicates_across_name_variants(self):
        grouped = {tuple(e["source_supplier_ids"]) for e in self.state["entities"]
                   if len(e["source_supplier_ids"]) > 1}
        self.assertIn(("SAPGB:0000010001", "SAPGB:0000010002"), grouped)
        self.assertIn(("SAPGB:0000010003", "SAPGB:0000010004"), grouped)

    def test_placeholder_registration_ids_do_not_group_unrelated_vendors(self):
        for entity in self.state["entities"]:
            if any("Acme" in entity["display_name"] or "Beta" in entity["display_name"]
                   for _ in [0]):
                self.assertEqual(len(entity["source_supplier_ids"]), 1)

    def test_currency_totals_are_exact_and_separate(self):
        totals = {t["currency"]: t["amount"] for t in self.state["totals"]}
        self.assertEqual(totals, {"EUR": "1234.56", "GBP": "2250.00"})

    def test_the_mapping_manifest_reaches_the_snapshot(self):
        contract = self.state["dataset"]["matching_statistics"]["upstream_contract"]
        self.assertEqual(contract["source_system"], "SAPGB")
        self.assertIn("manifest_id", contract)

    def test_a_changed_mapping_produces_a_different_snapshot(self):
        first = self.state["dataset"]["snapshot"]
        other = prepare_dataset(VENDORS, INVOICES, source_system="SAPDE")
        second = other.analyze_with(Engine())["dataset"]["snapshot"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
