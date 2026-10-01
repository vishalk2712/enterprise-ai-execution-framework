import json
import os
from pathlib import Path
import tempfile
import unittest

from enterprise_ai.pipeline import run_pipeline
from enterprise_ai.server import demo_csv


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_DBT') == '1', 'Optional dbt integration environment required')
class ContractTests(unittest.TestCase):
    def test_real_dbt_success_and_failed_gate_retains_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            suppliers, spend = demo_csv()
            sp, ip = base/'suppliers.csv', base/'spend.csv'
            sp.write_text(suppliers, encoding='utf-8')
            ip.write_text(spend, encoding='utf-8')
            manifest = run_pipeline(sp, ip, base/'good')
            self.assertEqual(manifest['contracts']['status'], 'passed')
            self.assertTrue(all(r['status'] in ('pass', 'success') for r in manifest['contracts']['results']))
            self.assertTrue((Path(manifest['output_dir'])/'resolution-audit.jsonl').exists())
            ip.write_text(spend.replace('GBP', 'INVALID'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'matching did not run'):
                run_pipeline(sp, ip, base/'bad')
            failed = next((base/'bad').iterdir())
            self.assertTrue((failed/'contracts'/'dbt.log').exists())
            self.assertFalse((failed/'manifest.json').exists())
            self.assertFalse((failed/'state.json').exists())


if __name__ == '__main__':
    unittest.main()
