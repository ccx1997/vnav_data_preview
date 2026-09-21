"""Experimental batch event detector, NOT the deployed streaming Gate.

Uses observed XY/yaw only, no teacher output or source owner. Angular accumulation
is bounded to 15 s; lobe geometry/boundaries are estimated retrospectively from
complete saved windows. Confirmation latency/live recorder equivalence is untested.
"""
from dataclasses import dataclass, asdict
import math
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from data_collection_gate import TurnGate, PoseSample


@dataclass(frozen=True)
class Config:
    yaw_deadband_deg: float = 2.0
    turn_angle_deg: float = 8.0
    weak_angle_deg: float = 4.0
    detour_angle_deg: float = 5.0
    detour_lateral_m: float = .15
    weak_maximum_path_m: float = .8
    evidence_window_s: float = 15.0
    pre_context_s: float = 4.0
    post_context_s: float = 4.0
    merge_gap_s: float = 3.0
    max_clip_s: float = 45.0
    chunk_overlap_s: float = 2.0
    minimum_saved_s: float = 4.0
    detour_requires_review: bool = False


def lobes(yaw, deadband):
    """Directional-change hysteresis; small reversals do not add turn angle."""
    if len(yaw) < 2:
        return []
    lo = hi = 0
    direction = 0
    start = extreme = 0
    output = []
    for i in range(1, len(yaw)):
        if direction == 0:
            if yaw[i] <= yaw[lo]: lo = i
            if yaw[i] >= yaw[hi]: hi = i
            if yaw[i] - yaw[lo] >= deadband:
                start, extreme, direction = lo, i, 1
            elif yaw[hi] - yaw[i] >= deadband:
                start, extreme, direction = hi, i, -1
        elif direction * (yaw[i] - yaw[extreme]) > 1e-10:
            extreme = i
        elif direction * (yaw[extreme] - yaw[i]) >= deadband:
            output.append((start, extreme))
            start, extreme, direction = extreme, i, -direction
    if direction and extreme > start:
        output.append((start, extreme))
    return output


def fit_direction(xy, s):
    """Distance-weighted robust linear fit to actual vertices, not derivatives."""
    if len(xy) < 2 or s[-1] - s[0] < .01:
        return None
    weights = np.r_[np.diff(s)[0], np.diff(s)]
    weights = np.maximum(weights, 1e-6)
    design = np.column_stack([s - s[0], np.ones(len(s))])
    robust = np.ones(len(s))
    for _ in range(3):
        w = np.sqrt(weights * robust)
        coefficients = np.linalg.lstsq(design * w[:, None], xy * w[:, None], rcond=None)[0]
        error = np.linalg.norm(xy - design @ coefficients, axis=1)
        scale = max(.001, float(np.median(error)) * 1.4826)
        robust = np.minimum(1., 1.5 * scale / np.maximum(error, 1e-12))
    slope = coefficients[0]
    if np.linalg.norm(slope) < .1:
        return None
    direction = slope / np.linalg.norm(slope)
    residual = np.abs(np.cross(direction, xy - xy[0]))
    return direction, float(np.quantile(residual, .8))


def geometry(p):
    xy = p[:, 1:3]
    distances = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    length = float(distances[-1])
    translation = float(np.max(np.linalg.norm(xy - xy[0], axis=1)))
    result = {'path_m': length, 'max_translation_m': translation, 'heading_change_deg': None,
              'lateral_from_entry_m': None, 'fit_error_m': None}
    if length < .06:
        return result
    baseline = min(.5, length * .35)
    first_end = min(len(p), max(3, int(np.searchsorted(distances, baseline)) + 1))
    last_start = max(0, min(len(p) - 3, int(np.searchsorted(distances, length - baseline))))
    a = fit_direction(xy[:first_end], distances[:first_end])
    b = fit_direction(xy[last_start:], distances[last_start:])
    if a is None or b is None:
        return result
    direction_a, error_a = a
    direction_b, error_b = b
    signed_rad = math.atan2(float(np.cross(direction_a, direction_b)), float(direction_a @ direction_b))
    # Retain whole turns: endpoint atan2 alone folds a >180-degree route into
    # the opposite direction. Actual spaced vertices supply the winding count.
    route = [xy[0]]
    for point in xy[1:]:
        if np.linalg.norm(point - route[-1]) >= .03:
            route.append(point)
    if len(route) >= 3:
        steps = np.diff(np.asarray(route), axis=0)
        headings = np.unwrap(np.arctan2(steps[:, 1], steps[:, 0]))
        winding = round(float((headings[-1] - headings[0] - signed_rad) / (2 * math.pi)))
        signed_rad += winding * 2 * math.pi
    signed = math.degrees(signed_rad)
    result.update(heading_change_deg=signed,
                  lateral_from_entry_m=float(np.max(np.abs(np.cross(direction_a, xy - xy[0])))),
                  fit_error_m=max(error_a, error_b))
    return result


def stable_blocks(p):
    """Keep the existing 3 s / 3 cm / 3 deg stationary hard-stop boundary."""
    start = 0
    output = []
    for i in range(1, len(p)):
        if p[i, 0] - p[start, 0] < 3.0 - 1e-9:
            continue
        boundary = p[i, 0] - 3.0
        left = max(start, int(np.searchsorted(p[:, 0], boundary)))
        anchor = np.array([np.interp(boundary, p[:, 0], p[:, col]) for col in range(1, 4)])
        window = p[left:i + 1, 1:]
        if (np.max(np.linalg.norm(window[:, :2] - anchor[:2], axis=1)) <= .03 + 1e-9
                and np.max(np.abs(window[:, 2] - anchor[2])) <= math.radians(3) + 1e-9):
            output.append((p[start:i + 1], 'stable_stop'))
            start = i
    if start < len(p) - 1:
        output.append((p[start:], 'source_or_pose_boundary'))
    return output


def angular_support_s(p):
    delta = p[-1, 3] - p[0, 3]
    if abs(delta) < 1e-9:
        return 0.
    progress = (p[:, 3] - p[0, 3]) / delta
    a = int(np.argmax(progress >= .05))
    b = int(np.argmax(progress >= .95))
    return float(p[b, 0] - p[a, 0])


def classify_lobe(p, cfg):
    angle = math.degrees(p[-1, 3] - p[0, 3])
    total_angle = abs(angle)
    # Confirm from at most 15 seconds; a long weak drift cannot use its entire
    # unbounded duration to exceed an angle threshold.
    bounded_angle = 0.0
    for i in range(len(p)):
        j = max(0, int(np.searchsorted(p[:, 0], p[i, 0] - cfg.evidence_window_s)))
        bounded_angle = max(bounded_angle, abs(math.degrees(p[i, 3] - p[j, 3])))
    geom = geometry(p)
    delta = np.diff(p[:, 3])
    consistency = abs(float(delta.sum())) / max(float(np.abs(delta).sum()), 1e-12)
    heading = geom['heading_change_deg']
    aligned = heading is not None and heading * angle > 0
    branch = None
    if bounded_angle >= cfg.turn_angle_deg and geom['max_translation_m'] <= .10:
        branch = 'spin'
    elif bounded_angle >= cfg.turn_angle_deg and geom['path_m'] >= .15 and aligned and abs(heading) >= 3.:
        branch = 'turn'
    elif (not cfg.detour_requires_review and bounded_angle >= cfg.detour_angle_deg and geom['path_m'] >= .3 and aligned
          and abs(heading) >= 2. and geom['lateral_from_entry_m'] >= cfg.detour_lateral_m):
        branch = 'detour'
    elif (bounded_angle >= cfg.weak_angle_deg and .08 <= geom['path_m'] <= cfg.weak_maximum_path_m
          and angular_support_s(p) >= 1.5 and consistency >= .85 and aligned and abs(heading) >= max(2., total_angle * .4)
          and abs(heading) <= total_angle * 2 + 3
          and geom['fit_error_m'] <= max(.003, geom['path_m'] * .01)):
        branch = 'short_slow_turn'
    return {'start_s': float(p[0, 0]), 'end_s': float(p[-1, 0]), 'angle_deg': angle,
            'bounded_angle_deg': bounded_angle, 'consistency': consistency,
            'branch': branch, 'geometry': geom,
            'status': 'confirmed' if branch else 'uncertain' if bounded_angle >= cfg.weak_angle_deg else 'small_wobble'}


def detect(poses, cfg=Config()):
    gate = TurnGate()
    samples = [PoseSample(*row) for row in poses]
    prepared = gate._prepare_cache(samples)
    all_lobes, events = [], []
    core_id = 0
    for segment_id, segment in enumerate(prepared.segments):
        if len(segment) < 3:
            continue
        p = np.array([[s.timestamp_s, s.x_m, s.y_m, s.yaw_rad] for s in segment])
        for block_id, (block, boundary_reason) in enumerate(stable_blocks(p)):
            found = []
            for a, b in lobes(block[:, 3], math.radians(cfg.yaw_deadband_deg)):
                if b - a < 2:
                    continue
                result = classify_lobe(block[a:b + 1], cfg)
                result.update(segment_id=segment_id, block_id=block_id)
                all_lobes.append(result)
                if result['status'] == 'confirmed':
                    result['core_id'] = core_id
                    core_id += 1
                    found.append(result)
            groups = []
            for core in found:
                if groups and core['start_s'] - groups[-1][-1]['end_s'] <= cfg.merge_gap_s:
                    groups[-1].append(core)
                else:
                    groups.append([core])
            for cores in groups:
                core_start, core_end = cores[0]['start_s'], cores[-1]['end_s']
                capture_start = max(float(p[0, 0]), core_start - cfg.pre_context_s)
                capture_end = min(float(block[-1, 0]), core_end + cfg.post_context_s)
                event_id = f'{segment_id}:{block_id}:{cores[0]["core_id"]}'
                chunk_start = capture_start
                while chunk_start < capture_end - 1e-9:
                    chunk_end = min(capture_end, chunk_start + cfg.max_clip_s)
                    chunk_cores = [c for c in cores if min(c['end_s'], chunk_end) > max(c['start_s'], chunk_start)]
                    if chunk_cores:
                        events.append({'event_id': event_id, 'capture_start_s': chunk_start,
                                       'capture_end_s': chunk_end, 'cores': chunk_cores,
                                       'duration_s': chunk_end - chunk_start,
                                       'should_save': chunk_end - chunk_start >= cfg.minimum_saved_s,
                                       'pre_context_available_s': max(0., min(c['start_s'] for c in chunk_cores) - chunk_start),
                                       'post_context_available_s': max(0., chunk_end - max(c['end_s'] for c in chunk_cores)),
                                       'left_truncated': capture_start > core_start - cfg.pre_context_s + 1e-6,
                                       'right_truncated': capture_end < core_end + cfg.post_context_s - 1e-6,
                                       'right_boundary_reason': boundary_reason if capture_end == block[-1, 0] else 'context_complete'})
                    if chunk_end >= capture_end - 1e-9:
                        break
                    chunk_start = chunk_end - cfg.chunk_overlap_s
    accepted = any(event['should_save'] for event in events)
    decision = 'confirmed' if accepted else 'uncertain' if any(l['status'] != 'small_wobble' for l in all_lobes) else 'straight'
    return {'decision': decision, 'events': events, 'lobes': all_lobes,
            'valid_segments': len(prepared.segments), 'pose_breaks': [list(b) for b in prepared.breaks]}


def config_dict(cfg=Config()):
    return asdict(cfg)
