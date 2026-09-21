"""Paired CPU call latency, identical cached poses and no recorder I/O."""
import json, math, statistics, time
from pathlib import Path
from replay import module
OUT=Path(__file__).resolve().parent
mods={v:module(OUT/'baseline_gate.py' if v=='baseline' else OUT.parents[1]/'data_collection_gate.py','benchmark_'+v) for v in ('baseline','current')}
results=[]
for seconds in (15,60,120):
 for kind in ('straight','arc'):
  for version,mod in mods.items():
   samples=[]
   curvature=math.radians(12) if kind=='arc' else 0.
   for i in range(seconds*20+1):
    t=i/20.; a=.5*t*curvature
    samples.append(mod.PoseSample(t,math.sin(a)/curvature if curvature else .5*t,(1-math.cos(a))/curvature if curvature else 0.,a))
   timings=[]
   for i in range(23):
    gate=mod.TurnGate()
    tick=time.perf_counter();gate.start_collection(samples,seconds-6.);ms=(time.perf_counter()-tick)*1000
    if i>=3:timings.append(ms)
   results.append({'version':version,'cache_s':seconds,'poses':len(samples),'scenario':kind,'calls':len(timings),'p50_ms':statistics.median(timings),'p95_ms':sorted(timings)[18],'max_ms':max(timings)})
   print(results[-1],flush=True)
(OUT/'benchmark.json').write_text(json.dumps({'method':'20 Hz poses; 3 warmups + 20 fresh-instance start calls; paired inputs; no I/O; shared host, no speedup claim','results':results},indent=2))
