"""Read-only streaming replay. Six-second future availability, 60-second cache.

Each saved source window is replayed independently. EOF is an external truncation,
not an invented pose or missing media. This cannot measure population recall.
"""
import importlib.util, json, sys, time
from pathlib import Path
from dataclasses import asdict
import numpy as np
OUT = Path(__file__).resolve().parent
REPO = OUT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(OUT.parent / 'gate_offline_trial_20260921'))
from data import load_all

def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result

def replay(mod, poses, phase=0., delay=6.):
    samples = [mod.PoseSample(*(float(v) for v in p)) for p in poses]
    gate = mod.TurnGate()
    first, last = samples[0].timestamp_s, samples[-1].timestamp_s
    completed, starts, timings = [], [], []
    for ts in np.r_[np.arange(first + phase, last, .2), last]:
        cache = [p for p in samples if ts - 54. <= p.timestamp_s <= ts + delay + 1e-8]
        if len(cache) < 2: continue
        tick = time.perf_counter()
        if gate.active_start_timestamp_s is None:
            act, action = gate.start_collection(cache, float(ts))
            if act: starts.append({'decision_s':float(ts), 'start_s':action, 'available_until_s':cache[-1].timestamp_s})
        else:
            act, action = gate.end_collection(cache, float(ts))
            if act:
                clip = gate.last_completed_collection
                completed.append({**asdict(clip), 'should_save':clip.should_save, 'ending':'gate'})
        timings.append((time.perf_counter() - tick) * 1000)
    if gate.active_start_timestamp_s is not None:
        if hasattr(gate, 'finish_collection'):
            gate.finish_collection(samples, last)
            clip = gate.last_completed_collection
        else:
            start = gate.active_start_timestamp_s
            clip = mod.CompletedCollection(start, last, gate._hard_stop_target_confirmed(samples, start, last))
            gate.reset()
        completed.append({**asdict(clip), 'should_save':clip.should_save, 'ending':'source_eof'})
    return {'saved':any(c['should_save'] for c in completed), 'starts':starts, 'clips':completed,
            'call_ms_p50':float(np.median(timings)), 'call_ms_p95':float(np.quantile(timings,.95)), 'calls':len(timings)}

def main():
    label = sys.argv[1] if len(sys.argv)>1 else 'current'
    mod = module(OUT / 'baseline_gate.py' if label == 'baseline' else REPO / 'data_collection_gate.py', 'gate_replay_' + label)
    clips, _, issues = load_all()
    results = {}
    for i,c in enumerate(clips):
        results[c['id']] = {'task_id':c['task_id'], 'index':c['index'], **replay(mod,c['poses'])}
        if (i+1)%30==0: print(f'{label}: {i+1}/{len(clips)}', flush=True)
    result = {'version':label, 'future_delay_s':6, 'cache_s':60, 'probe_s':.2, 'results':results, 'issues':issues,
              'saved_sources':sum(r['saved'] for r in results.values()), 'sources':len(results)}
    (OUT / (label + '_replay.json')).write_text(json.dumps(result,indent=2))
    print({k:v for k,v in result.items() if k!='results'})
if __name__=='__main__': main()
