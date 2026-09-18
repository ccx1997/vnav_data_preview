"""Read existing clips and reproduce gate mechanisms without changing source data."""

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from recall_audit import load_baseline  # noqa: E402
from test_data_collection_gate import _motion_profile  # noqa: E402

# Historical failure reproduction must keep using the pre-fix gate.
TurnGate = load_baseline()[0].TurnGate

ROOT = Path('/mnt/chengchangxu/data/visual_nav_mv/20260917174539CCx')
READ_FILES = {}


def fingerprint(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]


def read_json(path):
    READ_FILES[str(path)] = fingerprint(path)
    return json.loads(path.read_text())


def inspect_clip(index):
    clip_id = f'20260917174539CCx_{index}'
    meta = ROOT / 'meta/unpacked' / f'meta_{clip_id}'
    export = read_json(meta / 'export_meta.json')
    task = read_json(meta / 'task.json')
    frames = meta / 'frames.jsonl'
    READ_FILES[str(frames)] = fingerprint(frames)
    rows = [json.loads(line) for line in frames.read_text().splitlines() if line.strip()]
    assert all(row['pose']['valid'] for row in rows)
    assert all(a['ts'] < b['ts'] for a, b in zip(rows, rows[1:]))
    poses = [row['pose'] for row in rows]
    yaw = [poses[0]['yaw']]
    for pose in poses[1:]:
        yaw.append(yaw[-1] + (pose['yaw'] - yaw[-1] + 180) % 360 - 180)
    window = export['sub_task']
    collection = export['collect_windows'][index - 1]
    assert window['from'] == collection['from'] and window['to'] == collection['to']
    video_dir = ROOT / 'videos' / f'videos_{clip_id}'
    manifest = read_json(video_dir / 'manifest.json')
    videos = []
    for result in manifest['results']:
        video = video_dir / f"{result['camera']}_continuous.mp4"
        READ_FILES[str(video)] = fingerprint(video)
        probe = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'json', str(video),
        ], text=True))
        videos.append({
            'camera': result['camera'], 'duration_s': float(probe['format']['duration']),
            'holes': result['holes'], 'missing_segments': result['missing_segments'],
        })
    return {
        'clip': index, 'title': task['title'], 'owner': collection['owner'],
        'start_s': window['from'], 'end_s': window['to'],
        'duration_s': window['to'] - window['from'], 'row_count': len(rows),
        'row_span_s': rows[-1]['ts'] - rows[0]['ts'],
        'max_translation_from_first_m': max(math.hypot(
            pose['x'] - poses[0]['x'], pose['y'] - poses[0]['y'],
        ) for pose in poses),
        'yaw_range_deg': max(yaw) - min(yaw),
        'max_pose_gap_s': max(b['ts'] - a['ts'] for a, b in zip(rows, rows[1:])),
        'videos': videos,
    }


def reproduce(name, segments, start, expected_end):
    samples = _motion_profile(segments)
    gate = TurnGate()
    # The documented rolling cache: 60 seconds total with a 6 second decision delay.
    def cache_at(timestamp):
        return [p for p in samples if timestamp - 54 <= p.timestamp_s <= timestamp + 6 + 1e-8]
    evidence = asdict(gate.evaluate(cache_at(start), start))
    assert gate.start_collection(cache_at(start), start) == (True, start)
    end = None
    for index in range(1, 31):
        timestamp = start + index * 0.2
        should_end, action = gate.end_collection(cache_at(timestamp), timestamp)
        if should_end:
            end = action
            break
    assert end is not None and math.isclose(end, expected_end, abs_tol=1e-8)
    return {'name': name, 'motion_segments_duration_speed_curvature': segments,
            'start_s': start, 'end_s': end, 'duration_s': end - start,
            'start_evidence': evidence}


if __name__ == '__main__':
    clips = [inspect_clip(index) for index in [43, 53, 20, 14]]
    reproductions = [
        reproduce('stationary_until_6s_then_turn', [(6, 0, 0), (6, .5, .6)], 2.0, 5.0),
        reproduce('straight_until_4.5s_then_turn', [(4.5, .2, 0), (8, .5, .6)], 3.0, 4.2),
    ]
    unchanged = all(fingerprint(Path(path)) == before for path, before in READ_FILES.items())
    assert unchanged
    result = {
        'clips': clips, 'synthetic_reproductions': reproductions,
        'source_files_unchanged': unchanged, 'source_fingerprints': READ_FILES,
        'gate_sha256': hashlib.sha256((REPO / 'data_collection_gate.py').read_bytes()).hexdigest(),
        'limits': 'Saved poses are approximately 5 Hz and exclude unsaved intervals; online gate logs/config/version are unavailable. Synthetic mechanisms are not exact replays of the four real triggers.',
    }
    output = Path(__file__).with_name('evidence.json')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'clips': clips, 'synthetic_reproductions': reproductions,
                      'source_files_unchanged': unchanged, 'source_file_count': len(READ_FILES)},
                     ensure_ascii=False, indent=2))
