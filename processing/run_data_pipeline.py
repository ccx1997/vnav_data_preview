#!/usr/bin/env python3
"""Download, preprocess, teacher-label, and pixelize selected VNav tasks."""
import argparse
from datetime import datetime, time
from pathlib import Path
import json
import math
import signal
import sys
from zoneinfo import ZoneInfo

from data_pipeline import PROJECT
from data_pipeline.common import PipelineError, load_environment, task_ids, write_json
from data_pipeline.runner import run_pipeline


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="任务 ID、ID 列表或单个 YYYY-MM-DD 起始日期")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--task-id", action="append", default=[], help="指定任务，可重复")
    group.add_argument("--task-ids", nargs="+", help="空格/逗号分隔的 ID 或 JSON 数组")
    group.add_argument("--since", "--date", help="起始日期/时间，默认 Asia/Shanghai，包含下界")
    parser.add_argument("--until", help="截止日期/时间，默认当前上海时间")
    parser.add_argument("--robot", default="", help="日期模式的机器人过滤条件")
    parser.add_argument("--source-root", type=Path, default=Path("/mnt/chengchangxu/data/visual_nav_mv"))
    parser.add_argument("--output-root", type=Path, default=Path("/mnt/chengchangxu/data/visual_nav_training"))
    parser.add_argument("--result-json", type=Path, help="另存最终 JSON（必须位于采集/训练根目录之外）")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--model-server-root", type=Path,
                        default=Path("/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server"))
    parser.add_argument("--teacher-config", type=Path)
    parser.add_argument("--static-map-root", type=Path)
    parser.add_argument("--teacher-python", type=Path, default=Path("/mnt/zrh/miniconda3/envs/navrl_try/bin/python"))
    parser.add_argument("--teacher-device", default="auto", help="auto 或 cuda:N/cpu")
    parser.add_argument("--teacher-url", help="显式复用/启动指定 URL；省略则选择空闲本地端口")
    parser.add_argument("--teacher-start-timeout", type=float, default=180)
    parser.add_argument("--no-start-teacher", action="store_true")
    parser.add_argument("--no-feishu", action="store_true", help="不发送本次运行通知")
    parser.add_argument("--dry-run", action="store_true", help="只做预检/日期发现和计划，不下载、启动教师或写数据")
    args = parser.parse_args(argv)
    if args.inputs and (args.task_id or args.task_ids or args.since):
        parser.error("位置参数不能与 --task-id/--task-ids/--since 混用")
    inputs = args.inputs or args.task_id or args.task_ids or []
    if len(inputs) == 1 and len(inputs[0]) == 10 and inputs[0][4:5] == "-" and inputs[0][7:8] == "-":
        args.since, inputs = inputs[0], []
    if not inputs and not args.since:
        parser.error("请提供任务 ID/列表或 --since 日期")
    if not args.since and (args.until or args.robot):
        parser.error("--until/--robot 仅用于日期模式")
    if args.jobs < 1 or not math.isfinite(args.teacher_start_timeout) or args.teacher_start_timeout <= 0:
        parser.error("jobs 和 teacher-start-timeout 必须为正数")
    if args.no_start_teacher and not args.teacher_url:
        parser.error("--no-start-teacher 需要 --teacher-url")
    args.ids = task_ids(inputs) if inputs else []
    if args.since:
        zone = ZoneInfo("Asia/Shanghai")
        def bound(value, end=False):
            parsed = datetime.fromisoformat(value)
            if len(value) == 10 and end:
                parsed = datetime.combine(parsed.date(), time(23, 59, 59, 999999))
            return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
        start = bound(args.since)
        stop = bound(args.until, True) if args.until else datetime.now(zone)
        if start > stop:
            parser.error("起始日期不能晚于截止时间")
        args.since, args.until = start.isoformat(), stop.isoformat()
    for name in ("source_root", "output_root", "model_server_root", "teacher_python"):
        setattr(args, name, getattr(args, name).resolve())
    if args.source_root.is_relative_to(args.output_root) or args.output_root.is_relative_to(args.source_root):
        parser.error("采集和训练根目录必须互不包含")
    args.teacher_config = (args.teacher_config or args.model_server_root / "model_server/config_rule10_continuous_ep011200_sim.yaml").resolve()
    args.static_map_root = (args.static_map_root or args.model_server_root / "model_server/maps").resolve()
    if args.result_json:
        args.result_json = args.result_json.resolve()
        if args.result_json.is_relative_to(args.source_root) or args.result_json.is_relative_to(args.output_root):
            parser.error("--result-json 必须位于采集/训练根目录之外，避免覆盖已有数据")
    return args


def main(argv=None):
    args = None
    try:
        args = parse_args(argv)
        document = run_pipeline(args, load_environment(PROJECT))
        code = (0 if document["status"] in ("success", "dry_run") else
                130 if document["status"] == "interrupted" else 1)
    except KeyboardInterrupt:
        document = dict(schema_version="vnav_data_pipeline_v1", status="interrupted",
                        error="运行被中断；已完成的结果保留，日志见 _pipeline_runs", tasks=[])
        code = 130
    except Exception as error:
        document = dict(schema_version="vnav_data_pipeline_v1", status="failed",
                        error=f"{type(error).__name__}: {error}", tasks=[])
        code = 2
    if args and args.result_json:
        try:
            write_json(args.result_json, document)
        except OSError as error:
            document.update(status="failed", result_json_error=str(error))
            code = 2
    print(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
