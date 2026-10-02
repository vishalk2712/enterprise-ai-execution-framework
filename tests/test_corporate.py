import json
from pathlib import Path
import tempfile
import unittest

from enterprise_ai.corporate_benchmark import fetch_pages, load_gleif


class CorporateAdapterTests(unittest.TestCase):
    def test_alias_labels_without_identifier_or_alias_feature_leakage(self):
        def entity(lei, legal, aliases):
            return {"id": lei, "attributes": {"entity": {"legalName": {"name": legal}, "otherNames": [{"name": name, "type": "PREVIOUS_LEGAL_NAME"} for name in aliases], "legalAddress": {"addressLines": ["Synthetic Street"], "city": "Synthetic Town", "country": "GB", "postalCode": "ZZ11ZZ"}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"page-001.json"
            path.write_text(json.dumps({"data": [entity("REGISTRY-A", "Cedar Ltd", ["Old Cedar Limited", "Cedar Ltd"]), entity("REGISTRY-B", "Cedar Holdings", [])]}))
            rows, groups, positives, provenance = load_gleif(directory)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(positives), 1)
            self.assertEqual(provenance["alias_records"], 1)
            for row in rows:
                self.assertEqual(row["tax_id"], "")
                self.assertEqual(row["registration_id"], "")
                self.assertNotIn("aliases", row)
                self.assertNotIn("lei", row)
            path.write_text(json.dumps({"data": [entity("DUP", "A", []), entity("DUP", "B", [])]}))
            with self.assertRaisesRegex(ValueError, "repeated LEI"):
                load_gleif(directory)

    def test_fetch_rejects_unbounded_sample_before_network(self):
        for pages in (0, 51):
            with self.assertRaisesRegex(ValueError, "1..50"):
                fetch_pages("not-created", pages)
