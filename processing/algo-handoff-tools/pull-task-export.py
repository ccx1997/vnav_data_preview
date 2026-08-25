#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练机：直接从 skdos-live 拉「任务数据导出」ZIP / JSONL，不必打开看板点下载。

对应看板「任务数据导出」：
  GET /api/annotation-tasks/{task_id}/export?format=zip&grids=png&pack=algo&sub_task=all

依赖：Python 3.8+、能访问 skdos-live。
      鉴权：X-API-Key。运行前必须通过环境变量 SKDOS_API_KEY 配置；
      仓库不保存真实 Key。

用法：
  python3 pull-task-export.py --list --robot Robot-U2-V1
  python3 pull-task-export.py --task 20260820180215WDK --out ./meta
  python3 pull-task-export.py --task 20260820180215WDK --sub-task 7 --out ./meta
  python3 pull-task-export.py --task 20260820180215WDK --format jsonl --sub-task 1 --out ./meta
  python3 pull-task-export.py --task 20260820180215WDK --out ./meta --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = os.environ.get("SKDOS_LIVE_URL") or "https://skdos-live-test.uniubi.com"
_DISPOSITION_NAME_RE = re.compile(
    r'filename\*=UTF-8\'\'([^;]+)|filename="([^"]+)"|filename=([^;\s]+)',
    re.IGNORECASE,
)


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        _die("base-url 为空")
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    return u


def _headers() -> Dict[str, str]:
    key = (os.environ.get("SKDOS_API_KEY") or "").strip()
    if not key:
        _die("缺少 SKDOS_API_KEY；请在本机环境变量中配置 API Key，勿写入仓库")
    return {
        "Accept": "*/*",
        "User-Agent": "skdos-pull-task-export/1",
        "X-API-Key": key,
    }


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_open(req: urllib.request.Request, timeout: int):
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())


def _http_error_body(e: urllib.error.HTTPError, limit: int = 400) -> str:
    try:
        raw = e.read() or b""
    except Exception:
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return str(obj.get("error") or obj.get("detail") or text)[:limit]
    except Exception:
        pass
    return text[:limit]


def api_get_json(url: str, headers: Dict[str, str], timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _http_open(req, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        extra = _http_error_body(e)
        hint = ""
        if e.code in (401, 403):
            hint = " 需要 X-API-Key（请配置环境变量 SKDOS_API_KEY）"
        _die("HTTP %s %s%s%s" % (e.code, url, (": " + extra) if extra else "", hint), 1)
    except urllib.error.URLError as e:
        _die("无法连接 %s: %s" % (url, e.reason), 1)
    return None


def filename_from_disposition(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    m = _DISPOSITION_NAME_RE.search(value)
    if not m:
        return fallback
    raw = m.group(1) or m.group(2) or m.group(3) or ""
    name = urllib.parse.unquote(raw).strip().strip('"')
    name = os.path.basename(name.replace("\\", "/"))
    if not name or name in (".", ".."):
        return fallback
    return name


def export_query(args: argparse.Namespace) -> List[Tuple[str, str]]:
    fmt = args.format
    pack = args.pack
    grids = args.grids
    if grids is None:
        grids = "png" if fmt == "zip" else "meta"
    sub = args.sub_task
    if fmt == "jsonl" and (not sub or sub == "all"):
        _die("JSONL 必须指定单个子任务：--sub-task 1（或 2、3…），不能 all")
    if not sub:
        sub = "all" if fmt == "zip" else "1"
    q = {
        "format": fmt,
        "grids": grids,
        "pack": pack,
        "sub_task": str(sub),
    }
    return list(q.items())


def export_url(base: str, task_id: str, query: List[Tuple[str, str]]) -> str:
    path = "/api/annotation-tasks/%s/export" % urllib.parse.quote(task_id, safe="")
    return base + path + "?" + urllib.parse.urlencode(query)


def default_filename(task_id: str, fmt: str, sub: str) -> str:
    ext = "zip" if fmt == "zip" else "jsonl"
    if sub == "all":
        return "meta_%s_all.%s" % (task_id, ext)
    return "meta_%s_%s.%s" % (task_id, sub, ext)


def cmd_list(args: argparse.Namespace) -> int:
    base = _normalize_base(args.base_url)
    url = base + "/api/annotation-tasks"
    if args.robot:
        url += "?robot=" + urllib.parse.quote(args.robot)
    if args.dry_run:
        print("[dry-run] GET %s" % url)
        return 0
    headers = _headers()
    body = api_get_json(url, headers, timeout=30)
    tasks = body.get("tasks") if isinstance(body, dict) else None
    if not isinstance(tasks, list):
        _die("列表响应不是 {tasks: [...]}", 1)
    if not tasks:
        print("没有任务" + ("（robot=%s）" % args.robot if args.robot else ""))
        return 0
    print("%-22s %-14s %-10s %s" % ("task_id", "robot_id", "status", "title"))
    for t in tasks:
        if not isinstance(t, dict):
            continue
        print("%-22s %-14s %-10s %s" % (
            str(t.get("task_id") or ""),
            str(t.get("robot_id") or ""),
            str(t.get("status") or ""),
            str(t.get("title") or ""),
        ))
    print("%d 条" % len(tasks))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    task_id = (args.task or "").strip()
    if not task_id:
        _die("需要 --task <task_id>，或用 --list 查看任务")
    base = _normalize_base(args.base_url)
    query = export_query(args)
    url = export_url(base, task_id, query)
    sub = dict(query).get("sub_task") or "all"
    fallback = default_filename(task_id, args.format, sub)
    out_dir = os.path.abspath(args.out)
    dest = os.path.join(out_dir, fallback)

    print("GET %s" % url)
    if args.dry_run:
        print("[dry-run] → %s" % dest)
        return 0

    headers = _headers()
    t0 = time.time()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = _http_open(req, args.timeout)
    except urllib.error.HTTPError as e:
        extra = _http_error_body(e)
        hint = ""
        if e.code in (401, 403):
            hint = " 需要 X-API-Key（请配置环境变量 SKDOS_API_KEY）"
        _die("导出失败 HTTP %s%s%s" % (e.code, (": " + extra) if extra else "", hint), 1)
    except urllib.error.URLError as e:
        _die("无法连接: %s" % e.reason, 1)

    with resp:
        name = filename_from_disposition(resp.headers.get("Content-Disposition"), fallback)
        dest = os.path.join(out_dir, name)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype and args.format == "zip":
            raw = resp.read()
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("error"):
                _die("导出失败: %s" % (obj.get("detail") or obj.get("error")), 1)
        os.makedirs(out_dir, exist_ok=True)
        tmp = dest + ".part"
        n = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
        os.replace(tmp, dest)

    elapsed = max(0.001, time.time() - t0)
    print("saved %s (%dB, %.1fs)" % (dest, n, elapsed))
    print("字段说明见 annotation-export-zip-algo-usage.md；拉视频：python3 pull-oss-videos.py --in %s --out ./videos" % dest)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="训练机直拉看板「任务数据导出」ZIP/JSONL（不必打开浏览器）",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE,
                   help="skdos-live 基址（默认 env SKDOS_LIVE_URL 或 %s）" % DEFAULT_BASE)
    p.add_argument("--list", action="store_true", help="列出任务，不下载")
    p.add_argument("--robot", default="", help="--list 时按 robot_id 过滤")
    p.add_argument("--task", default="", help="task_id，如 20260820180215WDK")
    p.add_argument("--out", default=".", help="保存目录（默认当前目录）")
    p.add_argument("--format", choices=("zip", "jsonl"), default="zip",
                   help="zip=算法包（含 grids PNG）；jsonl=仅 frames 行")
    p.add_argument("--sub-task", default="",
                   help="子任务序号，或 all。ZIP 默认 all；JSONL 必须指定单个")
    p.add_argument("--pack", choices=("algo", "full"), default="algo",
                   help="algo=算法包；full=另含 signals.jsonl + cells")
    p.add_argument("--grids", choices=("png", "meta", "none"), default=None,
                   help="zip 默认 png；jsonl 默认 meta")
    p.add_argument("--timeout", type=int, default=600, help="下载超时秒（默认 600）")
    p.add_argument("--dry-run", action="store_true", help="只打印将请求的 URL，不下载")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list:
        return cmd_list(args)
    return cmd_pull(args)


if __name__ == "__main__":
    sys.exit(main())
