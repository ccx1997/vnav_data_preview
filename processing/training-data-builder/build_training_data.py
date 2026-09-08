#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from vnav_training.pipeline import load_config, run_full, run_pilot


SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_TASK_ROOT = Path("/mnt/chengchangxu/data/visual_nav_mv/20260820180215WDK")
DEFAULT_OUTPUT_ROOT = Path("/mnt/chengchangxu/data/visual_nav_training")
DEFAULT_MODEL_SERVER_ROOT = Path(
    "/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server"
)
DEFAULT_TEACHER_CONFIG = DEFAULT_MODEL_SERVER_ROOT / (
    "model_server/config_rule10_continuous_ep023000_sim.yaml"
)
DEFAULT_STATIC_MAP_ROOT = DEFAULT_MODEL_SERVER_ROOT / "model_server/maps"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--config", type=Path, default=SCRIPT_ROOT / "config.json")
    parser.add_argument("--teacher-url", default="http://127.0.0.1:8103")
    parser.add_argument(
        "--model-server-root", type=Path, default=DEFAULT_MODEL_SERVER_ROOT
    )
    parser.add_argument("--teacher-config", type=Path, default=DEFAULT_TEACHER_CONFIG)
    parser.add_argument("--static-map-root", type=Path, default=DEFAULT_STATIC_MAP_ROOT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build synchronized multi-view VNav teacher-label data"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    pilot = subparsers.add_parser(
        "pilot", help="build a reviewable 12-case pilot only"
    )
    _common(pilot)
    pilot.add_argument("--revision", type=int, default=1)
    full = subparsers.add_parser(
        "full", help="build all eligible cases after explicit user approval"
    )
    _common(full)
    full.add_argument(
        "--confirm-full",
        required=True,
        help="must be exactly SATISFIED; do not use before review approval",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config.resolve())
    common = {
        "task_root": args.task_root.resolve(),
        "output_root": args.output_root.resolve(),
        "teacher_url": str(args.teacher_url),
        "model_server_root": args.model_server_root.resolve(),
        "teacher_config": args.teacher_config.resolve(),
        "static_map_root": args.static_map_root.resolve(),
        "config": config,
    }
    if args.mode == "pilot":
        output = run_pilot(revision=int(args.revision), **common)
    else:
        output = run_full(confirmation=str(args.confirm_full), **common)
    print(
        json.dumps(
            {"status": "success", "mode": args.mode, "output": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
