"""Compare the same 159 local sources with Sep21; never rewrite source data."""
import hashlib
import json
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[1]
OLD = OUT.parent / 'gate_method_revision_20260921'
sys.path.insert(0, str(OLD))
from replay import load_all, module, replay


def main():
    clips, before, issues = load_all()
    historical_hashes = json.loads((OLD / 'source_after.json').read_text())
    assert before == historical_hashes, 'Sources changed since previous audit; investigate before comparing.'
    gate = module(REPO / 'data_collection_gate.py', 'fixed_source_replay')
    results = {}
    baseline = json.loads((OLD / 'current_replay.json').read_text())['results']
    changes = []
    for i, clip in enumerate(clips):
        result = replay(gate, clip['poses'])
        results[clip['id']] = result
        assert all(s['start_s'] <= s['decision_s'] for s in result['starts'])
        # Ignore CPU timings; compare actions and final windows exactly.
        if any(result[key] != baseline[clip['id']][key] for key in ('saved', 'starts', 'clips')):
            changes.append(clip['id'])
        if (i + 1) % 30 == 0:
            print(f'{i+1}/{len(clips)} sources', flush=True)
    _, after, after_issues = load_all()
    assert before == after and issues == after_issues
    prior = json.loads((OLD / 'summary.json').read_text())
    audit_agrees = sum((row['label'] == 'turn') == results[row['id']]['saved'] for row in prior['audit'])
    previous = json.loads((OUT.parent / 'gate_offline_trial_20260921/review_labels.json').read_text())['tasks']
    metrics = {}
    for label in ('positive', 'negative', 'ambiguous'):
        ids = [c['id'] for c in clips if c['index'] in previous.get(c['task_id'], {}).get(label, [])]
        metrics[label] = {'count': len(ids), 'started': sum(bool(results[k]['starts']) for k in ids),
                          'saved': sum(results[k]['saved'] for k in ids)}
    summary = {'sources': len(clips), 'source_files_unchanged': len(after), 'source_issues': issues,
               'changed_actions_or_windows': changes, 'reference_metrics': metrics,
               'audit_agrees': audit_agrees, 'audit_total': len(prior['audit']),
               'saved_sources': sum(r['saved'] for r in results.values()),
               'saved_windows': sum(c['should_save'] for r in results.values() for c in r['clips']),
               'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (REPO / 'data_collection_gate.py', REPO / 'test_data_collection_gate.py')}}
    (OUT / 'source_replay.json').write_text(json.dumps({'summary': summary, 'results': results}, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert metrics['positive']['saved'] == 32 and metrics['negative']['started'] == 0
    assert audit_agrees == len(prior['audit'])


if __name__ == '__main__':
    main()
