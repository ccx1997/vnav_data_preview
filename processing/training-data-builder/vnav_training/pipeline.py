from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image

from .core import (
    OCCUPANCY_FUSION_VERSION,
    PIPELINE_VERSION,
    ROUTE_VERSION,
    STATE_SAMPLER_VERSION,
    RouteData,
    RouteProjection,
    boundary_keep_interval,
    build_route_mask,
    build_routes,
    crop_static_map,
    decode_selected_frames,
    estimate_pose_delta,
    estimate_pose_velocity,
    estimate_route_curvature,
    find_first_pose_jump,
    forward_route,
    fuse_local_static_occupancy,
    integrate_commands,
    load_static_map,
    load_teacher_occupancy,
    project_to_route,
    rollout_collision,
    sample_initial_state,
    save_npz_atomic,
    sha256_file,
    stable_json_hash,
)
from .visualization import render_case, render_overview


@dataclass(frozen=True)
class CameraMatch:
    camera_id: str
    video_path: str
    frame_index: int
    pts_s: float
    target_video_s: float
    delta_ms: float
    estimated_position_delta_m: float
    estimated_yaw_delta_deg: float
    pose_neighbor_gap_s: float
    camera_offset_ms: float = 0.0


@dataclass(frozen=True)
class RGBHistoryMatch:
    camera_id: str
    video_path: str
    frame_index: int
    pts_s: float
    offset_s: float


@dataclass(frozen=True)
class HistoryFrame:
    row_index: int
    meta_ts: float
    grid_path: Path
    grid_pose: tuple[float, float, float]


@dataclass(frozen=True)
class Candidate:
    sub_task_id: str
    source_sub_task_id: str
    map_segment_index: int
    row_index: int
    meta_ts: float
    meta_ts_ms: int
    map_name: str
    graph_name: str
    grid_path: Path
    grid_pose: tuple[float, float, float]
    route: RouteData
    projection: RouteProjection
    camera_matches: Mapping[str, CameraMatch]
    history_frames: tuple[HistoryFrame, ...]
    rgb_history_matches: Mapping[str, tuple[RGBHistoryMatch, ...]]


@dataclass
class SubtaskContext:
    sub_task_id: str
    source_sub_task_id: str
    suffix: str
    map_name: str
    map_name_source: str
    map_segment_index: int
    source_row_start: int
    source_row_stop: int
    video_window_source: str
    camera_ids: tuple[str, ...]
    meta_dir: Path
    video_dir: Path
    rows: list[dict[str, Any]]
    routes: list[RouteData]
    candidates: list[Candidate]
    audits: list[dict[str, Any]]
    keep_interval: tuple[float, float]
    trim_summary: dict[str, Any]
    pose_jump: dict[str, Any] | None


@dataclass(frozen=True)
class MapSegment:
    map_name: str
    map_name_source: str
    segment_index: int
    start_index: int
    stop_index: int


@dataclass
class LabelResult:
    candidate: Candidate
    status: str
    reject_reason: str | None
    document: dict[str, Any]
    source_grid: np.ndarray | None = None
    raw_local_occupancy: np.ndarray | None = None
    teacher_occupancy: np.ndarray | None = None
    static_occupancy: np.ndarray | None = None
    static_local_occupancy: np.ndarray | None = None
    route_mask: np.ndarray | None = None
    world_to_pixel: np.ndarray | None = None
    forward_route: np.ndarray | None = None
    rollout: np.ndarray | None = None
    history_occupancies: np.ndarray | None = None
    history_poses: np.ndarray | None = None
    history_stamps: np.ndarray | None = None


@dataclass
class PreparedLabel:
    candidate: Candidate
    base_document: dict[str, Any]
    source_grid: np.ndarray
    raw_local_occupancy: np.ndarray
    teacher_occupancy: np.ndarray
    static_occupancy: np.ndarray
    static_local_occupancy: np.ndarray
    route_mask: np.ndarray
    world_to_pixel: np.ndarray
    forward_route: np.ndarray
    initial_state: dict[str, Any]
    history_occupancies: np.ndarray
    history_poses: np.ndarray
    history_stamps: np.ndarray
    occupancy_fusion: dict[str, Any]
    static_map: Any


class TeacherRuntime:
    def __init__(
        self,
        *,
        url: str,
        model_server_root: Path,
        config_path: Path,
        map_root: Path,
        config: Mapping[str, Any],
    ) -> None:
        root_text = str(model_server_root.resolve())
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from model_server import (  # type: ignore
            GridMeta,
            ModelClient,
            Pose2D,
            TeacherHistoryObservation,
            TeacherInferenceSample,
        )
        from model_server.indoor_regions import classify_indoor_pose  # type: ignore

        self.GridMeta = GridMeta
        self.Pose2D = Pose2D
        self.TeacherHistoryObservation = TeacherHistoryObservation
        self.TeacherInferenceSample = TeacherInferenceSample
        self.classify_indoor_pose = classify_indoor_pose
        self.client = ModelClient(url, timeout_s=120.0)
        self.url = url
        self.health = dict(self.client.health())
        if self.health.get("status") != "ok" or not self.health.get("ready"):
            raise RuntimeError(f"teacher service is not ready: {self.health}")
        teacher_health = dict(self.health.get("teacher_batch") or {})
        if not teacher_health.get("enabled"):
            raise RuntimeError("teacher service does not expose /teacher/infer_batch")
        if int(teacher_health.get("max_history_frames_per_sample") or 0) < int(
            config["teacher_history_max_frames"]
        ):
            raise RuntimeError("teacher history limit is below the pipeline contract")
        self.config = dict(config)
        self.config_path = config_path.resolve()
        self.config_sha256 = sha256_file(self.config_path)
        checkpoint = Path(str(self.health.get("checkpoint") or ""))
        self.checkpoint_path = checkpoint.resolve() if checkpoint.is_file() else checkpoint
        self.checkpoint_sha256 = sha256_file(checkpoint) if checkpoint.is_file() else None
        self.model_id = str(self.health.get("model_id") or checkpoint.name)
        self.teacher_version = (
            f"{self.model_id}:{(self.checkpoint_sha256 or 'unknown')[:12]}:"
            f"{self.config_sha256[:12]}"
        )
        self.static_maps = {}
        self._history_cache: OrderedDict[tuple[Any, ...], np.ndarray] = OrderedDict()
        self.history_cache_hits = 0
        self.history_cache_misses = 0
        for map_name, graph_name in dict(config["map_mapping"]).items():
            map_path = map_root / f"{graph_name}.npz"
            if map_path.is_file():
                self.static_maps[map_name] = load_static_map(map_path, graph_name)

    def fused_history_occupancy(
        self,
        frame: HistoryFrame,
        static_map: Any,
    ) -> np.ndarray:
        """Load one immutable fused history grid through a bounded run-local cache."""

        key = (
            str(frame.grid_path),
            frame.grid_pose,
            str(static_map.sha256),
            str(self.config["occupancy_fusion_version"]),
        )
        cached = self._history_cache.get(key)
        if cached is not None:
            self.history_cache_hits += 1
            self._history_cache.move_to_end(key)
            return cached
        _source, historical_raw = load_teacher_occupancy(frame.grid_path)
        historical_static, _transform = crop_static_map(
            static_map,
            frame.grid_pose,
            size=int(self.config["static_crop_size"]),
            resolution_m=float(self.config["static_crop_resolution_m"]),
        )
        fused, _static_local = fuse_local_static_occupancy(
            historical_raw,
            historical_static,
        )
        fused.setflags(write=False)
        self._history_cache[key] = fused
        self.history_cache_misses += 1
        maximum = int(self.config["teacher_history_cache_size"])
        while len(self._history_cache) > maximum:
            self._history_cache.popitem(last=False)
        return fused

    def history_cache_stats(self) -> dict[str, int]:
        return {
            "size": len(self._history_cache),
            "maximum": int(self.config["teacher_history_cache_size"]),
            "hits": self.history_cache_hits,
            "misses": self.history_cache_misses,
        }

    def infer_batch(
        self,
        prepared: Sequence[PreparedLabel],
        *,
        run_id: str,
    ) -> list[tuple[list[dict[str, float]], dict[str, Any], str, str]]:
        if not prepared:
            return []
        samples = []
        hashes: list[tuple[str, str]] = []
        for value in prepared:
            candidate = value.candidate
            occupancy = value.teacher_occupancy
            row_route = np.asarray(candidate.route.sparse_points, dtype=np.float32)
            accelerate = np.zeros(len(row_route), dtype=np.int8)
            grid = self.GridMeta(
                width=int(occupancy.shape[1]),
                height=int(occupancy.shape[0]),
                resolution_m=0.05,
                origin_x=-5.0,
                origin_y=-5.0,
                frame_id="body",
                stamp_s=float(candidate.meta_ts),
                alignment="robot_aligned",
            )
            pose = self.Pose2D(*candidate.grid_pose)
            sample_id = f"{candidate.sub_task_id}:{candidate.row_index}"
            metadata = {
                "sample_id": sample_id,
                "graph_name": candidate.graph_name,
                "reference_curvature": float(value.initial_state["initial_curvature"]),
                "current_linear_mps": float(value.initial_state["initial_linear_mps"]),
                "current_angular_rps": float(value.initial_state["initial_angular_rps"]),
                "dataset_id": candidate.sub_task_id,
                "meta_ts": candidate.meta_ts,
                "run_id": run_id,
            }
            history = tuple(
                self.TeacherHistoryObservation(
                    occupancy=value.history_occupancies[index],
                    pose=self.Pose2D(*value.history_poses[index]),
                    stamp_s=float(value.history_stamps[index]),
                )
                for index in range(len(value.history_stamps))
            )
            request_document = {
                "occupancy_sha256": hashlib.sha256(
                    np.ascontiguousarray(occupancy).tobytes()
                ).hexdigest(),
                "route_sha256": hashlib.sha256(
                    np.ascontiguousarray(row_route).tobytes()
                ).hexdigest(),
                "accelerate_sha256": hashlib.sha256(accelerate.tobytes()).hexdigest(),
                "grid": asdict(grid),
                "pose": asdict(pose),
                "metadata": metadata,
                "history": [
                    {
                        "occupancy_sha256": hashlib.sha256(
                            np.ascontiguousarray(item.occupancy).tobytes()
                        ).hexdigest(),
                        "pose": asdict(item.pose),
                        "stamp_s": item.stamp_s,
                    }
                    for item in history
                ],
            }
            request_hash = stable_json_hash(request_document)
            stable_document = dict(request_document)
            stable_document["metadata"] = {
                key: item for key, item in metadata.items() if key != "run_id"
            }
            hashes.append((request_hash, stable_json_hash(stable_document)))
            samples.append(
                self.TeacherInferenceSample(
                    occupancy=occupancy,
                    grid=grid,
                    pose=pose,
                    global_path=row_route,
                    accelerate=accelerate,
                    metadata=metadata,
                    history=history,
                )
            )

        response = dict(self.client.infer_teacher_batch(samples, timeout_s=120.0))
        outputs = []
        for prediction, (request_hash, sample_content_hash) in zip(
            response["predictions"], hashes
        ):
            commands = [
                {
                    "linear_mps": float(command["linear_mps"]),
                    "angular_rps": float(command["angular_rps"]),
                    "duration_s": float(command["duration_s"]),
                }
                for command in prediction["commands"]
            ]
            sample_response = {
                "status": response["status"],
                "batch_request_id": response["batch_request_id"],
                "model_id": response.get("model_id"),
                "sample_count": response["sample_count"],
                "forward_passes": response["forward_passes"],
                "batch_inference_ms": response["inference_ms"],
                **dict(prediction),
            }
            outputs.append(
                (commands, sample_response, request_hash, sample_content_hash)
            )
        return outputs


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, documents: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for document in documents:
            stream.write(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                )
                + "\n"
            )


def load_config(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    required_versions = {
        "pipeline_version": PIPELINE_VERSION,
        "route_version": ROUTE_VERSION,
        "state_sampler_version": STATE_SAMPLER_VERSION,
        "occupancy_fusion_version": OCCUPANCY_FUSION_VERSION,
    }
    for key, expected in required_versions.items():
        if document.get(key) != expected:
            raise ValueError(f"{key} must be {expected!r}")
    return document


def _ffprobe_pts(video_path: Path) -> np.ndarray:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-of",
        "json",
        str(video_path),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    document = json.loads(completed.stdout)
    values = [
        float(frame["best_effort_timestamp_time"])
        for frame in document.get("frames", ())
        if frame.get("best_effort_timestamp_time") is not None
    ]
    if not values:
        raise RuntimeError(f"video_corrupt: no PTS in {video_path}")
    result = np.asarray(values, dtype=np.float64)
    if np.any(np.diff(result) < 0.0):
        raise RuntimeError(f"video timestamp_non_monotonic: {video_path}")
    return result


def _video_time(timestamp: float, windows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    elapsed = 0.0
    for window in windows:
        start = float(window["from"])
        stop = float(window["to"])
        if start <= timestamp <= stop:
            return elapsed + timestamp - start, start - elapsed
        elapsed += max(0.0, stop - start)
    raise ValueError("video_gap")


def _nearest_pts(pts: np.ndarray, target: float) -> tuple[int, float]:
    index = int(np.searchsorted(pts, target, side="left"))
    candidates = [min(len(pts) - 1, index)]
    if index > 0:
        candidates.append(index - 1)
    best = min(candidates, key=lambda item: abs(float(pts[item]) - target))
    return best, float(pts[best])


def _rgb_history_indices(
    pts: np.ndarray,
    current_index: int,
    *,
    window_s: float,
    max_hz: float,
) -> tuple[int, ...]:
    """Select an actual-PTS history whose instantaneous rate never exceeds max_hz."""

    if not 0 <= int(current_index) < len(pts):
        raise ValueError("video_gap")
    if window_s <= 0.0 or max_hz <= 0.0:
        raise ValueError("rgb_history_config_invalid")
    minimum_interval = 1.0 / float(max_hz)
    current_pts = float(pts[int(current_index)])
    lower = current_pts - float(window_s)
    selected = [int(current_index)]
    last_pts = current_pts
    for index in range(int(current_index) - 1, -1, -1):
        value = float(pts[index])
        if value < lower - 1.0e-9:
            break
        if last_pts - value >= minimum_interval - 1.0e-9:
            selected.append(index)
            last_pts = value
    selected.reverse()
    return tuple(selected)


def _history_frames_for_row(
    rows: Sequence[Mapping[str, Any]],
    current_index: int,
    meta_dir: Path,
    config: Mapping[str, Any],
) -> tuple[HistoryFrame, ...]:
    current_stamp = float(rows[current_index]["ts"])
    retention_s = float(config["teacher_history_retention_s"])
    maximum = int(config["teacher_history_max_frames"])
    frames: list[HistoryFrame] = []
    for row_index in range(current_index - 1, -1, -1):
        row = rows[row_index]
        stamp = float(row["ts"])
        if current_stamp - stamp > retention_s + 1.0e-9:
            break
        pose = row.get("grid_pose") or {}
        grid_path = meta_dir / str(row.get("grid_png") or "")
        if not (
            row.get("grid_valid")
            and row.get("grid_png")
            and pose.get("valid")
            and pose.get("match") == "ros_grid_sync"
            and grid_path.is_file()
        ):
            continue
        frames.append(
            HistoryFrame(
                row_index=row_index,
                meta_ts=stamp,
                grid_path=grid_path,
                grid_pose=(
                    float(pose["x"]),
                    float(pose["y"]),
                    float(pose["yaw_rad"]),
                ),
            )
        )
    frames = list(reversed(frames[:maximum]))
    if not frames:
        raise ValueError("teacher_history_insufficient")
    ages = np.asarray([current_stamp - frame.meta_ts for frame in frames])
    tolerance = float(config["teacher_history_alignment_tolerance_s"])
    older_target = float(config["teacher_older_history_offset_s"])
    # Rule10OccupancyHistory returns an all-zero older channel when the target
    # timestamp predates the oldest registered frame; proximity alone is not
    # enough when the nearest frame lies on the younger side of the target.
    if float(np.max(ages)) + 1.0e-6 < older_target:
        raise ValueError("teacher_history_insufficient")
    for target in (
        older_target,
        float(config["teacher_recent_history_offset_s"]),
    ):
        if float(np.min(np.abs(ages - target))) > tolerance:
            raise ValueError("teacher_history_insufficient")
    return tuple(frames)


def _subtask_suffix(sub_task_id: str) -> str:
    value = sub_task_id.rsplit("_", 1)[-1]
    if not value.isdigit():
        raise ValueError(f"cannot determine subtask suffix from {sub_task_id}")
    return value


def _candidate_audit_base(
    sub_task_id: str, row_index: int, row: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "sub_task_id": sub_task_id,
        "row_index": row_index,
        "meta_ts": row.get("ts"),
        "meta_ts_ms": row.get("ts_ms"),
        "map_name": row.get("map_name"),
        "grid_png": row.get("grid_png"),
    }


def _canonical_map_name(value: Any, config: Mapping[str, Any]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for configured in sorted(config["map_mapping"], key=len, reverse=True):
        name = str(configured)
        if raw == name or raw.startswith(f"{name}_"):
            return name
    match = re.fullmatch(r"(.+?_map)(?:_\d{8}_\d{6})?", raw)
    return match.group(1) if match else raw


def _collect_current_maps(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, dict):
        current = value.get("current_map")
        if current:
            output.append(str(current))
        for child in value.values():
            output.extend(_collect_current_maps(child))
    elif isinstance(value, list):
        for child in value:
            output.extend(_collect_current_maps(child))
    return output


def split_rows_by_map(
    rows: Sequence[Mapping[str, Any]],
    meta_dir: Path,
    config: Mapping[str, Any],
) -> list[MapSegment]:
    """Split a source subtask at exact per-frame map transitions.

    A uniform task snapshot is a safe fallback only when every frame lacks a
    map.  If multiple frame maps exist, missing rows make the transition time
    ambiguous and are rejected instead of being guessed.
    """

    if not rows:
        return []
    raw_frame_maps = [row.get("map_name") or row.get("current_map") for row in rows]
    canonical = [_canonical_map_name(value, config) for value in raw_frame_maps]
    observed = {value for value in canonical if value}
    frame_field = (
        "frames.map_name"
        if any(row.get("map_name") for row in rows)
        else "frames.current_map"
    )
    if observed:
        if any(not value for value in canonical):
            if len(observed) != 1:
                raise ValueError(
                    f"map_timeline_incomplete: {meta_dir.name} has missing frame maps "
                    f"around multiple maps {sorted(observed)}"
                )
            only = next(iter(observed))
            canonical = [value or only for value in canonical]
            source = f"{frame_field}+uniform_missing_fill"
        else:
            source = frame_field
    else:
        task_path = meta_dir / "task.json"
        if not task_path.is_file():
            raise ValueError(f"map_timeline_missing: {task_path}")
        task_document = json.loads(task_path.read_text(encoding="utf-8"))
        snapshots = {
            _canonical_map_name(value, config)
            for value in _collect_current_maps(task_document)
            if _canonical_map_name(value, config)
        }
        if len(snapshots) != 1:
            raise ValueError(
                f"map_timeline_incomplete: {meta_dir.name} has no frame maps and "
                f"task snapshots resolve to {sorted(snapshots)}"
            )
        fallback = next(iter(snapshots))
        canonical = [fallback] * len(rows)
        source = "task_snapshot_uniform"

    segments: list[MapSegment] = []
    start = 0
    for index in range(1, len(canonical) + 1):
        if index == len(canonical) or canonical[index] != canonical[start]:
            segments.append(
                MapSegment(
                    map_name=canonical[start],
                    map_name_source=source,
                    segment_index=len(segments) + 1,
                    start_index=start,
                    stop_index=index,
                )
            )
            start = index
    return segments


def _load_video_alignment(
    source_sub_task_id: str,
    video_dir: Path,
    config: Mapping[str, Any],
) -> tuple[
    list[Mapping[str, Any]],
    str,
    tuple[str, ...],
    dict[str, Path],
    dict[str, np.ndarray],
]:
    manifest = json.loads((video_dir / "manifest.json").read_text(encoding="utf-8"))
    result_by_camera = {
        str(item["camera"]): item
        for item in manifest.get("results", ())
        if isinstance(item, Mapping) and item.get("ok") and item.get("camera")
    }
    camera_count = int(manifest.get("camera_count") or len(result_by_camera))
    if (
        not manifest.get("concat_complete")
        or camera_count <= 0
        or int(manifest.get("concat_ok") or 0) != camera_count
        or len(result_by_camera) != camera_count
    ):
        raise ValueError(f"video manifest incomplete: {video_dir}")
    windows, window_source = _effective_video_windows(manifest)
    if not windows:
        raise ValueError(f"video manifest has no keep_windows: {video_dir}")
    recommended = tuple(str(value) for value in config["camera_ids"])
    camera_ids = tuple(value for value in recommended if value in result_by_camera) + tuple(
        sorted(value for value in result_by_camera if value not in recommended)
    )
    video_paths: dict[str, Path] = {}
    pts_by_camera: dict[str, np.ndarray] = {}
    for camera_id in camera_ids:
        result = result_by_camera.get(camera_id)
        if not result:
            raise ValueError(f"video_missing: {source_sub_task_id}/{camera_id}")
        video_path = Path(str(result["out"]))
        if not video_path.is_file():
            video_path = video_dir / f"{camera_id}_continuous.mp4"
        if not video_path.is_file():
            raise ValueError(f"video_missing: {video_path}")
        video_paths[camera_id] = video_path
        pts_by_camera[camera_id] = _ffprobe_pts(video_path)
    return windows, window_source, camera_ids, video_paths, pts_by_camera


def _effective_video_windows(
    manifest: Mapping[str, Any],
) -> tuple[list[Mapping[str, float]], str]:
    local_trim = manifest.get("local_trim")
    if isinstance(local_trim, Mapping):
        try:
            start = float(local_trim["from"])
            stop = float(local_trim["to"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if math.isfinite(start) and math.isfinite(stop) and stop > start:
                return [{"from": start, "to": stop}], "manifest.local_trim"
    return list(manifest.get("keep_windows") or ()), "manifest.keep_windows"


def _build_map_segment_context(
    *,
    meta_dir: Path,
    video_dir: Path,
    source_sub_task_id: str,
    source_rows: Sequence[dict[str, Any]],
    segment: MapSegment,
    segment_count: int,
    windows: Sequence[Mapping[str, Any]],
    video_window_source: str,
    video_paths: Mapping[str, Path],
    pts_by_camera: Mapping[str, np.ndarray],
    config: Mapping[str, Any],
) -> SubtaskContext:
    rows = list(source_rows[segment.start_index : segment.stop_index])
    map_name = segment.map_name
    if segment_count == 1:
        sub_task_id = source_sub_task_id
    else:
        safe_map_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", map_name)
        sub_task_id = (
            f"{source_sub_task_id}__map_{segment.start_index:07d}_"
            f"{safe_map_name}"
        )

    pose_jump = find_first_pose_jump(
        rows,
        pair_max_gap_s=float(config["pose_jump_pair_max_gap_s"]),
        distance_m=float(config["pose_jump_distance_m"]),
        speed_mps=float(config["pose_jump_speed_mps"]),
        yaw_step_deg=float(config["pose_jump_yaw_step_deg"]),
        yaw_rate_rps=float(config["pose_jump_yaw_rate_rps"]),
    )
    usable_rows = rows if pose_jump is None else rows[: pose_jump.row_index]
    keep_start, keep_end, trim_summary = boundary_keep_interval(usable_rows, config)
    pose_jump_document = asdict(pose_jump) if pose_jump is not None else None
    if pose_jump_document is not None:
        pose_jump_document["previous_row_index"] += segment.start_index
        pose_jump_document["row_index"] += segment.start_index
    if pose_jump is not None:
        trim_summary = {
            **trim_summary,
            "pose_jump_tail_trim_s": round(
                float(rows[-1]["ts"]) - pose_jump.jump_ts,
                6,
            ),
            "pose_jump_valid_end_ts": pose_jump.previous_ts,
        }
    route_rows = []
    for row_index, row in enumerate(rows):
        before_pose_jump = pose_jump is None or row_index < pose_jump.row_index
        if before_pose_jump and keep_start <= float(row["ts"]) <= keep_end:
            route_rows.append(row)
        else:
            trimmed = dict(row)
            trimmed["pose"] = {"valid": False}
            route_rows.append(trimmed)
    routes, row_routes = build_routes(sub_task_id, route_rows, config)
    audits: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    for segment_row_index, row in enumerate(rows):
        row_index = segment.start_index + segment_row_index
        audit = _candidate_audit_base(sub_task_id, row_index, row)
        audit.update(
            {
                "source_sub_task_id": source_sub_task_id,
                "map_name": map_name,
                "source_map_name": row.get("map_name") or row.get("current_map"),
                "map_name_source": segment.map_name_source,
                "map_segment_index": segment.segment_index,
                "video_window_source": video_window_source,
            }
        )
        reject_reason = None
        grid_pose_document = row.get("grid_pose") or {}
        if not row.get("grid_valid") or not row.get("grid_png"):
            reject_reason = "grid_missing"
        elif not grid_pose_document.get("valid") or grid_pose_document.get("match") != "ros_grid_sync":
            reject_reason = "grid_schema_mismatch"
        elif map_name not in config["map_mapping"]:
            reject_reason = "teacher_map_unsupported"
        elif pose_jump is not None and segment_row_index >= pose_jump.row_index:
            reject_reason = "pose_jump_tail_discarded"
        elif not (keep_start <= float(row["ts"]) <= keep_end):
            reject_reason = "boundary_idle_trimmed"
        route = row_routes.get(segment_row_index)
        if reject_reason is None and route is None:
            reject_reason = "route_gap"
        if (
            reject_reason is None
            and route is not None
            and (
                route.max_lateral_error_m
                > float(config["route_rdp_epsilon_m"]) + 1.0e-9
                or route.max_tangent_error_deg
                > float(config["route_tangent_error_max_deg"]) + 1.0e-9
            )
        ):
            reject_reason = "route_simplification_error"
        grid_path = meta_dir / str(row.get("grid_png") or "")
        if reject_reason is None and not grid_path.is_file():
            reject_reason = "grid_missing"
        projection = None
        grid_pose = None
        if reject_reason is None:
            try:
                grid_pose = (
                    float(grid_pose_document["x"]),
                    float(grid_pose_document["y"]),
                    float(grid_pose_document["yaw_rad"]),
                )
                projection = project_to_route(grid_pose[:2], route.sparse_points)
            except (KeyError, TypeError, ValueError):
                reject_reason = "pose_invalid"
        if (
            reject_reason is None
            and projection is not None
            and projection.distance_m > float(config["route_projection_max_m"])
        ):
            reject_reason = "route_projection_error"
        if (
            reject_reason is None
            and projection is not None
            and projection.remaining_m < float(config["minimum_forward_route_m"])
        ):
            reject_reason = "route_insufficient"

        camera_matches: dict[str, CameraMatch] = {}
        rgb_history_matches: dict[str, tuple[RGBHistoryMatch, ...]] = {}
        if reject_reason is None:
            try:
                target_video_s, absolute_offset = _video_time(float(row["ts"]), windows)
                for camera_id in video_paths:
                    frame_index, pts_s = _nearest_pts(pts_by_camera[camera_id], target_video_s)
                    delta_s = pts_s - target_video_s
                    if abs(delta_s) > float(config["video_sync_max_s"]):
                        raise ValueError("video_sync_error")
                    matched_absolute_ts = pts_s + absolute_offset
                    position_delta, yaw_delta, neighbor_gap = estimate_pose_delta(
                        rows,
                        segment_row_index,
                        matched_absolute_ts,
                        float(config["pose_neighbor_gap_max_s"]),
                    )
                    if position_delta > float(config["position_delta_max_m"]):
                        raise ValueError("video_sync_error")
                    if yaw_delta > float(config["yaw_delta_max_deg"]):
                        raise ValueError("video_sync_error")
                    camera_matches[camera_id] = CameraMatch(
                        camera_id=camera_id,
                        video_path=str(video_paths[camera_id]),
                        frame_index=frame_index,
                        pts_s=pts_s,
                        target_video_s=target_video_s,
                        delta_ms=delta_s * 1000.0,
                        estimated_position_delta_m=position_delta,
                        estimated_yaw_delta_deg=yaw_delta,
                        pose_neighbor_gap_s=neighbor_gap,
                    )
                    history_indices = _rgb_history_indices(
                        pts_by_camera[camera_id],
                        frame_index,
                        window_s=float(config["rgb_history_window_s"]),
                        max_hz=float(config["rgb_history_max_hz"]),
                    )
                    history_values = tuple(
                        RGBHistoryMatch(
                            camera_id=camera_id,
                            video_path=str(video_paths[camera_id]),
                            frame_index=history_index,
                            pts_s=float(pts_by_camera[camera_id][history_index]),
                            offset_s=float(
                                pts_by_camera[camera_id][history_index] - pts_s
                            ),
                        )
                        for history_index in history_indices
                    )
                    if (
                        not history_values
                        or history_values[0].offset_s
                        > -float(config["rgb_history_window_s"])
                        + float(config["teacher_history_alignment_tolerance_s"])
                    ):
                        raise ValueError("rgb_history_insufficient")
                    rgb_history_matches[camera_id] = history_values
            except ValueError as error:
                reject_reason = str(error)

        history_frames: tuple[HistoryFrame, ...] = ()
        if reject_reason is None:
            try:
                history_frames = _history_frames_for_row(
                    rows,
                    segment_row_index,
                    meta_dir,
                    config,
                )
            except ValueError as error:
                reject_reason = str(error)

        if reject_reason is None:
            assert route is not None and projection is not None and grid_pose is not None
            candidate = Candidate(
                sub_task_id=sub_task_id,
                source_sub_task_id=source_sub_task_id,
                map_segment_index=segment.segment_index,
                row_index=row_index,
                meta_ts=float(row["ts"]),
                meta_ts_ms=int(row.get("ts_ms") or round(float(row["ts"]) * 1000.0)),
                map_name=map_name,
                graph_name=str(config["map_mapping"][map_name]),
                grid_path=grid_path,
                grid_pose=grid_pose,
                route=route,
                projection=projection,
                camera_matches=camera_matches,
                history_frames=history_frames,
                rgb_history_matches=rgb_history_matches,
            )
            candidates.append(candidate)
            audit.update(
                {
                    "status": "eligible",
                    "reject_reason": None,
                    "route_id": route.route_id,
                    "route_projection_distance_m": projection.distance_m,
                    "route_remaining_m": projection.remaining_m,
                    "camera_matches": {
                        key: asdict(value) for key, value in camera_matches.items()
                    },
                    "teacher_history_count": len(history_frames),
                    "teacher_history_window_s": (
                        float(row["ts"]) - history_frames[0].meta_ts
                    ),
                    "rgb_history_counts": {
                        key: len(value) for key, value in rgb_history_matches.items()
                    },
                }
            )
        else:
            audit.update({"status": "rejected", "reject_reason": reject_reason})
        audits.append(audit)
    return SubtaskContext(
        sub_task_id=sub_task_id,
        source_sub_task_id=source_sub_task_id,
        suffix=_subtask_suffix(source_sub_task_id),
        map_name=map_name,
        map_name_source=segment.map_name_source,
        map_segment_index=segment.segment_index,
        source_row_start=segment.start_index,
        source_row_stop=segment.stop_index,
        video_window_source=video_window_source,
        camera_ids=tuple(video_paths),
        meta_dir=meta_dir,
        video_dir=video_dir,
        rows=rows,
        routes=routes,
        candidates=sorted(candidates, key=lambda item: item.meta_ts),
        audits=audits,
        keep_interval=(keep_start, keep_end),
        trim_summary=trim_summary,
        pose_jump=pose_jump_document,
    )


def build_subtask_contexts(
    meta_dir: Path,
    video_dir: Path,
    config: Mapping[str, Any],
) -> list[SubtaskContext]:
    source_sub_task_id = meta_dir.name.removeprefix("meta_")
    rows = [
        json.loads(line)
        for line in (meta_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError(f"empty frames.jsonl: {meta_dir}")
    segments = split_rows_by_map(rows, meta_dir, config)
    (
        windows,
        video_window_source,
        _camera_ids,
        video_paths,
        pts_by_camera,
    ) = _load_video_alignment(source_sub_task_id, video_dir, config)
    return [
        _build_map_segment_context(
            meta_dir=meta_dir,
            video_dir=video_dir,
            source_sub_task_id=source_sub_task_id,
            source_rows=rows,
            segment=segment,
            segment_count=len(segments),
            windows=windows,
            video_window_source=video_window_source,
            video_paths=video_paths,
            pts_by_camera=pts_by_camera,
            config=config,
        )
        for segment in segments
    ]


def build_subtask_context(
    meta_dir: Path,
    video_dir: Path,
    config: Mapping[str, Any],
) -> SubtaskContext:
    contexts = build_subtask_contexts(meta_dir, video_dir, config)
    if len(contexts) != 1:
        raise ValueError(
            f"{meta_dir.name} contains {len(contexts)} map segments; "
            "use build_subtask_contexts"
        )
    return contexts[0]


def discover_contexts(task_root: Path, config: Mapping[str, Any]) -> list[SubtaskContext]:
    meta_root = task_root / "meta" / "unpacked"
    video_root = task_root / "videos"
    contexts = []
    for meta_dir in sorted(meta_root.glob("meta_*")):
        if not (meta_dir / "frames.jsonl").is_file():
            continue
        sub_task_id = meta_dir.name.removeprefix("meta_")
        video_dir = video_root / f"videos_{sub_task_id}"
        if not video_dir.is_dir():
            raise FileNotFoundError(f"video directory is missing: {video_dir}")
        contexts.extend(build_subtask_contexts(meta_dir, video_dir, config))
    if not contexts:
        raise FileNotFoundError(f"no subtasks found below {task_root}")
    return contexts


def _route_document(route: RouteData, config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "route_id": route.route_id,
        "sub_task_id": route.sub_task_id,
        "segment_index": route.segment_index,
        "start_ts": route.start_ts,
        "end_ts": route.end_ts,
        "length_m": route.length_m,
        "dense_point_count": len(route.dense_points),
        "sparse_point_count": len(route.sparse_points),
        "dense_points": route.dense_points,
        "sparse_points": route.sparse_points,
        "max_lateral_error_m": route.max_lateral_error_m,
        "max_tangent_error_deg": route.max_tangent_error_deg,
        "route_version": config["route_version"],
        "parameters": {
            "rdp_epsilon_m": config["route_rdp_epsilon_m"],
            "tangent_error_max_deg": config["route_tangent_error_max_deg"],
            "turn_heading_change_deg": config["turn_heading_change_deg"],
            "turn_spacing_m": config["turn_spacing_m"],
            "straight_spacing_m": config["straight_spacing_m"],
        },
    }


def _pilot_targets(
    contexts: Sequence[SubtaskContext], config: Mapping[str, Any]
) -> list[tuple[SubtaskContext, float]]:
    by_suffix = {context.suffix: context for context in contexts}
    output: list[tuple[SubtaskContext, float]] = []
    for suffix, count_value in dict(config["pilot_case_counts"]).items():
        context = by_suffix.get(str(suffix))
        if context is None:
            raise ValueError(f"pilot subtask _{suffix} is missing")
        if not context.candidates:
            raise ValueError(f"pilot subtask {context.sub_task_id} has no eligible candidates")
        count = int(count_value)
        start = context.candidates[0].meta_ts
        stop = context.candidates[-1].meta_ts
        for index in range(count):
            fraction = (index + 0.5) / count
            output.append((context, start + fraction * (stop - start)))
    return output


def _ordered_candidates(
    context: SubtaskContext, target_ts: float, used: set[tuple[str, int]]
) -> list[Candidate]:
    return sorted(
        (
            candidate
            for candidate in context.candidates
            if (candidate.sub_task_id, candidate.row_index) not in used
        ),
        key=lambda candidate: (abs(candidate.meta_ts - target_ts), candidate.meta_ts),
    )


def _validate_commands(
    commands: Sequence[Mapping[str, float]], config: Mapping[str, Any]
) -> str | None:
    for command in commands:
        values = (
            float(command["linear_mps"]),
            float(command["angular_rps"]),
            float(command["duration_s"]),
        )
        if not all(math.isfinite(value) for value in values):
            return "teacher_output_nonfinite"
        if values[2] <= 0.0:
            return "teacher_output_out_of_bounds"
        if abs(values[0]) > float(config["max_linear_mps"]) + 1.0e-6:
            return "teacher_output_out_of_bounds"
        if abs(values[1]) > float(config["max_angular_rps"]) + 1.0e-6:
            return "teacher_output_out_of_bounds"
    return None


def _label_base_document(candidate: Candidate) -> dict[str, Any]:
    return {
        "sub_task_id": candidate.sub_task_id,
        "source_sub_task_id": candidate.source_sub_task_id,
        "map_segment_index": candidate.map_segment_index,
        "row_index": candidate.row_index,
        "meta_ts": candidate.meta_ts,
        "meta_ts_ms": candidate.meta_ts_ms,
        "map_name": candidate.map_name,
        "graph_name": candidate.graph_name,
        "route_id": candidate.route.route_id,
    }


def _rejected_label(
    candidate: Candidate,
    reason: str,
    error: BaseException | None = None,
) -> LabelResult:
    document = {
        **_label_base_document(candidate),
        "status": "rejected",
        "reject_reason": reason,
    }
    if error is not None:
        document["error"] = f"{type(error).__name__}: {error}"
    return LabelResult(
        candidate=candidate,
        status="rejected",
        reject_reason=reason,
        document=document,
    )


def _prepare_label(
    candidate: Candidate,
    runtime: TeacherRuntime,
    config: Mapping[str, Any],
) -> PreparedLabel:
    source_grid, raw_occupancy = load_teacher_occupancy(candidate.grid_path)
    if raw_occupancy.shape != (200, 200):
        raise ValueError("grid_schema_mismatch")
    static_map = runtime.static_maps.get(candidate.map_name)
    if static_map is None:
        raise ValueError("teacher_map_unsupported")
    static_occupancy, world_to_pixel = crop_static_map(
        static_map,
        candidate.grid_pose,
        size=int(config["static_crop_size"]),
        resolution_m=float(config["static_crop_resolution_m"]),
    )
    occupancy, static_local_occupancy = fuse_local_static_occupancy(
        raw_occupancy,
        static_occupancy,
    )
    indoor_decision = runtime.classify_indoor_pose(
        candidate.graph_name, candidate.grid_pose[:2]
    )
    curvature = estimate_route_curvature(
        candidate.route.sparse_points,
        candidate.projection,
        lookahead_m=float(config["curvature_lookahead_m"]),
        limit=float(config["curvature_limit"]),
    )
    stamped_poses = [
        (frame.meta_ts, *frame.grid_pose) for frame in candidate.history_frames
    ] + [(candidate.meta_ts, *candidate.grid_pose)]
    motion_estimate = estimate_pose_velocity(
        stamped_poses,
        window_s=float(config["actual_velocity_window_s"]),
        max_linear_mps=float(
            config[
                "indoor_max_linear_mps"
                if indoor_decision.indoor
                else "outdoor_max_linear_mps"
            ]
        ),
        max_angular_rps=float(config["max_angular_rps"]),
    )
    initial_state = sample_initial_state(
        dataset_id=candidate.sub_task_id,
        meta_ts=candidate.meta_ts,
        teacher_version=runtime.teacher_version,
        curvature=curvature,
        indoor=bool(indoor_decision.indoor),
        motion_estimate=motion_estimate,
        config=config,
    )

    history_stamps = np.asarray(
        [frame.meta_ts for frame in candidate.history_frames], dtype=np.float64
    )
    if initial_state["observation_mode"] == "static_repeat":
        history_occupancies = np.repeat(
            occupancy[None, :, :], len(candidate.history_frames), axis=0
        )
        history_poses = np.repeat(
            np.asarray(candidate.grid_pose, dtype=np.float64)[None, :],
            len(candidate.history_frames),
            axis=0,
        )
    else:
        occupancy_values = []
        pose_values = []
        for frame in candidate.history_frames:
            occupancy_values.append(runtime.fused_history_occupancy(frame, static_map))
            pose_values.append(frame.grid_pose)
        history_occupancies = np.stack(occupancy_values).astype(np.int8, copy=False)
        history_poses = np.asarray(pose_values, dtype=np.float64)

    raw_obstacle_count = int(np.count_nonzero(raw_occupancy))
    static_local_obstacle_count = int(np.count_nonzero(static_local_occupancy))
    fused_obstacle_count = int(np.count_nonzero(occupancy))
    added_obstacle_count = int(
        np.count_nonzero((raw_occupancy == 0) & (static_local_occupancy != 0))
    )
    route_mask = build_route_mask(
        candidate.route.sparse_points,
        candidate.grid_pose,
        candidate.projection,
        size=int(config["static_crop_size"]),
        resolution_m=float(config["static_crop_resolution_m"]),
        width_cells=int(config["route_width_cells"]),
    )
    return PreparedLabel(
        candidate=candidate,
        base_document=_label_base_document(candidate),
        source_grid=source_grid,
        raw_local_occupancy=raw_occupancy,
        teacher_occupancy=occupancy,
        static_occupancy=static_occupancy,
        static_local_occupancy=static_local_occupancy,
        route_mask=route_mask,
        world_to_pixel=world_to_pixel,
        forward_route=forward_route(candidate.route.sparse_points, candidate.projection),
        initial_state=initial_state,
        history_occupancies=history_occupancies,
        history_poses=history_poses,
        history_stamps=history_stamps,
        occupancy_fusion={
            "version": config["occupancy_fusion_version"],
            "operation": "obstacle_union_current_and_history",
            "local_shape": list(occupancy.shape),
            "static_crop_shape": list(static_occupancy.shape),
            "raw_local_obstacle_cells": raw_obstacle_count,
            "static_obstacle_cells_in_local_extent": static_local_obstacle_count,
            "static_only_added_obstacle_cells": added_obstacle_count,
            "fused_local_obstacle_cells": fused_obstacle_count,
            "fused_local_occupancy_sha256": hashlib.sha256(
                np.ascontiguousarray(occupancy).tobytes()
            ).hexdigest(),
            "history_count": len(history_stamps),
        },
        static_map=static_map,
    )


def _finalize_label(
    prepared: PreparedLabel,
    runtime: TeacherRuntime,
    config: Mapping[str, Any],
    inference: tuple[list[dict[str, float]], dict[str, Any], str, str],
    determinism: Mapping[str, Any],
) -> LabelResult:
    candidate = prepared.candidate
    commands, response, request_hash, sample_content_hash = inference
    reject_reason = _validate_commands(commands, config)
    try:
        rollout = integrate_commands(commands, dt_s=float(config["rollout_dt_s"]))
        collision, collision_index = rollout_collision(
            rollout,
            prepared.teacher_occupancy,
            prepared.static_occupancy,
            resolution_m=float(config["static_crop_resolution_m"]),
            footprint_length_m=float(config["footprint_length_m"]),
            footprint_width_m=float(config["footprint_width_m"]),
        )
        if collision and reject_reason is None:
            reject_reason = "teacher_rollout_collision"
        diagnostics = dict(response.get("diagnostics") or {})
        diagnostics.setdefault("inference_ms", response.get("batch_inference_ms"))
        diagnostics["batch_inference_ms"] = response.get("batch_inference_ms")
        document = {
            **prepared.base_document,
            "status": "accepted" if reject_reason is None else "rejected",
            "reject_reason": reject_reason,
            "case_id": None,
            "grid_path": str(candidate.grid_path),
            "grid_pose": {
                "x": candidate.grid_pose[0],
                "y": candidate.grid_pose[1],
                "yaw_rad": candidate.grid_pose[2],
            },
            "camera_matches": {
                key: asdict(value) for key, value in candidate.camera_matches.items()
            },
            "rgb_history_source": {
                key: [asdict(item) for item in values]
                for key, values in candidate.rgb_history_matches.items()
            },
            "route_projection": asdict(candidate.projection),
            "occupancy_fusion": prepared.occupancy_fusion,
            "initial_state": prepared.initial_state,
            "teacher_history": {
                "observation_mode": prepared.initial_state["observation_mode"],
                "provided_count": len(prepared.history_stamps),
                "retention_s": float(config["teacher_history_retention_s"]),
                "stamps_s": prepared.history_stamps,
                "ages_s": candidate.meta_ts - prepared.history_stamps,
                "source_row_indices": [
                    frame.row_index for frame in candidate.history_frames
                ],
                "occupancy_sha256": [
                    hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()
                    for value in prepared.history_occupancies
                ],
                "poses": prepared.history_poses,
            },
            "teacher": {
                "url": runtime.url,
                "model_id": response.get("model_id") or runtime.model_id,
                "model_profile": response.get("model_profile") or "rule10",
                "batch_request_id": response.get("batch_request_id"),
                "batch_sample_count": response.get("sample_count"),
                "forward_passes": response.get("forward_passes"),
                "batch_inference_ms": response.get("batch_inference_ms"),
                "commands": commands,
                "diagnostics": diagnostics,
                "request_input_sha256": request_hash,
                "sample_content_sha256": sample_content_hash,
                "determinism": dict(determinism),
            },
            "provenance": {
                "pipeline_version": config["pipeline_version"],
                "route_version": config["route_version"],
                "occupancy_fusion_version": config["occupancy_fusion_version"],
                "teacher_version": runtime.teacher_version,
                "teacher_health": runtime.health,
                "teacher_config": str(runtime.config_path),
                "teacher_config_sha256": runtime.config_sha256,
                "checkpoint": str(runtime.checkpoint_path),
                "checkpoint_sha256": runtime.checkpoint_sha256,
                "static_map": str(prepared.static_map.path),
                "static_map_sha256": prepared.static_map.sha256,
            },
            "rollout": {
                "collision": collision,
                "collision_state_index": collision_index,
                "state_count": len(rollout),
                "duration_s": float(rollout[-1, 3]),
            },
            "relative_action": [
                {
                    "delta_v_mps": float(item["linear_mps"])
                    - float(prepared.initial_state["initial_linear_mps"]),
                    "delta_w_rps": float(item["angular_rps"])
                    - float(prepared.initial_state["initial_angular_rps"]),
                    "duration_s": float(item["duration_s"]),
                }
                for item in commands
            ],
        }
        return LabelResult(
            candidate=candidate,
            status=document["status"],
            reject_reason=reject_reason,
            document=document,
            source_grid=prepared.source_grid,
            raw_local_occupancy=prepared.raw_local_occupancy,
            teacher_occupancy=prepared.teacher_occupancy,
            static_occupancy=prepared.static_occupancy,
            static_local_occupancy=prepared.static_local_occupancy,
            route_mask=prepared.route_mask,
            world_to_pixel=prepared.world_to_pixel,
            forward_route=prepared.forward_route,
            rollout=rollout,
            history_occupancies=prepared.history_occupancies,
            history_poses=prepared.history_poses,
            history_stamps=prepared.history_stamps,
        )
    except Exception as error:
        return _rejected_label(candidate, "teacher_output_processing_failed", error)


def label_candidates(
    candidates: Sequence[Candidate],
    runtime: TeacherRuntime,
    config: Mapping[str, Any],
    *,
    run_id: str,
    verify_determinism: bool,
) -> list[LabelResult]:
    results: list[LabelResult | None] = [None] * len(candidates)
    prepared_values: list[PreparedLabel] = []
    prepared_indices: list[int] = []
    known_reasons = {
        "grid_schema_mismatch",
        "occupancy_schema_mismatch",
        "teacher_map_unsupported",
        "route_behind_only",
        "velocity_history_insufficient",
        "velocity_history_invalid",
    }
    for index, candidate in enumerate(candidates):
        try:
            prepared_values.append(_prepare_label(candidate, runtime, config))
            prepared_indices.append(index)
        except Exception as error:
            reason = str(error) if str(error) in known_reasons else "teacher_preprocessing_failed"
            results[index] = _rejected_label(candidate, reason, error)

    if prepared_values:
        try:
            inference_values = runtime.infer_batch(prepared_values, run_id=run_id)
        except Exception:
            inference_values = []
            for prepared in prepared_values:
                try:
                    inference_values.append(
                        runtime.infer_batch((prepared,), run_id=run_id)[0]
                    )
                except Exception as error:
                    inference_values.append(error)

        repeated_values: Sequence[Any] = ()
        if verify_determinism:
            try:
                repeated_values = runtime.infer_batch(prepared_values, run_id=run_id)
            except Exception:
                repeated_values = (None,) * len(prepared_values)
        for local_index, (result_index, prepared, inference) in enumerate(
            zip(prepared_indices, prepared_values, inference_values)
        ):
            if isinstance(inference, BaseException):
                results[result_index] = _rejected_label(
                    prepared.candidate, "teacher_request_failed", inference
                )
                continue
            determinism: dict[str, Any] = {"checked": False, "match": None}
            if verify_determinism:
                repeated = repeated_values[local_index]
                if repeated is None:
                    match = False
                else:
                    first_commands, _first_response, first_hash, first_content = inference
                    repeat_commands, _repeat_response, repeat_hash, repeat_content = repeated
                    first = np.asarray(
                        [
                            (item["linear_mps"], item["angular_rps"], item["duration_s"])
                            for item in first_commands
                        ]
                    )
                    second = np.asarray(
                        [
                            (item["linear_mps"], item["angular_rps"], item["duration_s"])
                            for item in repeat_commands
                        ]
                    )
                    match = bool(
                        first.shape == second.shape
                        and np.allclose(first, second, atol=1.0e-7, rtol=0.0)
                        and first_hash == repeat_hash
                        and first_content == repeat_content
                    )
                determinism = {"checked": True, "match": match}
                if not match:
                    results[result_index] = _rejected_label(
                        prepared.candidate, "teacher_output_nondeterministic"
                    )
                    continue
            results[result_index] = _finalize_label(
                prepared, runtime, config, inference, determinism
            )
    return [value for value in results if value is not None]


def label_candidate(
    candidate: Candidate,
    runtime: TeacherRuntime,
    config: Mapping[str, Any],
    *,
    run_id: str,
    verify_determinism: bool,
) -> LabelResult:
    return label_candidates(
        (candidate,),
        runtime,
        config,
        run_id=run_id,
        verify_determinism=verify_determinism,
    )[0]


def _ordered_sample_camera_ids(
    sample: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[str, ...]:
    matches = sample.get("camera_matches") or {}
    available = {str(value) for value in matches}
    recommended = tuple(str(value) for value in config["camera_ids"])
    return tuple(value for value in recommended if value in available) + tuple(
        sorted(value for value in available if value not in recommended)
    )


def _materialize_case(
    run_dir: Path,
    result: LabelResult,
    case_id: str,
    config: Mapping[str, Any],
    *,
    include_preview: bool,
) -> dict[str, Any]:
    if result.status != "accepted":
        raise ValueError("cannot materialize a rejected label")
    assert result.source_grid is not None
    assert result.raw_local_occupancy is not None
    assert result.teacher_occupancy is not None
    assert result.static_occupancy is not None
    assert result.static_local_occupancy is not None
    assert result.route_mask is not None
    assert result.world_to_pixel is not None
    assert result.forward_route is not None
    assert result.rollout is not None
    assert result.history_occupancies is not None
    assert result.history_poses is not None
    assert result.history_stamps is not None
    case_dir = run_dir / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    Image.fromarray(result.source_grid).save(case_dir / "grid.png")
    fused_grid = np.where(result.teacher_occupancy != 0, 0, 255).astype(np.uint8)
    Image.fromarray(fused_grid).save(case_dir / "fused_grid.png")
    save_npz_atomic(
        case_dir / "inputs.npz",
        raw_local_occupancy=result.raw_local_occupancy,
        local_occupancy=result.teacher_occupancy,
        static_occupancy=result.static_occupancy,
        static_local_occupancy=result.static_local_occupancy,
        forward_route_mask=result.route_mask,
        world_to_pixel=result.world_to_pixel,
        sparse_route=result.candidate.route.sparse_points,
        forward_route=result.forward_route,
        teacher_rollout=result.rollout,
        teacher_history_occupancy=result.history_occupancies,
        teacher_history_pose=result.history_poses,
        teacher_history_stamp_s=result.history_stamps,
    )
    document = dict(result.document)
    document["case_id"] = case_id
    artifacts = {
        "case_dir": str(case_dir),
        "grid_png": str(case_dir / "grid.png"),
        "fused_grid_png": str(case_dir / "fused_grid.png"),
        "inputs_npz": str(case_dir / "inputs.npz"),
        "camera_pngs": {
            camera_id: str(case_dir / f"{camera_id}.png")
            for camera_id in result.candidate.camera_matches
        },
    }
    if include_preview:
        artifacts["preview_png"] = str(case_dir / "preview.png")
    document["artifacts"] = artifacts
    write_json(case_dir / "sample.json", document)
    return document


def _decode_case_cameras(
    run_dir: Path,
    samples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> None:
    grouped: dict[str, dict[int, Path]] = {}
    shared_paths: dict[tuple[str, str, int], Path] = {}
    for sample in samples:
        for camera_id in _ordered_sample_camera_ids(sample, config):
            current = sample["camera_matches"][camera_id]
            values = list(sample["rgb_history_source"][camera_id])
            values.append(current)
            for value in values:
                video_path = str(value["video_path"])
                frame_index = int(value["frame_index"])
                key = (str(sample["source_sub_task_id"]), camera_id, frame_index)
                target = shared_paths.get(key)
                if target is None:
                    target = (
                        run_dir
                        / "rgb_frames"
                        / str(sample["source_sub_task_id"])
                        / camera_id
                        / f"{frame_index:08d}.png"
                    )
                    shared_paths[key] = target
                    grouped.setdefault(video_path, {})[frame_index] = target

    for video_path, selected in grouped.items():
        decode_selected_frames(Path(video_path), selected)

    repeat_count = int(
        round(float(config["rgb_history_window_s"]) * float(config["rgb_history_max_hz"]))
    ) + 1
    repeat_offsets = np.linspace(
        -float(config["rgb_history_window_s"]), 0.0, repeat_count
    )
    for sample in samples:
        case_id = str(sample["case_id"])
        observation_mode = str(sample["initial_state"]["observation_mode"])
        camera_documents = {}
        for camera_id in _ordered_sample_camera_ids(sample, config):
            current = sample["camera_matches"][camera_id]
            current_key = (
                str(sample["source_sub_task_id"]),
                camera_id,
                int(current["frame_index"]),
            )
            shared_current = shared_paths[current_key]
            case_current = run_dir / "cases" / case_id / f"{camera_id}.png"
            case_current.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(shared_current, case_current)
            except OSError:
                shutil.copy2(shared_current, case_current)
            if observation_mode == "static_repeat":
                relative = str(case_current.relative_to(run_dir))
                entries = [
                    {
                        "filename": relative,
                        "offset_s": round(float(offset), 6),
                        "frame_index": int(current["frame_index"]),
                        "pts_s": float(current["pts_s"]),
                        "repeated_source": True,
                    }
                    for offset in repeat_offsets
                ]
            else:
                entries = []
                for value in sample["rgb_history_source"][camera_id]:
                    key = (
                        str(sample["source_sub_task_id"]),
                        camera_id,
                        int(value["frame_index"]),
                    )
                    entries.append(
                        {
                            "filename": str(shared_paths[key].relative_to(run_dir)),
                            "offset_s": round(float(value["offset_s"]), 6),
                            "frame_index": int(value["frame_index"]),
                            "pts_s": float(value["pts_s"]),
                            "repeated_source": False,
                        }
                    )
            camera_documents[camera_id] = entries
        sample["rgb_history"] = {
            "window_s": float(config["rgb_history_window_s"]),
            "max_hz": float(config["rgb_history_max_hz"]),
            "observation_mode": observation_mode,
            "cameras": camera_documents,
        }


def _render_pilot_cases(
    run_dir: Path,
    samples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> None:
    for sample in samples:
        case_dir = run_dir / "cases" / str(sample["case_id"])
        with np.load(case_dir / "inputs.npz", allow_pickle=False) as archive:
            raw_local_occupancy = np.asarray(archive["raw_local_occupancy"])
            fused_local_occupancy = np.asarray(archive["local_occupancy"])
            static_occupancy = np.asarray(archive["static_occupancy"])
            route_mask = np.asarray(archive["forward_route_mask"])
            rollout = np.asarray(archive["teacher_rollout"])
        render_case(
            case_dir,
            sample,
            camera_names=config["camera_names"],
            raw_local_occupancy=raw_local_occupancy,
            fused_local_occupancy=fused_local_occupancy,
            static_occupancy=static_occupancy,
            route_mask=route_mask,
            rollout=rollout,
            resolution_m=float(config["static_crop_resolution_m"]),
        )
    render_overview(run_dir, samples, columns=3)


def _context_summary(context: SubtaskContext) -> dict[str, Any]:
    rejected: dict[str, int] = {}
    for audit in context.audits:
        reason = audit.get("reject_reason")
        if reason:
            rejected[str(reason)] = rejected.get(str(reason), 0) + 1
    return {
        "sub_task_id": context.sub_task_id,
        "source_sub_task_id": context.source_sub_task_id,
        "map_name": context.map_name,
        "map_name_source": context.map_name_source,
        "map_segment_index": context.map_segment_index,
        "source_row_range": [context.source_row_start, context.source_row_stop],
        "video_window_source": context.video_window_source,
        "camera_ids": list(context.camera_ids),
        "camera_count": len(context.camera_ids),
        "map_segment_time_range": [
            float(context.rows[0]["ts"]),
            float(context.rows[-1]["ts"]),
        ],
        "row_count": len(context.rows),
        "route_count": len(context.routes),
        "eligible_count": len(context.candidates),
        "keep_interval": list(context.keep_interval),
        "trim_summary": context.trim_summary,
        "pose_jump": context.pose_jump,
        "reject_counts": rejected,
    }


def _camera_set_warnings(
    contexts: Sequence[SubtaskContext],
    config: Mapping[str, Any],
) -> list[str]:
    recommended = tuple(str(value) for value in config["camera_ids"])
    by_source = {
        context.source_sub_task_id: context.camera_ids for context in contexts
    }
    return [
        "video_camera_set_nonstandard:%s:actual=%s:recommended=%s"
        % (
            source_sub_task_id,
            ",".join(actual),
            ",".join(recommended),
        )
        for source_sub_task_id, actual in sorted(by_source.items())
        if actual != recommended
    ]


def _prepare_run_directory(final_dir: Path) -> tuple[Path, Path]:
    if final_dir.exists():
        raise FileExistsError(f"output already exists and will not be overwritten: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_dir.with_name(f".{final_dir.name}.in_progress_{os.getpid()}")
    if temporary.exists():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    temporary.mkdir(parents=True)
    return temporary, final_dir


def _finalize_run(temporary: Path, final_dir: Path) -> Path:
    if final_dir.exists():
        raise FileExistsError(f"cannot finalize over existing output: {final_dir}")
    temporary.replace(final_dir)
    return final_dir


def _rewrite_artifact_paths(document: Any, temporary: Path, final_dir: Path) -> Any:
    temporary_text = str(temporary)
    final_text = str(final_dir)
    if isinstance(document, str):
        return document.replace(temporary_text, final_text)
    if isinstance(document, list):
        return [_rewrite_artifact_paths(item, temporary, final_dir) for item in document]
    if isinstance(document, dict):
        return {
            key: _rewrite_artifact_paths(value, temporary, final_dir)
            for key, value in document.items()
        }
    return document


def _persist_manifests(
    run_dir: Path,
    contexts: Sequence[SubtaskContext],
    attempts: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    run_document: Mapping[str, Any],
) -> None:
    selected_lookup = {
        (str(sample["sub_task_id"]), int(sample["row_index"])): str(sample["case_id"])
        for sample in samples
    }
    source_documents = []
    for context in contexts:
        for audit in context.audits:
            document = dict(audit)
            case_id = selected_lookup.get((context.sub_task_id, int(audit["row_index"])))
            document["selected_for_run"] = case_id is not None
            document["case_id"] = case_id
            source_documents.append(document)
    write_jsonl(run_dir / "source_manifest.jsonl", source_documents)
    write_jsonl(
        run_dir / "routes.jsonl",
        (
            _route_document(route, config)
            for context in contexts
            for route in context.routes
        ),
    )
    write_jsonl(run_dir / "teacher_labels.jsonl", attempts)
    write_jsonl(
        run_dir / "rgb_history_manifest.jsonl",
        (
            {
                "case_id": sample["case_id"],
                "sub_task_id": sample["sub_task_id"],
                "row_index": sample["row_index"],
                "meta_ts": sample["meta_ts"],
                "initial_state": sample["initial_state"],
                "rgb_history": sample["rgb_history"],
            }
            for sample in samples
        ),
    )
    write_json(run_dir / "run.json", run_document)


def run_pilot(
    *,
    task_root: Path,
    output_root: Path,
    revision: int,
    teacher_url: str,
    model_server_root: Path,
    teacher_config: Path,
    static_map_root: Path,
    config: Mapping[str, Any],
) -> Path:
    task_id = task_root.name
    final_dir = (
        output_root
        / task_id
        / str(config["pipeline_version"])
        / "pilot"
        / f"review_{int(revision):03d}"
    )
    temporary, final_dir = _prepare_run_directory(final_dir)
    started = time.time()
    run_id = f"{task_id}:pilot:review_{int(revision):03d}"
    try:
        contexts = discover_contexts(task_root, config)
        runtime = TeacherRuntime(
            url=teacher_url,
            model_server_root=model_server_root,
            config_path=teacher_config,
            map_root=static_map_root,
            config=config,
        )
        targets = _pilot_targets(contexts, config)
        used: set[tuple[str, int]] = set()
        accepted_results: list[LabelResult] = []
        attempts: list[dict[str, Any]] = []
        for target_index, (context, target_ts) in enumerate(targets, start=1):
            accepted = None
            for candidate in _ordered_candidates(context, target_ts, used)[:100]:
                result = label_candidate(
                    candidate,
                    runtime,
                    config,
                    run_id=run_id,
                    verify_determinism=True,
                )
                attempt = dict(result.document)
                attempt["pilot_target_ts"] = target_ts
                attempt["pilot_target_index"] = target_index
                attempts.append(attempt)
                used.add((candidate.sub_task_id, candidate.row_index))
                if result.status == "accepted":
                    accepted = result
                    break
            if accepted is None:
                raise RuntimeError(
                    f"no accepted pilot case near target {target_index} "
                    f"({context.sub_task_id}, {target_ts:.3f})"
                )
            accepted_results.append(accepted)

        samples = []
        for index, result in enumerate(accepted_results, start=1):
            case_id = f"case_{index:03d}"
            sample = _materialize_case(
                temporary, result, case_id, config, include_preview=True
            )
            samples.append(sample)
            for attempt in reversed(attempts):
                if (
                    attempt.get("sub_task_id") == sample["sub_task_id"]
                    and int(attempt.get("row_index", -1)) == int(sample["row_index"])
                ):
                    attempt["case_id"] = case_id
                    break
        _decode_case_cameras(temporary, samples, config)
        _render_pilot_cases(temporary, samples, config)
        reject_counts: dict[str, int] = {}
        for attempt in attempts:
            reason = attempt.get("reject_reason")
            if reason:
                reject_counts[str(reason)] = reject_counts.get(str(reason), 0) + 1
        run_document = {
            "run_id": run_id,
            "mode": "pilot",
            "review_revision": int(revision),
            "status": "success",
            "task_root": str(task_root),
            "output_dir": str(final_dir),
            "pipeline_version": config["pipeline_version"],
            "route_version": config["route_version"],
            "state_sampler_version": config["state_sampler_version"],
            "occupancy_fusion_version": config["occupancy_fusion_version"],
            "case_count": len(samples),
            "attempt_count": len(attempts),
            "teacher_attempt_reject_counts": reject_counts,
            "subtasks": [_context_summary(context) for context in contexts],
            "warnings": _camera_set_warnings(contexts, config),
            "teacher_health": runtime.health,
            "teacher_config_sha256": runtime.config_sha256,
            "checkpoint_sha256": runtime.checkpoint_sha256,
            "teacher_history_cache": runtime.history_cache_stats(),
            "overview_png": str(final_dir / "overview.png"),
            "started_at": datetime.fromtimestamp(started).astimezone().isoformat(),
            "completed_at": datetime.now().astimezone().isoformat(),
            "elapsed_s": round(time.time() - started, 3),
            "full_generation_authorized": False,
        }
        rewritten_samples = _rewrite_artifact_paths(samples, temporary, final_dir)
        rewritten_attempts = _rewrite_artifact_paths(attempts, temporary, final_dir)
        _persist_manifests(
            temporary,
            contexts,
            rewritten_attempts,
            rewritten_samples,
            config,
            run_document,
        )
        for sample in rewritten_samples:
            write_json(
                temporary / "cases" / str(sample["case_id"]) / "sample.json",
                sample,
            )
        return _finalize_run(temporary, final_dir)
    except Exception:
        failure = {
            "run_id": run_id,
            "mode": "pilot",
            "status": "failed",
            "task_root": str(task_root),
            "started_at": datetime.fromtimestamp(started).astimezone().isoformat(),
            "failed_at": datetime.now().astimezone().isoformat(),
        }
        write_json(temporary / "run_failed.json", failure)
        raise


def run_full(
    *,
    task_root: Path,
    output_root: Path,
    confirmation: str,
    teacher_url: str,
    model_server_root: Path,
    teacher_config: Path,
    static_map_root: Path,
    config: Mapping[str, Any],
) -> Path:
    if confirmation != "SATISFIED":
        raise ValueError("full mode requires --confirm-full SATISFIED")
    task_id = task_root.name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_dir = (
        output_root
        / task_id
        / str(config["pipeline_version"])
        / "full"
        / f"run_{timestamp}"
    )
    temporary, final_dir = _prepare_run_directory(final_dir)
    started = time.time()
    run_id = f"{task_id}:full:{timestamp}"
    try:
        contexts = discover_contexts(task_root, config)
        candidates = [
            candidate
            for context in contexts
            for candidate in sorted(context.candidates, key=lambda item: item.meta_ts)
        ]
        if not candidates:
            raise RuntimeError(f"no eligible candidates below {task_root}")
        runtime = TeacherRuntime(
            url=teacher_url,
            model_server_root=model_server_root,
            config_path=teacher_config,
            map_root=static_map_root,
            config=config,
        )
        attempts: list[dict[str, Any]] = []
        samples: list[dict[str, Any]] = []
        batch_size = int(config["teacher_batch_size"])
        if not 1 <= batch_size <= 64:
            raise ValueError("teacher_batch_size must be within 1..64")
        for start in range(0, len(candidates), batch_size):
            batch_results = label_candidates(
                candidates[start : start + batch_size],
                runtime,
                config,
                run_id=run_id,
                verify_determinism=False,
            )
            for result in batch_results:
                attempts.append(result.document)
                if result.status != "accepted":
                    continue
                case_id = f"sample_{len(samples) + 1:07d}"
                sample = _materialize_case(
                    temporary, result, case_id, config, include_preview=False
                )
                samples.append(sample)
                attempts[-1]["case_id"] = case_id
        _decode_case_cameras(temporary, samples, config)
        run_document = {
            "run_id": run_id,
            "mode": "full",
            "status": "success",
            "task_root": str(task_root),
            "output_dir": str(final_dir),
            "pipeline_version": config["pipeline_version"],
            "route_version": config["route_version"],
            "state_sampler_version": config["state_sampler_version"],
            "occupancy_fusion_version": config["occupancy_fusion_version"],
            "candidate_count": len(candidates),
            "accepted_count": len(samples),
            "rejected_count": len(candidates) - len(samples),
            "subtasks": [_context_summary(context) for context in contexts],
            "warnings": _camera_set_warnings(contexts, config),
            "teacher_health": runtime.health,
            "teacher_config_sha256": runtime.config_sha256,
            "checkpoint_sha256": runtime.checkpoint_sha256,
            "teacher_history_cache": runtime.history_cache_stats(),
            "started_at": datetime.fromtimestamp(started).astimezone().isoformat(),
            "completed_at": datetime.now().astimezone().isoformat(),
            "elapsed_s": round(time.time() - started, 3),
            "full_generation_authorized": True,
        }
        rewritten_samples = _rewrite_artifact_paths(samples, temporary, final_dir)
        rewritten_attempts = _rewrite_artifact_paths(attempts, temporary, final_dir)
        _persist_manifests(
            temporary,
            contexts,
            rewritten_attempts,
            rewritten_samples,
            config,
            run_document,
        )
        for sample in rewritten_samples:
            write_json(
                temporary / "cases" / str(sample["case_id"]) / "sample.json",
                sample,
            )
        return _finalize_run(temporary, final_dir)
    except Exception:
        write_json(
            temporary / "run_failed.json",
            {
                "run_id": run_id,
                "mode": "full",
                "status": "failed",
                "task_root": str(task_root),
                "started_at": datetime.fromtimestamp(started).astimezone().isoformat(),
                "failed_at": datetime.now().astimezone().isoformat(),
            },
        )
        raise
