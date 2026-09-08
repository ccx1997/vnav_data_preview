from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnav_training.batch import (  # noqa: E402
    find_completed_full_run,
    scan_tasks,
    task_layout_error,
    task_layout_warnings,
)
from vnav_training.pipeline import load_config  # noqa: E402


CONFIG = load_config(ROOT / "config.json")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _ready_task(
    source_root: Path,
    task_id: str,
    camera_ids: tuple[str, ...] | None = None,
) -> Path:
    task_root = source_root / task_id
    subtask_id = f"{task_id}_1"
    meta_dir = task_root / "meta" / "unpacked" / f"meta_{subtask_id}"
    meta_dir.mkdir(parents=True)
    (meta_dir / "frames.jsonl").write_text("{}\n", encoding="utf-8")
    video_dir = task_root / "videos" / f"videos_{subtask_id}"
    video_dir.mkdir(parents=True)
    results = []
    actual_camera_ids = camera_ids or tuple(CONFIG["camera_ids"])
    for camera_id in actual_camera_ids:
        video = video_dir / f"{camera_id}_continuous.mp4"
        video.write_bytes(b"video")
        results.append({"camera": camera_id, "ok": True, "out": str(video)})
    _write_json(
        video_dir / "manifest.json",
        {
            "concat_complete": True,
            "camera_count": len(actual_camera_ids),
            "concat_ok": len(actual_camera_ids),
            "results": results,
        },
    )
    return task_root


def _completed_run(output_root: Path, task_id: str, accepted: int = 3) -> Path:
    run_dir = (
        output_root
        / task_id
        / CONFIG["pipeline_version"]
        / "full"
        / "run_20260827_120000"
    )
    (run_dir / "cases").mkdir(parents=True)
    (run_dir / "source_manifest.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "teacher_labels.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "rgb_history_manifest.jsonl").write_text("{}\n", encoding="utf-8")
    _write_json(
        run_dir / "run.json",
        {
            "mode": "full",
            "status": "success",
            "pipeline_version": CONFIG["pipeline_version"],
            "full_generation_authorized": True,
            "accepted_count": accepted,
        },
    )
    return run_dir


def test_task_layout_and_completed_run_contract(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    task = _ready_task(source_root, "task_ready")
    assert task_layout_error(task, CONFIG["camera_ids"]) is None
    assert find_completed_full_run(
        output_root, task.name, CONFIG["pipeline_version"]
    ) is None
    run_dir = _completed_run(output_root, task.name)
    assert (
        find_completed_full_run(output_root, task.name, CONFIG["pipeline_version"])
        == run_dir
    )


def test_zero_sample_success_is_not_treated_as_completed(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    _completed_run(output_root, "empty_task", accepted=0)
    assert (
        find_completed_full_run(
            output_root, "empty_task", CONFIG["pipeline_version"]
        )
        is None
    )


def test_scan_selects_only_ready_unprocessed_tasks(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    _ready_task(source_root, "already")
    _ready_task(source_root, "pending")
    (source_root / "incomplete" / "meta").mkdir(parents=True)
    _completed_run(output_root, "already")
    scans = scan_tasks(source_root, output_root, CONFIG)
    assert {scan.task_id: scan.status for scan in scans} == {
        "already": "already_completed",
        "incomplete": "incomplete",
        "pending": "pending",
    }


def test_nonstandard_camera_set_is_ready_with_explicit_warning(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    cameras = ("cam0", "cam1", "cam2", "cam3", "cam6")
    task = _ready_task(source_root, "five_camera", cameras)

    assert task_layout_error(task, CONFIG["camera_ids"]) is None
    warnings = task_layout_warnings(task, CONFIG["camera_ids"])
    assert len(warnings) == 1
    assert "actual=cam0,cam1,cam2,cam3,cam6" in warnings[0]
    scans = scan_tasks(source_root, output_root, CONFIG)
    assert scans[0].status == "pending"
    assert scans[0].warnings == warnings
