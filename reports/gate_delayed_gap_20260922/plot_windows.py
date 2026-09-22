"""Show actual replay windows; grey spans are missing poses, not filled paths."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parent
old = json.loads((OUT / 'baseline_gap_replay.json').read_text())['results']
new = json.loads((OUT / 'fixed_gap_replay.json').read_text())['results']
fig, axes = plt.subplots(2, 1, figsize=(10, 4.7), sharex=True)
for ax, name, gap in zip(axes, ('2_holes_1s', '2_holes_8s'), (1, 8)):
    for left in (16, 26):
        ax.axvspan(left, left + gap, color='#aeb6be', alpha=.45)
    for y, data, label in ((1, old, 'Before'), (0, new, 'Fixed')):
        row = next(r for r in data if r['case'] == name and r['clock'] == 'media' and r['phase_s'] == 0)
        for clip in row['clips']:
            start, end = clip['start_timestamp_s'], clip['end_timestamp_s']
            ax.broken_barh([(start, end-start)], (y-.19, .38),
                          facecolors='#258b71' if clip['should_save'] else '#d08832')
            ax.text((start+end)/2, y, f'{start:.1f} - {end:.1f}',
                    ha='center', va='center', color='white', fontsize=9)
    ax.plot([8, 38], [1.7, 1.7], lw=4, color='#356ba7')
    ax.text(23, 1.81, 'Simulated continuous turn: 8 - 38 s', ha='center', fontsize=9)
    ax.set_title(f'Two {gap} s pose outages (grey); recording may span unknown poses', fontsize=11)
    ax.set_yticks([0, 1], ['Fixed', 'Before'])
    ax.set_ylim(-.6, 2.2)
    ax.set_xlim(0, 50)
    ax.grid(axis='x', alpha=.25)
axes[-1].set_xlabel('Time (s)')
fig.tight_layout()
fig.savefig(OUT / 'window_comparison.png', dpi=150)
