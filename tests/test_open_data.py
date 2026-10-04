"""Real source shapes, payment reconciliation and reference/human separation."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enterprise_ai.engine import Engine
from enterprise_ai.ingest import IngestError, SourceMapping, parse_date, detect_date_order, validate_prepared
from enterprise_ai.matching import MatchConfig
from enterprise_ai.open_data import prepare_payments
from enterprise_ai.procurement_model import load_observations, select_cohorts, build_pairs
from enterprise_ai.pair_model import fit_pair_model, partitions_for


class PublicPaymentTests(unittest.TestCase):
    def source(self, root):
        body = ('Entity,Supplier,Date,Transaction,Amount,Type,Description\n'
                'CAB,Example Ltd,03-Mar-25,100,"1,000.50",Software,Licence\n'
                'CAB,Example Ltd,03-Mar-25,100,200.00,Support,Service\n'
                'CAB,Example Ltd,03-Mar-25,100,200.00,Support,Service\n'
                'CAB,Example Limited,10-Mar-25,101,-25.25,Support,Credit\n').encode()
        source = {'publisher':'fixture','file':'payments.csv','url':'https://assets.publishing.service.gov.uk/fixture.csv',
                  'date':'Date','transaction':'Transaction','category':'Type','description':'Description'}
        (root/source['file']).write_bytes(body)
        receipt = {'url':source['url'],'sha256':hashlib.sha256(body).hexdigest()}
        (root/(source['file']+'.source.json')).write_text(json.dumps(receipt),encoding='utf-8')
        return source

    def test_all_lines_survive_repeated_references_and_identical_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            source=self.source(Path(temp)); prepared=prepare_payments(temp,[source])
            validate_prepared(prepared)
            self.assertEqual(prepared.manifest['spend']['rows'],4)
            self.assertEqual(prepared.manifest['suppliers']['rows'],2)
            evidence=prepared.manifest['public_data']
            self.assertEqual(evidence['reconciliation']['difference'],'0.00')
            self.assertEqual(evidence['reconciliation']['canonical_total'],'1375.25')
            self.assertEqual(evidence['sources'][0]['repeated_transaction_lines'],2)
            engine=Engine(':memory:')
            try:
                state=prepared.analyze_with(engine)
                self.assertEqual(state['totals'][0]['amount'],'1375.25')
                self.assertEqual(state['dataset']['invoice_count'],4)
                self.assertEqual(state['dataset']['entity_count'],2)
                self.assertEqual(engine.export_labels(),'')
                self.assertTrue(all(r['country']=='ZZ' and not r['registration_id'] for r in engine._records('suppliers')))
                answer=engine.query('What is total published payments?')
                self.assertIn('Published payments',answer['answer'])
                self.assertNotIn('Net spend',answer['answer'])
                self.assertIn('published payment lines',engine.export_report())
            finally:
                engine.close()

    def test_mutated_source_is_refused_before_ingestion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=self.source(root)
            with (root/source['file']).open('ab') as handle: handle.write(b'altered')
            with self.assertRaises(IngestError): prepare_payments(root,[source])

    def test_measurement_policy_is_snapshot_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            source=self.source(Path(temp)); prepared=prepare_payments(temp,[source])
            prepared.manifest['public_data']['amount_basis']='net'
            with self.assertRaises(IngestError): validate_prepared(prepared)

    def test_two_digit_year_policy_and_boundary(self):
        for value,expected in [('03-Mar-25','2025-03-03'),('31-Dec-69','2069-12-31'),('01-Jan-70','1970-01-01'),('01-Jan-00','2000-01-01')]:
            self.assertEqual(parse_date(value),expected)
        self.assertEqual(parse_date('03-Mar-25',two_digit_year_pivot=20),'1925-03-03')
        self.assertIsNone(parse_date('29-Feb-25'))
        self.assertEqual(detect_date_order(['03/04/25']), 'ambiguous')
        self.assertEqual(detect_date_order(['13/04/25']), 'dmy')
        with self.assertRaises(IngestError): parse_date('03-Mar-25',two_digit_year_pivot=True)

    def test_pivot_changes_mapping_fingerprint(self):
        columns={'supplier_id':'supplier_id','name':'name','country':'country'}
        a=SourceMapping('FIX','suppliers',columns,two_digit_year_pivot=70)
        b=SourceMapping('FIX','suppliers',columns,two_digit_year_pivot=20)
        self.assertNotEqual(a.mapping_id,b.mapping_id)
        self.assertEqual(SourceMapping.from_json(a.to_json()).two_digit_year_pivot,70)


def release(ocid, company, name, address=''):
    return {'ocid':ocid,'awards':[{'status':'active','suppliers':[{'id':'p'}]}],
            'parties':[{'id':'p','name':name,'roles':['supplier'],'identifier':{'scheme':'GB-COH','id':company},
                        'address':{'streetAddress':address}}]}


class ProcurementReferenceTests(unittest.TestCase):
    def write(self, root, rows):
        path=root/'cohort.jsonl.gz'
        with gzip.open(path,'wt',encoding='utf-8') as f:
            f.writelines(json.dumps(r)+'\n' for r in rows)
        return path

    def test_only_valid_asserted_supplier_ids_supply_reference_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.write(Path(temp),[release('a','00012345','Alpha Ltd','Actual first'),
                                       release('b','00012345','Alpha Supply','Actual second'),
                                       release('c','0','Unknown Supplier')])
            groups,counts=load_observations(path)
            self.assertEqual(set(groups),{'00012345'})
            self.assertEqual(counts['invalid_coh_observations'],1)
            rows=[v['record'] for v in groups['00012345'].values()]
            self.assertEqual({r['address'].strip() for r in rows},{'Actual first','Actual second'})
            self.assertTrue(all(not r['registration_id'] and not r['tax_id'] for r in rows))
            pairs,stats,provenance=build_pairs(groups,{'train':['00012345']},MatchConfig())
            self.assertEqual(pairs[0]['reference_label'],'Match')
            self.assertNotIn('human_label',pairs[0])
            self.assertEqual(pairs[0]['model_features']['shared_registration_id'],0)
            self.assertEqual(pairs[0]['model_features']['identifier_conflict'],0)
            self.assertEqual(len(provenance),2)

    def test_shared_name_different_ids_excludes_both_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.write(Path(temp),[release('a','00012345','Alpha Ltd'),release('b','00054321','Alpha Limited')])
            groups,counts=load_observations(path)
            self.assertEqual(groups,{})
            self.assertEqual(counts['name_collision_groups_excluded'],2)

    def test_incremental_release_input_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            row=release('a','00012345','Alpha')
            with self.assertRaises(ValueError): load_observations(self.write(Path(temp),[row,row]))

    def test_truncated_gzip_never_returns_a_cohort(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.write(Path(temp),[release('a','00012345','Alpha')])
            path.write_bytes(path.read_bytes()[:-8])
            with self.assertRaises(EOFError): load_observations(path)

    def test_company_splits_are_disjoint_and_reproducible(self):
        groups={str(i):{'a':{},'b':{}} if i%2 else {'a':{}} for i in range(200)}
        splits=select_cohorts(groups,100)
        self.assertEqual(splits,select_cohorts(dict(reversed(list(groups.items()))),100))
        self.assertEqual([len(splits[k]) for k in ('train','validation','test')],[60,20,20])
        self.assertFalse(set(splits['train']) & set(splits['test']))

    def test_reference_label_calibration_is_refused(self):
        with self.assertRaisesRegex(ValueError,'calibration'):
            fit_pair_model([],MatchConfig().config_id,'supplier',calibrate=True,label_field='reference_label')

    def test_reference_labels_require_provenance_and_no_human_label(self):
        for row in ({'reference_label':'Match'},{'reference_label':'Match','human_label':'Match','label_provenance':'source'}):
            with self.assertRaisesRegex(ValueError,'provenance'):
                fit_pair_model([row],MatchConfig().config_id,'supplier',label_field='reference_label')


if __name__=='__main__': unittest.main()
