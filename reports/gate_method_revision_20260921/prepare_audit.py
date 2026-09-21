"""Read-only fixed-seed audit, anonymous inputs separate from algorithm results."""
import json, random, sys
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parent / 'gate_offline_trial_20260921'))
from data import load_all
clips, fingerprints, issues = load_all()
rng = random.Random(2026092103)
selected = rng.sample(clips, 24)
controls = [('202609201240128Up', 19), ('202609201240128Up', 20), ('20260918200506GCy', 42), ('202609201240128Up', 25), ('20260920122356WIt', 14), ('20260918200506GCy', 29)]
extra = [c for pair in controls for c in clips if (c['task_id'], c['index']) == pair and c['id'] not in {s['id'] for s in selected}]
entries = [(c, 'random') for c in selected] + [(c, 'targeted') for c in extra]
rng.shuffle(entries)
private, public = {}, {}
for i, (c, group) in enumerate(entries, 1):
    key = f'A{i:02}'
    private[key] = {'id': c['id'], 'task_id': c['task_id'], 'index': c['index'], 'sampling': group}
    public[key] = {'duration_s': c['duration_s'], 'poses_t_x_y_yaw_rad': c['poses'].tolist()}
for offset in range(0, len(entries), 5):
    page = list(public.items())[offset:offset + 5]
    fig, axes = plt.subplots(len(page), 3, figsize=(15, 2.7 * len(page)), squeeze=False)
    for (key, row), (ax, cross, yawax) in zip(page, axes):
        p = np.asarray(row['poses_t_x_y_yaw_rad']); xy = p[:, 1:3] - p[0, 1:3]
        chord = xy[-1]; norm = np.linalg.norm(chord)
        direction = chord / norm if norm > 1e-6 else np.array([1., 0.])
        along = xy @ direction; lateral = xy @ np.array([-direction[1], direction[0]])
        ax.plot(xy[:, 0], xy[:, 1], '-o', ms=2); ax.scatter(*xy[0], c='green'); ax.scatter(*xy[-1], c='red'); ax.set_aspect('equal', adjustable='datalim')
        ax.set_title(f'{key} | {row["duration_s"]:.2f}s | equal XY scale'); ax.set_xlabel('X (m)'); ax.set_ylabel('Y (m)')
        cross.plot(p[:, 0], lateral * 100, '-o', ms=2); cross.set_title('Cross-track vs start/end chord'); cross.set_xlabel('Time (s)'); cross.set_ylabel('Cross-track (cm), separate scale')
        yaw = np.rad2deg(np.unwrap(p[:, 3]) - p[0, 3]); yawax.plot(p[:, 0], yaw, '-o', ms=2); yawax.set_xlabel('Time (s)'); yawax.set_ylabel('Yaw change (deg)'); yawax.set_title(f'Path {np.linalg.norm(np.diff(xy, axis=0), axis=1).sum():.2f}m')
        for panel in (ax, cross, yawax): panel.grid(alpha=.25)
    fig.tight_layout(); fig.savefig(OUT / 'blind' / f'page_{offset // 5 + 1}.png', dpi=130); plt.close(fig)
(OUT / 'blind' / 'trajectories.json').write_text(json.dumps(public, indent=2))
(OUT / 'audit_key.json').write_text(json.dumps({'seed':2026092103, 'population':len(clips), 'random_count':24, 'entries':private}, indent=2))
(OUT / 'source_before.json').write_text(json.dumps(fingerprints, indent=2))
(OUT / 'source_issues.json').write_text(json.dumps(issues, indent=2))
(OUT / 'ledger.md').write_text('# Gate method revision 2026-09-21\n\nBaseline: `python3 -m unittest -q test_data_collection_gate.py`: 42 tests passed, 1.781 s.\n\nScope: intentional turn semantic change; preserve stdlib, tuple API, source data, discontinuity/hard-stop safeguards. Blinded sample seed 2026092103, random 24/159 plus separately identified controls. No production edit before blind review.\n')
print({'random':24, 'targeted':len(extra), 'source_files':len(fingerprints), 'issues':issues})
