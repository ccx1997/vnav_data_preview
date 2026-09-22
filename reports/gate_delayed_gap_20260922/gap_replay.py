"""Replay delayed recorder clocks without filling pose outages or touching sources."""
import argparse
from dataclasses import asdict
import importlib.util
import json
import math
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[1]
sys.path.insert(0, str(REPO))
from test_data_collection_gate import _arc, _motion_profile


def load_gate(path):
    spec = importlib.util.spec_from_file_location('gap_gate', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def replay(mod, samples, clock, phase=0.0):
    """Wall clock advances while pose delivery stops; all poses arrive on time."""
    samples = [mod.PoseSample(p.timestamp_s, p.x_m, p.y_m, p.yaw_rad) for p in samples]
    gate = mod.TurnGate()
    starts, clips, errors = [], [], []
    last = samples[-1].timestamp_s
    latest_decision = -math.inf

    def step(cache, decision):
        nonlocal latest_decision
        latest_decision = max(latest_decision, decision)
        try:
            if gate.active_start_timestamp_s is None:
                act, ts = gate.start_collection(cache, decision)
                if act:
                    starts.append({'decision_s': decision, 'start_s': ts,
                                   'start_after_decision': ts > decision})
            else:
                act, ts = gate.end_collection(cache, decision)
                if act:
                    clip = gate.last_completed_collection
                    clips.append({**asdict(clip), 'should_save': clip.should_save})
        except ValueError as exc:
            # Model the deployed caller's catch-and-skip; keep failures visible.
            errors.append({'decision_s': decision, 'error': str(exc)})

    for i in range(int(last / .2) + 1):
        now = i * .2 + phase
        if now > last + 1e-8:
            break
        cache = [p for p in samples if now - 60 <= p.timestamp_s <= now + 1e-8]
        if len(cache) < 2:
            continue
        decision = now - 6 if clock == 'media' else cache[-1].timestamp_s - 6
        if decision < samples[0].timestamp_s:
            continue
        step(cache, decision)
    # Explicit EOF drain: no invented future pose or stale pose-derived clock.
    decision = max(samples[0].timestamp_s, latest_decision + .2)
    cache = [p for p in samples if p.timestamp_s >= last - 60]
    while decision < last:
        step(cache, decision)
        decision += .2
    step(cache, last)
    if gate.active_start_timestamp_s is not None:
        gate.finish_collection(cache, last)
        clip = gate.last_completed_collection
        clips.append({**asdict(clip), 'should_save': clip.should_save})
    return {'starts': starts, 'clips': clips, 'errors': errors,
            'closed_at_eof': gate.active_start_timestamp_s is None}


def cases():
    samples = _motion_profile([(8, .5, 0), (30, .2, .4), (12, .5, 0)])
    yield 'no_gap', samples
    for gap in range(1, 9):
        for holes in ((16,), (16, 26)):
            yield f'{len(holes)}_holes_{gap}s', [p for p in samples
                if not any(a + 1e-8 < p.timestamp_s < a + gap - 1e-8 for a in holes)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    mod = load_gate(OUT / 'baseline_gate.py' if args.baseline else REPO / 'data_collection_gate.py')
    label = 'baseline' if args.baseline else 'fixed'
    reproductions = []
    samples = _motion_profile([(10, .1, 0), (20, .1, .4)])
    for gap in range(1, 9):
        restart = 10 + gap
        decision = restart - .6
        cache = [mod.PoseSample(p.timestamp_s, p.x_m, p.y_m, p.yaw_rad) for p in samples if (p.timestamp_s <= 10 + 1e-8 or p.timestamp_s >= restart - 1e-8)
                 and p.timestamp_s <= decision + 6 + 1e-8]
        gate = mod.TurnGate()
        start = gate.start_collection(cache, decision)
        try:
            end = gate.end_collection(cache, decision)
        except ValueError as exc:
            end = str(exc)
        reproductions.append({'gap_s': gap, 'decision_s': decision, 'start': start, 'end': end})
    results = []
    for name, samples in cases():
        for clock in ('media', 'latest_pose'):
            for phase in (0., .11):
                result = replay(mod, samples, clock, phase)
                saved = [c for c in result['clips'] if c['should_save']]
                passed = (not result['errors'] and not any(s['start_after_decision'] for s in result['starts'])
                          and result['closed_at_eof'] and len(saved) == 1
                          and saved[0]['start_timestamp_s'] <= 8
                          and saved[0]['end_timestamp_s'] >= 38
                          and saved[0]['end_timestamp_s'] - saved[0]['start_timestamp_s'] <= 45 + 1e-8)
                results.append({'case': name, 'clock': clock, 'phase_s': phase,
                                'passed': passed, 'saved_count': len(saved), **result})
        print(label, name, flush=True)
    summary = {'passed': sum(r['passed'] for r in results), 'total': len(results),
               'errors': sum(len(r['errors']) for r in results),
               'future_starts': sum(s['start_after_decision'] for r in results for s in r['starts'])}
    (OUT / f'{label}_gap_replay.json').write_text(json.dumps({
        'summary': summary, 'reproductions': reproductions, 'results': results}, indent=2))
    print(summary)
    if not args.baseline:
        assert summary['passed'] == summary['total']


if __name__ == '__main__':
    main()
