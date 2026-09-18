"""Verify saved clips contain true turns in the original false-positive scenarios."""

import json
import math
from pathlib import Path

import recall_audit as audit


def check(name, profile, baseline, *, noise_before_s=0.0):
    poses = audit._motion_profile(profile)
    samples = []
    for p in poses:
        noisy = p.timestamp_s < noise_before_s
        samples.append((
            p.timestamp_s,
            p.x_m + (0.005 * math.sin(p.timestamp_s * 7.0) if noisy else 0.0),
            p.y_m + (0.003 * math.sin(p.timestamp_s * 4.0) if noisy else 0.0),
            p.yaw_rad + (math.radians(0.1) * math.sin(p.timestamp_s * 3.0) if noisy else 0.0),
        ))
    timestamp = 0.0
    intervals = []
    for duration, speed, curvature in profile:
        if speed > 0.0 and curvature != 0.0:
            intervals.append((timestamp, timestamp + duration))
        timestamp += duration
    result = {'name': name, 'true_turn_intervals': intervals}
    for label, module in [('baseline', baseline), ('current', audit.current)]:
        output = audit.stream(module, samples)
        # The original shipped gate had no minimum-save policy; current callers
        # must use should_save. Recall comparisons separately apply 4s to BOTH.
        kept = [clip for clip in output['clips'] if label == 'baseline' or clip['should_save']]
        false_clips = [clip for clip in kept if not any(
            min(clip['end_s'], end) > max(clip['start_s'], start) for start, end in intervals
        )]
        result[label] = {'kept': kept, 'false_clips': false_clips}
        if label == 'current':
            assert not false_clips, (name, false_clips)
            assert all(clip['duration_s'] >= 4.0 for clip in kept)
            for start, end in intervals:
                coverage = sum(max(0.0, min(clip['end_s'], end) - max(clip['start_s'], start)) for clip in kept)
                assert coverage >= end - start - 1e-6, (name, coverage, start, end)
    return result


if __name__ == '__main__':
    baseline, _sha = audit.load_baseline()
    cases = [
        check('parked_then_turn', [(6, 0, 0), (6, .5, .6), (10, .5, 0)], baseline),
        check('millimetre_drift_then_turn', [(6, 0, 0), (6, .5, .6), (10, .5, 0)], baseline, noise_before_s=6.0),
        check('straight_then_turn', [(4.5, .2, 0), (6, .5, .6), (10, .5, 0)], baseline),
        check('creep_then_stop_before_future_turn', [(2, .04, 0), (3.3, 0, 0), (4, .5, .6), (12, 0, 0)], baseline),
        check('stationary_drift_only', [(20, 0, 0)], baseline, noise_before_s=20.0),
        check('straight_only', [(20, .5, 0)], baseline),
    ]
    result = {
        'cases': cases,
        'baseline_false_saved_clips': sum(len(case['baseline']['false_clips']) for case in cases),
        'current_false_saved_clips': sum(len(case['current']['false_clips']) for case in cases),
        'limits': 'Targeted synthetic scenarios, not a production-wide precision estimate.',
    }
    Path(__file__).with_name('precision_repair_evidence.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
