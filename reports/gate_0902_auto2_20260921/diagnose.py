"""Read saved collection windows and reproduce near-straight turn starts.

Writes only evidence.json alongside this script. Never changes source data.
"""

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import types

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import data_collection_gate as gate_module

TASK_ID = "202609201240128Up"
BASELINE_REV = "048c0f6b6b9123b12ad5ce09e63adc17deea778f"
ROOT = Path("/mnt/chengchangxu/data/visual_nav_mv") / TASK_ID
SOURCES = {}


def fingerprint(path):
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def read_text(path):
    SOURCES[str(path)] = fingerprint(path)
    return path.read_text()


def read_json(path):
    return json.loads(read_text(path))


def inspect(directory, baseline):
    export = read_json(directory / "export_meta.json")
    task = read_json(directory / "task.json")
    rows = [json.loads(line) for line in read_text(directory / "frames.jsonl").splitlines() if line.strip()]
    window = export["sub_task"]
    collection = export["collect_windows"][window["index"] - 1]
    assert collection["from"] == window["from"] and collection["to"] == window["to"]
    assert export["keep_windows"] == [{"from": window["from"], "to": window["to"]}]
    assert rows and all(row["pose"]["valid"] for row in rows)
    assert all(a["ts"] < b["ts"] for a, b in zip(rows, rows[1:]))
    assert all(window["from"] <= row["ts"] <= window["to"] for row in rows)
    start = window["from"]
    poses = [gate_module.PoseSample(
        row["ts"] - start, row["pose"]["x"], row["pose"]["y"], math.radians(row["pose"]["yaw"]),
    ) for row in rows]
    yaw = [poses[0].yaw_rad]
    for a, b in zip(poses, poses[1:]):
        yaw.append(yaw[-1] + math.atan2(math.sin(b.yaw_rad - a.yaw_rad), math.cos(b.yaw_rad - a.yaw_rad)))
    first, last = poses[0], poses[-1]
    dx, dy = last.x_m - first.x_m, last.y_m - first.y_m
    chord = math.hypot(dx, dy)
    path = sum(math.hypot(b.x_m - a.x_m, b.y_m - a.y_m) for a, b in zip(poses, poses[1:]))
    lateral = max(abs(dx * (p.y_m - first.y_m) - dy * (p.x_m - first.x_m)) / chord for p in poses) if chord else None
    manifest = read_json(ROOT / "videos" / f"videos_{window['sub_task_id']}" / "manifest.json")
    assert manifest["keep_windows"] == export["keep_windows"]
    result = {
        "clip": window["index"], "title": task["title"], "owner": collection["owner"],
        "start_s": start, "end_s": window["to"], "duration_s": window["to"] - start,
        "row_count": len(rows), "row_span_s": last.timestamp_s - first.timestamp_s,
        "path_m": path, "chord_m": chord, "path_excess_fraction": path / chord - 1 if chord else None,
        "max_lateral_to_chord_m": lateral, "yaw_range_deg": math.degrees(max(yaw) - min(yaw)),
        "yaw_net_change_deg": math.degrees(yaw[-1] - yaw[0]),
        "max_pose_gap_s": max(b.timestamp_s - a.timestamp_s for a, b in zip(poses, poses[1:])),
        "source_window_unchanged_by_export": True,
    }
    if window["index"] not in (19, 20):
        return result

    gate = gate_module.TurnGate()
    evidence = gate.evaluate(poses, 1.0)
    started = gate.start_collection(poses, 0.0)
    assert started == (True, 0.0) and evidence.is_curve and not evidence.is_spin
    # The positive probe's smoothing and 1 m route fit inside saved data.
    radius = gate.config.pose_smoothing_window_s / 2
    assert first.timestamp_s < 1.0 - radius
    assert last.timestamp_s > evidence.observation_end_timestamp_s + radius
    absolute = [gate_module.PoseSample(p.timestamp_s + start, p.x_m, p.y_m, p.yaw_rad) for p in poses]
    absolute_gate = gate_module.TurnGate()
    assert absolute_gate.start_collection(absolute, start)[0]
    absolute_evidence = absolute_gate.evaluate(absolute, start + 1.0)
    assert math.isclose(evidence.reference_curvature_rad_per_m, absolute_evidence.reference_curvature_rad_per_m, abs_tol=1e-10)
    replay = {
        "scope": "Current default gate; saved ~5 Hz poses only; start and positive evidence reproduction, not exact online end replay.",
        "start_result_relative_s": started,
        "positive_probe": asdict(evidence),
        "curvature_deg_per_m": math.degrees(evidence.reference_curvature_rad_per_m),
        "spin_yaw_change_deg": math.degrees(evidence.spin_yaw_change_rad),
        "positive_probe_has_complete_smoothing_and_route_context": True,
        "absolute_timestamp_reproduction_passed": True,
        "probes_0_to_1_s": [asdict(gate.evaluate(poses, j * .2)) for j in range(6)],
    }
    # Explain the estimator using its actual selected vertices at the positive probe.
    original = gate_module._route_curvatures
    captured = []

    def capture(points):
        values = original(points)
        captured.append({"points_xy_m": points, "local_curvatures_deg_per_m": [math.degrees(v) for v in values]})
        return values

    gate_module._route_curvatures = capture
    try:
        gate.evaluate(poses, 1.0)
    finally:
        gate_module._route_curvatures = original
    replay["curvature_inputs"] = captured
    old_poses = [baseline.PoseSample(p.timestamp_s, p.x_m, p.y_m, p.yaw_rad) for p in poses]
    old_gate = baseline.TurnGate()
    old_evidence = old_gate.evaluate(old_poses, 1.0)
    replay["before_september_18_fix"] = {
        "revision": BASELINE_REV,
        "start_result_relative_s": old_gate.start_collection(old_poses, 0.0),
        "curvature_at_1_s_deg_per_m": math.degrees(old_evidence.reference_curvature_rad_per_m),
        "is_turn_at_1_s": old_evidence.is_turn,
    }
    result["current_gate_reproduction"] = replay
    videos = []
    for camera in manifest["results"]:
        video = ROOT / "videos" / f"videos_{window['sub_task_id']}" / f"{camera['camera']}_continuous.mp4"
        SOURCES[str(video)] = fingerprint(video)
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video),
        ], text=True))
        videos.append({"camera": camera["camera"], "duration_s": float(probe["format"]["duration"]),
                       "holes": camera["holes"], "missing_segments": camera["missing_segments"]})
    result["videos"] = videos
    return result


def main():
    old_source = subprocess.check_output(
        ["git", "show", f"{BASELINE_REV}:data_collection_gate.py"], cwd=REPO, text=True,
    )
    baseline = types.ModuleType("gate_before_september_18")
    sys.modules[baseline.__name__] = baseline
    exec(compile(old_source, f"<git {BASELINE_REV}:data_collection_gate.py>", "exec"), baseline.__dict__)
    directories = sorted((ROOT / "meta/unpacked").glob("meta_*"), key=lambda p: int(p.name.rsplit("_", 1)[1]))
    clips = [inspect(directory, baseline) for directory in directories]
    short = [clip for clip in clips if 4 <= clip["duration_s"] < 5]
    # A descriptive screen for this audit, not a production filter or ground truth.
    near_straight = [clip for clip in clips if clip["yaw_range_deg"] < 8 and clip["path_excess_fraction"] < .002]
    unchanged = all(fingerprint(Path(path)) == before for path, before in SOURCES.items())
    assert unchanged
    output = {
        "task_id": TASK_ID, "config": asdict(gate_module.TurnGateConfig()),
        "gate_sha256": hashlib.sha256((REPO / "data_collection_gate.py").read_bytes()).hexdigest(),
        "baseline_gate_sha256": hashlib.sha256(old_source.encode()).hexdigest(),
        "clips": clips,
        "summary": {
            "local_clip_count": len(clips),
            "turn_count": sum(c["owner"] == "turn" for c in clips),
            "straight_count": sum(c["owner"] == "straight" for c in clips),
            "duration_4_to_5_s_clips": [c["clip"] for c in short],
            "near_straight_screen": "yaw range < 8 deg AND path/chord - 1 < 0.002; descriptive only",
            "near_straight_screen_clips": [c["clip"] for c in near_straight],
            "short_near_straight_clips": [c["clip"] for c in short if c in near_straight],
        },
        "source_files_unchanged": unchanged, "source_file_count": len(SOURCES), "source_fingerprints": SOURCES,
        "limits": "No deployed gate version/config, full high-rate pose cache, start/end reason logs or discarded windows. Exact online end causes and population false-positive rate remain unverified.",
    }
    Path(__file__).with_name("evidence.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": output["summary"], "examples": [c for c in clips if c["clip"] in (19, 20)],
                      "source_files_unchanged": unchanged, "source_file_count": len(SOURCES)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
