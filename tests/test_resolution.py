import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from enterprise_ai.matching import MatchConfig, generate_candidates, score_pair, evaluate_records, sample_excluded_tax_pairs
from enterprise_ai.calibration import fit_calibration, probability
from enterprise_ai.engine import Engine
from enterprise_ai.server import demo_csv
from enterprise_ai.benchmarks import record, load_amazon


class ResolutionTests(unittest.TestCase):
    def test_first_word_typo_and_alias_candidates(self):
        rows = [record('1', 'Acme industrial'), record('2', 'Akme industrial')]
        rows.append({**record('3', 'Unrelated holding'), 'aliases': 'Acme industrial'})
        pairs, _ = generate_candidates(rows, MatchConfig())
        self.assertIn(('1', '2'), pairs)
        self.assertIn(('1', '3'), pairs)

    def test_cross_country_is_review_not_block(self):
        rows = [record('1', 'Acme', country='GB'), record('2', 'Acme', country='US')]
        evaluations, _ = evaluate_records(rows, MatchConfig())
        self.assertEqual(evaluations[0]['algorithmic_outcome'], 'Conflict_Review')
        self.assertIsNone(evaluations[0]['match_probability'])

    def test_multifeature_and_missing_values(self):
        left, right = record('1', 'abcdef', '10 High Street'), record('2', 'abcdeg', 'High Street 10')
        result = score_pair(left, right, MatchConfig())
        self.assertAlmostEqual(result['features']['name_levenshtein'], 5/6)
        self.assertEqual(result['features']['address_jaccard'], 1)
        right['address'] = ''
        self.assertAlmostEqual(score_pair(left, right, MatchConfig())['similarity_score'], 5/6)

    def test_all_evaluated_near_misses_retained(self):
        rows = [record('1', 'abcdef'), record('2', 'abcdeg')]
        evaluations, stats = evaluate_records(rows, MatchConfig())
        self.assertEqual(stats['near_misses'], 1)
        self.assertEqual(evaluations[0]['algorithmic_outcome'], 'Below_Threshold')
        self.assertEqual(evaluations[0]['sampling_probability'], 1)

    def test_tax_sample_is_bounded_repeatable_and_not_a_label(self):
        rows = [{**record(str(i), str(i)), 'tax_id': 'SHARED'} for i in range(12)]
        config = MatchConfig(excluded_tax_sample_size=5)
        first, population = sample_excluded_tax_pairs(rows, {}, config)
        self.assertEqual(population, 66)
        self.assertEqual(len(first), 5)
        self.assertEqual(first, sample_excluded_tax_pairs(list(reversed(rows)), {}, config)[0])
        evaluations, _ = evaluate_records(rows, config)
        self.assertTrue(all('human_label' not in item for item in evaluations))

    def test_posting_and_evaluation_limits_are_visible(self):
        rows = [record(str(i), 'Acme') for i in range(5)]
        pairs, stats = generate_candidates(rows, MatchConfig(max_posting=2))
        self.assertEqual(len(pairs), 0)
        self.assertGreater(stats['oversized_postings_skipped'], 0)
        with self.assertRaisesRegex(ValueError, 'budget'):
            generate_candidates(rows, MatchConfig(max_evaluations=1))

    def test_invalid_config(self):
        for overrides in ({'ngram_size': 1}, {'name_weight': float('nan')}, {'max_evaluations': True}, {'near_miss_floor': .99}):
            with self.assertRaises(ValueError):
                MatchConfig(**overrides)

    def test_audit_and_reviews_survive_import_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'engine.sqlite')
            engine = Engine(path)
            state = engine.analyze(*demo_csv())
            run_id = state['dataset']['resolution_run_id']
            item = engine.evaluations()['evaluations'][0]
            original = item['algorithmic_outcome']
            decision = engine.review(item['evaluation_id'], 'Unsure', 'Test reviewer', 'Needs source evidence')
            with self.assertRaisesRegex(ValueError, 'changed'):
                engine.review(item['evaluation_id'], 'Match', 'Test reviewer', 'Stale update')
            engine.review(item['evaluation_id'], 'NonMatch', 'Test reviewer', 'Verified independently', decision['decision_id'])
            engine.analyze(*demo_csv())
            engine.close()
            engine = Engine(path)
            restored = engine.evaluations(run_id)['evaluations'][0]
            self.assertEqual(restored['algorithmic_outcome'], original)
            self.assertEqual(restored['human_label'], 'NonMatch')
            self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM review_decisions').fetchone()[0], 2)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                engine.db.execute('DELETE FROM pair_evaluations')
            exported = [json.loads(line) for line in engine.export_labels().splitlines()]
            self.assertEqual(exported[0]['human_label'], 'NonMatch')
            self.assertIsNone(exported[0]['split'])
            engine.close()

    def test_config_changes_effective_snapshot(self):
        a, b = Engine(), Engine(match_config=MatchConfig(name_weight=.6))
        try:
            left, right = a.analyze(*demo_csv())['dataset'], b.analyze(*demo_csv())['dataset']
            self.assertEqual(left['snapshot_id'], right['snapshot_id'])
            self.assertNotEqual(left['snapshot'], right['snapshot'])
        finally:
            a.close(); b.close()

    def test_calibration_rejects_leakage_and_wrong_domain(self):
        config = MatchConfig()
        rows = [{'evaluation_id': f'{split}-{i}', 'config_id': config.config_id, 'split': split,
                 'entity_group_ids': [f'{split}-{i}'], 'similarity_score': .9 if i%2 else .2,
                 'human_label': 'Match' if i%2 else 'NonMatch'} for split in ('train', 'validation', 'test') for i in range(10)]
        artifact = fit_calibration(rows, config.config_id, 'supplier')
        self.assertGreater(probability(.9, artifact, config.config_id, 'supplier'), probability(.2, artifact, config.config_id, 'supplier'))
        from enterprise_ai.pipeline import run_pipeline
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            suppliers, spend = demo_csv()
            (base/'suppliers.csv').write_text(suppliers, encoding='utf-8')
            (base/'spend.csv').write_text(spend, encoding='utf-8')
            manifest = run_pipeline(base/'suppliers.csv', base/'spend.csv', base/'output', False, config, artifact)
            history = [json.loads(line) for line in (Path(manifest['output_dir'])/'resolution-audit.jsonl').read_text(encoding='utf-8').splitlines()]
            evaluation = next(row for row in history if row['table'] == 'pair_evaluations')
            self.assertEqual(json.loads(evaluation['payload_json'])['calibration_model_id'], artifact['model_id'])
        with self.assertRaises(ValueError):
            probability(.9, artifact, config.config_id, 'person')
        bad = copy.deepcopy(rows)
        bad[-1]['entity_group_ids'] = bad[0]['entity_group_ids']
        with self.assertRaisesRegex(ValueError, 'leakage'):
            fit_calibration(bad, config.config_id, 'supplier')
        bad = dict(artifact, slope=float('nan'))
        with self.assertRaises(ValueError):
            probability(.9, bad, config.config_id, 'supplier')

    def test_amazon_native_adapter_and_incomplete_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'train_ground_truth.tsv').write_text('source1_entity_id\tmatched_entity_ids\nA\tB,C\n')
            for source, rid in enumerate('ABC', 1):
                (base/f'train_source{source}.tsv').write_text(f'entity_id\tbusiness_name\tbusiness_address\tcountry\n{rid}\tAcme\t10 High Street\tUnited Kingdom\n')
            rows, labels, groups, _ = load_amazon(base)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(labels), 2)
            self.assertEqual(set(groups.values()), {'A'})
            (base/'train_source3.tsv').write_text('entity_id\tbusiness_name\tbusiness_address\tcountry\n')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                load_amazon(base)


if __name__ == '__main__':
    unittest.main()
