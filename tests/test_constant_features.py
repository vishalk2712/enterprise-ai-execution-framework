"""Regression coverage for identifiable coefficients and legacy migration."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from enterprise_ai.engine import Engine, ValidationError
from enterprise_ai.matching import MatchConfig
from enterprise_ai.pair_model import (FEATURE_NAMES, correct_constant_features, fingerprint,
    fit_pair_model, predict, training_feature_support, validate_artifact)
from enterprise_ai.server import demo_csv
from enterprise_ai.model_correction import run_correction
from test_pair_model import labeled_fixture, model_fixture


def legacy_fixture(rows=None):
    rows = labeled_fixture() if rows is None else rows
    artifact = model_fixture()
    artifact['weights'] = [0.0] * len(FEATURE_NAMES)
    artifact['weights'][FEATURE_NAMES.index('address_jaccard')] = 5.0
    artifact['weights'][FEATURE_NAMES.index('address_present')] = -1.2
    artifact['weights'][FEATURE_NAMES.index('name_levenshtein')] = 0.5
    artifact.update(training_rows=sum(r['split'] == 'train' for r in rows),
                    labels_sha256=fingerprint(rows), probability_status='model_estimate',
                    label_field='human_label')
    artifact.pop('model_id')
    artifact['model_id'] = fingerprint(artifact)
    return artifact


def reseal(artifact):
    artifact['model_id'] = fingerprint({k: v for k, v in artifact.items() if k != 'model_id'})
    return artifact


class ConstantFeatureCorrectionTests(unittest.TestCase):
    def test_nonbinary_constants_fold_into_intercept_without_mutating_original(self):
        rows = labeled_fixture()
        original = legacy_fixture(rows)
        untouched = copy.deepcopy(original)
        fixed = correct_constant_features(original, rows)
        self.assertEqual(original, untouched)
        self.assertAlmostEqual(fixed['intercept'], original['intercept'] - 1.2 + .5 * .8)
        self.assertEqual(fixed['weights'][FEATURE_NAMES.index('address_jaccard')], 5.0)
        self.assertEqual(fixed['review_threshold'], original['review_threshold'])
        self.assertNotEqual(fixed['model_id'], original['model_id'])
        self.assertEqual(fixed['constant_feature_correction']['source_model_id'], original['model_id'])
        self.assertFalse(fixed['constant_feature_correction']['refit'])
        self.assertEqual(fixed['training_feature_support']['constant_features']['name_levenshtein'], .8)
        for row in rows:
            self.assertAlmostEqual(predict(row['model_features'], original), predict(row['model_features'], fixed), places=14)
        validate_artifact(fixed, MatchConfig().config_id, 'supplier')

    def test_unsupported_feature_changes_cannot_promote_corrected_candidates(self):
        rows = labeled_fixture()
        old = legacy_fixture(rows)
        old['weights'][FEATURE_NAMES.index('same_country')] = -1.14
        for row in rows: row['model_features']['same_country'] = 1.0
        old['labels_sha256'] = fingerprint(rows)
        reseal(old)
        fixed = correct_constant_features(old, rows)
        a = rows[0]['model_features']
        b = dict(a, same_country=0.0, address_present=0.0, name_levenshtein=0.0)
        self.assertNotEqual(predict(a, old), predict(b, old))
        self.assertEqual(predict(a, fixed), predict(b, fixed))

    def test_migration_support_uses_only_training_not_holdout_values(self):
        rows = labeled_fixture()
        for row in rows:
            if row['split'] != 'train': row['model_features']['same_country'] = float(row['human_label'] == 'Match')
        fixed = correct_constant_features(legacy_fixture(rows), rows)
        self.assertEqual(fixed['training_feature_support']['constant_features']['same_country'], 0.0)
        self.assertNotIn('address_jaccard', fixed['training_feature_support']['constant_features'])

    def test_migration_requires_exact_original_labels_and_row_count(self):
        rows = labeled_fixture()
        old = legacy_fixture(rows)
        changed = copy.deepcopy(rows)
        changed[-1]['model_features']['same_country'] = 1.0
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            correct_constant_features(old, changed)
        old['training_rows'] += 1
        reseal(old)
        with self.assertRaisesRegex(ValueError, 'row count'):
            correct_constant_features(old, rows)

    def test_no_double_correction_and_no_silent_calibration_migration(self):
        rows = labeled_fixture()
        old = legacy_fixture(rows)
        fixed = correct_constant_features(old, rows)
        with self.assertRaisesRegex(ValueError, 'already'):
            correct_constant_features(fixed, rows)
        old.update(calibration={'method': 'heldout-platt-v1', 'slope': 1.0, 'intercept': 0.0, 'rows': 20},
                   probability_status='calibrated_pair_estimate')
        reseal(old)
        with self.assertRaisesRegex(ValueError, 'uncalibrated'):
            correct_constant_features(old, rows)

    def test_invalid_training_features_are_rejected(self):
        with self.assertRaises(ValueError): training_feature_support([])
        for invalid in ({}, dict.fromkeys(FEATURE_NAMES, True), dict.fromkeys(FEATURE_NAMES, float('nan'))):
            with self.assertRaises(ValueError): training_feature_support([invalid])

    def test_declared_constant_nonzero_coefficient_fails_even_with_valid_fingerprint(self):
        fixed = correct_constant_features(legacy_fixture(), labeled_fixture())
        fixed['weights'][FEATURE_NAMES.index('address_present')] = -1.0
        reseal(fixed)
        with self.assertRaisesRegex(ValueError, 'zero coefficients'):
            validate_artifact(fixed, MatchConfig().config_id, 'supplier')

    def test_malformed_support_is_rejected_even_with_valid_fingerprint(self):
        fixed = correct_constant_features(legacy_fixture(), labeled_fixture())
        mutations = [None, {}, dict(fixed['training_feature_support'], rows=True),
                     dict(fixed['training_feature_support'], rows=999),
                     dict(fixed['training_feature_support'], constant_features={}),
                     dict(fixed['training_feature_support'], ranges={})]
        for support in mutations:
            changed = copy.deepcopy(fixed)
            changed['training_feature_support'] = support
            reseal(changed)
            with self.subTest(support=support), self.assertRaises(ValueError):
                validate_artifact(changed, MatchConfig().config_id, 'supplier')
        for values in ({'min': True, 'max': 1.0}, {'min': .9, 'max': .8}, {'min': 0, 'max': float('inf')}):
            changed = copy.deepcopy(fixed)
            changed['training_feature_support']['ranges']['same_country'] = values
            if all(isinstance(v, (int, float)) and v != float('inf') for v in values.values()): reseal(changed)
            with self.assertRaises(ValueError):
                validate_artifact(changed, MatchConfig().config_id, 'supplier')

    def test_legacy_artifacts_remain_loadable_and_tampered_support_is_bound(self):
        validate_artifact(model_fixture(), MatchConfig().config_id, 'supplier')
        fixed = correct_constant_features(legacy_fixture(), labeled_fixture())
        fixed['training_feature_support']['ranges']['address_jaccard']['max'] = .9
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            validate_artifact(fixed, MatchConfig().config_id, 'supplier')

    def test_cli_tool_preserves_inputs_and_writes_aggregate_provenance(self):
        rows = labeled_fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, labels, output, report = [root / n for n in ('old.json', 'labels.jsonl', 'new.json', 'report.json')]
            source.write_text(json.dumps(legacy_fixture(rows)), encoding='utf-8')
            labels.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            originals = [source.read_bytes(), labels.read_bytes()]
            evidence = run_correction(source, labels, output, report)
            self.assertEqual(originals, [source.read_bytes(), labels.read_bytes()])
            self.assertEqual(evidence['rows_checked'], len(rows))
            self.assertTrue(all(v['cutoff_decisions_changed'] == 0 for v in evidence['comparison'].values()))
            self.assertNotIn('record_keys', report.read_text(encoding='utf-8'))
            self.assertEqual(json.loads(output.read_text())['model_id'], evidence['corrected_model_id'])
            with self.assertRaisesRegex(ValueError, 'no overwrites'):
                run_correction(source, labels, output, report)
            with self.assertRaisesRegex(ValueError, 'distinct'):
                run_correction(source, labels, source, root / 'another-report.json')

    def test_released_derivative_preserves_identity_and_stales_old_approval(self):
        root = Path(__file__).resolve().parent.parent
        current = json.loads((root / 'models/contracts-finder-pair-model.json').read_text(encoding='utf-8'))
        original = json.loads((root / 'models/archive/contracts-finder-pair-model-v09.json').read_text(encoding='utf-8'))
        self.assertEqual(current['constant_feature_correction']['source_model_id'], original['model_id'])
        validate_artifact(current, MatchConfig().config_id, 'supplier')
        validate_artifact(original, MatchConfig().config_id, 'supplier')
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / 'engine.sqlite')
            engine = Engine(db, pair_model=original)
            try:
                before = engine.analyze(*demo_csv())
                action = engine.stage_action(before['entities'][0]['entity_id'])
                engine.approve_action(action['action_id'])
            finally:
                engine.close()
            corrected = Engine(db, pair_model=current)
            try:
                after = corrected.analyze(*demo_csv())
                self.assertEqual(before['totals'], after['totals'])
                self.assertEqual(before['entities'], after['entities'])
                self.assertNotEqual(before['dataset']['snapshot'], after['dataset']['snapshot'])
                with self.assertRaises(ValidationError): corrected.execute_action(action['action_id'])
                self.assertFalse(corrected.portal()['suppliers'])
            finally:
                corrected.close()


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_ML') == '1', 'Optional NumPy training environment required')
class ConstantFeatureTrainingTests(unittest.TestCase):
    def test_training_freezes_constant_weights_and_still_learns_variable_evidence(self):
        model = fit_pair_model(labeled_fixture(), MatchConfig().config_id, 'supplier')
        constants = model['training_feature_support']['constant_features']
        self.assertEqual(len(constants), len(FEATURE_NAMES) - 1)
        self.assertTrue(all(model['weights'][FEATURE_NAMES.index(n)] == 0.0 for n in constants))
        self.assertGreater(model['weights'][FEATURE_NAMES.index('address_jaccard')], 0.0)
        self.assertEqual(model['test']['precision'], 1.0)
        self.assertEqual(model['test']['recall'], 1.0)
        test = [r for r in labeled_fixture() if r['split'] == 'test']
        a = test[0]['model_features']
        self.assertEqual(predict(a, model), predict(dict(a, same_country=1.0, name_levenshtein=0.0), model))

    def test_holdout_variation_does_not_unmask_a_constant_training_column(self):
        rows = labeled_fixture()
        for row in rows:
            if row['split'] != 'train': row['model_features']['shared_tax_id'] = float(row['human_label'] == 'Match')
        model = fit_pair_model(rows, MatchConfig().config_id, 'supplier')
        self.assertEqual(model['training_feature_support']['constant_features']['shared_tax_id'], 0.0)
        self.assertEqual(model['weights'][FEATURE_NAMES.index('shared_tax_id')], 0.0)

    def test_variable_training_column_remains_supported_when_holdouts_are_constant(self):
        rows = labeled_fixture()
        for row in rows:
            row['model_features']['same_country'] = float(row['human_label'] == 'Match') if row['split'] == 'train' else 0.0
        model = fit_pair_model(rows, MatchConfig().config_id, 'supplier')
        self.assertNotIn('same_country', model['training_feature_support']['constant_features'])
        self.assertGreater(model['weights'][FEATURE_NAMES.index('same_country')], 0.0)

    def test_all_constant_training_is_intercept_only_without_nan(self):
        rows = labeled_fixture()
        for row in rows: row['model_features'] = dict.fromkeys(FEATURE_NAMES, .5)
        model = fit_pair_model(rows, MatchConfig().config_id, 'supplier')
        self.assertEqual(model['weights'], [0.0] * len(FEATURE_NAMES))
        self.assertEqual(predict(dict.fromkeys(FEATURE_NAMES, 0.0), model), .5)
        self.assertEqual(predict(dict.fromkeys(FEATURE_NAMES, 1.0), model), .5)

    def test_calibration_preserves_training_support_and_zero_coefficients(self):
        rows = labeled_fixture()
        for row in copy.deepcopy([r for r in rows if r['split'] == 'test']):
            row['split'] = 'calibration'
            row['evaluation_id'] = row['evaluation_id'].replace('test', 'calibration')
            row['record_keys'] = [k.replace('test', 'calibration') for k in row['record_keys']]
            row['entity_group_ids'] = list(row['record_keys'])
            row['pair_key'] = fingerprint(sorted(row['record_keys']))
            rows.append(row)
        model = fit_pair_model(rows, MatchConfig().config_id, 'supplier', calibrate=True)
        self.assertEqual(model['probability_status'], 'calibrated_pair_estimate')
        self.assertEqual(model['training_feature_support']['rows'], 20)
        self.assertTrue(all(model['weights'][FEATURE_NAMES.index(n)] == 0 for n in model['training_feature_support']['constant_features']))
        validate_artifact(model, MatchConfig().config_id, 'supplier')


if __name__ == '__main__': unittest.main()
