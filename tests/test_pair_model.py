"""Classifier policy, reproducibility and leakage tests; no downloaded data."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from enterprise_ai.benchmarks import record
from enterprise_ai.engine import Engine, ValidationError
from enterprise_ai.matching import MatchConfig, score_pair, evaluate_records
from enterprise_ai.model_experiment import partition_cohort
from enterprise_ai.pair_model import FEATURE_NAMES, FEATURE_SCHEMA, VERSION, apply_model, feature_vector, fingerprint, fit_pair_model, partitions_for, predict, select_threshold, validate_artifact
from enterprise_ai.pipeline import run_pipeline
from enterprise_ai.server import demo_csv


def model_fixture(domain="supplier", threshold=.6):
    artifact = {"version": VERSION, "feature_schema": FEATURE_SCHEMA, "feature_names": list(FEATURE_NAMES),
                "config_id": MatchConfig().config_id, "domain": domain, "intercept": -3.0,
                "weights": [6.0 if n == "name_sorted_levenshtein" else 0.0 for n in FEATURE_NAMES], "review_threshold": threshold}
    artifact["model_id"] = fingerprint(artifact)
    return artifact


def labeled_fixture():
    rows = []
    # Same name score in both classes; independent address evidence separates.
    for split in ("train", "validation", "test"):
        for i in range(20):
            label = i % 2
            features = dict.fromkeys(FEATURE_NAMES, 0.0)
            features.update(name_levenshtein=.8, address_jaccard=float(label), address_present=1.0)
            keys = [f"{split}-{i}-left", f"{split}-{i}-right"]
            rows.append({"evaluation_id": f"{split}-{i}", "config_id": MatchConfig().config_id,
                "human_label": "Match" if label else "NonMatch", "split": split, "entity_group_ids": list(keys),
                "record_keys": keys, "pair_key": fingerprint(sorted(keys)), "feature_schema": FEATURE_SCHEMA, "model_features": features})
    return rows


class PairModelTests(unittest.TestCase):
    def test_reordered_names_and_missing_evidence(self):
        result = score_pair(record("1", "Alpha Supply"), record("2", "Supply Alpha"), MatchConfig())
        features = feature_vector(result)
        self.assertEqual(features["name_sorted_levenshtein"], 1)
        self.assertLess(features["name_levenshtein"], 1)
        self.assertEqual(features["address_present"], 0)
        self.assertEqual(features["postcode_present"], 0)
        self.assertEqual(features["name_token_jaccard"], 1)

    def test_artifact_rejects_wrong_domain_configuration_order_and_tampering(self):
        artifact = model_fixture()
        validate_artifact(artifact, MatchConfig().config_id, "supplier")
        for changed in (dict(artifact, domain="person"), dict(artifact, config_id="other"), dict(artifact, intercept=123),
                        dict(artifact, weights=[0]), dict(artifact, feature_names=list(reversed(FEATURE_NAMES))), dict(artifact, review_threshold=float("nan"))):
            with self.assertRaises(ValueError):
                validate_artifact(changed, MatchConfig().config_id, "supplier")
        with self.assertRaisesRegex(ValueError, "domain"):
            Engine(pair_model=model_fixture("person-spider-v2"))
        with self.assertRaisesRegex(ValueError, "Choose"):
            Engine(calibration={}, pair_model=artifact)

    def test_invalid_features_fail_closed(self):
        features = dict.fromkeys(FEATURE_NAMES, 0.5)
        for value in (True, float("nan"), float("inf"), -.1, 1.1, "0.5"):
            with self.assertRaises(ValueError):
                predict(dict(features, name_levenshtein=value), model_fixture())
        with self.assertRaises(ValueError):
            predict({}, model_fixture())

    def test_model_changes_review_only_preserving_conflict_and_sampling(self):
        evaluated, _ = evaluate_records([record("1", "Alpha Supply", country="GB"), record("2", "Supply Alpha", country="GB")], MatchConfig())
        before = evaluated[0]["algorithmic_outcome"]
        apply_model(evaluated, model_fixture(), MatchConfig())
        self.assertEqual(evaluated[0]["algorithmic_outcome"], "Review_Candidate")
        self.assertEqual(evaluated[0]["heuristic_outcome"], before)
        self.assertEqual(evaluated[0]["probability_status"], "model_estimate")
        for outcome in ("Conflict_Review", "Excluded_Sampled", "Authority_Grouped"):
            evaluated[0]["algorithmic_outcome"] = outcome
            apply_model(evaluated, model_fixture(), MatchConfig())
            self.assertEqual(evaluated[0]["algorithmic_outcome"], outcome)

    def test_engine_groups_and_model_bound_approvals_and_label_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"engine.sqlite")
            original = Engine(path)
            initial = original.analyze(*demo_csv())
            action = original.stage_action(initial["entities"][0]["entity_id"])
            original.approve_action(action["action_id"])
            original.close()
            learned = Engine(path, pair_model=model_fixture())
            try:
                state = learned.analyze(*demo_csv())
                self.assertEqual(initial["dataset"]["snapshot_id"], state["dataset"]["snapshot_id"])
                self.assertNotEqual(initial["dataset"]["snapshot"], state["dataset"]["snapshot"])
                self.assertEqual(initial["entities"], state["entities"])
                self.assertEqual(initial["totals"], state["totals"])
                with self.assertRaises(ValidationError):
                    learned.execute_action(action["action_id"])
                self.assertFalse(learned.portal()["suppliers"])
                item = learned.evaluations()["evaluations"][0]
                learned.review(item["evaluation_id"], "Match", "Fixture", "Independent fixture evidence")
                exported = json.loads(learned.export_labels().splitlines()[0])
                self.assertEqual(set(exported["model_features"]), set(FEATURE_NAMES))
                self.assertEqual(exported["pair_key"], fingerprint(sorted(exported["record_keys"])))
                self.assertIsNone(exported["split"])
                learned.analyze(*demo_csv())
                next_item = next(e for e in learned.evaluations()["evaluations"] if (e["left_id"], e["right_id"]) == (item["left_id"], item["right_id"]))
                learned.review(next_item["evaluation_id"], "Match", "Fixture", "Repeated pair")
                labels = [json.loads(line) for line in learned.export_labels().splitlines()]
                self.assertEqual(labels[0]["pair_key"], labels[1]["pair_key"])
            finally:
                learned.close()

    def test_pipeline_exports_model_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for filename, text in zip(("suppliers.csv", "spend.csv"), demo_csv()):
                (base/filename).write_text(text, encoding="utf-8")
            artifact = model_fixture()
            manifest = run_pipeline(base/"suppliers.csv", base/"spend.csv", base/"runs", False, pair_model=artifact)
            self.assertEqual(manifest["pair_model_id"], artifact["model_id"])
            rows = [json.loads(line) for line in (Path(manifest["output_dir"])/"resolution-audit.jsonl").read_text().splitlines()]
            payload = json.loads(next(r for r in rows if r["table"] == "pair_evaluations")["payload_json"])
            self.assertEqual(payload["matching_model_id"], artifact["model_id"])

    def test_group_record_and_repeated_pair_leakage(self):
        rows = labeled_fixture()
        partitions_for(rows, MatchConfig().config_id)
        bad = copy.deepcopy(rows)
        bad[-1]["entity_group_ids"] = bad[0]["entity_group_ids"]
        with self.assertRaisesRegex(ValueError, "group leakage"):
            partitions_for(bad, MatchConfig().config_id)
        bad = copy.deepcopy(rows)
        bad[-1]["record_keys"][0] = bad[0]["record_keys"][0]
        bad[-1]["pair_key"] = fingerprint(sorted(bad[-1]["record_keys"]))
        with self.assertRaisesRegex(ValueError, "record leakage"):
            partitions_for(bad, MatchConfig().config_id)
        bad = copy.deepcopy(rows)
        bad[1].update(record_keys=bad[0]["record_keys"], pair_key=bad[0]["pair_key"])
        with self.assertRaisesRegex(ValueError, "Unique pair_key"):
            partitions_for(bad, MatchConfig().config_id)
        bad = copy.deepcopy(rows)
        bad[-1]["human_label"] = "Below_Threshold"
        with self.assertRaisesRegex(ValueError, "labels"):
            partitions_for(bad, MatchConfig().config_id)

    def test_threshold_ties_and_no_feasible_fallback(self):
        self.assertEqual(select_threshold([1, 0, 1], [.9, .9, .8], .95), 1)
        self.assertEqual(select_threshold([1, 1, 0], [.9, .8, .7], .95), .8)

    def test_exclude_previous_cohort_and_split_entities(self):
        groups = {str(i): [] for i in range(2100)}
        splits, excluded = partition_cohort(groups, max_groups=100)
        self.assertEqual([len(splits[s]) for s in ("train", "validation", "test")], [60, 20, 20])
        self.assertTrue(set(excluded).isdisjoint(set().union(*map(set, splits.values()))))
        self.assertTrue(set(splits["train"]).isdisjoint(splits["test"]))
        self.assertEqual((splits, excluded), partition_cohort(dict(reversed(list(groups.items()))), max_groups=100))


@unittest.skipUnless(os.environ.get("OUTCOME_TEST_ML") == "1", "Optional NumPy training environment required")
class PairTrainingTests(unittest.TestCase):
    def test_multivariate_training_separates_same_name_score(self):
        artifact = fit_pair_model(labeled_fixture(), MatchConfig().config_id, "supplier")
        self.assertEqual(artifact["test"]["recall"], 1)
        self.assertEqual(artifact["test"]["precision"], 1)
        self.assertLess(artifact["test"]["brier_score"], .01)
        validate_artifact(artifact, MatchConfig().config_id, "supplier")
        test = [row for row in labeled_fixture() if row["split"] == "test"]
        selected = [row for row in test if predict(row["model_features"], artifact) >= artifact["review_threshold"]]
        self.assertEqual(len(selected), artifact["test"]["review_pairs"])

    def test_test_labels_do_not_change_selected_weights_or_threshold(self):
        rows = labeled_fixture()
        original = fit_pair_model(rows, MatchConfig().config_id, "supplier")
        changed = copy.deepcopy(rows)
        for row in changed:
            if row["split"] == "test":
                row["human_label"] = "NonMatch" if row["human_label"] == "Match" else "Match"
        perturbed = fit_pair_model(changed, MatchConfig().config_id, "supplier")
        for field in ("weights", "intercept", "regularization", "review_threshold", "validation"):
            self.assertEqual(original[field], perturbed[field])
        self.assertNotEqual(original["test"], perturbed["test"])


if __name__ == "__main__":
    unittest.main()
