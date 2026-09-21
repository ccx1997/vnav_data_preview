"""Summarize fixed audit and read-only streaming results; verify source hashes."""
import json, hashlib, sys
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(OUT.parent/'gate_offline_trial_20260921'))
from data import load_all
clips, fingerprints, issues=load_all()
before=json.loads((OUT/'source_before.json').read_text())
assert before == fingerprints, 'SOURCE MUTATION: never refresh or overwrite source data'
(OUT/'source_after.json').write_text(json.dumps(fingerprints,indent=2))
current=json.loads((OUT/'current_replay.json').read_text())['results']
baseline=json.loads((OUT/'baseline_replay.json').read_text())['results']
key=json.loads((OUT/'audit_key.json').read_text())
labels=json.loads((OUT/'blind_labels.json').read_text())['labels']
previous=json.loads((OUT.parent/'gate_offline_trial_20260921/review_labels.json').read_text())['tasks']
metrics={}
for label in ('positive','negative','ambiguous'):
 ids=[k for k,v in current.items() if v['index'] in previous.get(v['task_id'],{}).get(label,[])]
 metrics[label]={'count':len(ids),'baseline_started':sum(bool(baseline[k]['starts']) for k in ids),'baseline_saved':sum(baseline[k]['saved'] for k in ids),'current_started':sum(bool(current[k]['starts']) for k in ids),'current_saved':sum(current[k]['saved'] for k in ids)}
audit=[]
for name,v in key['entries'].items():
 item={**v,'anonymous_id':name,**labels[name],'current_saved':current[v['id']]['saved']}
 item['agrees']=(item['label']=='turn')==item['current_saved'];audit.append(item)
assert all(x['agrees'] for x in audit)
assert metrics['positive']['current_saved']==32 and metrics['negative']['current_saved']==0
regression=json.loads((OUT/'streaming_regression.json').read_text());assert regression['passed']==regression['total']
saved=[c for v in current.values() for c in v['clips'] if c['should_save']]
summary={'sources':len(clips),'source_files_unchanged':len(fingerprints),'source_issues':issues,'audit':audit,'reference_metrics':metrics,
         'source_clips_with_saved_windows_before':sum(v['saved'] for v in baseline.values()),'source_clips_with_saved_windows_after':sum(v['saved'] for v in current.values()),
         'saved_window_count':len(saved),'saved_duration_median_s':float(np.median([c['end_timestamp_s']-c['start_timestamp_s'] for c in saved])),
         'streaming_regression_passed':regression['passed'],'streaming_regression_total':regression['total'],
         'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (OUT.parents[1]/'data_collection_gate.py',OUT.parents[1]/'test_data_collection_gate.py')}}
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
selected=[]
for pair in [('202609201240128Up',19),('202609201240128Up',20),('20260918200506GCy',42),('20260917174539CCx',29)]:
 selected.append(next(c for c in clips if (c['task_id'],c['index'])==pair))
fig,axes=plt.subplots(4,2,figsize=(11,10))
for c,(ax,yax) in zip(selected,axes):
 p=c['poses'];xy=p[:,1:3]-p[0,1:3];yaw=np.rad2deg(np.unwrap(p[:,3])-p[0,3]);r=current[c['id']]
 ax.plot(xy[:,0],xy[:,1],'-o',ms=2,color='#386fa4');ax.scatter(*xy[0],c='green',s=20);ax.set_aspect('equal',adjustable='datalim');ax.set_xlabel('X (m)');ax.set_ylabel('Y (m)');ax.grid(alpha=.25)
 status='Retained turn' if r['saved'] else 'Rejected as turn'
 ax.set_title(f"{c['task_id'][-3:]} #{c['index']} | {status}")
 yax.plot(p[:,0],yaw,'-o',ms=2,color='#386fa4');yax.set_xlabel('Source time (s)');yax.set_ylabel('Yaw change (degrees)');yax.grid(alpha=.25)
 for window in r['clips']:
  if window['should_save']:yax.axvspan(window['start_timestamp_s'],window['end_timestamp_s'],alpha=.15,color='green')
fig.tight_layout();fig.savefig(OUT/'examples.png',dpi=140);plt.close(fig)
print(json.dumps({k:v for k,v in summary.items() if k not in ('audit','source_issues')},indent=2))
