#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orin / 域控本机：按时间窗把 mediamtx 录制段拼成连续 MP4，**完全不经 OSS**。

算法侧在车上训练用：直接读本地盘 `/data/roc/rec/<cam>/*.mp4`，ffmpeg 拼接后写到
本机 `--out` 目录。不需要 skdos-live、不需要上传、不需要笔记本下载。

依赖（Orin 通常已有）:
  - Python 3.8+
  - ffmpeg（PATH 中）

段布局（与 edge segment_uploader 一致）:
  $ROC_REC_DIR/<camera>/YYYY-MM-DD_HH-MM-SS-ffffff.mp4
  默认 ROC_REC_DIR=/data/roc/rec

用法示例（在 Orin 上）:

  # 看本机有哪些相机 / 段
  python3 export-local-continuous-video.py --list

  # 按任务时间窗导出（unix 秒 = 任务 started_ts ~ ended_ts）
  python3 export-local-continuous-video.py \\
    --from 1723000000 --to 1723001200 \\
    --cameras cam0,cam1,cam2,cam3,cam7 \\
    --out /data/roc/export/videos_20260817153045aB3

  # 只规划不写盘
  python3 export-local-continuous-video.py --from ... --to ... --dry-run

  # 任务窗前向 pad（默认 90s，避免漏掉「任务开始时正在写的那段」）
  python3 export-local-continuous-video.py --from ... --to ... --pad-before 90

看板「任务数据导出」拿 frames/pose；本脚本只出视频。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

# mediamtx recordPath 默认名: 2026-07-14_16-24-15-542842.mp4（本地时区）
_SEG_NAME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{6})\.mp4$"
)
_SEG_DUR_DEFAULT = 60.0
_SEG_DUR_MAX = 600.0
_INPROGRESS_GRACE = 30.0
_PAD_BEFORE_DEFAULT = 90.0

# 与 skdos-live camera-positions 活跃五路对齐（脚本自包含，不 import 远端）
_CAM_POS_ZH = {
    "cam0": "左",
    "cam1": "右",
    "cam2": "前下",
    "cam3": "前上",
    "cam7": "后上",
    "cam4": "左中",
    "cam5": "右中",
    "cam6": "后左",
}


def _parse_seg_filename(name: str) -> Optional[float]:
    m = _SEG_NAME_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi, s, us = (int(x) for x in m.groups())
    try:
        base = time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    except (OverflowError, ValueError):
        return None
    return base + us / 1_000_000.0


def list_segments(
    rec_dir: str,
    camera: str,
    *,
    skip_inprogress: bool = True,
) -> List[Dict[str, Any]]:
    """→ [{name, path, start_ts, duration_s, size}] 升序。"""
    cam_dir = os.path.join(rec_dir, camera)
    if not os.path.isdir(cam_dir):
        return []
    try:
        names = os.listdir(cam_dir)
    except OSError:
        return []
    now = time.time()
    entries: List[Tuple[float, float, int, str]] = []  # start, mtime, size, name
    for name in names:
        ts = _parse_seg_filename(name)
        if ts is None:
            continue
        path = os.path.join(cam_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size <= 0:
            continue
        entries.append((ts, st.st_mtime, st.st_size, name))
    entries.sort(key=lambda x: x[0])
    if entries and skip_inprogress and (now - entries[-1][1]) < _INPROGRESS_GRACE:
        entries = entries[:-1]
    out: List[Dict[str, Any]] = []
    for i, (ts, mtime, size, name) in enumerate(entries):
        if i + 1 < len(entries):
            dur = entries[i + 1][0] - ts
        else:
            dur = mtime - ts
        if dur <= 0:
            dur = _SEG_DUR_DEFAULT
        dur = float(min(dur, _SEG_DUR_MAX))
        out.append(
            {
                "name": name,
                "path": os.path.join(cam_dir, name),
                "start_ts": ts,
                "duration_s": dur,
                "end_ts": ts + dur,
                "size": size,
            }
        )
    return out


def overlap(
    segs: List[Dict[str, Any]], from_ts: float, to_ts: float
) -> List[Dict[str, Any]]:
    return [
        s
        for s in segs
        if s["start_ts"] + s["duration_s"] >= from_ts and s["start_ts"] <= to_ts
    ]


def discover_cameras(rec_dir: str) -> List[str]:
    if not os.path.isdir(rec_dir):
        return []
    cams = []
    try:
        for name in sorted(os.listdir(rec_dir)):
            if name.startswith(".") or name in ("timelines",):
                continue
            d = os.path.join(rec_dir, name)
            if not os.path.isdir(d):
                continue
            if any(_parse_seg_filename(n) is not None for n in os.listdir(d)):
                cams.append(name)
    except OSError:
        return []
    return cams


def _ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG", "ffmpeg")


def _run_ffmpeg(args: List[str], timeout: int = 3600) -> None:
    cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"] + args
    try:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未找到。请安装或设置 FFMPEG=/path/to/ffmpeg")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg 超时")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError("ffmpeg 失败: %s" % (err or e))


def _escape_concat_path(p: str) -> str:
    # concat demuxer: file '/abs/path'
    return p.replace("'", r"'\''")


def concat_copy(paths: List[str], out_path: str, work_dir: str) -> None:
    """ffmpeg concat demuxer -c copy。"""
    lst = os.path.join(work_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % _escape_concat_path(os.path.abspath(p)))
    _run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            lst,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-an",
            out_path,
        ]
    )


def concat_via_ts(paths: List[str], out_path: str, work_dir: str) -> None:
    """每段 remux 到 MPEG-TS 再 concat -c copy（fMP4 / 时间戳不齐时兜底）。"""
    ts_files = []
    for i, p in enumerate(paths):
        ts = os.path.join(work_dir, "seg_%04d.ts" % i)
        _run_ffmpeg(
            [
                "-i",
                p,
                "-c",
                "copy",
                "-bsf:v",
                "h264_mp4toannexb",
                "-f",
                "mpegts",
                ts,
            ]
        )
        ts_files.append(ts)
    lst = os.path.join(work_dir, "concat_ts.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in ts_files:
            f.write("file '%s'\n" % _escape_concat_path(p))
    _run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            lst,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-an",
            out_path,
        ]
    )


def export_camera(
    camera: str,
    segs: List[Dict[str, Any]],
    out_path: str,
    *,
    dry_run: bool = False,
    force_ts: bool = False,
) -> Dict[str, Any]:
    paths = [s["path"] for s in segs]
    result: Dict[str, Any] = {
        "camera": camera,
        "position_zh": _CAM_POS_ZH.get(camera, camera),
        "segment_count": len(segs),
        "segments": [
            {
                "name": s["name"],
                "start_ts": s["start_ts"],
                "duration_s": round(s["duration_s"], 3),
                "size": s["size"],
            }
            for s in segs
        ],
        "out": out_path,
        "ok": False,
        "mode": None,
        "bytes": 0,
        "error": None,
    }
    if not paths:
        result["error"] = "no segments"
        return result
    if dry_run:
        result["ok"] = True
        result["mode"] = "dry-run"
        return result

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    work = tempfile.mkdtemp(prefix="local-vx-")
    try:
        if force_ts:
            concat_via_ts(paths, out_path, work)
            result["mode"] = "ts-remux"
        else:
            try:
                concat_copy(paths, out_path, work)
                result["mode"] = "copy"
            except RuntimeError as e1:
                # fMP4 常见 copy 失败 → TS 兜底
                try:
                    concat_via_ts(paths, out_path, work)
                    result["mode"] = "ts-remux-fallback"
                    result["copy_error"] = str(e1)
                except RuntimeError as e2:
                    result["error"] = "copy: %s; ts: %s" % (e1, e2)
                    return result
        st = os.stat(out_path)
        result["bytes"] = st.st_size
        result["ok"] = st.st_size > 0
        if not result["ok"]:
            result["error"] = "empty output"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def cmd_list(rec_dir: str, cameras: Optional[List[str]]) -> int:
    cams = cameras or discover_cameras(rec_dir)
    if not cams:
        print("rec_dir=%s 下未发现相机目录/段" % rec_dir, file=sys.stderr)
        return 1
    print("rec_dir=%s" % rec_dir)
    for cam in cams:
        segs = list_segments(rec_dir, cam)
        pos = _CAM_POS_ZH.get(cam, "")
        label = "%s · %s" % (cam, pos) if pos else cam
        if not segs:
            print("  %s: 0 段" % label)
            continue
        total = sum(s["duration_s"] for s in segs)
        bytes_ = sum(s["size"] for s in segs)
        print(
            "  %s: %d 段 · ~%.0fs · %.1f MB · 首 %s → 末 %s"
            % (
                label,
                len(segs),
                total,
                bytes_ / 1e6,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(segs[0]["start_ts"])),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(segs[-1]["start_ts"])),
            )
        )
    return 0


def load_sub_tasks(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """从 task.json 读 sub_tasks；可按 --sub-task 过滤。"""
    if not args.task_json:
        return []
    with open(args.task_json, "r", encoding="utf-8") as f:
        meta = json.load(f)
    raw = meta.get("sub_tasks") or []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index") or 0)
        sid = str(item.get("sub_task_id") or "")
        a = item.get("from", item.get("from_ts"))
        b = item.get("to", item.get("to_ts"))
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if fb <= fa:
            continue
        keeps: List[Tuple[float, float]] = []
        for w in item.get("keep_windows") or []:
            if not isinstance(w, dict):
                continue
            try:
                ka, kb = float(w.get("from")), float(w.get("to"))
            except (TypeError, ValueError):
                continue
            if kb > ka:
                keeps.append((ka, kb))
        empty = bool(item.get("empty")) or not keeps
        out.append({
            "index": idx or (len(out) + 1),
            "sub_task_id": sid or ("%s_%s" % (meta.get("task_id") or "task", idx or len(out) + 1)),
            "from": fa,
            "to": fb,
            "keep_windows": keeps or [(fa, fb)],
            "empty": empty,
        })
    want = getattr(args, "sub_task", None)
    if want:
        out = [s for s in out if int(s["index"]) == int(want)]
    return [s for s in out if not s.get("empty")]


def load_keep_windows(args: argparse.Namespace) -> List[Tuple[float, float]]:
    """优先 --windows，其次 task.json keep_windows，再回退 --from/--to。"""
    windows: List[Tuple[float, float]] = []
    if args.windows:
        for part in str(args.windows).split(","):
            part = part.strip()
            if not part or "-" not in part:
                continue
            a, b = part.split("-", 1)
            try:
                fa, fb = float(a), float(b)
            except ValueError:
                continue
            if fb > fa:
                windows.append((fa, fb))
        if windows:
            return _dedupe_windows(windows)
    if args.task_json:
        with open(args.task_json, "r", encoding="utf-8") as f:
            meta = json.load(f)
        raw = meta.get("keep_windows") or []
        for item in raw:
            if not isinstance(item, dict):
                continue
            a = item.get("from", item.get("from_ts"))
            b = item.get("to", item.get("to_ts"))
            try:
                fa, fb = float(a), float(b)
            except (TypeError, ValueError):
                continue
            if fb > fa:
                windows.append((fa, fb))
        if not windows:
            started = meta.get("started_ts")
            ended = meta.get("ended_ts")
            if started is not None and ended is not None:
                windows.append((float(started), float(ended)))
    if not windows and args.from_ts is not None and args.to_ts is not None:
        windows.append((float(args.from_ts), float(args.to_ts)))
    return _dedupe_windows(windows)


def _dedupe_windows(windows: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    seen = set()
    out: List[Tuple[float, float]] = []
    for w in windows:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def cmd_export(args: argparse.Namespace) -> int:
    rec_dir = args.rec_dir
    subs = load_sub_tasks(args)
    if subs:
        rc = 0
        for st in subs:
            one = argparse.Namespace(**vars(args))
            one.from_ts = st["from"]
            one.to_ts = st["to"]
            one.windows = ",".join("%.6f-%.6f" % w for w in st["keep_windows"])
            one.out = os.path.join(args.out, "videos_%s" % st["sub_task_id"]) if len(subs) > 1 or args.sub_task else args.out
            print("sub_task %s" % st["sub_task_id"])
            rc = max(rc, cmd_export_windows(one, label=st["sub_task_id"]))
        return rc
    return cmd_export_windows(args)


def cmd_export_windows(args: argparse.Namespace, label: str = "") -> int:
    rec_dir = args.rec_dir
    windows = load_keep_windows(args)
    if not windows:
        print("需要 --from/--to，或 --task-json（含 keep_windows），或 --windows", file=sys.stderr)
        return 2
    from_ts = windows[0][0]
    to_ts = windows[-1][1]
    if not (to_ts > from_ts):
        print("时间窗无效", file=sys.stderr)
        return 2
    pad = max(0.0, float(args.pad_before))
    from_eff = from_ts - pad

    cams = args.cameras
    if not cams:
        cams = discover_cameras(rec_dir)
    if not cams:
        print("无相机。指定 --cameras 或确认 rec_dir 有段。", file=sys.stderr)
        return 1

    out_dir = args.out
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)

    print(
        "keep %d 窗 · bounding [%s , %s] pad_before=%.0fs → scan [%.0f , %.0f]"
        % (
            len(windows),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(from_ts)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(to_ts)),
            pad,
            from_eff,
            to_ts,
        )
    )
    print("rec_dir=%s out=%s cameras=%s" % (rec_dir, out_dir, ",".join(cams)))

    results = []
    ok_n = 0
    for cam in cams:
        all_segs = list_segments(rec_dir, cam, skip_inprogress=not args.include_inprogress)
        segs = []
        for i, (wa, wb) in enumerate(windows):
            scan_from = wa - pad if i == 0 else wa
            segs.extend(overlap(all_segs, scan_from, wb))
        # 去重保序
        seen_names = set()
        uniq = []
        for s in segs:
            if s["name"] in seen_names:
                continue
            seen_names.add(s["name"])
            uniq.append(s)
        segs = uniq
        out_path = os.path.join(out_dir, "%s_continuous.mp4" % cam)
        if not segs:
            print("  [%s] 窗口内 0 段 (本机共 %d 段)" % (cam, len(all_segs)))
            results.append(
                {
                    "camera": cam,
                    "position_zh": _CAM_POS_ZH.get(cam, cam),
                    "segment_count": 0,
                    "ok": False,
                    "error": "no segments in window",
                    "out": out_path,
                }
            )
            continue
        print(
            "  [%s · %s] %d 段 · 拼 → %s%s"
            % (
                cam,
                _CAM_POS_ZH.get(cam, "?"),
                len(segs),
                out_path,
                " (dry-run)" if args.dry_run else "",
            )
        )
        r = export_camera(
            cam, segs, out_path, dry_run=args.dry_run, force_ts=args.force_ts
        )
        results.append(r)
        if r.get("ok"):
            ok_n += 1
            if not args.dry_run:
                print("    ok mode=%s bytes=%s" % (r.get("mode"), r.get("bytes")))
        else:
            print("    FAIL: %s" % r.get("error"), file=sys.stderr)

    manifest = {
        "kind": "local_continuous_video",
        "rec_dir": rec_dir,
        "keep_windows": [{"from": a, "to": b} for a, b in windows],
        "from_ts": from_ts,
        "to_ts": to_ts,
        "pad_before": pad,
        "from_eff": from_eff,
        "cameras": cams,
        "out_dir": out_dir,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "results": results,
        "ok_count": ok_n,
        "fail_count": len(results) - ok_n,
    }
    if not args.dry_run:
        man_path = os.path.join(out_dir, "manifest.json")
        with open(man_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print("manifest → %s" % man_path)
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))

    print("done ok=%d fail=%d" % (ok_n, len(results) - ok_n))
    return 0 if ok_n > 0 else 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Orin 本机拼连续视频（不经 OSS）。读 $ROC_REC_DIR/<cam>/*.mp4"
    )
    p.add_argument(
        "--rec-dir",
        default=os.environ.get("ROC_REC_DIR", "/data/roc/rec"),
        help="录制根目录（默认 /data/roc/rec 或 env ROC_REC_DIR）",
    )
    p.add_argument(
        "--cameras",
        default="",
        help="逗号分隔相机 id，默认自动发现有段的目录",
    )
    p.add_argument("--list", action="store_true", help="只列出本机段概况")
    p.add_argument("--from", dest="from_ts", type=float, help="时间窗起点 unix 秒（无 keep 时回退）")
    p.add_argument("--to", dest="to_ts", type=float, help="时间窗终点 unix 秒（无 keep 时回退）")
    p.add_argument(
        "--task-json",
        default="",
        help="看板导出的 task.json：优先读 keep_windows，否则 started_ts/ended_ts",
    )
    p.add_argument(
        "--windows",
        default="",
        help="手动 keep 窗，逗号分隔 from-to，如 100-200,300-400",
    )
    p.add_argument(
        "--sub-task",
        dest="sub_task",
        type=int,
        default=None,
        help="只导出第 N 个子任务（读 task.json 的 sub_tasks）",
    )
    p.add_argument(
        "--out",
        default="./local_video_export",
        help="输出目录（每路 camX_continuous.mp4 + manifest.json）",
    )
    p.add_argument(
        "--pad-before",
        type=float,
        default=_PAD_BEFORE_DEFAULT,
        help="任务窗前向扩展秒（默认 90，纳入任务开始前已开录的段）",
    )
    p.add_argument("--dry-run", action="store_true", help="只规划，不调用 ffmpeg")
    p.add_argument(
        "--force-ts",
        action="store_true",
        help="强制经 MPEG-TS remux 再拼（copy 失败时可开）",
    )
    p.add_argument(
        "--include-inprogress",
        action="store_true",
        help="包含 mtime 很新的「可能仍在写」末段（风险：文件不完整）",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cams = [c.strip() for c in (args.cameras or "").split(",") if c.strip()]
    args.cameras = cams or None

    if args.list:
        return cmd_list(args.rec_dir, args.cameras)

    if not args.task_json and args.from_ts is None and not args.windows:
        print("导出需要 --task-json，或 --from/--to，或 --windows。或用 --list 查看本机段。", file=sys.stderr)
        print(
            "  例: python3 export-local-continuous-video.py "
            "--task-json task.json --cameras cam0,cam1 --out /tmp/vx",
            file=sys.stderr,
        )
        return 2
    return cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
