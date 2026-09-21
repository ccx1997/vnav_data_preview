"""Standalone review figures; source files are read only."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from data import load_all, OUT


def main():
    clips, _, _ = load_all()
    clips = {c['id']: c for c in clips}
    results = json.loads((OUT / 'conservative_trial.json').read_text())['results']
    examples = [
        ('202609201240128Up_19', '8Up #19: straight (user negative)'),
        ('202609201240128Up_20', '8Up #20: straight (user negative)'),
        ('20260920122356WIt_30', 'WIt #30: S-shaped maneuver retained'),
        ('20260918200506GCy_42', 'GCy #42: remaining false positive'),
    ]
    fig, axes = plt.subplots(4, 2, figsize=(12, 10))
    for row, (cid, title) in enumerate(examples):
        clip, result = clips[cid], results[cid]
        p = clip['poses']; xy = p[:, 1:3] - p[0, 1:3]
        yaw = np.rad2deg(np.unwrap(p[:, 3]) - p[0, 3])
        axes[row, 0].plot(xy[:, 0], xy[:, 1], color='#3366aa', marker='.', markersize=3)
        axes[row, 0].scatter(*xy[0], color='green', s=25, zorder=5)
        axes[row, 0].set_aspect('equal', adjustable='datalim')
        axes[row, 0].set_xlabel('X from first pose (m)'); axes[row, 0].set_ylabel('Y (m)')
        axes[row, 1].plot(p[:, 0], yaw, color='#3366aa')
        for event in result['events']:
            if not event['should_save']: continue
            axes[row, 1].axvspan(event['capture_start_s'], event['capture_end_s'], color='#66bb6a', alpha=.16)
            for core in event['cores']:
                axes[row, 1].axvspan(core['start_s'], core['end_s'], color='#ff9800', alpha=.30)
        axes[row, 1].set_xlabel('Time in existing clip (s)'); axes[row, 1].set_ylabel('Yaw change (deg)')
        axes[row, 0].set_title(title, fontsize=11)
        for ax in axes[row]: ax.grid(alpha=.25)
    fig.suptitle('Conservative offline trial: green = available capture, orange = detected core\nGeometry review only; XY axes use equal physical scale', fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, .95))
    fig.savefig(OUT / 'examples.png', dpi=140)
    plt.close(fig)

    # Show the desired padding outside existing source windows honestly.
    selected = ['202609201240128Up_14', '20260920122356WIt_11', '20260920122356WIt_30']
    fig, axes = plt.subplots(3, 1, figsize=(12, 6.5))
    for ax, cid in zip(axes, selected):
        clip, result = clips[cid], results[cid]
        ax.broken_barh([(0, clip['duration_s'])], (.1, .2), color='#b0bec5', label='Existing source window')
        for i, event in enumerate(result['events']):
            if not event['should_save']: continue
            a = min(c['start_s'] for c in event['cores'])
            b = max(c['end_s'] for c in event['cores'])
            ax.broken_barh([(a - 4, b - a + 8)], (.5, .2), facecolors='none', edgecolors='#4caf50', linestyles='dashed', label='Requested core +/- 4s' if i == 0 else None)
            ax.broken_barh([(event['capture_start_s'], event['duration_s'])], (.5, .2), color='#a5d6a7', label='Available capture' if i == 0 else None)
            for j, core in enumerate(event['cores']):
                ax.broken_barh([(core['start_s'], core['end_s'] - core['start_s'])], (.5, .2), color='#ffb74d', label='Core' if i == 0 and j == 0 else None)
        ax.set_title(cid); ax.set_yticks([]); ax.grid(axis='x', alpha=.25); ax.set_xlabel('Seconds relative to existing source clip')
        ax.legend(loc='upper right', fontsize=8, ncol=2)
    fig.suptitle('Missing context cannot be recovered from already-cropped clips')
    fig.tight_layout(rect=(0, 0, 1, .96)); fig.savefig(OUT / 'context_limits.png', dpi=140); plt.close(fig)


if __name__ == '__main__':
    main()
