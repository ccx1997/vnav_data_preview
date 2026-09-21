"""Frozen conservative ablation and explicitly scoped geometry-review metrics."""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from candidate import Config, detect, config_dict
from data import load_all, fingerprint, OUT, TASKS, review_sheet


def main():
    clips, before, issues = load_all()
    cfg = replace(Config(), detour_lateral_m=.20, detour_requires_review=True)
    results = {c['id']: detect(c['poses'], cfg) for c in clips}
    labels = json.loads((OUT / 'review_labels.json').read_text())
    metrics = {}
    for tid, groups in labels['tasks'].items():
        all_indices = groups['positive'] + groups['negative'] + groups['ambiguous']
        assert len(all_indices) == len(set(all_indices)) == sum(c['task_id'] == tid for c in clips)
        metrics[tid] = {'role': groups['role']}
        for group in ('positive', 'negative', 'ambiguous'):
            metrics[tid][group] = {decision: [i for i in groups[group] if results[f'{tid}_{i}']['decision'] == decision]
                                    for decision in ('confirmed', 'uncertain', 'straight')}
    events = [e for r in results.values() for e in r['events'] if e['should_save']]
    counts = {tid: dict(Counter(results[c['id']]['decision'] for c in clips if c['task_id'] == tid)) for tid in TASKS}
    context = {'saved_windows': len(events), 'both_4s_available': sum(not e['left_truncated'] and not e['right_truncated'] for e in events),
               'left_truncated': sum(e['left_truncated'] for e in events), 'right_truncated': sum(e['right_truncated'] for e in events),
               'limits': 'Only existing per-clip poses are available. Missing pre/post data are not downloaded, invented, or bridged.'}
    first_snapshot = json.loads((OUT / 'source_before.json').read_text())
    unchanged = all(fingerprint(Path(p)) == f for p, f in before.items()) and all(fingerprint(Path(p)) == f for p, f in first_snapshot.items())
    assert unchanged
    output = {'scope': 'Offline retrospective prototype; per-clip geometry reference, not production precision/recall.',
              'config': config_dict(cfg), 'results': results, 'counts': counts, 'reference_metrics': metrics,
              'context': context, 'source_issues': issues, 'source_files_unchanged': unchanged,
              'source_file_count': len(before),
              'code_hashes': {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() for name in ('candidate.py', 'evaluate_conservative.py', 'review_labels.json')}}
    (OUT / 'conservative_trial.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    for tid in TASKS[:3]:
        selected = [c for c in clips if c['task_id'] == tid]
        for offset in range(0, len(selected), 11):
            review_sheet(selected[offset:offset + 11], OUT / f"conservative_{tid[-3:]}_{offset+1}_{min(offset+11,len(selected))}.png", results)
    print(json.dumps({'counts': counts, 'reference_metrics': metrics, 'context': context,
                      'source_files_unchanged': unchanged}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
