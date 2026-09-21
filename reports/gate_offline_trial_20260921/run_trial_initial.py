"""Compare experimental event configurations on existing local clips only."""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

from data import load_all, fingerprint, TASKS, OUT, review_sheet
from candidate import Config, detect, config_dict

sys.path.insert(0, str(OUT.parents[1]))
from data_collection_gate import TurnGate, PoseSample


def current_replay(p, duration):
    """Saved-window-only streaming diagnostic, never joins unobserved gaps."""
    gate = TurnGate()
    samples = [PoseSample(*row) for row in p]
    starts, completed = [], []
    for t in np.arange(0., duration + 1e-9, .2):
        cache = [v for v in samples if t - 54. <= v.timestamp_s <= t + 6. + 1e-9]
        if len(cache) < 2:
            continue
        if gate.active_start_timestamp_s is None:
            yes, action = gate.start_collection(cache, float(t))
            if yes: starts.append(action)
        else:
            yes, action = gate.end_collection(cache, float(t))
            if yes:
                c = gate.last_completed_collection
                completed.append({'start_s': c.start_timestamp_s, 'end_s': c.end_timestamp_s,
                                  'should_save': c.should_save, 'end_reason': 'gate'})
    # Explicit external EOF truncation: verify evidence belongs to final window.
    if gate.active_start_timestamp_s is not None:
        start = gate.active_start_timestamp_s
        end = min(duration, start + gate.config.maximum_collection_interval_s)
        confirmed = gate._hard_stop_target_confirmed(samples, start, end)
        completed.append({'start_s': start, 'end_s': end,
                          'should_save': end - start >= 4. and confirmed, 'end_reason': 'source_EOF'})
    return {'starts': starts, 'completed': completed,
            'has_start': bool(starts), 'has_saved_window': any(c['should_save'] for c in completed)}


def main():
    clips, before, issues = load_all()
    configs = {
        'initial_lateral_015': Config(),
        'selected_lateral_020': replace(Config(), detour_lateral_m=.20),
        'sensitivity_angle_10': replace(Config(), detour_lateral_m=.20, turn_angle_deg=10.),
        'sensitivity_angle_12': replace(Config(), detour_lateral_m=.20, turn_angle_deg=12.),
    }
    all_results = {}
    for name, cfg in configs.items():
        started = time.monotonic()
        results = {c['id']: detect(c['poses'], cfg) for c in clips}
        all_results[name] = results
        print(name, {t: dict(Counter(results[c['id']]['decision'] for c in clips if c['task_id'] == t)) for t in TASKS},
              'seconds', round(time.monotonic() - started, 2), flush=True)
    current = {}
    for number, clip in enumerate(clips):
        current[clip['id']] = current_replay(clip['poses'], clip['duration_s'])
        if number % 20 == 0:
            print('current replay', number + 1, '/', len(clips), flush=True)
    after_ok = all(fingerprint(Path(path)) == original for path, original in before.items())
    first_snapshot = json.loads((OUT / 'source_before.json').read_text())
    assert after_ok and all(fingerprint(Path(path)) == original for path, original in first_snapshot.items())
    output = {'scope': 'Experimental offline event extraction, not online deployment or full-stream recall.',
              'selection': '8Up calibration: initial .15m detour admits #16; selected .20m before WIt/GCy/CCx evaluation. Other configs are sensitivity analyses, not alternate selected thresholds.',
              'configs': {name: config_dict(cfg) for name, cfg in configs.items()},
              'clips': [{k: v for k, v in c.items() if k != 'poses'} for c in clips],
              'results': all_results, 'current_saved_window_replay': current, 'source_issues': issues,
              'source_files_unchanged': after_ok, 'source_file_count': len(before),
              'source_fingerprints': before,
              'code_hashes': {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
                              for name in ('data.py', 'candidate.py', 'run_trial.py')}}
    (OUT / 'trial.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    selected = all_results['selected_lateral_020']
    for tid in TASKS[:2]:
        chosen = [c for c in clips if c['task_id'] == tid]
        for offset in range(0, len(chosen), 11):
            review_sheet(chosen[offset:offset + 11], OUT / f"result_{tid[-3:]}_{offset+1}_{min(offset+11,len(chosen))}.png", selected)
    print('source protection passed', len(before), flush=True)


if __name__ == '__main__':
    main()
