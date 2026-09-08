#!/usr/bin/env python3
"""Validate Rule-10 history training runs and summarize their features."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np


SCRIPT_ROOT = Path(__file__).resolve().parent


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def quantiles(values: Iterable[float]) -> dict[str, float | int | None]:
    array = np.asarray(list(values), dtype=np.float64)
    if not len(array):
        return {"count": 0, "min": None, "p05": None, "p50": None, "p95": None, "max": None}
    return {
        "count": int(len(array)),
        "min": float(np.min(array)),
        "p05": float(np.quantile(array, 0.05)),
        "p50": float(np.quantile(array, 0.50)),
        "p95": float(np.quantile(array, 0.95)),
        "max": float(np.max(array)),
    }


def source_snapshot(source_root: Path) -> dict[str, Any]:
    tasks = {}
    total_files = 0
    total_bytes = 0
    for task in sorted(path for path in source_root.iterdir() if path.is_dir()):
        files = [path for path in task.rglob("*") if path.is_file()]
        size = sum(path.stat().st_size for path in files)
        total_files += len(files)
        total_bytes += size
        tasks[task.name] = {
            "file_count": len(files),
            "total_bytes": size,
            "frames_jsonl_count": sum(path.name == "frames.jsonl" for path in files),
            "video_count": sum(path.suffix.lower() == ".mp4" for path in files),
            "grid_png_count": sum(
                path.suffix.lower() == ".png" and "grids" in path.parts for path in files
            ),
        }
    return {
        "source_root": str(source_root.resolve()),
        "task_count": len(tasks),
        "file_count": total_files,
        "total_bytes": total_bytes,
        "tasks": tasks,
    }


def discover_runs(root: Path, pipeline_version: str) -> list[Path]:
    runs = []
    for task in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        candidates = []
        for run_json in (task / pipeline_version / "full").glob("run_*/run.json"):
            try:
                document = read_json(run_json)
            except Exception:
                continue
            if (
                document.get("status") == "success"
                and document.get("mode") == "full"
                and document.get("pipeline_version") == pipeline_version
            ):
                candidates.append(run_json.parent)
        if candidates:
            runs.append(sorted(candidates)[-1])
    return runs


def _safe_artifact(run_dir: Path, filename: str) -> Path | None:
    path = (run_dir / filename).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return path


def validate_run(
    run_dir: Path,
    config: Mapping[str, Any],
    aggregate_counters: dict[str, Counter[str]] | None = None,
    aggregate_numeric: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    numeric: dict[str, list[float]] = defaultdict(list)
    run = read_json(run_dir / "run.json")
    source_rows = read_jsonl(run_dir / "source_manifest.jsonl")
    labels = read_jsonl(run_dir / "teacher_labels.jsonl")
    rgb_manifest = read_jsonl(run_dir / "rgb_history_manifest.jsonl")
    accepted_labels = [item for item in labels if item.get("status") == "accepted"]
    rejected_labels = [item for item in labels if item.get("status") == "rejected"]
    case_paths = sorted((run_dir / "cases").glob("*/sample.json"))
    batch_requests: dict[str, tuple[int, int]] = {}
    recommended_camera_ids = tuple(str(value) for value in config["camera_ids"])
    observed_camera_sets: set[tuple[str, ...]] = set()

    expected = (
        {
            "candidate_count": len(labels),
            "accepted_count": len(accepted_labels),
            "rejected_count": len(rejected_labels),
        }
        if run.get("mode") == "full"
        else {
            "attempt_count": len(labels),
            "case_count": len(accepted_labels),
        }
    )
    for key, value in expected.items():
        if int(run.get(key) or 0) != value:
            errors.append({"scope": "run", "reason": f"{key}_mismatch", "expected": value, "actual": run.get(key)})
    if len(case_paths) != len(accepted_labels):
        errors.append({"scope": "run", "reason": "case_count_mismatch", "cases": len(case_paths), "accepted": len(accepted_labels)})
    if len(rgb_manifest) != len(case_paths):
        errors.append({"scope": "run", "reason": "rgb_manifest_count_mismatch", "rgb": len(rgb_manifest), "cases": len(case_paths)})

    for label in rejected_labels:
        counters["reject_reason"][str(label.get("reject_reason") or "unknown")] += 1
    for label in labels:
        teacher = dict(label.get("teacher") or {})
        request_id = teacher.get("batch_request_id")
        if request_id is None:
            continue
        request_key = str(request_id)
        request_value = (
            int(teacher.get("batch_sample_count") or 0),
            int(teacher.get("forward_passes") or 0),
        )
        previous = batch_requests.setdefault(request_key, request_value)
        if previous != request_value:
            errors.append(
                {
                    "scope": "run",
                    "reason": "batch_metadata_inconsistent",
                    "batch_request_id": request_key,
                }
            )

    expected_source_rows = sum(int(item.get("row_count") or 0) for item in run.get("subtasks") or ())
    if expected_source_rows and len(source_rows) != expected_source_rows:
        errors.append(
            {
                "scope": "run",
                "reason": "source_manifest_count_mismatch",
                "expected": expected_source_rows,
                "actual": len(source_rows),
            }
        )
    for row in source_rows:
        status = str(row.get("status") or "unknown")
        counters["source_status"][status] += 1
        if status == "rejected":
            counters["source_reject_reason"][str(row.get("reject_reason") or "unknown")] += 1
        if status == "eligible":
            numeric["source_route_projection_distance_m"].append(
                float(row.get("route_projection_distance_m") or 0.0)
            )
            numeric["source_route_remaining_m"].append(
                float(row.get("route_remaining_m") or 0.0)
            )
            for match in (row.get("camera_matches") or {}).values():
                numeric["source_video_sync_delta_ms"].append(float(match.get("delta_ms") or 0.0))
                numeric["source_video_pose_delta_m"].append(
                    float(match.get("estimated_position_delta_m") or 0.0)
                )
                numeric["source_video_yaw_delta_deg"].append(
                    float(match.get("estimated_yaw_delta_deg") or 0.0)
                )

    for sample_path in case_paths:
        sample = read_json(sample_path)
        case_id = str(sample.get("case_id") or sample_path.parent.name)

        def error(reason: str, **details: Any) -> None:
            errors.append({"case_id": case_id, "reason": reason, **details})

        mode = str((sample.get("initial_state") or {}).get("observation_mode") or "")
        counters["observation_mode"][mode] += 1
        counters["map_name"][str(sample.get("map_name"))] += 1
        counters["sub_task_id"][str(sample.get("sub_task_id"))] += 1
        initial = dict(sample.get("initial_state") or {})
        initial_linear = float(initial.get("initial_linear_mps") or 0.0)
        initial_angular = float(initial.get("initial_angular_rps") or 0.0)
        numeric["initial_linear_mps"].append(initial_linear)
        numeric["initial_angular_rps"].append(initial_angular)
        numeric["initial_curvature"].append(float(initial.get("initial_curvature") or 0.0))
        counters["sampling_branch"][str(initial.get("sampling_branch") or "unknown")] += 1
        counters["indoor_profile"][str(bool(initial.get("indoor_profile"))).lower()] += 1
        counters["initial_exact_zero"][
            str(initial_linear == 0.0 and initial_angular == 0.0).lower()
        ] += 1
        counters["initial_linear_sign"][
            "negative" if initial_linear < 0.0 else "positive" if initial_linear > 0.0 else "zero"
        ] += 1
        counters["initial_angular_sign"][
            "negative" if initial_angular < 0.0 else "positive" if initial_angular > 0.0 else "zero"
        ] += 1
        motion = dict(initial.get("motion_estimate") or {})
        for key in ("raw_linear_mps", "raw_angular_rps", "raw_lateral_mps", "window_s"):
            if key in motion:
                numeric[f"motion_{key}"].append(float(motion[key]))
        counters["linear_clipped"][str(bool(motion.get("linear_clipped"))).lower()] += 1
        counters["angular_clipped"][str(bool(motion.get("angular_clipped"))).lower()] += 1
        if mode == "dynamic_history":
            counters["dynamic_linear_clipped"][
                str(bool(motion.get("linear_clipped"))).lower()
            ] += 1
            counters["dynamic_angular_clipped"][
                str(bool(motion.get("angular_clipped"))).lower()
            ] += 1

        teacher = dict(sample.get("teacher") or {})
        if int(teacher.get("forward_passes") or 0) != 1:
            error("forward_passes_not_one", value=teacher.get("forward_passes"))
        numeric["teacher_batch_sample_count"].append(
            float(teacher.get("batch_sample_count") or 0)
        )
        commands = list(teacher.get("commands") or ())
        if not commands:
            error("commands_empty")
        for command in commands:
            values = [float(command[key]) for key in ("linear_mps", "angular_rps", "duration_s")]
            if not all(math.isfinite(value) for value in values) or values[2] <= 0.0:
                error("command_invalid", command=command)
            numeric["command_linear_mps"].append(values[0])
            numeric["command_angular_rps"].append(values[1])
            numeric["command_duration_s"].append(values[2])
        raw = dict((teacher.get("diagnostics") or {}).get("raw_prediction") or {})
        counters["selected_action"][str(raw.get("selected_action"))] += 1
        if raw.get("execution_constraints_applied") is not False:
            error("execution_constraints_applied")
        history_diagnostics = dict(raw.get("occupancy_history") or {})
        history_targets = {
            "older_selected_age_s": float(config["teacher_older_history_offset_s"]),
            "recent_selected_age_s": float(config["teacher_recent_history_offset_s"]),
        }
        for key, target in history_targets.items():
            value = history_diagnostics.get(key)
            if value is None:
                error("teacher_history_channel_missing", channel=key)
                continue
            numeric[key].append(float(value))
            if abs(float(value) - target) > float(
                config["teacher_history_alignment_tolerance_s"]
            ) + 1.0e-6:
                error(
                    "teacher_history_channel_outside_tolerance",
                    channel=key,
                    value=float(value),
                    target=target,
                )
        if bool((sample.get("rollout") or {}).get("collision")):
            error("accepted_rollout_collision")

        npz_path = sample_path.parent / "inputs.npz"
        if not npz_path.is_file():
            error("inputs_npz_missing")
            continue
        with np.load(npz_path, allow_pickle=False) as archive:
            required = {
                "raw_local_occupancy",
                "local_occupancy",
                "static_occupancy",
                "static_local_occupancy",
                "teacher_history_occupancy",
                "teacher_history_pose",
                "teacher_history_stamp_s",
                "teacher_rollout",
            }
            missing = sorted(required - set(archive.files))
            if missing:
                error("npz_keys_missing", keys=missing)
                continue
            raw_local = np.asarray(archive["raw_local_occupancy"])
            local = np.asarray(archive["local_occupancy"])
            static = np.asarray(archive["static_occupancy"])
            static_local = np.asarray(archive["static_local_occupancy"])
            history = np.asarray(archive["teacher_history_occupancy"])
            poses = np.asarray(archive["teacher_history_pose"])
            stamps = np.asarray(archive["teacher_history_stamp_s"])
            if raw_local.shape != (200, 200) or local.shape != (200, 200):
                error("local_shape_invalid", raw=list(raw_local.shape), local=list(local.shape))
            if static.shape != (400, 400) or static_local.shape != (200, 200):
                error("static_shape_invalid", static=list(static.shape), local=list(static_local.shape))
            if not set(np.unique(local)).issubset({0, 100}):
                error("local_value_domain_invalid")
            recomputed = np.where((raw_local != 0) | (static_local != 0), 100, 0).astype(np.int8)
            if not np.array_equal(recomputed, local):
                error("occupancy_union_mismatch")
            if history.ndim != 3 or history.shape[1:] != (200, 200):
                error("history_shape_invalid", shape=list(history.shape))
            if poses.shape != (len(history), 3) or stamps.shape != (len(history),):
                error("history_metadata_shape_invalid")
            if len(stamps) > int(config["teacher_history_max_frames"]):
                error("history_too_long", count=len(stamps))
            if len(stamps) and (np.any(np.diff(stamps) <= 0.0) or stamps[-1] >= float(sample["meta_ts"])):
                error("history_timestamp_invalid")
            numeric["teacher_history_count"].append(float(len(stamps)))
            if len(stamps):
                numeric["teacher_history_window_s"].append(float(sample["meta_ts"]) - float(stamps[0]))
            current_pose = np.asarray(
                [sample["grid_pose"]["x"], sample["grid_pose"]["y"], sample["grid_pose"]["yaw_rad"]]
            )
            if mode == "static_repeat":
                if float(initial.get("initial_linear_mps") or 0.0) != 0.0 or float(initial.get("initial_angular_rps") or 0.0) != 0.0:
                    error("static_repeat_speed_nonzero")
                if not np.all(history == local[None, :, :]):
                    error("static_repeat_occupancy_mismatch")
                if not np.allclose(poses, current_pose[None, :], atol=1.0e-9, rtol=0.0):
                    error("static_repeat_pose_mismatch")
            elif mode == "dynamic_history":
                if not math.isclose(float(initial["initial_linear_mps"]), float(motion["linear_mps"]), abs_tol=1.0e-9):
                    error("dynamic_linear_not_pose_estimate")
                if not math.isclose(float(initial["initial_angular_rps"]), float(motion["angular_rps"]), abs_tol=1.0e-9):
                    error("dynamic_angular_not_pose_estimate")
            else:
                error("observation_mode_invalid", mode=mode)

        rgb = dict(sample.get("rgb_history") or {})
        if float(rgb.get("max_hz") or 0.0) > float(config["rgb_history_max_hz"]) + 1.0e-9:
            error("rgb_declared_rate_too_high")
        rgb_cameras = dict(rgb.get("cameras") or {})
        matched_camera_ids = {str(value) for value in (sample.get("camera_matches") or {})}
        actual_camera_ids = tuple(
            value for value in recommended_camera_ids if value in rgb_cameras
        ) + tuple(sorted(value for value in rgb_cameras if value not in recommended_camera_ids))
        if not actual_camera_ids:
            error("rgb_camera_set_empty")
        if set(actual_camera_ids) != matched_camera_ids:
            error(
                "rgb_camera_set_mismatch",
                rgb=sorted(actual_camera_ids),
                matches=sorted(matched_camera_ids),
            )
        observed_camera_sets.add(actual_camera_ids)
        counters["camera_set"][",".join(actual_camera_ids)] += 1
        for camera_id in actual_camera_ids:
            entries = list(rgb_cameras.get(camera_id) or ())
            if not entries:
                error("rgb_history_empty", camera_id=camera_id)
                continue
            filenames = []
            pts = []
            offsets = []
            for entry in entries:
                artifact = _safe_artifact(run_dir, str(entry.get("filename") or ""))
                if artifact is None or not artifact.is_file():
                    error("rgb_history_file_missing", camera_id=camera_id, filename=entry.get("filename"))
                filenames.append(str(entry.get("filename")))
                pts.append(float(entry.get("pts_s") or 0.0))
                offsets.append(float(entry.get("offset_s") or 0.0))
            numeric["rgb_history_count"].append(float(len(entries)))
            numeric["rgb_history_window_s"].append(float(max(offsets) - min(offsets)))
            if mode == "static_repeat":
                if len(set(filenames)) != 1 or not all(bool(item.get("repeated_source")) for item in entries):
                    error("rgb_static_repeat_mismatch", camera_id=camera_id)
            else:
                differences = np.diff(np.asarray(pts, dtype=np.float64))
                if len(differences) and float(np.min(differences)) < 1.0 / float(config["rgb_history_max_hz"]) - 1.0e-6:
                    error("rgb_rate_too_high", camera_id=camera_id, min_interval_s=float(np.min(differences)))
                if len(differences):
                    minimum_interval = float(np.min(differences))
                    numeric["rgb_min_interval_s"].append(minimum_interval)
                    numeric["rgb_max_effective_hz"].append(1.0 / minimum_interval)

        fusion = dict(sample.get("occupancy_fusion") or {})
        numeric["raw_obstacle_cells"].append(float(fusion.get("raw_local_obstacle_cells") or 0))
        numeric["fused_obstacle_cells"].append(float(fusion.get("fused_local_obstacle_cells") or 0))
        numeric["static_added_obstacle_cells"].append(float(fusion.get("static_only_added_obstacle_cells") or 0))

    for actual_camera_ids in sorted(observed_camera_sets):
        if actual_camera_ids != recommended_camera_ids:
            warnings.append(
                {
                    "scope": "run",
                    "reason": "video_camera_set_nonstandard",
                    "actual": list(actual_camera_ids),
                    "recommended": list(recommended_camera_ids),
                }
            )

    if aggregate_counters is not None:
        for key, values in counters.items():
            aggregate_counters[key].update(values)
    if aggregate_numeric is not None:
        for key, values in numeric.items():
            aggregate_numeric[key].extend(values)

    return {
        "run_dir": str(run_dir),
        "run_id": run.get("run_id"),
        "task_id": run_dir.parents[2].name,
        "candidate_count": len(labels),
        "accepted_count": len(accepted_labels),
        "rejected_count": len(rejected_labels),
        "teacher_batch": {
            "request_count": len(batch_requests),
            "sample_slots": sum(value[0] for value in batch_requests.values()),
            "batch_sizes": quantiles(value[0] for value in batch_requests.values()),
            "all_forward_passes_one": all(
                value[1] == 1 for value in batch_requests.values()
            ),
        },
        "validation": {"passed": not errors, "error_count": len(errors), "warning_count": len(warnings), "errors": errors[:200], "warnings": warnings[:200]},
        "categorical": {key: dict(sorted(value.items())) for key, value in sorted(counters.items())},
        "numeric": {key: quantiles(value) for key, value in sorted(numeric.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--training-root", type=Path, default=Path("/mnt/chengchangxu/data/visual_nav_training"))
    parser.add_argument("--source-root", type=Path, default=Path("/mnt/chengchangxu/data/visual_nav_mv"))
    parser.add_argument("--config", type=Path, default=SCRIPT_ROOT / "config.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = read_json(args.config.resolve())
    runs = discover_runs(args.training_root.resolve(), str(config["pipeline_version"]))
    aggregate_counters: dict[str, Counter[str]] = defaultdict(Counter)
    aggregate_numeric: dict[str, list[float]] = defaultdict(list)
    reports = [
        validate_run(run, config, aggregate_counters, aggregate_numeric)
        for run in runs
    ]
    accepted_count = sum(item["accepted_count"] for item in reports)
    rejected_count = sum(item["rejected_count"] for item in reports)
    candidate_count = sum(item["candidate_count"] for item in reports)
    static_count = aggregate_counters["observation_mode"].get("static_repeat", 0)
    dynamic_count = aggregate_counters["observation_mode"].get("dynamic_history", 0)
    exact_zero_count = aggregate_counters["initial_exact_zero"].get("true", 0)
    linear_clipped_count = aggregate_counters["linear_clipped"].get("true", 0)
    angular_clipped_count = aggregate_counters["angular_clipped"].get("true", 0)
    dynamic_linear_clipped_count = aggregate_counters["dynamic_linear_clipped"].get("true", 0)
    dynamic_angular_clipped_count = aggregate_counters["dynamic_angular_clipped"].get("true", 0)
    document = {
        "pipeline_version": config["pipeline_version"],
        "training_root": str(args.training_root.resolve()),
        "run_count": len(reports),
        "candidate_count": candidate_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "all_passed": bool(reports) and all(item["validation"]["passed"] for item in reports),
        "teacher_batch": {
            "request_count": sum(item["teacher_batch"]["request_count"] for item in reports),
            "sample_slots": sum(item["teacher_batch"]["sample_slots"] for item in reports),
            "all_forward_passes_one": all(
                item["teacher_batch"]["all_forward_passes_one"] for item in reports
            ),
        },
        "feature_summary": {
            "ratios": {
                "acceptance_rate": accepted_count / candidate_count if candidate_count else None,
                "static_repeat_fraction": static_count / accepted_count if accepted_count else None,
                "initial_exact_zero_fraction": exact_zero_count / accepted_count if accepted_count else None,
                "pose_estimate_linear_clipped_fraction": linear_clipped_count / accepted_count if accepted_count else None,
                "pose_estimate_angular_clipped_fraction": angular_clipped_count / accepted_count if accepted_count else None,
                "dynamic_linear_velocity_clipped_fraction": dynamic_linear_clipped_count / dynamic_count if dynamic_count else None,
                "dynamic_angular_velocity_clipped_fraction": dynamic_angular_clipped_count / dynamic_count if dynamic_count else None,
            },
            "categorical": {
                key: dict(sorted(value.items()))
                for key, value in sorted(aggregate_counters.items())
            },
            "numeric": {
                key: quantiles(value)
                for key, value in sorted(aggregate_numeric.items())
            },
        },
        "source_snapshot": source_snapshot(args.source_root.resolve()),
        "runs": reports,
    }
    output = args.output or (args.training_root.resolve() / f"{config['pipeline_version']}_validation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "run_count": document["run_count"], "candidate_count": document["candidate_count"], "accepted_count": document["accepted_count"], "rejected_count": document["rejected_count"], "all_passed": document["all_passed"]}, ensure_ascii=False))
    return 0 if document["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
