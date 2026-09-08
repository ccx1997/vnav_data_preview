from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnav_training.core import (  # noqa: E402
    StaticMap,
    build_route_mask,
    build_routes,
    crop_static_map,
    estimate_pose_delta,
    estimate_pose_velocity,
    find_first_pose_jump,
    forward_route,
    fuse_local_static_occupancy,
    integrate_commands,
    load_teacher_occupancy,
    project_to_route,
    rollout_collision,
    sample_initial_state,
)
from vnav_training import pipeline as pipeline_module  # noqa: E402
from vnav_training.pipeline import (  # noqa: E402
    _effective_video_windows,
    _history_frames_for_row,
    _load_video_alignment,
    _nearest_pts,
    _rgb_history_indices,
    load_config,
    split_rows_by_map,
)


CONFIG = load_config(ROOT / "config.json")


def _row(ts: float, x: float, y: float, yaw_deg: float = 0.0) -> dict:
    return {
        "ts": ts,
        "pose": {"valid": True, "x": x, "y": y, "yaw": yaw_deg},
    }


def test_nearest_pts_and_pose_delta() -> None:
    pts = np.asarray([0.0, 0.066, 0.133, 0.200])
    index, value = _nearest_pts(pts, 0.14)
    assert index == 2
    assert value == 0.133
    rows = [_row(10.0, 0.0, 0.0), _row(10.2, 0.2, 0.0)]
    position, yaw, gap = estimate_pose_delta(rows, 0, 10.04, 0.5)
    assert position == pytest_approx(0.04)
    assert yaw == pytest_approx(0.0)
    assert gap == pytest_approx(0.2)


def test_local_trim_overrides_stale_manifest_keep_window() -> None:
    windows, source = _effective_video_windows(
        {
            "keep_windows": [{"from": 10.0, "to": 500.0}],
            "local_trim": {"from": 300.0, "to": 451.0},
        }
    )
    assert windows == [{"from": 300.0, "to": 451.0}]
    assert source == "manifest.local_trim"

    windows, source = _effective_video_windows(
        {"keep_windows": [{"from": 10.0, "to": 20.0}]}
    )
    assert windows == [{"from": 10.0, "to": 20.0}]
    assert source == "manifest.keep_windows"


def test_video_alignment_accepts_actual_five_camera_manifest(
    tmp_path: Path, monkeypatch: object
) -> None:
    camera_ids = ("cam0", "cam1", "cam2", "cam3", "cam6")
    results = []
    for camera_id in camera_ids:
        video_path = tmp_path / f"{camera_id}_continuous.mp4"
        video_path.write_bytes(b"video")
        results.append({"camera": camera_id, "ok": True, "out": str(video_path)})
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "concat_complete": True,
                "camera_count": len(camera_ids),
                "concat_ok": len(camera_ids),
                "keep_windows": [{"from": 10.0, "to": 20.0}],
                "results": results,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        pipeline_module,
        "_ffprobe_pts",
        lambda _path: np.asarray([0.0, 0.1], dtype=np.float64),
    )

    windows, source, actual, paths, pts = _load_video_alignment(
        "task_1", tmp_path, CONFIG
    )

    assert windows == [{"from": 10.0, "to": 20.0}]
    assert source == "manifest.keep_windows"
    assert actual == camera_ids
    assert tuple(paths) == camera_ids
    assert tuple(pts) == camera_ids


def test_route_simplification_keeps_turn_and_sparse_straight() -> None:
    rows = []
    timestamp = 0.0
    for x in np.linspace(0.0, 5.0, 26):
        rows.append(_row(timestamp, float(x), 0.0, 0.0))
        timestamp += 0.2
    for y in np.linspace(0.2, 5.0, 25):
        # This test exercises XY route simplification.  Keep yaw continuous so
        # the independent pose-jump guard does not intentionally split it.
        rows.append(_row(timestamp, 5.0, float(y), 0.0))
        timestamp += 0.2
    routes, row_routes = build_routes("test_1", rows, CONFIG)
    assert len(routes) == 1
    route = routes[0]
    assert route.max_lateral_error_m <= CONFIG["route_rdp_epsilon_m"] + 1.0e-6
    assert route.max_tangent_error_deg <= CONFIG["route_tangent_error_max_deg"] + 1.0e-6
    assert len(route.sparse_points) < len(route.dense_points)
    corner_distance = np.linalg.norm(route.sparse_points - np.asarray([5.0, 0.0]), axis=1)
    assert float(corner_distance.min()) < 0.25
    assert len(row_routes) == len(rows)


def test_first_pose_jump_marks_tail_and_ignores_long_gap() -> None:
    rows = [
        _row(0.0, 0.0, 0.0),
        _row(0.5, 0.2, 0.0),
        _row(1.0, 8.0, -5.0, 90.0),
        _row(1.5, 8.1, -5.0, 90.0),
    ]
    jump = find_first_pose_jump(
        rows,
        pair_max_gap_s=CONFIG["pose_jump_pair_max_gap_s"],
        distance_m=CONFIG["pose_jump_distance_m"],
        speed_mps=CONFIG["pose_jump_speed_mps"],
        yaw_step_deg=CONFIG["pose_jump_yaw_step_deg"],
        yaw_rate_rps=CONFIG["pose_jump_yaw_rate_rps"],
    )
    assert jump is not None
    assert jump.previous_row_index == 1
    assert jump.row_index == 2
    assert jump.distance_m > 2.0
    assert jump.yaw_delta_deg == pytest_approx(90.0)

    long_gap = [_row(0.0, 0.0, 0.0), _row(5.0, 3.0, 0.0)]
    assert (
        find_first_pose_jump(
            long_gap,
            pair_max_gap_s=CONFIG["pose_jump_pair_max_gap_s"],
            distance_m=CONFIG["pose_jump_distance_m"],
            speed_mps=CONFIG["pose_jump_speed_mps"],
            yaw_step_deg=CONFIG["pose_jump_yaw_step_deg"],
            yaw_rate_rps=CONFIG["pose_jump_yaw_rate_rps"],
        )
        is None
    )


def test_map_timeline_splits_at_frame_map_switch(tmp_path: Path) -> None:
    rows = [
        {"ts": 1.0, "map_name": "B10_map_20260420_170044"},
        {"ts": 2.0, "map_name": "B10_map"},
        {"ts": 3.0, "map_name": "P_map_20260415_231828"},
        {"ts": 4.0, "map_name": "P_map"},
    ]
    segments = split_rows_by_map(rows, tmp_path, CONFIG)
    assert [segment.map_name for segment in segments] == ["B10_map", "P_map"]
    assert [(segment.start_index, segment.stop_index) for segment in segments] == [
        (0, 2),
        (2, 4),
    ]
    assert [segment.segment_index for segment in segments] == [1, 2]


def test_map_timeline_rejects_ambiguous_missing_switch_rows(tmp_path: Path) -> None:
    import pytest

    rows = [
        {"ts": 1.0, "map_name": "B10_map"},
        {"ts": 2.0},
        {"ts": 3.0, "map_name": "P_map"},
    ]
    with pytest.raises(ValueError, match="map_timeline_incomplete"):
        split_rows_by_map(rows, tmp_path, CONFIG)


def test_map_timeline_uses_only_uniform_task_snapshot_fallback(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "start_snapshot": {"current_map": "B9_map_20260420_171732"},
                "end_snapshot": {"current_map": "B9_map_20260420_171732"},
            }
        ),
        encoding="utf-8",
    )
    segments = split_rows_by_map([{"ts": 1.0}, {"ts": 2.0}], tmp_path, CONFIG)
    assert len(segments) == 1
    assert segments[0].map_name == "B9_map"
    assert segments[0].map_name_source == "task_snapshot_uniform"


def test_forward_suffix_and_route_mask_hide_past() -> None:
    route = np.asarray([[-4.0, 0.0], [0.0, 0.0], [4.0, 0.0]])
    projection = project_to_route((0.0, 0.0), route)
    suffix = forward_route(route, projection)
    assert np.allclose(suffix[0], (0.0, 0.0))
    assert np.all(suffix[:, 0] >= -1.0e-9)
    mask = build_route_mask(
        route,
        (0.0, 0.0, 0.0),
        projection,
        size=400,
        resolution_m=0.05,
        width_cells=0,
    )
    assert mask[200, 240] == 1
    assert mask[200, 160] == 0


def test_static_crop_orientation_and_transform(tmp_path: Path) -> None:
    occupancy = np.zeros((9, 9), dtype=np.int8)
    occupancy[4, 6] = 100
    static_map = StaticMap(
        graph_name="test",
        path=tmp_path / "map.npz",
        occupancy=occupancy,
        resolution_m=1.0,
        origin_x=-4.0,
        origin_y=-4.0,
        sha256="test",
    )
    crop, transform = crop_static_map(
        static_map, (0.0, 0.0, 0.0), size=8, resolution_m=1.0
    )
    assert crop[4, 6] == 100
    pixel = transform @ np.asarray([2.0, 0.0, 1.0])
    assert np.allclose(pixel[:2], (4.0, 6.0))


def test_grid_value_conversion_does_not_flip(tmp_path: Path) -> None:
    source = np.full((3, 4), 255, dtype=np.uint8)
    source[0, 1] = 0
    path = tmp_path / "grid.png"
    Image.fromarray(source).save(path)
    loaded, teacher = load_teacher_occupancy(path)
    assert np.array_equal(loaded, source)
    assert teacher[0, 1] == 100
    assert teacher[2, 1] == 0


def test_static_obstacles_are_fused_only_inside_local_extent() -> None:
    local = np.zeros((4, 4), dtype=np.int8)
    local[0, 0] = 100
    static = np.zeros((8, 8), dtype=np.int8)
    static[3, 4] = 100
    static[1, 1] = 100
    fused, static_local = fuse_local_static_occupancy(local, static)
    assert static_local.shape == local.shape
    assert fused[0, 0] == 100
    assert fused[1, 2] == 100
    assert int(np.count_nonzero(fused)) == 2
    assert local[1, 2] == 0


def test_initial_state_is_reproducible_and_bounded() -> None:
    motion = {
        "linear_mps": 0.24,
        "angular_rps": -0.12,
        "raw_linear_mps": 0.24,
        "raw_angular_rps": -0.12,
    }
    first = sample_initial_state(
        dataset_id="dataset",
        meta_ts=123.456,
        teacher_version="teacher",
        curvature=-0.8,
        indoor=True,
        motion_estimate=motion,
        config=CONFIG,
    )
    second = sample_initial_state(
        dataset_id="dataset",
        meta_ts=123.456,
        teacher_version="teacher",
        curvature=-0.8,
        indoor=True,
        motion_estimate=motion,
        config=CONFIG,
    )
    assert first == second
    assert abs(first["initial_linear_mps"]) <= CONFIG["indoor_max_linear_mps"]
    assert abs(first["initial_angular_rps"]) <= CONFIG["max_angular_rps"]
    assert first["initial_angular_rps"] <= 0.0


def test_pose_velocity_and_zero_repeat_policy() -> None:
    estimate = estimate_pose_velocity(
        (
            (0.0, 0.0, 0.0, 0.0),
            (0.2, 0.1, 0.0, 0.04),
            (0.4, 0.2, 0.004, 0.08),
        ),
        window_s=0.5,
        max_linear_mps=0.75,
        max_angular_rps=0.5,
    )
    assert estimate["linear_mps"] == pytest_approx(0.5, abs=0.02)
    assert estimate["angular_rps"] == pytest_approx(0.2, abs=1.0e-6)
    assert abs(estimate["raw_lateral_mps"]) < 0.03

    always_zero = dict(CONFIG, zero_initial_speed_probability=1.0)
    state = sample_initial_state(
        dataset_id="dataset",
        meta_ts=1.0,
        teacher_version="teacher",
        curvature=0.4,
        indoor=False,
        motion_estimate=estimate,
        config=always_zero,
    )
    assert state["initial_linear_mps"] == 0.0
    assert state["initial_angular_rps"] == 0.0
    assert state["observation_mode"] == "static_repeat"

    never_zero = dict(CONFIG, zero_initial_speed_probability=0.0)
    state = sample_initial_state(
        dataset_id="dataset",
        meta_ts=1.0,
        teacher_version="teacher",
        curvature=0.4,
        indoor=False,
        motion_estimate=estimate,
        config=never_zero,
    )
    assert state["initial_linear_mps"] == pytest_approx(estimate["linear_mps"])
    assert state["initial_angular_rps"] == pytest_approx(estimate["angular_rps"])
    assert state["observation_mode"] == "dynamic_history"


def test_rgb_history_uses_actual_pts_at_no_more_than_ten_hz() -> None:
    pts = np.arange(0.0, 2.0, 1.0 / 15.0)
    indices = _rgb_history_indices(pts, 20, window_s=1.0, max_hz=10.0)
    selected = pts[np.asarray(indices)]
    assert indices[-1] == 20
    assert selected[-1] - selected[0] >= 0.75
    assert np.all(np.diff(selected) >= 0.1 - 1.0e-9)


def test_teacher_history_must_reach_the_full_older_offset(tmp_path: Path) -> None:
    def history_row(index: int, stamp: float) -> dict:
        filename = f"grid_{index}.png"
        Image.fromarray(np.full((200, 200), 255, dtype=np.uint8)).save(
            tmp_path / filename
        )
        return {
            "ts": stamp,
            "grid_valid": True,
            "grid_png": filename,
            "grid_pose": {
                "valid": True,
                "match": "ros_grid_sync",
                "x": stamp,
                "y": 0.0,
                "yaw_rad": 0.0,
            },
        }

    too_young = [
        history_row(0, 9.05),
        history_row(1, 9.50),
        history_row(2, 10.00),
    ]
    try:
        _history_frames_for_row(too_young, 2, tmp_path, CONFIG)
    except ValueError as error:
        assert str(error) == "teacher_history_insufficient"
    else:
        raise AssertionError("a 0.95 s oldest frame must not fill the 1.0 s channel")

    covered = [
        history_row(3, 8.95),
        history_row(4, 9.50),
        history_row(5, 10.00),
    ]
    frames = _history_frames_for_row(covered, 2, tmp_path, CONFIG)
    assert 10.0 - frames[0].meta_ts == pytest_approx(1.05)


def test_action_integration_and_collision() -> None:
    commands = [
        {"linear_mps": 0.5, "angular_rps": 0.0, "duration_s": 1.0},
        {"linear_mps": 0.0, "angular_rps": 0.5, "duration_s": 1.0},
    ]
    rollout = integrate_commands(commands, dt_s=0.05)
    assert rollout[-1, 0] == pytest_approx(0.5, abs=1.0e-6)
    assert rollout[-1, 2] == pytest_approx(0.5, abs=1.0e-6)
    local = np.zeros((200, 200), dtype=np.int8)
    static = np.zeros((400, 400), dtype=np.int8)
    collision, index = rollout_collision(
        rollout,
        local,
        static,
        resolution_m=0.05,
        footprint_length_m=0.7,
        footprint_width_m=0.6,
    )
    assert not collision
    assert index is None
    local[97:104, 112:116] = 100
    collision, index = rollout_collision(
        rollout,
        local,
        static,
        resolution_m=0.05,
        footprint_length_m=0.7,
        footprint_width_m=0.6,
    )
    assert collision
    assert index is not None


def pytest_approx(value: float, *, abs: float | None = None):
    import pytest

    kwargs = {} if abs is None else {"abs": abs}
    return pytest.approx(value, **kwargs)
