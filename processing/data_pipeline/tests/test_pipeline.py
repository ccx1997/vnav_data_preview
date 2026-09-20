from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import sys

import numpy as np
import pytest

PROCESSING = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROCESSING))

from run_data_pipeline import parse_args, main
from data_pipeline import runner, teacher, validation
from data_pipeline.common import PipelineError, pipeline_lock, read_json, snapshot, write_json
from vnav_coordinates.export import export_pixels


@pytest.mark.parametrize("argv,expected", [
    (["task_a"], ["task_a"]),
    (["task_a", "task_b", "task_a"], ["task_a", "task_b"]),
    (["--task-id", "task_a", "--task-id", "task_b"], ["task_a", "task_b"]),
    (["--task-ids", ' ["task_a", "task_b"]', "task_a"], ["task_a", "task_b"]),
    (["--task-ids", "task_a,task_b"], ["task_a", "task_b"]),
    (["--task-ids", "task_a, task_b"], ["task_a", "task_b"]),
])
def test_id_forms_and_stable_deduplication(argv, expected):
    assert parse_args(argv).ids == expected


def test_date_bounds_and_exclusive_selection():
    args = parse_args(["2026-09-01", "--until", "2026-09-02"])
    assert args.since == "2026-09-01T00:00:00+08:00"
    assert args.until == "2026-09-02T23:59:59.999999+08:00"
    for argv in (["task_a", "--since", "2026-09-01"], ["task_a", "--robot", "robot"],
                 ["--since", "2026-09-02", "--until", "2026-09-01"], []):
        with pytest.raises(SystemExit):
            parse_args(argv)
    with pytest.raises(PipelineError):
        parse_args(["../outside"])


def test_output_paths_cannot_overwrite_source():
    with pytest.raises(SystemExit):
        parse_args(["task_a", "--source-root", "/tmp/raw", "--result-json", "/tmp/raw/task_a/frames.jsonl"])
    with pytest.raises(SystemExit):
        parse_args(["task_a", "--source-root", "/tmp/data", "--output-root", "/tmp/data/out"])


def test_discovery_uses_full_ids_api_and_rejects_truncation(monkeypatch):
    args = parse_args(["--since", "2026-09-01", "--robot", "R1"])
    payload = dict(tasks=[dict(task_id="a"), dict(task_id="b"), dict(task_id="a")], truncated=False)
    def fake(command, **kwargs):
        assert command[1].endswith("list-task-ids.py")
        assert "--from" in command and "--to" in command and command[-2:] == ["--robot", "R1"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.discover(args, {}) == ["a", "b"]
    payload["truncated"] = True
    with pytest.raises(PipelineError, match="截断"):
        runner.discover(args, {})


@pytest.mark.parametrize("start", ["2026-09-20T00:00:00+08:00", "2026-09-19T16:00:00+00:00"])
def test_discovery_sends_unix_seconds_for_timezone_aware_input(monkeypatch, start):
    args = parse_args(["--since", start, "--until", "2026-09-20T00:01:00+08:00"])
    def fake(command, **kwargs):
        assert float(command[command.index("--from") + 1]) == 1789833600
        assert float(command[command.index("--to") + 1]) == 1789833660
        return SimpleNamespace(returncode=0, stdout='{"tasks":[],"truncated":false}')
    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.discover(args, {}) == []


def test_empty_discovery_does_not_scan_or_process_all_tasks(tmp_path, monkeypatch):
    args = parse_args(["--since", "2026-09-01", "--source-root", str(tmp_path / "raw"),
                       "--output-root", str(tmp_path / "out"), "--no-feishu"])
    monkeypatch.setattr(runner, "preflight", lambda *a: {})
    monkeypatch.setattr(runner, "discover", lambda *a: [])
    monkeypatch.setattr(runner, "process_task", lambda *a: pytest.fail("must not process all source tasks"))
    result = runner.run_pipeline(args, {})
    assert result["status"] == "success" and result["tasks"] == []


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    args = parse_args(["task_a", "--source-root", str(tmp_path / "raw"),
                       "--output-root", str(tmp_path / "out"), "--no-feishu"])
    report = tmp_path / "reports"
    report.mkdir()
    events = []
    config = {"pipeline_version": "v1"}
    assets = dict(maps={}, checkpoint_sha256="weights", teacher_config_sha256="config")
    run = args.output_root / "task_a/v1/full/run_1"
    source = args.source_root / "task_a"
    def download(*a, **k):
        events.append("download")
        source.mkdir(parents=True)
        (source / "frames.jsonl").write_text('{"map_name":"manual"}\n')
    monkeypatch.setattr(runner, "cloud_readiness", lambda *a: {"ready":True})
    monkeypatch.setattr(runner, "run_logged", download)
    monkeypatch.setattr(runner, "validate_source", lambda *a: {"passed":True})
    monkeypatch.setattr(runner, "check_run_source", lambda *a: None)
    @contextmanager
    def service(*a):
        events.append("start_teacher")
        try:
            yield "http://127.0.0.1:8103"
        finally:
            events.append("stop_teacher")
    monkeypatch.setattr(runner, "teacher_service", service)
    def build(**kwargs):
        assert kwargs["task_ids"] == ("task_a",) and not kwargs["auto_start_teacher"]
        events.append("build")
        write_json(run / "run.json", {"status":"success"})
        return None, {"tasks":[dict(task_id="task_a", status="generated", output=str(run))]}
    monkeypatch.setattr(runner, "run_pending_batch", build)
    monkeypatch.setattr(runner, "validate_teacher", lambda *a: dict(
        candidate_count=2, accepted_count=1, rejected_count=1, without_black_frames_count=1))
    def pixels(output, *a, **kw):
        events.append("pixels")
        output.mkdir(parents=True)
        (output / "samples.jsonl").write_text("{}\n")
    monkeypatch.setattr(runner, "export_pixels", pixels)
    monkeypatch.setattr(runner, "validate_pixels", lambda *a: {"passed":True, "row_count":1})
    return args, report, config, assets, events, source, run


def test_new_task_then_complete_reuse_never_downloads_or_labels_again(workflow):
    args, report, config, assets, events, source, run = workflow
    first = runner.process_task(args, "task_a", config, assets, {}, report / "first")
    assert first["status"] == "completed"
    assert events == ["download", "start_teacher", "build", "stop_teacher", "pixels"]
    before = snapshot(source)
    events.clear()
    second = runner.process_task(args, "task_a", config, assets, {}, report / "second")
    assert second["status"] == "completed" and all(second["reused"].values())
    assert events == [] and snapshot(source) == before
    assert second["source_data_dir"] == str(source) and second["teacher_data_dir"] == str(run)


def test_incomplete_existing_source_is_preserved_and_not_fetched(workflow, monkeypatch):
    args, report, config, assets, events, source, run = workflow
    source.mkdir(parents=True)
    (source / "frames.jsonl").write_text('manual map and trim')
    before = snapshot(source)
    def broken(*a):
        raise PipelineError("incomplete source")
    monkeypatch.setattr(runner, "validate_source", broken)
    result = runner.process_task(args, "task_a", config, assets, {}, report / "failure")
    assert result["status"] == "failed" and result["stage"] == "source"
    assert events == [] and snapshot(source) == before


def test_changed_source_blocks_existing_labels_without_regeneration(workflow):
    args, report, config, assets, events, source, run = workflow
    assert runner.process_task(args, "task_a", config, assets, {}, report / "first")["status"] == "completed"
    (source / "frames.jsonl").write_text('{"map_name":"new_manual"}\n')
    events.clear()
    result = runner.process_task(args, "task_a", config, assets, {}, report / "changed")
    assert result["status"] == "failed" and result["stage"] == "teacher"
    assert events == [] and "new_manual" in (source / "frames.jsonl").read_text()


def test_corrupt_existing_pixels_are_reported_not_overwritten(workflow, monkeypatch):
    args, report, config, assets, events, source, run = workflow
    runner.process_task(args, "task_a", config, assets, {}, report / "first")
    pixel = args.output_root / "task_a/pixel_exports/run_1/samples.jsonl"
    pixel.write_text("damaged")
    events.clear()
    def invalid(*a):
        raise PipelineError("hash mismatch")
    monkeypatch.setattr(runner, "validate_pixels", invalid)
    result = runner.process_task(args, "task_a", config, assets, {}, report / "second")
    assert result["status"] == "failed" and result["stage"] == "pixels"
    assert pixel.read_text() == "damaged" and events == []


def test_teacher_cleanup_on_build_failure(workflow, monkeypatch):
    args, report, config, assets, events, source, run = workflow
    def broken(**kw):
        raise PipelineError("inference failed")
    monkeypatch.setattr(runner, "run_pending_batch", broken)
    result = runner.process_task(args, "task_a", config, assets, {}, report / "broken")
    assert result["status"] == "failed" and events == ["download", "start_teacher", "stop_teacher"]


def test_partial_failure_continues_and_keeps_json_result(tmp_path, monkeypatch, capsys):
    args = parse_args(["a", "b", "--source-root", str(tmp_path / "raw"), "--output-root", str(tmp_path / "out")])
    monkeypatch.setattr(runner, "preflight", lambda *a: {})
    calls = []
    def task(args, task, *rest):
        calls.append(task)
        return dict(task_id=task, status="failed" if task == "a" else "completed")
    monkeypatch.setattr(runner, "process_task", task)
    result = runner.run_pipeline(args, {})
    assert calls == ["a", "b"] and result["status"] == "partial_failure"
    assert result["counts"] == dict(selected=2, completed=1, failed=1)
    assert read_json(result["result_json"]) == result


def test_interrupt_preserves_completed_task_in_summary(tmp_path, monkeypatch):
    args = parse_args(["a", "b", "c", "--source-root", str(tmp_path / "raw"), "--output-root", str(tmp_path / "out")])
    monkeypatch.setattr(runner, "preflight", lambda *a: {})
    def task(args, task, *rest):
        if task == "b":
            raise KeyboardInterrupt
        return dict(task_id=task, status="completed")
    monkeypatch.setattr(runner, "process_task", task)
    result = runner.run_pipeline(args, {})
    assert result["status"] == "interrupted" and result["tasks"][0]["status"] == "completed"
    assert result["tasks"][2]["status"] == "not_started"


def test_no_data_writes_in_dry_run(tmp_path, monkeypatch):
    args = parse_args(["a", "--dry-run", "--source-root", str(tmp_path / "raw"),
                       "--output-root", str(tmp_path / "out")])
    monkeypatch.setattr(runner, "preflight", lambda *a: {})
    result = runner.run_pipeline(args, {})
    assert result["status"] == "dry_run" and list(tmp_path.iterdir()) == []


def test_stdout_is_one_json_document(monkeypatch, capsys):
    import run_data_pipeline
    monkeypatch.setattr(run_data_pipeline, "load_environment", lambda *a: {})
    monkeypatch.setattr(run_data_pipeline, "run_pipeline", lambda *a: dict(status="success", tasks=[]))
    assert main(["a"]) == 0
    assert json.loads(capsys.readouterr().out) == dict(status="success", tasks=[])


def test_lock_excludes_concurrent_pipeline(tmp_path):
    with pipeline_lock(tmp_path):
        with pytest.raises(PipelineError, match="已有流水线"):
            with pipeline_lock(tmp_path):
                pytest.fail("second lock acquired")


def test_pixel_validation_checks_identity_hashes_and_roundtrip(tmp_path):
    run = tmp_path / "run_1"
    case = run / "cases/sample_0000001"
    case.mkdir(parents=True)
    sample = dict(case_id=case.name, status="accepted", map_name="B10_map", route_id="r",
                  sub_task_id="task_1", meta_ts=100., grid_pose=dict(x=1.,y=2.,yaw_rad=8.),
                  teacher_history=dict(poses=[[.5,1.5,.1]], stamps_s=[99.]),
                  teacher=dict(commands=[dict(linear_mps=.2)]), initial_state={}, rgb_history={})
    write_json(run / "run.json", dict(status="success", accepted_count=1))
    write_json(case / "sample.json", sample)
    (run / "teacher_labels.jsonl").write_text(json.dumps(sample) + "\n")
    np.savez(case / "inputs.npz", teacher_history_pose=[[.5,1.5,.1]], teacher_history_stamp_s=[99.],
             forward_route=[[1.,2.],[3.,4.]])
    map_path = tmp_path / "map.npz"
    np.savez(map_path, occupancy=np.zeros((50,50)), origin_xy=[-1.,-2.], resolution_m=.1)
    maps = {"B10_map":str(map_path)}
    output = tmp_path / "pixels"
    export_pixels(output, maps, run=run)
    assert validation.validate_pixels(output, run, maps)["passed"]
    with (output / "samples.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(PipelineError, match="已改变"):
        validation.validate_pixels(output, run, maps)


def test_teacher_rejects_wrong_checkpoint_without_starting_process(tmp_path, monkeypatch):
    args = SimpleNamespace(teacher_url="http://127.0.0.1:8103")
    monkeypatch.setattr(teacher, "teacher_health", lambda *a: dict(status="ok", ready=True))
    monkeypatch.setattr(teacher.subprocess, "Popen", lambda *a,**k: pytest.fail("must not start a replacement"))
    with pytest.raises(PipelineError):
        with teacher.teacher_service(args, {}, {}, tmp_path):
            pytest.fail("incompatible service reused")
    assert read_json(tmp_path / "teacher_lifecycle.json")["owned_teacher_stopped"] is False


def test_cloud_preflight_requires_ready_current_revision(monkeypatch):
    item = dict(sub_task_id="task_a_1", camera="cam0", status="ready", object_key="obj", edits_revision=2)
    task = dict(task_id="task_a", status="stopped", edits_revision=2,
                video_continuous={"items":[item]})
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({"task":task})
    class Opener:
        def open(self, request, **kwargs): return Response()
    monkeypatch.setattr(runner.urllib.request, "build_opener", lambda *a: Opener())
    environment = {"SKDOS_API_KEY":"test-key"}
    assert runner.cloud_readiness("task_a", environment)["cameras_by_subtask"] == {"task_a_1":["cam0"]}
    item["edits_revision"] = 1
    with pytest.raises(PipelineError, match="修订过期"):
        runner.cloud_readiness("task_a", environment)
    item["edits_revision"] = 2
    item["object_key"] = None
    with pytest.raises(PipelineError):
        runner.cloud_readiness("task_a", environment)
    item["object_key"] = "obj"
    task["video_continuous"]["items"].append(item.copy())
    with pytest.raises(PipelineError, match="重复"):
        runner.cloud_readiness("task_a", environment)


def test_changed_manual_map_and_trim_are_detected_in_legacy_run(tmp_path):
    source = tmp_path / "task_a"
    meta = source / "meta/unpacked/meta_task_a_1"
    meta.mkdir(parents=True)
    frame_path = meta / "frames.jsonl"
    frame_path.write_text('{"ts":100,"map_name":"P_map"}\n')
    write_json(meta / "task.json", {})
    manifest = source / "videos/videos_task_a_1/manifest.json"
    write_json(manifest, {})
    for path in (frame_path, meta / "task.json", manifest):
        os.utime(path, (100,100))
    config = {key:"v1" for key in ("pipeline_version", "route_version", "state_sampler_version", "occupancy_fusion_version")}
    assets = dict(checkpoint_sha256="weights", teacher_config_sha256="config")
    run = tmp_path / "run_1"
    write_json(run / "run.json", dict(config, **assets, status="success", mode="full",
        full_generation_authorized=True, accepted_count=1, task_root=str(source), completed_at="2026-01-01T00:00:00+08:00"))
    (run / "source_manifest.jsonl").write_text(json.dumps(dict(source_sub_task_id="task_a_1", row_index=0,
        meta_ts=100, source_map_name="P_map")) + "\n")
    validation.check_run_source(run, source, assets, config)
    frame_path.write_text('{"ts":100,"map_name":"B10_map"}\n')
    os.utime(frame_path, (100,100))
    with pytest.raises(PipelineError, match="map_name"):
        validation.check_run_source(run, source, assets, config)
    frame_path.write_text('{"ts":100,"map_name":"P_map"}\n')
    os.utime(frame_path, (100,100))
    write_json(manifest, {"local_trim":{"from":10,"to":20}})
    with pytest.raises(PipelineError, match="裁剪晚于"):
        validation.check_run_source(run, source, assets, config)


def test_owned_teacher_is_stopped_when_health_validation_fails(tmp_path, monkeypatch):
    args = SimpleNamespace(teacher_url="http://127.0.0.1:8103", no_start_teacher=False,
        model_server_root=tmp_path / "repo/dev/model_server", teacher_config=tmp_path / "config.yaml",
        teacher_python=Path(sys.executable), teacher_device="cpu", teacher_start_timeout=1)
    healths = iter([None, dict(ready=True, pid=42)])
    monkeypatch.setattr(teacher, "teacher_health", lambda *a: next(healths))
    class Process:
        pid = 42
        code = None
        def poll(self): return self.code
        def terminate(self): self.code = 0
        def wait(self, **kwargs): return self.code
    process = Process()
    def start(command, **kwargs):
        assert command[-1] == "--batch"
        assert kwargs["cwd"] == tmp_path / "repo"
        return process
    monkeypatch.setattr(teacher.subprocess, "Popen", start)
    with pytest.raises(PipelineError):
        with teacher.teacher_service(args, {}, {}, tmp_path):
            pytest.fail("bad service accepted")
    assert process.poll() == 0
    assert read_json(tmp_path / "teacher_lifecycle.json")["owned_teacher_stopped"] is True


def test_missing_teacher_assets_fail_before_discovery_or_source_creation(tmp_path, monkeypatch):
    args = parse_args(["--since", "2026-09-01", "--source-root", str(tmp_path / "source"),
                       "--output-root", str(tmp_path / "output"), "--teacher-config", str(tmp_path / "missing.yaml")])
    monkeypatch.setattr(runner, "discover", lambda *a: pytest.fail("must validate teacher first"))
    with pytest.raises(PipelineError, match="缺少教师依赖"):
        runner.run_pipeline(args, {})
    assert not args.source_root.exists() and not args.output_root.exists()
