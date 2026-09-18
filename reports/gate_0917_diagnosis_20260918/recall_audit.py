"""Compare old/new gates on known geometric events, without modifying source data."""

import hashlib
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import types

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import data_collection_gate as current
from test_data_collection_gate import _arc, _motion_profile

BASELINE_REV = '048c0f6b6b9123b12ad5ce09e63adc17deea778f'


def load_baseline():
    source = subprocess.check_output(
        ['git', 'show', f'{BASELINE_REV}:data_collection_gate.py'], cwd=REPO, text=True,
    )
    module = types.ModuleType('gate_before_precision_changes')
    sys.modules[module.__name__] = module
    exec(compile(source, f'<git {BASELINE_REV}:data_collection_gate.py>', 'exec'), module.__dict__)
    return module, hashlib.sha256(source.encode()).hexdigest()


def stream(module, samples, decision_offset=0.0):
    gate = module.TurnGate()
    clips = []
    starts = []
    for index in range(int((samples[-1][0] - 6.0 - decision_offset) / 0.2) + 1):
        timestamp = decision_offset + index * 0.2
        cache = [p for p in samples if timestamp - 54 <= p[0] <= timestamp + 6.0 + 1e-8]
        if gate.active_start_timestamp_s is None:
            yes, action = gate.start_collection(cache, timestamp)
            if yes:
                starts.append(action)
        else:
            start = gate.active_start_timestamp_s
            yes, end = gate.end_collection(cache, timestamp)
            if yes:
                completed = getattr(gate, 'last_completed_collection', None)
                # Apply the user-requested 4-second policy to BOTH versions so
                # losses here cannot be attributed just to the new save policy.
                clips.append({'start_s': start, 'end_s': end,
                              'duration_s': end - start, 'at_least_4s': end - start >= 4.0,
                              'should_save': completed.should_save if completed else end - start >= 4.0})
    return {'starts': starts, 'clips': clips, 'active_start_s': gate.active_start_timestamp_s}


def run_case(name, profile, baseline, *, poses=None, decision_offset=0.0):
    if poses is None:
        poses = _motion_profile(profile)
    samples = [(p.timestamp_s, p.x_m, p.y_m, p.yaw_rad) for p in poses]
    results = {}
    for label, module in [('baseline', baseline), ('current', current)]:
        gate = module.TurnGate()
        anchor = 2.0 + decision_offset
        cache = [p for p in samples if p[0] <= anchor + 6.0 + 1e-8]
        evidence = gate.evaluate(cache, anchor)
        result = stream(module, samples, decision_offset)
        result['anchor_evidence'] = {
            'timestamp_s': anchor,
            'is_turn': evidence.is_turn, 'is_curve': evidence.is_curve,
            'is_spin': evidence.is_spin, 'future_path_m': evidence.future_path_m,
            'spin_translation_m': evidence.spin_translation_m,
            'yaw_change_deg': math.degrees(evidence.spin_yaw_change_rad or 0.0),
            'curvature_deg_per_m': (None if evidence.reference_curvature_rad_per_m is None
                                    else math.degrees(evidence.reference_curvature_rad_per_m)),
        }
        results[label] = result
    return {'name': name, 'profile_duration_speed_curvature': profile,
            'decision_offset_s': decision_offset, **results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('recall_evidence.json'))
    parser.add_argument('--require-no-recall-loss', action='store_true')
    args = parser.parse_args()
    baseline, baseline_sha = load_baseline()
    cases = []
    for speed in (0.06, 0.10, 0.14, 0.15, 0.16, 0.20, 0.40):
        cases.append(run_case(f'20s_arc_30deg_per_m_at_{speed}mps', [
            (20.0, speed, math.radians(30.0)), (12.0, 0.0, 0.0),
        ], baseline))
    cases.append(run_case('20s_tight_arc_170deg_per_m_at_0.08mps', [
        (20.0, 0.08, math.radians(170.0)), (12.0, 0.0, 0.0),
    ], baseline))
    cases.append(run_case('stop_go_0.5s_move_1.5s_pause', [
        part for _ in range(10) for part in [(0.5, 0.4, math.radians(30.0)), (1.5, 0.0, 0.0)]
    ] + [(12.0, 0.0, 0.0)], baseline))
    cases.append(run_case('4s_slow_turn_after_3s_stop', [
        (3.0, 0.0, 0.0), (4.0, 0.10, math.radians(30.0)), (12.0, 0.0, 0.0),
    ], baseline))
    for speed in (0.08, 0.10):
        cases.append(run_case(f'short_true_arc_{speed}mps_before_hard_stop', [
            (2.0, speed, 0.6), (3.3, 0.0, 0.0), (4.0, 0.5, 0.6), (12.0, 0.0, 0.0),
        ], baseline))
    sparse = _arc(math.radians(30.0), 20.0, speed_mps=0.2, sample_rate_hz=2.0)
    last = sparse[-1]
    sparse.extend(current.PoseSample(20.0 + index * 0.5, last.x_m, last.y_m, last.yaw_rad)
                  for index in range(1, 25))
    cases.append(run_case('2Hz_0.20mps_arc_with_0.01s_probe_offset', [
        (20.0, 0.2, math.radians(30.0)), (12.0, 0.0, 0.0),
    ], baseline, poses=sparse, decision_offset=0.01))
    for duration, speed in ((2.0, 0.19), (2.0, 0.20), (3.0, 0.115), (3.0, 0.12)):
        finite = _arc(math.radians(30.0), duration, speed_mps=speed, sample_rate_hz=2.0)
        last = finite[-1]
        finite.extend(current.PoseSample(duration + index * 0.5, last.x_m, last.y_m, last.yaw_rad)
                      for index in range(1, 25))
        cases.append(run_case(f'finite_2Hz_{duration}s_{speed}mps_arc', [
            (duration, speed, math.radians(30.0)), (12.0, 0.0, 0.0),
        ], baseline, poses=finite, decision_offset=0.01))
    result = {
        'baseline_git_head': BASELINE_REV,
        'baseline_sha256': baseline_sha,
        'current_sha256': hashlib.sha256((REPO / 'data_collection_gate.py').read_bytes()).hexdigest(),
        'parameters': {'pose_hz': '10, except the explicit 2Hz case',
                       'decision_delay_s': 6, 'cache_s': 60, 'probe_s': 0.2},
        'cases': cases,
        'limits': 'Constructed recall counterexamples, not a representative corpus or production recall estimate.',
    }
    for case in cases:
        start = 0.0
        intervals = []
        for duration, speed, curvature in case['profile_duration_speed_curvature']:
            if speed > 0.0 and curvature != 0.0:
                intervals.append((start, start + duration))
            start += duration
        for label in ('baseline', 'current'):
            # Streaming windows cannot overlap; measure true turning time only,
            # rather than counting saved clips or stationary context as recall.
            case[label]['covered_turn_s'] = sum(
                max(0.0, min(clip['end_s'], end) - max(clip['start_s'], start))
                for clip in case[label]['clips'] if clip['should_save']
                for start, end in intervals
            )
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for case in cases:
        print(json.dumps({
            'case': case['name'],
            'baseline_clips': case['baseline']['clips'],
            'current_clips': case['current']['clips'],
            'current_starts': case['current']['starts'],
            'current_anchor_evidence': case['current']['anchor_evidence'],
        }))
    if args.require_no_recall_loss:
        for case in cases:
            assert case['current']['covered_turn_s'] + 1e-6 >= case['baseline']['covered_turn_s'], case['name']
        print(f'PASS: no loss of saved true-turn coverage in all {len(cases)} comparisons')
