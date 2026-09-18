"""Read-only audit of a frozen simulation index; never renders or modifies source data."""
from __future__ import annotations

import collections
import concurrent.futures
import csv
import json
from pathlib import Path
import zipfile

import numpy as np

OUT = Path(__file__).resolve().parent
ROOT = Path(json.loads((OUT / 'snapshot.json').read_text())['source']).parent
VERSIONS = ['legacy_v1', 'v2_unique_obstacles']
CAMERAS = ['front', 'rear_left', 'rear_right']


def audit(row):
    result = dict(row)
    issues = []
    folder = ROOT / row['sample_id']
    try:
        for name in ['rollout.npz', 'visual.npz', 'map.png', 'map_original.png', 'publication.json']:
            if not (folder / name).is_file() or (folder / name).stat().st_size == 0:
                issues.append('missing_or_empty:' + name)
        result['npz_bytes'] = sum((folder / n).stat().st_size for n in ['rollout.npz', 'visual.npz'])
        with np.load(folder / 'rollout.npz', allow_pickle=False) as z:
            meta = json.loads(str(z['metadata_json']))
            ids, kinds = z['obstacle_ids'].tolist(), z['obstacle_kinds'].tolist()
            times, poses = z['timestamps_s'], z['poses_xyyaw']
            route = z['route_control_points']
            result.update(
                unique_obstacle_ids=len(set(ids)), unique_obstacle_assets=len(set(kinds)),
                obstacle_ids=ids, obstacle_assets=kinds,
                repeated_id_instances=len(ids)-len(set(ids)),
                repeated_asset_instances=len(kinds)-len(set(kinds)),
                door_count=len(meta.get('doors', [])),
                materials=meta.get('materials', {}),
                recorded_success=meta.get('success'), stop_reason=meta.get('stop_reason'),
                final_goal_distance_m=meta.get('final_goal_distance_m'),
                final_speed_mps=meta.get('final_speed_mps'),
                checkpoint_sha256=meta.get('checkpoint_sha256'),
                source_scene_id=meta.get('source_scene_id'),
                time_scale=meta.get('time_scale'), max_linear_mps=meta.get('max_linear_mps'),
                actual_path_length_m=float(np.linalg.norm(np.diff(poses[:, :2], axis=0), axis=1).sum()),
                heading_change_deg=float(np.rad2deg(np.abs(np.diff(np.unwrap(poses[:, 2]))).sum())),
            )
            if len(ids) != row['obstacle_count'] or len(kinds) != len(ids):
                issues.append('obstacle_count_mismatch')
            if meta.get('generation_settings_version', 'legacy_v1') != row['generation_settings_version']:
                issues.append('rollout_version_mismatch')
            if row['generation_settings_version'] == VERSIONS[1] and (len(set(ids)) != len(ids) or len(set(kinds)) != len(kinds)):
                issues.append('v2_duplicate_obstacles')
            if str(z['sample_id']) != row['sample_id'] or meta.get('sample_id') != row['sample_id']:
                issues.append('rollout_sample_id_mismatch')
            if poses.shape != (row['num_frames'], 3) or times.shape != (row['num_frames'],):
                issues.append('rollout_frame_shape_mismatch')
            if not np.isfinite(poses).all() or not np.allclose(times, np.arange(len(times)) * .5, rtol=0, atol=1e-7):
                issues.append('pose_or_2hz_timestamps_invalid')
            if not np.isclose(float(meta['duration_s']), row['duration_s'], atol=1e-6):
                issues.append('duration_mismatch')
            if not np.isclose(np.linalg.norm(np.diff(route, axis=0), axis=1).sum(), row['route_length_m'], atol=1e-5):
                issues.append('route_length_mismatch')
            if meta.get('success') is not True:
                issues.append('recorded_success_not_true')
        with np.load(folder / 'visual.npz', allow_pickle=False) as z:
            cameras = z['camera_names'].tolist()
            result['camera_names'] = cameras
            if cameras != CAMERAS:
                issues.append('camera_order_mismatch')
            if not np.array_equal(z['timestamps_s'], times) or not np.array_equal(z['poses_xyyaw'], poses):
                issues.append('visual_rollout_alignment_mismatch')
            vm = json.loads(str(z['metadata_json']))
            if vm.get('generation_settings_version', 'legacy_v1') != row['generation_settings_version']:
                issues.append('visual_version_mismatch')
            if vm.get('sample_id') != row['sample_id']:
                issues.append('visual_sample_id_mismatch')
        # Read the RGB NPY header only, avoiding full-image decompression of the dataset.
        with zipfile.ZipFile(folder / 'visual.npz') as archive:
            info = archive.getinfo('rgb.npy')
            with archive.open(info) as stream:
                version = np.lib.format.read_magic(stream)
                reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                shape, fortran, dtype = reader(stream)
                header_bytes = stream.tell()
            result['rgb_shape'] = list(shape)
            if shape != (row['num_frames'], 3, 512, 640, 3) or dtype != np.dtype('uint8') or fortran:
                issues.append('rgb_header_mismatch')
            if info.file_size != header_bytes + int(np.prod(shape)) * dtype.itemsize:
                issues.append('rgb_member_size_mismatch')
    except Exception as exc:
        issues.append(type(exc).__name__ + ':' + str(exc))
    result['issues'] = issues
    return result


def distribution(values):
    a = np.asarray(values, dtype=float)
    return dict(zip(['min', 'p25', 'median', 'p75', 'p95', 'max'], np.percentile(a, [0,25,50,75,95,100]).tolist()), mean=float(a.mean()), total=float(a.sum()))


def summarize(rows):
    checked = [r for r in rows if 'obstacle_assets' in r]
    out = {
        'count': len(rows), 'frames': sum(r['num_frames'] for r in rows),
        'camera_images': sum(r['num_frames'] for r in rows) * 3,
        'duration_hours': sum(r['duration_s'] for r in rows)/3600,
        'route_km': sum(r['route_length_m'] for r in rows)/1000,
        'obstacle_instances': sum(r['obstacle_count'] for r in rows),
        'mode_counts': dict(collections.Counter(r['mode'] for r in rows)),
        'npz_gib': sum(r.get('npz_bytes', 0) for r in rows)/1024**3,
        'issues_count': sum(bool(r['issues']) for r in rows),
        'metadata_checked_count': len(checked),
        'repeated_asset_cases': sum(r['repeated_asset_instances'] > 0 for r in checked),
        'repeated_id_cases': sum(r['repeated_id_instances'] > 0 for r in checked),
        'repeated_asset_instances': sum(r['repeated_asset_instances'] for r in checked),
        'distinct_assets': len({k for r in checked for k in r['obstacle_assets']}),
        'unique_assets_per_case': distribution([r['unique_obstacle_assets'] for r in checked]),
        'asset_counts': dict(collections.Counter(k for r in checked for k in r['obstacle_assets']).most_common()),
        'recorded_success_counts': dict(collections.Counter(str(r.get('recorded_success')) for r in rows)),
        'created_at_min': min(r['created_at'] for r in rows), 'created_at_max': max(r['created_at'] for r in rows),
        'outdoor_side_bias_count': sum(r['mode']=='outdoor' and r['side_bias'] for r in rows),
        'materials': {},
    }
    for key in ['num_frames', 'duration_s', 'route_length_m', 'obstacle_count', 'generation_attempts']:
        out[key] = distribution([r[key] for r in rows])
    out['obstacles_per_10m'] = 10*out['obstacle_instances']/(out['route_km']*1000)
    for category in sorted({k for r in checked for k in r['materials']}):
        out['materials'][category] = dict(collections.Counter(r['materials'][category]['name'] for r in checked if category in r['materials']))
    return out


def main():
    rows = [json.loads(line) for line in (OUT/'index.snapshot.jsonl').read_text().splitlines() if line.strip()]
    duplicates = {k:v for k,v in collections.Counter(r['sample_id'] for r in rows).items() if v > 1}
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for i, row in enumerate(pool.map(audit, rows), 1):
            results.append(row)
            if i % 500 == 0:
                print(f'Audited {i}/{len(rows)}', flush=True)
    (OUT/'audit_records.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in results))
    summary = {'all':summarize(results), 'duplicate_sample_ids':duplicates}
    for v in VERSIONS:
        subset = [r for r in results if r['generation_settings_version'] == v]
        summary[v] = summarize(subset)
        for mode in ['indoor','outdoor']:
            summary[v+'/'+mode] = summarize([r for r in subset if r['mode'] == mode])
    summary['issue_details'] = [{ 'sample_id':r['sample_id'], 'issues':r['issues']} for r in results if r['issues']]
    for key in ['seed','source_scene_id']:
        counts = collections.Counter(r.get(key) for r in results)
        summary['duplicate_'+key] = {str(k):v for k,v in counts.items() if k is not None and v>1}
    (OUT/'statistics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    columns = ['sample_id','generation_settings_version','mode','num_frames','duration_s','route_length_m','obstacle_count','unique_obstacle_assets','repeated_asset_instances','door_count','scale','side_bias','generation_attempts','created_at','npz_bytes']
    with (OUT/'samples.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, columns, extrasaction='ignore', lineterminator='\n'); writer.writeheader(); writer.writerows(results)
    for key, value in summary.items():
        if isinstance(value, dict) and 'count' in value:
            print(key, {k:value[k] for k in ['count','frames','duration_hours','route_km','obstacle_instances','repeated_asset_cases','distinct_assets','issues_count']})
    print('ISSUES', summary['issue_details'][:20], flush=True)


if __name__ == '__main__':
    main()
