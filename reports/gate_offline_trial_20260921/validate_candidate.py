"""Known-geometry regression for the conservative offline prototype."""
from dataclasses import replace
import json
import math
from pathlib import Path
import sys

import numpy as np

from candidate import Config, detect

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[1]
sys.path.insert(0, str(REPO))
from test_data_collection_gate import _arc, _motion_profile
from data_collection_gate import PoseSample

CFG = replace(Config(), detour_lateral_m=.20, detour_requires_review=True)


def array(poses):
    return np.array([[p.timestamp_s, p.x_m, p.y_m, p.yaw_rad] for p in poses])


def union(windows):
    merged = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return merged


def check(name, poses, truth):
    result = detect(array(poses), CFG)
    saved = [event for event in result['events'] if event['should_save']]
    windows = union([(e['capture_start_s'], e['capture_end_s']) for e in saved])
    covered = sum(max(0., min(b, y) - max(a, x)) for a, b in windows for x, y in truth)
    expected = sum(y - x for x, y in truth)
    false = [e for e in saved if not any(min(e['capture_end_s'], b) > max(e['capture_start_s'], a) for a, b in truth)]
    passed = covered >= expected - 1e-6 and not false and all(e['duration_s'] <= 45. + 1e-8 for e in saved)
    return {'name': name, 'passed': passed, 'truth': truth, 'covered_true_s': covered,
            'expected_true_s': expected, 'saved_windows': windows, 'false_saved_count': len(false), 'result': result}


def main():
    historical = json.loads((REPO / 'reports/gate_0917_diagnosis_20260918/recall_repair_evidence.json').read_text())
    output = []
    for case in historical['cases']:
        profile = case['profile_duration_speed_curvature']
        if '2Hz' in case['name']:
            duration, speed, curvature = profile[0]
            poses = _arc(curvature, duration, speed_mps=speed, sample_rate_hz=2.)
            last = poses[-1]
            poses.extend(PoseSample(duration + i * .5, last.x_m, last.y_m, last.yaw_rad) for i in range(1, 25))
        else:
            poses = _motion_profile(profile)
        truth, t = [], 0.
        for duration, speed, curvature in profile:
            if speed > 0 and curvature:
                truth.append((t, t + duration))
            t += duration
        output.append(check(case['name'], poses, truth))
    output.append(check('straight_only', _motion_profile([(20, .5, 0)]), []))
    stationary = [PoseSample(i * .1, .005 * math.sin(i * .7), .003 * math.cos(i * .4), math.radians(.1) * math.sin(i * .3)) for i in range(201)]
    output.append(check('stationary_millimetre_drift', stationary, []))
    moving = [PoseSample(i * .1, i * .05, .01 * math.sin(i * .1 * 2 * math.pi / 3), math.radians(1) * math.sin(i * .1 * 2 * math.pi / 3)) for i in range(201)]
    output.append(check('moving_centimetre_wobble', moving, []))
    output.append(check('S_turn_zero_net_yaw', _motion_profile([(5, .5, 0), (3, .5, .3), (3, .5, -.3), (8, .5, 0)]), [(5, 11)]))
    output.append(check('parked_context_before_turn', _motion_profile([(10, 0, 0), (3, .4, .6), (8, .5, 0)]), [(10, 13)]))
    output.append(check('long_turn_split_at_45s', _motion_profile([(5, .5, 0), (110, .2, .3), (8, .5, 0)]), [(5, 115)]))
    for duration in (2., 10.):
        poses = [PoseSample(i * .1, 0., 0., math.radians(20) * min(1., max(0., (i * .1 - 5) / duration))) for i in range(int((duration + 12) * 10) + 1)]
        output.append(check(f'20deg_spin_over_{duration}s', poses, [(5., 5. + duration)]))
    reverse = [PoseSample(i * .1, .5 * (i * .1 if i < 100 else 20 - i * .1), 0., 0.) for i in range(201)]
    output.append(check('straight_forward_then_reverse', reverse, []))
    jumps = [PoseSample(i * .1, i * .05 + (10 if i >= 100 else 0), 0., math.radians(60) if i >= 100 else 0.) for i in range(201)]
    output.append(check('pose_jump_not_turn', jumps, []))
    result = {'scope': 'Offline event/save-window regression. Not the original streaming API tests or production recall.',
              'historical_count': len(historical['cases']), 'cases': output,
              'passed': sum(c['passed'] for c in output), 'total': len(output)}
    (OUT / 'regression.json').write_text(json.dumps(result, indent=2) + '\n')
    for case in output:
        print('PASS' if case['passed'] else 'FAIL', case['name'],
              f"{case['covered_true_s']:.3f}/{case['expected_true_s']:.3f}s", case['saved_windows'])
    print(f"{result['passed']}/{result['total']} passed")
    assert result['passed'] == result['total']


if __name__ == '__main__':
    main()
