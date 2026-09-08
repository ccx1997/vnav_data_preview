from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import cv2
import numpy as np
from PIL import Image


PIPELINE_VERSION = "vnav_teacher_rule10_history_v2"
ROUTE_VERSION = "pose_truncate_rdp_turn_v2"
STATE_SAMPLER_VERSION = "pose_velocity_zero20_v2"
OCCUPANCY_FUSION_VERSION = "local_static_obstacle_union_history_v2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(document: Mapping[str, Any]) -> str:
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def wrap_angle(angle: float) -> float:
    return math.atan2(math.sin(float(angle)), math.cos(float(angle)))


def angle_delta(first: float, second: float) -> float:
    return wrap_angle(float(second) - float(first))


def _polyline_metrics(points: np.ndarray) -> np.ndarray:
    if len(points) < 1:
        return np.empty(0, dtype=np.float64)
    return np.r_[
        0.0,
        np.cumsum(np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1)),
    ]


@dataclass(frozen=True)
class RouteProjection:
    progress_m: float
    remaining_m: float
    distance_m: float
    segment_index: int
    point: tuple[float, float]


@dataclass(frozen=True)
class RouteData:
    route_id: str
    sub_task_id: str
    segment_index: int
    start_ts: float
    end_ts: float
    dense_points: np.ndarray
    sparse_points: np.ndarray
    metrics: np.ndarray
    max_lateral_error_m: float
    max_tangent_error_deg: float

    @property
    def length_m(self) -> float:
        return float(self.metrics[-1]) if len(self.metrics) else 0.0


@dataclass(frozen=True)
class PoseJump:
    previous_row_index: int
    row_index: int
    previous_ts: float
    jump_ts: float
    dt_s: float
    distance_m: float
    speed_mps: float
    yaw_delta_deg: float
    yaw_rate_rps: float


def project_to_route(point: Sequence[float], route: np.ndarray) -> RouteProjection:
    points = np.asarray(route, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] < 2 or len(points) < 2:
        raise ValueError("route must contain at least two XY points")
    query = np.asarray(point[:2], dtype=np.float64)
    starts = points[:-1, :2]
    vectors = points[1:, :2] - starts
    length_sq = np.einsum("ij,ij->i", vectors, vectors)
    delta = query - starts
    fractions = np.divide(
        np.einsum("ij,ij->i", delta, vectors),
        length_sq,
        out=np.zeros_like(length_sq),
        where=length_sq > 1.0e-12,
    )
    fractions = np.clip(fractions, 0.0, 1.0)
    projected = starts + fractions[:, None] * vectors
    distances = np.linalg.norm(projected - query, axis=1)
    index = int(np.argmin(distances))
    segment_lengths = np.sqrt(length_sq)
    metrics = np.r_[0.0, np.cumsum(segment_lengths)]
    progress = float(metrics[index] + fractions[index] * segment_lengths[index])
    return RouteProjection(
        progress_m=progress,
        remaining_m=max(0.0, float(metrics[-1] - progress)),
        distance_m=float(distances[index]),
        segment_index=index,
        point=(float(projected[index, 0]), float(projected[index, 1])),
    )


def point_at_progress(route: np.ndarray, progress_m: float) -> np.ndarray:
    points = np.asarray(route, dtype=np.float64)
    metrics = _polyline_metrics(points)
    return _point_at_metrics(points, metrics, progress_m)


def _point_at_metrics(
    points: np.ndarray, metrics: np.ndarray, progress_m: float
) -> np.ndarray:
    if len(points) < 2 or metrics[-1] <= 0.0:
        raise ValueError("route must contain two distinct points")
    value = float(np.clip(progress_m, 0.0, metrics[-1]))
    index = min(
        len(points) - 2,
        max(0, int(np.searchsorted(metrics, value, side="right") - 1)),
    )
    length = metrics[index + 1] - metrics[index]
    fraction = 0.0 if length <= 1.0e-12 else (value - metrics[index]) / length
    return points[index, :2] + fraction * (points[index + 1, :2] - points[index, :2])


def densify_route(route: np.ndarray, step_m: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(route, dtype=np.float64)
    metrics = _polyline_metrics(points)
    if len(points) < 2 or metrics[-1] <= 0.0:
        raise ValueError("route must contain two distinct points")
    sample_s = np.arange(0.0, metrics[-1], max(0.01, float(step_m)))
    sample_s = np.r_[sample_s, metrics[-1]]
    sampled = np.column_stack(
        (
            np.interp(sample_s, metrics, points[:, 0]),
            np.interp(sample_s, metrics, points[:, 1]),
        )
    )
    return sampled, sample_s


def forward_route(route: np.ndarray, projection: RouteProjection) -> np.ndarray:
    points = np.asarray(route, dtype=np.float64)
    suffix = np.vstack(
        (
            np.asarray(projection.point, dtype=np.float64),
            points[projection.segment_index + 1 :, :2],
        )
    )
    keep = np.r_[True, np.linalg.norm(np.diff(suffix, axis=0), axis=1) > 1.0e-9]
    suffix = suffix[keep]
    if len(suffix) < 2:
        raise ValueError("route_behind_only")
    return suffix


def _rolling_median(values: np.ndarray, radius: int = 2) -> np.ndarray:
    source = np.asarray(values, dtype=np.float64)
    output = np.empty_like(source)
    for index in range(len(source)):
        start = max(0, index - int(radius))
        stop = min(len(source), index + int(radius) + 1)
        output[index] = np.median(source[start:stop], axis=0)
    return output


def _point_segment_distances(points: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    vector = end - start
    length_sq = float(np.dot(vector, vector))
    if length_sq <= 1.0e-12:
        return np.linalg.norm(points - start, axis=1)
    fraction = np.clip(((points - start) @ vector) / length_sq, 0.0, 1.0)
    projected = start + fraction[:, None] * vector
    return np.linalg.norm(points - projected, axis=1)


def _local_tangent(points: np.ndarray, index: int) -> np.ndarray:
    start = max(0, index - 1)
    stop = min(len(points) - 1, index + 1)
    return points[stop] - points[start]


def _angle_between(first: np.ndarray, second: np.ndarray) -> float:
    first_norm = float(np.linalg.norm(first))
    second_norm = float(np.linalg.norm(second))
    if first_norm <= 1.0e-12 or second_norm <= 1.0e-12:
        return 0.0
    cosine = float(np.clip(np.dot(first, second) / (first_norm * second_norm), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _simplify_indices(
    points: np.ndarray,
    epsilon_m: float,
    tangent_error_deg: float,
) -> list[int]:
    kept: set[int] = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start, stop = stack.pop()
        if stop <= start + 1:
            continue
        chord = points[stop] - points[start]
        internal_indices = np.arange(start + 1, stop)
        internal = points[internal_indices]
        distances = _point_segment_distances(internal, points[start], points[stop])
        angles = np.asarray(
            [_angle_between(_local_tangent(points, int(index)), chord) for index in internal_indices],
            dtype=np.float64,
        )
        distance_score = distances / max(1.0e-9, float(epsilon_m))
        angle_score = angles / max(1.0e-9, float(tangent_error_deg))
        scores = np.maximum(distance_score, angle_score)
        best_local = int(np.argmax(scores))
        if float(scores[best_local]) <= 1.0:
            continue
        split = int(internal_indices[best_local])
        kept.add(split)
        stack.append((start, split))
        stack.append((split, stop))
    return sorted(kept)


def _adaptive_subdivide(
    points: np.ndarray,
    turn_heading_change_deg: float,
    turn_spacing_m: float,
    straight_spacing_m: float,
) -> np.ndarray:
    turn_vertex = np.zeros(len(points), dtype=bool)
    for index in range(1, len(points) - 1):
        before = points[index] - points[index - 1]
        after = points[index + 1] - points[index]
        turn_vertex[index] = _angle_between(before, after) >= float(turn_heading_change_deg)
    output = [points[0]]
    for index, (start, stop) in enumerate(zip(points[:-1], points[1:])):
        distance = float(np.linalg.norm(stop - start))
        turning = bool(turn_vertex[index] or turn_vertex[index + 1])
        spacing = float(turn_spacing_m if turning else straight_spacing_m)
        count = max(1, int(math.ceil(distance / max(1.0e-6, spacing))))
        for part in range(1, count + 1):
            output.append(start + (stop - start) * (part / count))
    return np.asarray(output, dtype=np.float64)


def _route_errors(
    dense: np.ndarray,
    sparse: np.ndarray,
    *,
    tangent_window_m: float,
    end_margin_m: float,
) -> tuple[float, float]:
    """Measure simplification error on the route portion that can yield anchors.

    Lateral error is a global geometric distance.  Tangent error is evaluated
    with a forward-tracked sparse projection so a self-intersection cannot
    select a geometrically coincident but temporally wrong branch.  A local
    metric window suppresses centimetre-scale localization jitter while still
    measuring the route direction seen by the 20 m teacher crop.
    """

    lateral = 0.0
    for point in dense:
        projection = project_to_route(point, sparse)
        lateral = max(lateral, projection.distance_m)

    dense_metrics = _polyline_metrics(dense)
    sparse_metrics = _polyline_metrics(sparse)
    validation_stop = max(0.0, float(dense_metrics[-1]) - max(0.0, end_margin_m))
    if validation_stop <= 0.0:
        return float(lateral), 0.0
    sparse_starts = sparse[:-1]
    sparse_vectors = np.diff(sparse, axis=0)
    sparse_length_sq = np.einsum("ij,ij->i", sparse_vectors, sparse_vectors)
    last_segment = 0
    half_window = 0.5 * max(0.1, float(tangent_window_m))
    tangent = 0.0
    sample_progress = np.arange(0.0, validation_stop, 0.05)
    sample_progress = np.r_[sample_progress, validation_stop]
    for progress in sample_progress:
        point = _point_at_metrics(dense, dense_metrics, float(progress))
        search_start = max(0, last_segment - 2)
        start_progress = sparse_metrics[min(len(sparse_metrics) - 1, last_segment + 1)]
        search_stop = min(
            len(sparse_vectors),
            max(
                search_start + 1,
                int(
                    np.searchsorted(
                        sparse_metrics,
                        start_progress + 3.0,
                        side="right",
                    )
                ),
            ),
        )
        starts = sparse_starts[search_start:search_stop]
        vectors = sparse_vectors[search_start:search_stop]
        lengths_sq = sparse_length_sq[search_start:search_stop]
        fractions = np.divide(
            np.einsum("ij,ij->i", point - starts, vectors),
            lengths_sq,
            out=np.zeros_like(lengths_sq),
            where=lengths_sq > 1.0e-12,
        )
        fractions = np.clip(fractions, 0.0, 1.0)
        projected = starts + fractions[:, None] * vectors
        local_index = int(np.argmin(np.linalg.norm(projected - point, axis=1)))
        segment_index = search_start + local_index
        last_segment = max(last_segment, segment_index)
        sparse_progress = float(
            sparse_metrics[segment_index]
            + fractions[local_index]
            * (sparse_metrics[segment_index + 1] - sparse_metrics[segment_index])
        )
        dense_vector = _point_at_metrics(
            dense,
            dense_metrics,
            min(float(dense_metrics[-1]), float(progress) + half_window),
        ) - _point_at_metrics(
            dense,
            dense_metrics,
            max(0.0, float(progress) - half_window),
        )
        sparse_vector = _point_at_metrics(
            sparse,
            sparse_metrics,
            min(float(sparse_metrics[-1]), sparse_progress + half_window),
        ) - _point_at_metrics(
            sparse,
            sparse_metrics,
            max(0.0, sparse_progress - half_window),
        )
        if min(float(np.linalg.norm(dense_vector)), float(np.linalg.norm(sparse_vector))) <= 0.05:
            continue
        tangent = max(tangent, _angle_between(dense_vector, sparse_vector))
    return float(lateral), float(tangent)


def split_pose_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    time_gap_s: float,
    distance_m: float,
    speed_mps: float,
    yaw_step_deg: float,
    yaw_rate_rps: float,
) -> list[list[tuple[int, float, float, float, float]]]:
    segments: list[list[tuple[int, float, float, float, float]]] = []
    current: list[tuple[int, float, float, float, float]] = []
    previous: tuple[int, float, float, float, float] | None = None
    for row_index, row in enumerate(rows):
        pose = row.get("pose") or {}
        try:
            item = (
                row_index,
                float(row["ts"]),
                float(pose["x"]),
                float(pose["y"]),
                math.radians(float(pose["yaw"])),
            )
        except (KeyError, TypeError, ValueError):
            item = None
        if item is None or not pose.get("valid") or not all(math.isfinite(v) for v in item[1:]):
            if len(current) >= 2:
                segments.append(current)
            current, previous = [], None
            continue
        split = False
        if previous is not None:
            dt = item[1] - previous[1]
            move = math.hypot(item[2] - previous[2], item[3] - previous[3])
            yaw_move = abs(angle_delta(previous[4], item[4]))
            split = (
                dt <= 0.0
                or dt > float(time_gap_s)
                or move > float(distance_m)
                or (dt > 0.0 and move / dt > float(speed_mps))
                or math.degrees(yaw_move) > float(yaw_step_deg)
                or (dt > 0.0 and yaw_move / dt > float(yaw_rate_rps))
            )
        if split:
            if len(current) >= 2:
                segments.append(current)
            current = []
        current.append(item)
        previous = item
    if len(current) >= 2:
        segments.append(current)
    return segments


def find_first_pose_jump(
    rows: Sequence[Mapping[str, Any]],
    *,
    pair_max_gap_s: float,
    distance_m: float,
    speed_mps: float,
    yaw_step_deg: float,
    yaw_rate_rps: float,
) -> PoseJump | None:
    """Return the first short-gap pose discontinuity.

    Long missing-data gaps are left to normal route segmentation: a large
    displacement across such a gap is not enough evidence that the coordinate
    frame changed.  Once a short-gap discontinuity is found, callers must treat
    that row and every later row as unusable instead of starting another route.
    """

    previous: tuple[int, float, float, float, float] | None = None
    for row_index, row in enumerate(rows):
        pose = row.get("pose") or {}
        try:
            item = (
                row_index,
                float(row["ts"]),
                float(pose["x"]),
                float(pose["y"]),
                math.radians(float(pose["yaw"])),
            )
        except (KeyError, TypeError, ValueError):
            previous = None
            continue
        if not pose.get("valid") or not all(math.isfinite(value) for value in item[1:]):
            previous = None
            continue
        if previous is not None:
            dt = item[1] - previous[1]
            if 0.0 < dt <= float(pair_max_gap_s):
                move = math.hypot(item[2] - previous[2], item[3] - previous[3])
                yaw_move = abs(angle_delta(previous[4], item[4]))
                speed = move / dt
                yaw_rate = yaw_move / dt
                if (
                    move > float(distance_m)
                    or speed > float(speed_mps)
                    or math.degrees(yaw_move) > float(yaw_step_deg)
                    or yaw_rate > float(yaw_rate_rps)
                ):
                    return PoseJump(
                        previous_row_index=previous[0],
                        row_index=item[0],
                        previous_ts=previous[1],
                        jump_ts=item[1],
                        dt_s=dt,
                        distance_m=move,
                        speed_mps=speed,
                        yaw_delta_deg=math.degrees(yaw_move),
                        yaw_rate_rps=yaw_rate,
                    )
        previous = item
    return None


def build_routes(
    sub_task_id: str,
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> tuple[list[RouteData], dict[int, RouteData]]:
    raw_segments = split_pose_rows(
        rows,
        time_gap_s=float(config["route_split_time_gap_s"]),
        distance_m=float(config["route_split_distance_m"]),
        speed_mps=float(config["route_split_speed_mps"]),
        yaw_step_deg=float(config["route_split_yaw_step_deg"]),
        yaw_rate_rps=float(config["route_split_yaw_rate_rps"]),
    )
    routes: list[RouteData] = []
    row_routes: dict[int, RouteData] = {}
    for segment_index, segment in enumerate(raw_segments):
        raw = np.asarray([(item[2], item[3]) for item in segment], dtype=np.float64)
        smooth = _rolling_median(raw, radius=2)
        keep = np.r_[True, np.linalg.norm(np.diff(smooth, axis=0), axis=1) >= 0.02]
        dense = smooth[keep]
        if len(dense) < 2 or float(_polyline_metrics(dense)[-1]) < 2.0:
            continue
        indices = _simplify_indices(
            dense,
            float(config["route_rdp_epsilon_m"]),
            float(config["route_tangent_error_max_deg"]),
        )
        base_sparse = dense[indices]
        sparse = _adaptive_subdivide(
            base_sparse,
            float(config["turn_heading_change_deg"]),
            float(config["turn_spacing_m"]),
            float(config["straight_spacing_m"]),
        )
        lateral_error, tangent_error = _route_errors(
            dense,
            sparse,
            tangent_window_m=float(config["route_tangent_window_m"]),
            end_margin_m=float(config["route_validation_end_margin_m"]),
        )
        start_ts, end_ts = float(segment[0][1]), float(segment[-1][1])
        route_id = f"{sub_task_id}:route_{segment_index:03d}"
        route = RouteData(
            route_id=route_id,
            sub_task_id=sub_task_id,
            segment_index=segment_index,
            start_ts=start_ts,
            end_ts=end_ts,
            dense_points=dense,
            sparse_points=sparse,
            metrics=_polyline_metrics(sparse),
            max_lateral_error_m=lateral_error,
            max_tangent_error_deg=tangent_error,
        )
        routes.append(route)
        for item in segment:
            row_routes[item[0]] = route
    return routes, row_routes


def boundary_keep_interval(
    rows: Sequence[Mapping[str, Any]], config: Mapping[str, Any]
) -> tuple[float, float, dict[str, float]]:
    samples: list[tuple[float, float, float, float]] = []
    for row in rows:
        pose = row.get("pose") or {}
        if not pose.get("valid"):
            continue
        try:
            samples.append(
                (
                    float(row["ts"]),
                    float(pose["x"]),
                    float(pose["y"]),
                    math.radians(float(pose["yaw"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if len(samples) < 2:
        raise ValueError("pose_invalid")
    values = np.asarray(samples, dtype=np.float64)
    smoothed_xy = _rolling_median(values[:, 1:3], radius=2)
    yaw_unwrapped = np.unwrap(values[:, 3])
    smoothed_yaw = _rolling_median(yaw_unwrapped[:, None], radius=2)[:, 0]
    times = values[:, 0]
    window = float(config["stationary_measurement_window_s"])
    moving = np.zeros(len(values), dtype=bool)
    for index, timestamp in enumerate(times):
        target = timestamp + window
        stop = int(np.searchsorted(times, target, side="left"))
        if stop >= len(values):
            stop = index
            start = int(np.searchsorted(times, timestamp - window, side="left"))
        else:
            start = index
        if stop == start:
            continue
        position_delta = float(np.linalg.norm(smoothed_xy[stop] - smoothed_xy[start]))
        yaw_delta = abs(math.degrees(smoothed_yaw[stop] - smoothed_yaw[start]))
        moving[index] = (
            position_delta >= float(config["moving_position_delta_m"])
            or yaw_delta >= float(config["moving_yaw_delta_deg"])
        )
    moving_indices = np.flatnonzero(moving)
    if not len(moving_indices):
        return float(times[0]), float(times[-1]), {"prefix_trim_s": 0.0, "suffix_trim_s": 0.0}
    context = float(config["transition_context_s"])
    minimum = float(config["boundary_stationary_min_s"])
    start = float(times[0])
    end = float(times[-1])
    proposed_start = max(start, float(times[moving_indices[0]]) - context)
    proposed_end = min(end, float(times[moving_indices[-1]]) + context)
    keep_start = proposed_start if proposed_start - start >= minimum else start
    keep_end = proposed_end if end - proposed_end >= minimum else end
    return keep_start, keep_end, {
        "prefix_trim_s": round(keep_start - start, 6),
        "suffix_trim_s": round(end - keep_end, 6),
    }


def estimate_pose_delta(
    rows: Sequence[Mapping[str, Any]],
    anchor_index: int,
    target_ts: float,
    neighbor_gap_max_s: float,
) -> tuple[float, float, float]:
    row = rows[anchor_index]
    pose = row.get("pose") or {}
    if not pose.get("valid"):
        raise ValueError("pose_invalid")
    anchor_ts = float(row["ts"])
    direction = 1 if target_ts >= anchor_ts else -1
    neighbor_index = anchor_index + direction
    while 0 <= neighbor_index < len(rows):
        neighbor_row = rows[neighbor_index]
        neighbor = neighbor_row.get("pose") or {}
        if neighbor.get("valid"):
            break
        neighbor_index += direction
    else:
        raise ValueError("pose_gap")
    neighbor_ts = float(rows[neighbor_index]["ts"])
    gap = abs(neighbor_ts - anchor_ts)
    if gap <= 1.0e-9 or gap > float(neighbor_gap_max_s):
        raise ValueError("pose_gap")
    fraction = abs(float(target_ts) - anchor_ts) / gap
    dx = (float(neighbor["x"]) - float(pose["x"])) * fraction
    dy = (float(neighbor["y"]) - float(pose["y"])) * fraction
    yaw0 = math.radians(float(pose["yaw"]))
    yaw1 = math.radians(float(neighbor["yaw"]))
    yaw_delta = abs(angle_delta(yaw0, yaw1)) * fraction
    return float(math.hypot(dx, dy)), float(math.degrees(yaw_delta)), gap


@dataclass(frozen=True)
class StaticMap:
    graph_name: str
    path: Path
    occupancy: np.ndarray
    resolution_m: float
    origin_x: float
    origin_y: float
    sha256: str


def load_static_map(path: Path, graph_name: str) -> StaticMap:
    with np.load(path, allow_pickle=False) as archive:
        occupancy = np.asarray(archive["occupancy"], dtype=np.int8)
        resolution = float(np.asarray(archive["resolution_m"]).item())
        origin = np.asarray(archive["origin_xy"], dtype=np.float64).reshape(2)
    occupancy = np.where(occupancy == 0, 0, 100).astype(np.int8)
    return StaticMap(
        graph_name=graph_name,
        path=path,
        occupancy=occupancy,
        resolution_m=resolution,
        origin_x=float(origin[0]),
        origin_y=float(origin[1]),
        sha256=sha256_file(path),
    )


def crop_static_map(
    static_map: StaticMap,
    pose: Sequence[float],
    *,
    size: int = 400,
    resolution_m: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.indices((int(size), int(size)), dtype=np.float64)
    body_x = (cols - float(size) / 2.0) * float(resolution_m)
    body_y = -(rows - float(size) / 2.0) * float(resolution_m)
    yaw = float(pose[2])
    cosine, sine = math.cos(yaw), math.sin(yaw)
    world_x = float(pose[0]) + cosine * body_x - sine * body_y
    world_y = float(pose[1]) + sine * body_x + cosine * body_y
    map_cols = np.floor(
        (world_x - static_map.origin_x) / static_map.resolution_m + 1.0e-7
    ).astype(np.int64)
    y_cells = np.floor(
        (world_y - static_map.origin_y) / static_map.resolution_m + 1.0e-7
    ).astype(np.int64)
    map_rows = static_map.occupancy.shape[0] - 1 - y_cells
    valid = (
        (map_rows >= 0)
        & (map_rows < static_map.occupancy.shape[0])
        & (map_cols >= 0)
        & (map_cols < static_map.occupancy.shape[1])
    )
    output = np.full((int(size), int(size)), 100, dtype=np.int8)
    output[valid] = static_map.occupancy[map_rows[valid], map_cols[valid]]
    world_to_body = np.asarray(
        [
            [cosine, sine, -(cosine * float(pose[0]) + sine * float(pose[1]))],
            [-sine, cosine, sine * float(pose[0]) - cosine * float(pose[1])],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    body_to_pixel = np.asarray(
        [
            [0.0, -1.0 / resolution_m, size / 2.0],
            [1.0 / resolution_m, 0.0, size / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    world_to_pixel_xy = body_to_pixel @ world_to_body
    return output, world_to_pixel_xy


def build_route_mask(
    route: np.ndarray,
    pose: Sequence[float],
    projection: RouteProjection,
    *,
    size: int = 400,
    resolution_m: float = 0.05,
    width_cells: int = 3,
) -> np.ndarray:
    sampled, sample_s = densify_route(route, step_m=resolution_m)
    delta = sampled - np.asarray(pose[:2], dtype=np.float64)
    cosine, sine = math.cos(float(pose[2])), math.sin(float(pose[2]))
    local_x = cosine * delta[:, 0] + sine * delta[:, 1]
    local_y = -sine * delta[:, 0] + cosine * delta[:, 1]
    cols = np.rint(size / 2.0 + local_x / resolution_m).astype(np.int64)
    rows = np.rint(size / 2.0 - local_y / resolution_m).astype(np.int64)
    visible = (
        (rows >= 0)
        & (rows < size)
        & (cols >= 0)
        & (cols < size)
        & (sample_s >= projection.progress_m - 0.5 * resolution_m)
    )
    mask = np.zeros((size, size), dtype=np.uint8)
    for row, col in zip(rows[visible], cols[visible]):
        row0, row1 = max(0, row - width_cells), min(size, row + width_cells + 1)
        col0, col1 = max(0, col - width_cells), min(size, col + width_cells + 1)
        mask[row0:row1, col0:col1] = 1
    return mask


def load_teacher_occupancy(path: Path) -> tuple[np.ndarray, np.ndarray]:
    source = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    teacher = np.where(source == 0, 100, 0).astype(np.int8)
    return source, teacher


def fuse_local_static_occupancy(
    local_occupancy: np.ndarray,
    static_occupancy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Union static obstacles into the centered extent of a local grid.

    Both arrays use the teacher value convention (100 obstacle, 0 free) and
    share the same robot-aligned resolution.  Static cells outside the local
    grid footprint are deliberately ignored.
    """

    local = np.asarray(local_occupancy, dtype=np.int8)
    static = np.asarray(static_occupancy, dtype=np.int8)
    if local.ndim != 2 or static.ndim != 2:
        raise ValueError("occupancy_schema_mismatch")
    if static.shape[0] < local.shape[0] or static.shape[1] < local.shape[1]:
        raise ValueError("occupancy_schema_mismatch")
    row_start = (static.shape[0] - local.shape[0]) // 2
    col_start = (static.shape[1] - local.shape[1]) // 2
    static_local = static[
        row_start : row_start + local.shape[0],
        col_start : col_start + local.shape[1],
    ].copy()
    fused = np.where((local != 0) | (static_local != 0), 100, 0).astype(np.int8)
    return fused, static_local


def estimate_route_curvature(
    route: np.ndarray,
    projection: RouteProjection,
    *,
    lookahead_m: float = 1.0,
    limit: float = 2.0,
) -> float:
    start = float(projection.progress_m)
    stop = min(start + max(0.1, float(lookahead_m)), start + projection.remaining_m)
    sample_s = np.linspace(start, stop, max(3, int((stop - start) / 0.05) + 1))
    points = np.asarray([point_at_progress(route, value) for value in sample_s])
    vectors = np.diff(points, axis=0)
    lengths = np.linalg.norm(vectors, axis=1)
    valid = lengths > 1.0e-6
    if np.count_nonzero(valid) < 2:
        return 0.0
    headings = np.unwrap(np.arctan2(vectors[valid, 1], vectors[valid, 0]))
    curvature = np.diff(headings) / np.maximum(1.0e-6, 0.5 * (lengths[valid][:-1] + lengths[valid][1:]))
    if not len(curvature):
        return 0.0
    return float(np.clip(np.median(curvature), -abs(limit), abs(limit)))


def estimate_pose_velocity(
    stamped_poses: Sequence[Sequence[float]],
    *,
    window_s: float,
    max_linear_mps: float,
    max_angular_rps: float,
) -> dict[str, Any]:
    """Estimate executed differential-drive velocity from recent grid poses.

    Each input row is ``(stamp_s, x, y, yaw_rad)``.  Segment translations are
    projected onto the midpoint body heading before taking a robust median, so
    lateral localization noise does not masquerade as forward speed.
    """

    values = np.asarray(stamped_poses, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 4 or len(values) < 2:
        raise ValueError("velocity_history_insufficient")
    if not np.all(np.isfinite(values)) or np.any(np.diff(values[:, 0]) <= 0.0):
        raise ValueError("velocity_history_invalid")
    current_stamp = float(values[-1, 0])
    cutoff = current_stamp - float(window_s)
    start = max(0, int(np.searchsorted(values[:, 0], cutoff, side="left")) - 1)
    selected = values[start:]
    if len(selected) < 2:
        raise ValueError("velocity_history_insufficient")

    dt = np.diff(selected[:, 0])
    delta_xy = np.diff(selected[:, 1:3], axis=0)
    yaw_delta = np.asarray(
        [angle_delta(first, second) for first, second in zip(selected[:-1, 3], selected[1:, 3])],
        dtype=np.float64,
    )
    midpoint_yaw = selected[:-1, 3] + 0.5 * yaw_delta
    forward = (
        delta_xy[:, 0] * np.cos(midpoint_yaw)
        + delta_xy[:, 1] * np.sin(midpoint_yaw)
    )
    lateral = (
        -delta_xy[:, 0] * np.sin(midpoint_yaw)
        + delta_xy[:, 1] * np.cos(midpoint_yaw)
    )
    segment_linear = forward / dt
    segment_angular = yaw_delta / dt
    segment_lateral = lateral / dt
    raw_linear = float(np.median(segment_linear))
    raw_angular = float(np.median(segment_angular))
    raw_lateral = float(np.median(segment_lateral))
    clipped_linear = float(
        np.clip(raw_linear, -abs(float(max_linear_mps)), abs(float(max_linear_mps)))
    )
    clipped_angular = float(
        np.clip(raw_angular, -abs(float(max_angular_rps)), abs(float(max_angular_rps)))
    )
    return {
        "source": "grid_pose_midpoint_median_v1",
        "window_s": float(current_stamp - selected[0, 0]),
        "pose_count": int(len(selected)),
        "segment_count": int(len(dt)),
        "raw_linear_mps": raw_linear,
        "raw_angular_rps": raw_angular,
        "raw_lateral_mps": raw_lateral,
        "linear_mps": clipped_linear,
        "angular_rps": clipped_angular,
        "linear_clipped": not math.isclose(raw_linear, clipped_linear, abs_tol=1.0e-12),
        "angular_clipped": not math.isclose(raw_angular, clipped_angular, abs_tol=1.0e-12),
    }


def sample_initial_state(
    *,
    dataset_id: str,
    meta_ts: float,
    teacher_version: str,
    curvature: float,
    indoor: bool,
    motion_estimate: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    seed_text = (
        f"{dataset_id}|{meta_ts:.9f}|{config['state_sampler_version']}|{teacher_version}"
    )
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "little")
    rng = np.random.default_rng(seed)
    zero_probability = float(config["zero_initial_speed_probability"])
    if not 0.0 <= zero_probability <= 1.0:
        raise ValueError("zero_initial_speed_probability must be within 0..1")
    if float(rng.random()) < zero_probability:
        linear = 0.0
        angular = 0.0
        branch = "zero_static_repeat"
        observation_mode = "static_repeat"
    else:
        linear = float(motion_estimate["linear_mps"])
        angular = float(motion_estimate["angular_rps"])
        branch = "pose_velocity"
        observation_mode = "dynamic_history"
    return {
        "initial_linear_mps": round(float(linear), 9),
        "initial_angular_rps": round(angular, 9),
        "initial_curvature": round(float(curvature), 9),
        "sampling_branch": branch,
        "observation_mode": observation_mode,
        "zero_probability": zero_probability,
        "rng_seed": seed,
        "state_sampler_version": str(config["state_sampler_version"]),
        "indoor_profile": bool(indoor),
        "motion_estimate": dict(motion_estimate),
    }


def integrate_commands(
    commands: Sequence[Mapping[str, float]], dt_s: float = 0.05
) -> np.ndarray:
    states = [(0.0, 0.0, 0.0, 0.0)]
    x = y = yaw = elapsed = 0.0
    for command in commands:
        linear = float(command["linear_mps"])
        angular = float(command["angular_rps"])
        duration = float(command["duration_s"])
        steps = max(1, int(math.ceil(duration / max(1.0e-6, dt_s))))
        step = duration / steps
        for _ in range(steps):
            if abs(angular) < 1.0e-9:
                x += linear * step * math.cos(yaw)
                y += linear * step * math.sin(yaw)
            else:
                next_yaw = yaw + angular * step
                radius = linear / angular
                x += radius * (math.sin(next_yaw) - math.sin(yaw))
                y -= radius * (math.cos(next_yaw) - math.cos(yaw))
                yaw = next_yaw
            yaw = wrap_angle(yaw)
            elapsed += step
            states.append((x, y, yaw, elapsed))
    return np.asarray(states, dtype=np.float64)


def _collision_in_grid(
    occupancy: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    resolution_m: float,
) -> bool:
    height, width = occupancy.shape
    cols = np.rint(width / 2.0 + x / resolution_m).astype(np.int64)
    rows = np.rint(height / 2.0 - y / resolution_m).astype(np.int64)
    outside = (rows < 0) | (rows >= height) | (cols < 0) | (cols >= width)
    if np.any(outside):
        return True
    return bool(np.any(occupancy[rows, cols] != 0))


def rollout_collision(
    rollout: np.ndarray,
    local_occupancy: np.ndarray,
    static_occupancy: np.ndarray,
    *,
    resolution_m: float,
    footprint_length_m: float,
    footprint_width_m: float,
) -> tuple[bool, int | None]:
    offsets_x = np.arange(
        -0.5 * footprint_length_m,
        0.5 * footprint_length_m + 0.5 * resolution_m,
        resolution_m,
    )
    offsets_y = np.arange(
        -0.5 * footprint_width_m,
        0.5 * footprint_width_m + 0.5 * resolution_m,
        resolution_m,
    )
    footprint_x, footprint_y = np.meshgrid(offsets_x, offsets_y)
    for index, (center_x, center_y, yaw, _elapsed) in enumerate(rollout[1:], start=1):
        cosine, sine = math.cos(float(yaw)), math.sin(float(yaw))
        x = center_x + cosine * footprint_x - sine * footprint_y
        y = center_y + sine * footprint_x + cosine * footprint_y
        if _collision_in_grid(local_occupancy, x, y, resolution_m) or _collision_in_grid(
            static_occupancy, x, y, resolution_m
        ):
            return True, index
    return False, None


def save_npz_atomic(path: Path, **arrays: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(temporary, **arrays)
    temporary.replace(path)


def decode_selected_frames(
    video_path: Path,
    selected: Mapping[int, Path],
) -> None:
    if not selected:
        return
    targets = sorted(int(value) for value in selected)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"video_corrupt: {video_path}")
    target_set = set(targets)
    maximum = targets[-1]
    frame_index = 0
    written: set[int] = set()
    try:
        while frame_index <= maximum:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index in target_set:
                output = selected[frame_index]
                output.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(output), frame):
                    raise RuntimeError(f"cannot write decoded frame: {output}")
                written.add(frame_index)
            frame_index += 1
    finally:
        capture.release()
    missing = sorted(target_set - written)
    if missing:
        raise RuntimeError(f"video_gap: missing frame indices {missing} in {video_path}")
