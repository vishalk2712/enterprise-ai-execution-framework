"""Checks for source loss, binding, private evidence and real CLI/XLSX paths."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace

from enterprise_ai.engine import Engine
from enterprise_ai.ingest import (IngestError, SourceMapping, apply_mapping, infer_mapping,
                                  load_prepared, parse_amount, prepare_dataset, profile_source)
from test_ingest import VENDORS, INVOICES


class AdapterSafetyTests(unittest.TestCase):
    def test_tax_number_is_never_inferred_as_legal_registration(self):
        source = VENDORS.replace(b'REGISTRATION_ID', b'STCD1')
        mapping = infer_mapping(profile_source(source), 'suppliers', data=source)
        self.assertNotIn('registration_id', mapping.columns)
        prepared = prepare_dataset(source, INVOICES)
        engine = Engine(); self.addCleanup(engine.close)
        self.assertEqual(prepared.analyze_with(engine)['dataset']['entity_count'], 6)

    def test_explicit_registration_mapping_remains_available(self):
        source = VENDORS.replace(b'REGISTRATION_ID', b'STCD1')
        mapping = infer_mapping(profile_source(source), 'suppliers', data=source,
                                column_overrides={'registration_id': 'STCD1'})
        self.assertEqual(mapping.columns['registration_id'], 'STCD1')

    def test_bank_extras_are_retained_locally_but_not_in_engine_manifest(self):
        prepared = prepare_dataset(VENDORS, INVOICES)
        self.assertEqual(prepared.extras['suppliers']['SRC:0000010001']['BANKN'], '12345678')
        self.assertNotIn('12345678', json.dumps(prepared.manifest))
        report = profile_source(VENDORS).report()
        bank = next(c for c in report['columns'] if c['name'] == 'BANKN')
        self.assertEqual(bank['samples'], ['[REDACTED]'])
        self.assertIn('MWSKZ_AMT', prepared.extras['spend']['SRC:0000010001:1001'])

    def test_invalid_amount_text_and_double_negatives_are_not_sanitized_into_money(self):
        for text in ('invoice 123', '1e3', '100oops', '-(250)', '(-250)', '12,34.56'):
            self.assertIsNone(parse_amount(text), text)

    def test_ambiguous_or_mixed_decimal_styles_require_an_explicit_policy(self):
        header = 'invoice_id;supplier_id;invoice_date;amount;currency\n'
        for values in (('1.234',), ('1,23', '2.34')):
            source = header + ''.join(f'I{i};S1;2026-01-01;{value};GBP\n' for i, value in enumerate(values))
            with self.assertRaises(IngestError):
                infer_mapping(profile_source(source), 'spend', data=source)

    def test_lossy_currency_rounding_is_refused(self):
        for currency, amount in (('KWD', '1.234'), ('GBP', '1.234'), ('JPY', '1.25')):
            source = f'invoice_id,supplier_id,invoice_date,amount,currency\nI1,S1,2026-01-01,{amount},{currency}\n'
            mapping = infer_mapping(profile_source(source), 'spend', data=source, decimal_style='point')
            with self.assertRaisesRegex(IngestError, 'no rounding'):
                apply_mapping(source, mapping)

    def test_exact_three_minor_unit_values_remain_exact(self):
        source = 'invoice_id,supplier_id,invoice_date,amount,currency\nI1,S1,2026-01-01,1.230,KWD\n'
        mapping = infer_mapping(profile_source(source), 'spend', data=source, decimal_style='point')
        prepared = apply_mapping(source, mapping)
        self.assertIn('1.23,KWD', prepared.csv_text)
        self.assertTrue(any('No rounding' in note for note in prepared.notes))

    def test_rejected_row_refuses_the_batch_unless_mapping_policy_explicitly_allows_it(self):
        source = INVOICES.replace(b'28022026', b'not-a-date')
        mapping = infer_mapping(profile_source(source), 'spend', data=source)
        with self.assertRaisesRegex(IngestError, 'whole source refused'):
            apply_mapping(source, mapping)
        partial = apply_mapping(source, replace(mapping, allow_rejected_rows=True))
        self.assertEqual(partial.row_count, 3)
        self.assertEqual(len(partial.rejected), 1)
        self.assertNotEqual(partial.mapping_id, mapping.mapping_id)

    def test_malformed_width_or_duplicate_headers_never_discard_data(self):
        for source in ('a,b\nx,y,z\n', 'a,b\nx\n', 'a,a\nx,y\n'):
            with self.assertRaises(IngestError):
                profile_source(source, header_row=0)

    def test_zero_stripping_that_creates_key_collisions_is_rejected(self):
        source = 'supplier_id,name,country\n001,Alpha,GB\n1,Beta,GB\n'
        mapping = infer_mapping(profile_source(source), 'suppliers', data=source)
        self.assertIn('SRC:001', apply_mapping(source, mapping).csv_text)
        mapping = replace(mapping, transforms={'supplier_id': ['strip_leading_zeros']})
        with self.assertRaisesRegex(IngestError, 'conflicting rows'):
            apply_mapping(source, mapping)

    def test_mapping_version_and_policy_types_are_validated(self):
        mapping = infer_mapping(profile_source(VENDORS), 'suppliers', data=VENDORS)
        for changes in ({'version': 'unknown'}, {'passthrough': 'false'}, {'columns': {'surprise': 'LIFNR'}}):
            with self.assertRaises(IngestError):
                replace(mapping, **changes)

    def test_mapping_change_with_identical_csv_invalidates_approval(self):
        prepared = prepare_dataset(VENDORS, INVOICES)
        engine = Engine(); self.addCleanup(engine.close)
        first = prepared.analyze_with(engine)
        action = engine.stage_action(first['entities'][0]['entity_id'])
        sup = SourceMapping.from_json(json.dumps(prepared.manifest['mappings']['suppliers']))
        inv = SourceMapping.from_json(json.dumps(prepared.manifest['mappings']['spend']))
        changed = prepare_dataset(VENDORS, INVOICES, replace(sup, passthrough=False), inv)
        self.assertEqual(changed.suppliers_csv, prepared.suppliers_csv)
        second = changed.analyze_with(engine)
        self.assertNotEqual(first['dataset']['snapshot'], second['dataset']['snapshot'])
        self.assertEqual(engine._action(action['action_id'])['status'], 'stale')

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', *args], capture_output=True, text=True,
                                encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_cli_prepare_import_preserves_manifest_and_rejects_changed_csv(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'vendors.csv').write_bytes(VENDORS)
            (root/'invoices.csv').write_bytes(INVOICES)
            profile = self.cli('enterprise_ai.ingest', 'profile', '--input', str(root/'vendors.csv'))
            self.assertEqual(profile['row_count'], 6)
            self.cli('enterprise_ai.ingest', 'map', '--input', str(root/'vendors.csv'), '--target', 'suppliers', '--source-system', 'SAPGB', '--output', str(root/'sup.json'))
            self.cli('enterprise_ai.ingest', 'prepare', '--suppliers', str(root/'vendors.csv'), '--spend', str(root/'invoices.csv'), '--supplier-mapping', str(root/'sup.json'), '--source-system', 'SAPGB', '--output-dir', str(root/'prepared'))
            result = self.cli('enterprise_ai', '--db', str(root/'engine.sqlite'), 'analyze-prepared', '--input-dir', str(root/'prepared'), '--output', str(root/'report.md'))
            self.assertEqual(result['dataset']['matching_statistics']['upstream_contract']['source_system'], 'SAPGB')
            self.assertEqual(result['totals'][0]['amount'], '1234.56')
            csv = root/'prepared'/'spend.csv'
            csv.write_text(csv.read_text(encoding='utf-8').replace('1234.56', '9999.99'), encoding='utf-8')
            with self.assertRaisesRegex(IngestError, 'CSV changed'):
                load_prepared(root/'prepared')


class XLSXTests(unittest.TestCase):
    def setUp(self):
        if not importlib.util.find_spec('openpyxl'):
            if os.environ.get('OUTCOME_TEST_INGEST') == '1':
                self.fail('Dedicated XLSX checks require openpyxl')
            self.skipTest('Optional openpyxl is not installed')

    def workbook(self, path, rows):
        from openpyxl import Workbook
        book = Workbook()
        for row in rows: book.active.append(row)
        book.save(path); book.close()

    def test_actual_xlsx_prepare_and_engine_import(self):
        from datetime import datetime
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.workbook(root/'sup.xlsx', [['supplier_id', 'name', 'country'], ['001', 'Alpha Ltd', 'GB']])
            self.workbook(root/'inv.xlsx', [['invoice_id', 'supplier_id', 'invoice_date', 'amount', 'currency'], ['I1', '001', datetime(2026, 1, 1), 12.5, 'GBP']])
            prepared = prepare_dataset(root/'sup.xlsx', root/'inv.xlsx')
            engine = Engine(); self.addCleanup(engine.close)
            self.assertEqual(prepared.analyze_with(engine)['totals'][0]['amount'], '12.50')
            self.assertIn('SRC:001', prepared.suppliers_csv)

    def test_xlsx_formula_and_numeric_display_ids_require_values_export(self):
        from openpyxl import Workbook
        with TemporaryDirectory() as folder:
            path = Path(folder)/'sup.xlsx'
            self.workbook(path, [['supplier_id', 'name', 'country'], ['=1+1', 'Alpha', 'GB']])
            with self.assertRaisesRegex(IngestError, 'formula'):
                profile_source(path)
            book = Workbook(); book.active.append(['supplier_id','name','country']); book.active.append([1,'Alpha','GB']); book.active['A2'].number_format='00000'; book.save(path); book.close()
            with self.assertRaisesRegex(IngestError, 'as text'):
                profile_source(path)
