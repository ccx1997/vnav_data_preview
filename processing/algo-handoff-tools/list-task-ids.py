#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按起止日期时间列出范围内的全部采集任务 ID。

对应：GET /api/annotation-tasks/ids?from=&to=&robot=

未写时区的日期时间由服务端按 Asia/Shanghai 解析。只给日期时，
from 表示当天开始，to 表示当天结束。鉴权只从环境变量 SKDOS_API_KEY 读取。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE = os.environ.get("SKDOS_LIVE_URL") or "https://skdos-live-test.uniubi.com"


def _die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _normalize_base(url: str) -> str:
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        _die("base-url 为空")
    if not normalized.startswith("http://") and not normalized.startswith("https://"):
        normalized = "https://" + normalized
    return normalized


def _headers() -> Dict[str, str]:
    api_key = (os.environ.get("SKDOS_API_KEY") or "").strip()
    if not api_key:
        _die("缺少 SKDOS_API_KEY；请在本机环境变量中配置 API Key，勿写入仓库")
    return {
        "Accept": "application/json",
        "User-Agent": "skdos-list-task-ids/1",
        "X-API-Key": api_key,
    }


def _http_error_body(error: urllib.error.HTTPError, limit: int = 400) -> str:
    try:
        raw = error.read() or b""
    except Exception:
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return str(payload.get("error") or payload.get("detail") or text)[:limit]
    except Exception:
        pass
    return text[:limit]


def api_get_json(url: str, headers: Dict[str, str], timeout: int = 60) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        extra = _http_error_body(error)
        hint = ""
        if error.code in (401, 403):
            hint = " 需要 X-API-Key（请配置环境变量 SKDOS_API_KEY）"
        _die(
            "HTTP %s %s%s%s"
            % (error.code, url, (": " + extra) if extra else "", hint),
            1,
        )
    except urllib.error.URLError as error:
        _die("无法连接 %s: %s" % (url, error.reason), 1)
    return None


def build_ids_url(base: str, from_value: str, to_value: str, robot: str = "") -> str:
    query = {"from": from_value, "to": to_value}
    if robot:
        query["robot"] = robot
    return base + "/api/annotation-tasks/ids?" + urllib.parse.urlencode(query)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按起止时间列出范围内全部采集 task_id（不受看板 200 条限制）",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE,
        help="skdos-live 基址（默认 env SKDOS_LIVE_URL 或 test 域）",
    )
    parser.add_argument(
        "--from",
        dest="from_value",
        required=True,
        help="起始：unix 秒，或 YYYY-MM-DD / YYYY-MM-DD HH:MM[:SS]",
    )
    parser.add_argument(
        "--to",
        dest="to_value",
        required=True,
        help="结束（闭区间）；只给日期时包含当天整天",
    )
    parser.add_argument("--robot", default="", help="可选 robot_id，如 Robot-U2-V1")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="打印服务端 JSON")
    output.add_argument("--ids-only", action="store_true", help="每行只打印一个 task_id")
    parser.add_argument("--dry-run", action="store_true", help="只打印将请求的 URL")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时秒（默认 60）")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.timeout < 1:
        _die("--timeout 必须大于 0")
    base = _normalize_base(args.base_url)
    url = build_ids_url(base, args.from_value, args.to_value, args.robot)
    if args.dry_run:
        print("[dry-run] GET %s" % url)
        return 0

    body = api_get_json(url, _headers(), timeout=args.timeout)
    if not isinstance(body, dict):
        _die("响应不是 JSON 对象", 1)
    if body.get("error"):
        _die("查询失败: %s" % (body.get("detail") or body.get("error")), 1)
    tasks = body.get("tasks")
    if not isinstance(tasks, list):
        _die("响应缺少 tasks[]", 1)

    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    if args.ids_only:
        for task in tasks:
            if isinstance(task, dict) and task.get("task_id"):
                print(task["task_id"])
        return 0

    print("%-22s %-14s %-10s %s" % ("task_id", "robot_id", "status", "title"))
    for task in tasks:
        if not isinstance(task, dict):
            continue
        print("%-22s %-14s %-10s %s" % (
            str(task.get("task_id") or ""),
            str(task.get("robot_id") or ""),
            str(task.get("status") or ""),
            str(task.get("title") or ""),
        ))
    try:
        count = int(body.get("count"))
    except (TypeError, ValueError):
        count = len(tasks)
    suffix = "（已截断，请缩小时间窗）" if body.get("truncated") else ""
    print("%d 条%s" % (count, suffix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
