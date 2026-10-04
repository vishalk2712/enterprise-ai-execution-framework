"""Provenance-bound legacy model correction; no fitting or label creation.

Run with python -m enterprise_ai.model_correction --help. Input records and
reference labels stay local; the report contains aggregate diagnostics only.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .pair_model import FEATURE_NAMES, correct_constant_features, diagnostics, predict


def run_correction(model_path, labels_path, output_path, report_path):
    source, labels_file, output, report = map(Path, (model_path, labels_path, output_path, report_path))
    resolved = [path.resolve() for path in (source, labels_file, output, report)]
    if len(set(resolved)) != 4 or output.exists() or report.exists():
        raise ValueError("Use distinct input paths and new model/report output paths; no overwrites")
    source_bytes, labels_bytes = source.read_bytes(), labels_file.read_bytes()
    original = json.loads(source_bytes)
    rows = [json.loads(line) for line in labels_bytes.decode('utf-8-sig').splitlines() if line.strip()]
    corrected = correct_constant_features(original, rows)
    comparison = {}
    label_field = original.get('label_field', 'human_label')
    for split in ('train', 'validation', 'test'):
        subset = [row for row in rows if row['split'] == split]
        before = [predict(row['model_features'], original) for row in subset]
        after = [predict(row['model_features'], corrected) for row in subset]
        changed = sum((a >= original['review_threshold']) != (b >= corrected['review_threshold'])
                      for a, b in zip(before, after))
        delta = max(abs(a - b) for a, b in zip(before, after))
        if changed or delta > 1e-12:
            raise ValueError("Correction failed original-cohort score/cutoff equivalence; no outputs written")
        comparison[split] = {'rows': len(subset), 'max_prediction_delta': delta, 'cutoff_decisions_changed': changed,
                            'original_retrieved_pair_diagnostics': diagnostics(subset, before, original['review_threshold'], label_field),
                            'corrected_retrieved_pair_diagnostics': diagnostics(subset, after, corrected['review_threshold'], label_field)}
    synthetic = dict.fromkeys(FEATURE_NAMES, 0.0)
    synthetic.update(address_jaccard=1.0, address_present=1.0, same_country=1.0)
    cross_border = dict(synthetic, same_country=0.0)
    encoded_model = (json.dumps(corrected, indent=2, allow_nan=False) + '\n').encode('utf-8')
    evidence = {
        'protocol': 'Fold training-only constant contributions into the intercept; zero those coefficients. No refit, calibration, cutoff selection or new labels.',
        'source_model_id': original['model_id'], 'corrected_model_id': corrected['model_id'],
        'source_model_file_sha256': hashlib.sha256(source_bytes).hexdigest(),
        'corrected_model_file_sha256': hashlib.sha256(encoded_model).hexdigest(),
        'source_labels_file_sha256': hashlib.sha256(labels_bytes).hexdigest(),
        'source_labels_sha256': original['labels_sha256'], 'rows_checked': len(rows),
        'training_feature_support': corrected['training_feature_support'],
        'review_threshold': corrected['review_threshold'], 'probability_status': corrected['probability_status'],
        'comparison': comparison,
        'synthetic_numeric_counterfactual': {'description': 'Identical address, no name similarity; vary only same_country. Not observed supplier data or identity ground truth.',
            'same_country': {'original': predict(synthetic, original), 'corrected': predict(synthetic, corrected)},
            'different_country': {'original': predict(cross_border, original), 'corrected': predict(cross_border, corrected)}},
        'limitations': 'Original-cohort equivalence is not off-cohort equivalence or independent validation. Reference labels remain weak. This report measures retrieved pairs, not end-to-end recall; no authority, calibration or activation is added.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects originals even if another process races the
    # initial path checks. The aggregate completion report is written last.
    with output.open('xb') as handle:
        handle.write(encoded_model)
    with report.open('x', encoding='utf-8') as handle:
        json.dump(evidence, handle, indent=2, allow_nan=False)
        handle.write('\n')
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--labels', required=True, help='Exact original private pair JSONL, including explicit splits')
    parser.add_argument('--output', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    try:
        evidence = run_correction(args.model, args.labels, args.output, args.report)
        print(json.dumps({'source_model_id': evidence['source_model_id'], 'corrected_model_id': evidence['corrected_model_id'],
                          'rows_checked': evidence['rows_checked'], 'cutoff_decisions_changed': sum(v['cutoff_decisions_changed'] for v in evidence['comparison'].values())}, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f'Model correction failed: {exc}\n')


if __name__ == '__main__': main()
