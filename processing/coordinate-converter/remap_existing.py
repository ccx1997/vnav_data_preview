#!/usr/bin/env python3
"""Export latest successful full teacher runs to a NEW source-map batch.

Reads existing data only. Does not download, repair, rebuild, or overwrite runs.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path

import numpy as np
from PIL import Image

from export_pixels import DEFAULT_MAP_ROOT, ROOT
from vnav_coordinates.export import SOURCE_MAP_VERSION, export_pixels, save_json
from vnav_coordinates.geometry import digest


def read_json(path):
    return json.loads(Path(path).read_text())


def read_rows(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def latest_runs(root):
    selected = []
    for task in sorted(Path(root).iterdir()):
        if not task.is_dir() or task.name.startswith("_"):
            continue
        candidates = []
        for info in sorted(task.glob("vnav_teacher_rule10_history_v2/full/run_*/run.json")):
            data = read_json(info)
            if data.get("status") == "success" and data.get("accepted_count", 0) > 0:
                candidates.append(info.parent.resolve())
        if candidates:
            selected.append((task.name, candidates[-1]))
    return selected


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_export(output, run):
    """Independent raster and source-field validation of the published batch."""
    schema = read_json(output / "coordinate_schema.json")
    audit = read_json(output / "pixel_coordinate_audit.json")
    require(schema["version"] == audit["version"] == SOURCE_MAP_VERSION, "unexpected version")
    require(schema.get("map_scope") == ["P_map"], "source-map export must explicitly select P_map")
    require(audit["source_unchanged"], "source protection missing")
    for name, sha in audit["source_sha256"].items():
        require(digest(name) == sha, f"source changed: {name}")
    for name, sha in audit["output_sha256"].items():
        require(digest(output / name) == sha, f"output checksum mismatch: {name}")
    image = None
    if schema["handdraw_image"]:
        with Image.open(output / schema["handdraw_image"]) as source_image:
            image = np.asarray(source_image.convert("RGB"))

    def on_gray(points):
        xy = np.asarray(points, dtype=float).reshape(-1, 2)
        require(np.isfinite(xy).all(), "nonfinite handdraw point")
        h, w = image.shape[:2]
        ij = np.floor(xy + .5).astype(int)
        require(((ij >= 0) & (ij < [w, h])).all(), "handdraw point out of bounds")
        require((image[h - 1 - ij[:, 1], ij[:, 0]] == 192).all(), "handdraw point is not gray")
        return len(xy)

    accepted = {row["case_id"] for row in read_rows(run / "teacher_labels.jsonl")
                if row.get("status") == "accepted"}
    excluded_rows = list(read_rows(output / "excluded_rows.jsonl"))
    excluded = {row["case_id"] for row in excluded_rows}
    require(len(excluded) == len(excluded_rows) == audit["excluded_rows"], "duplicate exclusions")
    seen, maps, points, segments, max_error = set(), Counter(), 0, 0, 0.
    for row in read_rows(output / "samples.jsonl"):
        require(row["map_name"] == "P_map", "source-map contains an unrelated map")
        case = row["case_id"]
        require(case in accepted and case not in seen, "unexpected or duplicate output case")
        seen.add(case)
        maps[row["map_name"]] += 1
        sample_path = run / "cases" / case / "sample.json"
        npz_path = sample_path.parent / "inputs.npz"
        require(row["source_sample_json"] == str(sample_path) and row["inputs_npz_path"] == str(npz_path),
                "wrong source references")
        sample = read_json(sample_path)
        require(row["map_name"] == sample["map_name"], "map changed")
        require(row["commands"] == sample["teacher"]["commands"]
                and row["initial_state"] == sample["initial_state"]
                and row["source_rgb_history"] == sample.get("rgb_history"), "non-geometry fields changed")
        with np.load(npz_path, allow_pickle=False) as arrays:
            pose = sample["grid_pose"]
            world = dict(reference_pose=np.array([pose["x"], pose["y"], pose["yaw_rad"]]),
                         raw_poses=arrays["teacher_history_pose"], route=arrays["forward_route"])
            require(np.array_equal(row["raw_pose_stamps_s"], arrays["teacher_history_stamp_s"]),
                    "history times changed")
            trim = row.get("source_map_route_trim")
            if trim:
                require(trim["original_points"] == len(world["route"])
                        and 0 <= trim["retained_points"] <= len(world["route"]), "invalid route trim")
                world["route"] = world["route"][:trim["retained_points"]]
            meta = schema["maps"][row["map_name"]]
            for key, expected in world.items():
                pixel = np.asarray(row[key]).reshape(expected.shape)
                restored = (pixel[..., :2] + .5) * meta["resolution_m"] + meta["origin_xy"]
                error = float(np.max(np.abs(restored - expected[..., :2]))) if expected.size else 0.
                require(error < 1e-9, f"world roundtrip mismatch: {key}")
                max_error = max(max_error, error)
                if expected.shape[-1] == 3:
                    require(np.array_equal(pixel[..., 2], expected[..., 2]), "yaw changed")
        if row["map_name"] == "P_map":
            require(row["handdraw_map"] == "source_map", "wrong target map")
            for key, target in (("reference_pose", "handdraw_pixel_xy"),
                                ("raw_poses", "handdraw_raw_pixel_xy"),
                                ("route", "handdraw_route_pixel_xy")):
                points += on_gray(row[target])
                require(np.asarray(row["handdraw_point_valid"][key]).all(), "invalid kept point flag")
            xy = np.asarray(row["handdraw_route_pixel_xy"]).reshape(-1, 2)
            require(len(row["handdraw_route_segment_valid"]) == max(0, len(xy)-1)
                    and all(row["handdraw_route_segment_valid"]), "invalid kept segment flag")
            for a, b in zip(xy, xy[1:]):
                on_gray(np.linspace(a, b, max(2, int(np.ceil(np.max(np.abs(b-a)) * 2))+1)))
                segments += 1
        else:
            require(row["handdraw_map"] is None, "non-P_map acquired handdraw coordinates")
    require(not seen.intersection(excluded) and seen.union(excluded) == accepted, "case accounting mismatch")
    require(len(seen) == audit["counts"]["samples.jsonl"]["total"], "output count mismatch")
    return dict(passed=True, retained=len(seen), excluded=len(excluded), maps=dict(maps),
                gray_points_checked=points, gray_segments_checked=segments,
                world_roundtrip_max_error_m=max_error, source_and_output_hashes_verified=True)


def export_task(task, run, target, maps):
    item = dict(task=task, run=str(run), output=str(target))
    print(f"{task}: mapping {run.name}", flush=True)
    try:
        audit = export_pixels(target, maps, run=run, handdraw_map="source-map")
        item.update(status="success", input_count=audit["input_counts"]["samples.jsonl"]["total"],
                    retained=audit["counts"]["samples.jsonl"]["total"], excluded=audit["excluded_rows"],
                    exclusion_reasons=audit["exclusion_reasons"], trimmed_routes=audit["trimmed_routes"],
                    validation=validate_export(target, run), samples=str(target / "samples.jsonl"),
                    schema=str(target / "coordinate_schema.json"))
        print(f"{task}: retained={item['retained']} excluded={item['excluded']} trimmed={item['trimmed_routes']}", flush=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        item.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(f"{task}: FAILED {item['error']}", flush=True)
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-root", type=Path, default=Path("/mnt/chengchangxu/data/visual_nav_training"))
    parser.add_argument("--output", type=Path, required=True, help="new batch directory; never overwritten")
    parser.add_argument("--static-map-root", type=Path, default=DEFAULT_MAP_ROOT)
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=1,
                        help="independent CPU workers, each writes one new task directory")
    args = parser.parse_args()
    output = args.output.resolve()
    selected = latest_runs(args.training_root)
    require(bool(selected), "no existing successful full teacher runs")
    require(not any(output.is_relative_to(run) for _, run in selected), "output is inside a source run")
    output.mkdir(parents=True, exist_ok=False)
    config = read_json(ROOT.parent / "training-data-builder/config.json")
    maps = {name: args.static_map_root / f"{graph}.npz" for name, graph in config["map_mapping"].items()}
    summary = dict(status="running", version=SOURCE_MAP_VERSION,
                   selection="latest successful full run per task; no pilot or duplicate historical runs",
                   training_root=str(args.training_root.resolve()), output=str(output), jobs=args.jobs, tasks=[])
    save_json(output / "selection.json", [dict(task=task, run=str(run)) for task, run in selected])
    save_json(output / "summary.json", summary)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(export_task, task, run, output / task / run.name, maps) for task, run in selected]
        for future in as_completed(futures):
            summary["tasks"].append(future.result())
            summary["tasks"].sort(key=lambda item: item["task"])
            save_json(output / "summary.json", summary)
    failed = sum(t["status"] != "success" for t in summary["tasks"])
    summary.update(status="partial_failure" if failed else "success", failed_tasks=failed,
                   totals={key: sum(t.get(key, 0) for t in summary["tasks"] if t["status"] == "success")
                           for key in ("input_count", "retained", "excluded", "trimmed_routes")})
    save_json(output / "summary.json", summary)
    print(json.dumps(dict(status=summary["status"], output=str(output), totals=summary["totals"])), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
