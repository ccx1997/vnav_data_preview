"""Known-geometry event coverage using the actual production streaming API."""
import json, math, sys
from pathlib import Path
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(OUT.parents[1]))
from test_data_collection_gate import _arc, _motion_profile
from data_collection_gate import PoseSample
from replay import module, replay

def array(poses): return [(p.timestamp_s,p.x_m,p.y_m,p.yaw_rad) for p in poses]
def merged(windows):
    result=[]
    for a,b in sorted(windows):
        if result and a<=result[-1][1]+1e-8: result[-1][1]=max(b,result[-1][1])
        else: result.append([a,b])
    return result

def main():
    mod=module(OUT.parents[1]/'data_collection_gate.py','streaming_revision_validation')
    historical=json.loads((OUT.parent/'gate_0917_diagnosis_20260918/recall_repair_evidence.json').read_text())
    cases=[]
    for row in historical['cases']:
        profile=row['profile_duration_speed_curvature']
        if '2Hz' in row['name']:
            duration,speed,curvature=profile[0]
            poses=_arc(curvature,duration,speed_mps=speed,sample_rate_hz=2.)
            last=poses[-1]; poses.extend(PoseSample(duration+i*.5,last.x_m,last.y_m,last.yaw_rad) for i in range(1,25))
        else: poses=_motion_profile(profile)
        truth=[]; t=0.
        for duration,speed,curvature in profile:
            if speed>0 and curvature: truth.append((t,t+duration))
            t+=duration
        cases.append((row['name'],poses,truth))
    cases += [
        ('straight_only',_motion_profile([(20,.5,0)]),[]),
        ('S_turn_zero_net_yaw',_motion_profile([(8,.5,0),(3,.5,.3),(3,.5,-.3),(9,.5,0)]),[(8,14)]),
        ('parked_pre_context',_motion_profile([(10,0,0),(3,.4,.6),(8,.5,0)]),[(10,13)]),
        ('long_turn_split',_motion_profile([(8,.5,0),(110,.2,.3),(8,.5,0)]),[(8,118)]),
        ('EOF_new_turn_last_2s',_motion_profile([(8,.5,0),(2,.5,.6)]),[(8,10)]),
        ('wobble', [PoseSample(i*.1,i*.05,.01*math.sin(i*.2),math.radians(1.5)*math.sin(i*.2)) for i in range(201)],[]),
        ('brief_correction_then_stop',_motion_profile([(8,.5,0),(.5,.6,math.radians(6)/.3),(6,0,0)]),[]),
        ('unfinished_weak_arc_keeps_growing',_motion_profile([(15,.15,math.radians(5)/2.25),(5,0,0)]),[]),
        ('reverse_straight', [PoseSample(i*.1,.5*(i*.1 if i<=100 else 20-i*.1),0,0) for i in range(201)],[]),
        ('pose_jump', [PoseSample(i*.1,i*.05+(10 if i>=100 else 0),0,math.radians(60) if i>=100 else 0) for i in range(201)],[]),
    ]
    results=[]
    for name,poses,truth in cases:
        for phase in (0.,.11):
            output=replay(mod,array(poses),phase=phase)
            saved=[c for c in output['clips'] if c['should_save']]
            windows=merged([(c['start_timestamp_s'],c['end_timestamp_s']) for c in saved])
            covered=sum(max(0.,min(b,y)-max(a,x)) for a,b in windows for x,y in truth)
            expected=sum(y-x for x,y in truth)
            false=[c for c in saved if not any(min(c['end_timestamp_s'],b)>max(c['start_timestamp_s'],a) for a,b in truth)]
            ok=covered>=expected-1e-6 and not false and all(c['end_timestamp_s']-c['start_timestamp_s']<=45.+1e-8 for c in saved)
            results.append({'name':name,'phase_s':phase,'passed':ok,'covered_s':covered,'truth_s':expected,'windows':windows,'replay':output})
            print('PASS' if ok else 'FAIL',name,phase,covered,expected,windows,flush=True)
    (OUT/'streaming_regression.json').write_text(json.dumps({'results':results,'passed':sum(r['passed'] for r in results),'total':len(results)},indent=2))
    assert all(r['passed'] for r in results)
if __name__=='__main__':main()
