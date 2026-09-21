# Gate method revision 2026-09-21

Baseline: `python3 -m unittest -q test_data_collection_gate.py`: 42 tests passed, 1.781 s.

Scope: intentional turn semantic change; preserve stdlib, tuple API, source data, discontinuity/hard-stop safeguards. Blinded sample seed 2026092103, random 24/159 plus separately identified controls. No production edit before blind review.

Independent audit completed before production edits: 24 fixed-seed random + 5 targeted = 29. New blinded annotator: 13 turn, 16 straight; six shallow boundaries medium confidence. Main agent reviewed shallow-control figures. Previous expert agreed on all shared definite labels; GCy29 previously ambiguous, new annotator straight. Offline weak-branch change (5%-95% angular support >=1.5 s) agrees on 29/29; this audit becomes development evidence, not a held-out test. Production weak confirmation must additionally wait for lobe closure or settled heading; no online prefix-length assumption.

Baseline rolling replay: 141/159 source clips retain a window under the explicit 6 s / 60 s / 0.2 s + source-EOF policy. An initial harness JSON serialization failure from numpy bool was fixed by coercing input poses to Python floats; it did not alter source files.

Final review closed four concrete findings: (1) spin filtering support leaked past EOF, (2) local curvature incorrectly prevented recovery on 2 cm XY wobble, (3) the short recovery window prematurely ended 1 deg/s wide arcs, (4) Tmax continuation crossed a plain pose gap. Added direct regression tests; independent data expert reran the four tests successfully. Final unit suite: 60 passed; streaming known geometry: 54/54; 159-source replay: 84 accepted sources / 87 windows, 32/32 definite positives, 0/69 definite negatives, 29/29 blinded audit agreement. 640 source fingerprints unchanged. CPU start-call overhead increased and is disclosed in report. No remote deployment, source processing or teacher updates.
