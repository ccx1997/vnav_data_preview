#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from vnav_training.batch import run_pending_batch
from vnav_training.pipeline import load_config


SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = Path("/mnt/chengchangxu/data/visual_nav_mv")
DEFAULT_OUTPUT_ROOT = Path("/mnt/chengchangxu/data/visual_nav_training")
DEFAULT_MODEL_SERVER_ROOT = Path(
    "/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server"
)
DEFAULT_TEACHER_CONFIG = DEFAULT_MODEL_SERVER_ROOT / (
    "model_server/config_rule10_continuous_ep023000_sim.yaml"
)
DEFAULT_STATIC_MAP_ROOT = DEFAULT_MODEL_SERVER_ROOT / "model_server/maps"
DEFAULT_TEACHER_PYTHON = Path("/mnt/zrh/miniconda3/envs/navrl_try/bin/python")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover ready tasks without a successful full run and build them"
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--config", type=Path, default=SCRIPT_ROOT / "config.json")
    parser.add_argument("--teacher-url", default="http://127.0.0.1:8103")
    parser.add_argument("--model-server-root", type=Path, default=DEFAULT_MODEL_SERVER_ROOT)
    parser.add_argument("--teacher-config", type=Path, default=DEFAULT_TEACHER_CONFIG)
    parser.add_argument("--static-map-root", type=Path, default=DEFAULT_STATIC_MAP_ROOT)
    parser.add_argument("--teacher-python", type=Path, default=DEFAULT_TEACHER_PYTHON)
    parser.add_argument("--teacher-device", default="cuda:0")
    parser.add_argument("--teacher-start-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--no-start-teacher",
        action="store_true",
        help="require an already-ready teacher instead of starting one",
    )
    parser.add_argument("--dry-run", action="store_true", help="scan only")
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="limit the scan to one task ID; repeat for multiple tasks",
    )
    parser.add_argument(
        "--confirm-full",
        required=True,
        help="must be exactly SATISFIED; the one-click shell wrapper supplies it",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.confirm_full != "SATISFIED":
        raise ValueError("pending full mode requires --confirm-full SATISFIED")
    config = load_config(args.config.resolve())
    batch_dir, document = run_pending_batch(
        source_root=args.source_root.resolve(),
        output_root=args.output_root.resolve(),
        config=config,
        teacher_url=str(args.teacher_url),
        model_server_root=args.model_server_root.resolve(),
        teacher_config=args.teacher_config.resolve(),
        static_map_root=args.static_map_root.resolve(),
        teacher_python=args.teacher_python.resolve(),
        teacher_device=str(args.teacher_device),
        teacher_start_timeout_s=float(args.teacher_start_timeout_s),
        auto_start_teacher=not args.no_start_teacher,
        dry_run=bool(args.dry_run),
        task_ids=tuple(args.task_id),
    )
    print(
        json.dumps(
            {
                "status": document["status"],
                "batch_dir": str(batch_dir) if batch_dir else None,
                "counts": document["counts"],
                "tasks": document["tasks"],
            },
            ensure_ascii=False,
        )
    )
    return 1 if int(document["counts"].get("failed") or 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
