import json, sys
from pathlib import Path
from dataclasses import replace
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(OUT.parent/'gate_offline_trial_20260921'))
from data import load_all
from audit_candidate import Config, detect
cfg=replace(Config(),detour_requires_review=True)
clips,_,_=load_all()
results={c['id']:detect(c['poses'],cfg) for c in clips}
key=json.loads((OUT/'audit_key.json').read_text())['entries']
labels=json.loads((OUT/'blind_labels.json').read_text())['labels']
rows=[]
for k,v in key.items():
 row={**v,'anonymous_id':k,'label':labels[k]['label'],'decision':results[v['id']]['decision']}
 row['agrees']=(row['label']=='turn')==(row['decision']=='confirmed')
 rows.append(row)
print(json.dumps(rows,ensure_ascii=False,indent=2))
print('agrees',sum(r['agrees'] for r in rows),'/',len(rows))
(OUT/'audit_candidate_results.json').write_text(json.dumps({'config':cfg.__dict__,'audit':rows,'results':results},indent=2))
assert all(r['agrees'] for r in rows)
