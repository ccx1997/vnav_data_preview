from collections import defaultdict
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

from vnav_coordinates.export import export_pixels
from vnav_training.batch import run_pending_batch

from . import PROJECT
from .common import (PipelineError, digest, now, pipeline_lock, read_json, run_logged,
                     snapshot, task_ids, write_json)
from .teacher import preflight, teacher_service
from .validation import (check_run_source, require, validate_pixels, validate_source,
                         validate_teacher)


def discover(args, environment):
    # The service accepts unix seconds, but rejects ISO strings with UTC offsets.
    start = str(datetime.fromisoformat(args.since).timestamp())
    stop = str(datetime.fromisoformat(args.until).timestamp())
    command = [sys.executable, str(PROJECT / "processing/algo-handoff-tools/list-task-ids.py"),
               "--from", start, "--to", stop, "--json"]
    if args.robot:
        command += ["--robot", args.robot]
    result = subprocess.run(command, env=environment, text=True, capture_output=True)
    if result.returncode:
        http = re.search(r"HTTP \d{3}", result.stderr or "")
        detail = f"，{http.group()}" if http else ""
        raise PipelineError(f"日期任务查询失败（退出码 {result.returncode}{detail}），请检查凭据/日期/服务")
    payload = json.loads(result.stdout)
    require(isinstance(payload, dict) and isinstance(payload.get("tasks"), list), "日期查询缺少 tasks[]")
    require(not payload.get("truncated"), "日期查询被截断，请缩小日期范围；尚未下载")
    require(not payload.get("error"), "日期查询返回错误")
    ids = []
    for item in payload["tasks"]:
        require(isinstance(item, dict) and isinstance(item.get("task_id"), str), "查询含无效任务 ID")
        ids.extend(task_ids([item["task_id"]]))
    return list(dict.fromkeys(ids))


def cloud_readiness(task, environment):
    key = environment.get("SKDOS_API_KEY")
    require(bool(key), "新任务需要 SKDOS_API_KEY；请配置环境变量或项目 .env")
    base = environment.get("SKDOS_LIVE_URL", "https://skdos-live-test.uniubi.com").rstrip("/")
    request = urllib.request.Request(base + "/api/annotation-tasks/" + task, headers={"X-API-Key":key})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
            document = json.load(response)["task"]
    except urllib.error.HTTPError as error:
        raise PipelineError(f"云端任务查询 HTTP {error.code}") from None
    items = (document.get("video_continuous") or {}).get("items") or []
    cameras = defaultdict(set)
    seen = set()
    require(document.get("task_id") == task and document.get("status") in ("stopped", "completed")
            and bool(items), "云端任务仍在采集或没有连续视频；尚未下载")
    for item in items:
        sid, camera = item.get("sub_task_id"), item.get("camera")
        require(isinstance(sid, str) and sid.startswith(task + "_") and isinstance(camera, str)
                and bool(camera) and (sid, camera) not in seen, "云端子任务/相机身份无效或重复")
        require(item.get("status") == "ready" and bool(item.get("object_key"))
                and item.get("edits_revision") == document.get("edits_revision", 0),
                "云端视频尚未全部就绪或修订过期；保留现场，不自动重试")
        seen.add((sid, camera))
        cameras[sid].add(camera)
    return dict(ready=True, video_count=len(items), edits_revision=document.get("edits_revision", 0),
                cameras_by_subtask={sid:sorted(values) for sid,values in cameras.items()})


def existing_run(root, task, config):
    full = root / task / config["pipeline_version"] / "full"
    # A corrupt latest run must not silently fall back to an older result or be regenerated.
    for directory in sorted(full.glob("run_*"), reverse=True):
        info = read_json(directory / "run.json")
        if info.get("status") == "success":
            return directory.resolve()
    return None


def task_plan(args, task, config):
    source = args.source_root / task
    run = existing_run(args.output_root, task, config)
    pixel = args.output_root / task / "pixel_exports" / run.name if run else None
    return dict(task_id=task, status="planned", source_data_dir=str(source),
                annotation_data_dir=str(args.output_root / task),
                teacher_data_dir=str(run) if run else None, pixel_data_dir=str(pixel) if pixel else None,
                planned_actions=dict(source="validate_and_reuse" if source.exists() else "download_and_preprocess",
                                     teacher="validate_and_reuse" if run else "generate",
                                     pixels="validate_and_reuse" if pixel and pixel.exists() else "export"))


def notify(args, report, item, environment):
    if args.no_feishu:
        return dict(status="disabled")
    script = Path("/mnt/chengchangxu/.codex/skills/send-feishu-experiment/scripts/send_feishu_experiment.py")
    if not script.is_file() or not environment.get("FEISHU_WEBHOOK"):
        return dict(status="unavailable", reason="skill or FEISHU_WEBHOOK unavailable")
    conclusion = (f"四步完成：{item['counts']['accepted_count']} 个教师/像素样本；"
                  f"无黑帧 {item['counts']['without_black_frames_count']}；验证通过，源数据未变。"
                  if item["status"] == "completed" else f"{item['stage']} 失败：{item['error']}")
    command = [sys.executable, str(script), "--name", item["task_id"] + " 数据流水线",
               "--status", item["status"], "--optimization", "无需优化", "--conclusion", conclusion,
               "--extra", f"结果目录：{report}"]
    try:
        for suffix, extra in (("payload", ["--dry-run"]), ("response", [])):
            result = subprocess.run(command + extra, env=environment, text=True, capture_output=True, timeout=30)
            write_json(report / f"feishu_{suffix}.json", dict(returncode=result.returncode,
                                                           stdout=result.stdout, stderr=result.stderr))
            if result.returncode:
                return dict(status="failed", reason=f"Feishu {suffix} failed; no retry")
        return dict(status="sent")
    except (OSError, subprocess.TimeoutExpired) as error:
        return dict(status="failed", reason=type(error).__name__)


def process_task(args, task, config, assets, environment, report):
    source = args.source_root / task
    item = dict(task_id=task, status="running", stage="source", source_data_dir=str(source),
                annotation_data_dir=str(args.output_root / task), teacher_data_dir=None,
                pixel_data_dir=None, report_dir=str(report), reused={})
    report.mkdir()
    before = None
    try:
        before = snapshot(source) if source.exists() else None
        write_json(report / "source_before.json", before)
        print(f"[{task}] 下载/预处理或复用源数据", file=sys.stderr, flush=True)
        # Existing source is never fetched again, even when integrity checks fail.
        cloud = None
        item["reused"]["source"] = source.exists()
        if not source.exists():
            cloud = cloud_readiness(task, environment)
            write_json(report / "cloud_ready.json", cloud)
            run_logged(["bash", PROJECT / "processing/algo-handoff-tools/download-task.sh",
                        "--jobs", str(args.jobs), task, args.source_root], report / "download.log",
                       cwd=PROJECT, env=environment)
        source_validation = validate_source(source, cloud)
        write_json(report / "source_validation.json", source_validation)
        if before is None:
            before = snapshot(source)
            write_json(report / "source_before.json", before)
        item["stage"] = "teacher"
        run = existing_run(args.output_root, task, config)
        item["reused"]["teacher"] = run is not None
        if run is None:
            print(f"[{task}] 生成教师标签", file=sys.stderr, flush=True)
            with teacher_service(args, config, assets, report) as url:
                with (report / "teacher_build.log").open("x") as log, redirect_stdout(log):
                    _, batch = run_pending_batch(source_root=args.source_root, output_root=args.output_root,
                        config=config, teacher_url=url, model_server_root=args.model_server_root,
                        teacher_config=args.teacher_config, static_map_root=args.static_map_root,
                        teacher_python=args.teacher_python, teacher_device=args.teacher_device,
                        teacher_start_timeout_s=args.teacher_start_timeout, auto_start_teacher=False,
                        dry_run=False, task_ids=(task,))
                write_json(report / "teacher_batch.json", batch)
                result = next(value for value in batch["tasks"] if value["task_id"] == task)
                require(result["status"] in ("generated", "already_completed"),
                        f"教师未成功生成：{result.get('error') or result.get('reason') or result['status']}")
                run = Path(result.get("output") or result["completed_run"]).resolve()
        item["teacher_data_dir"] = str(run)
        check_run_source(run, source, assets, config)
        state_path = args.output_root / "_pipeline_state" / task / (run.name + ".json")
        baseline = dict(source=before, config=config, teacher_config_sha256=assets["teacher_config_sha256"],
                        checkpoint_sha256=assets["checkpoint_sha256"])
        if state_path.exists():
            require(read_json(state_path) == baseline, "源数据/参数与已有标签绑定记录不同；不自动重处理")
        print(f"[{task}] 验证教师和 RGB", file=sys.stderr, flush=True)
        item["counts"] = validate_teacher(run, config, report, args.jobs)
        item["stage"] = "pixels"
        pixel = args.output_root / task / "pixel_exports" / run.name
        item["pixel_data_dir"] = str(pixel)
        item["reused"]["pixels"] = pixel.exists()
        print(f"[{task}] 像素坐标{'复用验证' if pixel.exists() else '导出'}", file=sys.stderr, flush=True)
        if not pixel.exists():
            export_pixels(pixel, assets["maps"], run=run)
        pixel_validation = validate_pixels(pixel, run, assets["maps"])
        write_json(report / "pixel_validation.json", pixel_validation)
        item["pixel_manifest"] = str(pixel / "samples.jsonl")
        item["teacher_labels"] = str(run / "teacher_labels.jsonl")
        item["samples_without_black_frames"] = str(report / "samples_without_black_frames.jsonl")
        item["counts"]["pixel_count"] = pixel_validation["row_count"]
        item["source_counts"] = {k:v for k,v in source_validation.items() if k not in ("subtasks", "passed")}
        item["stage"] = "source_protection"
        require(snapshot(source) == before, "处理过程中源数据发生改变；请检查 source_before.json")
        if not state_path.exists():
            write_json(state_path, baseline)
        item.update(status="completed", stage="done", source_unchanged=True)
    except KeyboardInterrupt:
        item.update(status="interrupted", error="运行被中断；保留当前产物")
        raise
    except Exception as error:
        item.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(f"[{task}] {item['stage']} 失败：{item['error']}", file=sys.stderr, flush=True)
    finally:
        if before is not None:
            try:
                unchanged = snapshot(source) == before
                write_json(report / "source_protection.json", dict(passed=unchanged, checked_files=len(before)))
                item["source_unchanged"] = unchanged
                if not unchanged:
                    item.update(status="failed", error="源文件前后保护核验失败", stage="source_protection")
            except OSError as error:
                item.update(status="failed", stage="source_protection", error=str(error))
        write_json(report / "result.json", item)
    item["notification"] = notify(args, report, item, environment)
    write_json(report / "result.json", item)
    return item


def run_pipeline(args, environment):
    config = read_json(PROJECT / "processing/training-data-builder/config.json")
    assets = preflight(args, config)
    ids = discover(args, environment) if args.since else args.ids
    document = dict(schema_version="vnav_data_pipeline_v1", status="dry_run" if args.dry_run else "running",
                    selection=dict(task_ids=ids, since=args.since, until=args.until if args.since else None,
                                   robot=args.robot or None), started_at=now(), tasks=[])
    if args.dry_run:
        document["tasks"] = [task_plan(args, task, config) for task in ids]
        document["counts"] = dict(selected=len(ids), completed=0, failed=0)
        return document
    batch_id = now().replace(":", "").replace("+", "_") + "_" + uuid.uuid4().hex[:8]
    report = args.output_root / "_pipeline_runs" / batch_id
    report.mkdir(parents=True, exist_ok=False)
    document.update(report_dir=str(report), result_json=str(report / "result.json"))
    write_json(report / "preflight.json", assets)
    write_json(report / "pipeline_config.json", config)
    write_json(report / "result.json", document)
    try:
        with pipeline_lock(args.source_root):
            for task in ids:
                item = process_task(args, task, config, assets, environment, report / task)
                document["tasks"].append(item)
                write_json(report / "result.json", document)
    except (KeyboardInterrupt, Exception) as error:
        document.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                        error=f"{type(error).__name__}: {error}")
        accounted = {item["task_id"] for item in document["tasks"]}
        for task in ids:
            if task in accounted:
                continue
            path = report / task / "result.json"
            document["tasks"].append(read_json(path) if path.exists() else
                                     dict(task_id=task, status="not_started"))
    completed = sum(item["status"] == "completed" for item in document["tasks"])
    failed = len(ids) - completed
    status = document["status"] if "error" in document else (
        "success" if not failed else "partial_failure" if completed else "failed")
    document.update(status=status,
                    counts=dict(selected=len(ids), completed=completed, failed=failed), completed_at=now())
    write_json(report / "result.json", document)
    return document
