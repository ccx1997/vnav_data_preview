"""Read-only local collection access and independent trajectory review sheets."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path('/mnt/chengchangxu/data/visual_nav_mv')
OUT = Path(__file__).resolve().parent
TASKS = ['202609201240128Up', '20260920122356WIt', '20260918200506GCy', '20260917174539CCx']


def fingerprint(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]


def load_all():
    clips, fingerprints, issues = [], {}, []
    for task_id in TASKS:
        directories = sorted((ROOT / task_id / 'meta/unpacked').glob('meta_*'), key=lambda p: int(p.name.rsplit('_', 1)[1]))
        for directory in directories:
            try:
                paths = [directory / n for n in ('frames.jsonl', 'export_meta.json', 'task.json')]
                paths.append(ROOT / task_id / 'videos' / f'videos_{directory.name[5:]}' / 'manifest.json')
                for path in paths:
                    fingerprints[str(path)] = fingerprint(path)
                rows = [json.loads(line) for line in paths[0].read_text().splitlines() if line.strip()]
                export, task, video = [json.loads(p.read_text()) for p in paths[1:]]
                window = export['sub_task']
                keep = export['keep_windows']
                if len(keep) != 1 or keep != video['keep_windows']:
                    raise ValueError('non-single or inconsistent keep window; do not bridge cuts')
                origin = keep[0]['from']
                duration = keep[0]['to'] - origin
                poses = []
                invalid_count = 0
                for row in rows:
                    p = row.get('pose') or {}
                    if not p.get('valid'):
                        invalid_count += 1
                        continue
                    poses.append([row['ts'] - origin, p['x'], p['y'], np.deg2rad(p['yaw'])])
                array = np.asarray(poses, dtype=float)
                if len(array) < 3 or not np.isfinite(array).all() or np.any(np.diff(array[:, 0]) <= 0):
                    raise ValueError('insufficient, nonfinite or nonmonotonic valid poses')
                if array[0, 0] < -1e-6 or array[-1, 0] > duration + 1e-6:
                    raise ValueError('poses outside current keep window')
                if invalid_count:
                    raise ValueError(f'{invalid_count} invalid pose rows; leave source unchanged')
                original = next((w for w in export.get('collect_windows', [])
                                 if w['from'] <= origin + 1e-6 and w['to'] >= keep[0]['to'] - 1e-6), {})
                clips.append({'task_id': task_id, 'index': window['index'], 'title': task['title'],
                              'id': window['sub_task_id'], 'duration_s': duration, 'origin': origin,
                              'owner': original.get('owner', 'unknown'), 'poses': array,
                              'video_holes': {r['camera']: r['holes'] for r in video['results'] if r.get('holes')},
                              'source_dir': str(directory)})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append({'directory': str(directory), 'error': str(exc)})
    return clips, fingerprints, issues


def review_sheet(clips, destination, results=None):
    fig, axes = plt.subplots(len(clips), 2, figsize=(11, 2.0 * len(clips)), squeeze=False)
    for (xy_ax, yaw_ax), clip in zip(axes, clips):
        p = clip['poses']; xy = p[:, 1:3] - p[0, 1:3]
        # World XY with equal scale: does not amplify tiny sideways motion.
        xy_ax.plot(xy[:, 0], xy[:, 1], '-o', markersize=2, linewidth=1)
        xy_ax.scatter(*xy[0], color='green', s=18)
        xy_ax.set_aspect('equal', adjustable='datalim')
        xy_ax.grid(alpha=.25); xy_ax.set_xlabel('X (m)'); xy_ax.set_ylabel('Y (m)')
        yaw = np.rad2deg(np.unwrap(p[:, 3])); yaw -= yaw[0]
        yaw_ax.plot(p[:, 0], yaw, '-o', markersize=2, linewidth=1)
        yaw_ax.grid(alpha=.25); yaw_ax.set_xlabel('Time in source clip (s)'); yaw_ax.set_ylabel('Yaw change (deg)')
        label = f"{clip['task_id'][-3:]} #{clip['index']} / {clip['duration_s']:.2f}s"
        if results:
            result = results[clip['id']]
            label += f" / {result['decision']}"
            for event in result['events']:
                yaw_ax.axvspan(event['capture_start_s'], event['capture_end_s'], color='#66bb6a', alpha=.13)
                for core in event['cores']:
                    yaw_ax.axvspan(core['start_s'], core['end_s'], color='#ff9800', alpha=.28)
        xy_ax.set_title(label, fontsize=10)
    fig.tight_layout()
    fig.savefig(destination, dpi=110)
    plt.close(fig)


if __name__ == '__main__':
    clips, fingerprints, issues = load_all()
    for offset in range(0, 33, 11):
        selected = [c for c in clips if c['task_id'] == TASKS[0]][offset:offset + 11]
        review_sheet(selected, OUT / f'review_8Up_{offset + 1}_{offset + len(selected)}.png')
    (OUT / 'source_before.json').write_text(json.dumps(fingerprints, indent=2) + '\n')
    (OUT / 'source_issues.json').write_text(json.dumps(issues, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'counts': {t: sum(c['task_id'] == t for c in clips) for t in TASKS}, 'issues': issues,
                      'source_files': len(fingerprints)}, ensure_ascii=False, indent=2))
