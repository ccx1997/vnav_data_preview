#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练机：从看板导出的 JSONL / ZIP 拉 OSS 录像段，并按子任务 keep_windows 拼成连续 MP4。

推荐流程（算法）：
  1. 车上采完 → 看板「存储到云端」把录像传到 OSS
  2. 看板导出 JSONL（或 ZIP）到训练机
  3. 本脚本拉段 + 拼接（默认按 keep 窗定长，缺段补黑）

默认模式会重编码成等长时间轴；仅 --copy 保留旧的关键帧 copy 快路径。

依赖：Python 3.8+、ffmpeg（PATH）、能访问 OSS presigned URL。

用法：
  python3 pull-oss-videos.py --in meta_20260819120000aB3_all.zip --out ./videos
  python3 pull-oss-videos.py --in meta_20260819120000aB3_1.jsonl --out ./videos
  python3 pull-oss-videos.py --in meta_20260819120000aB3_1.zip --out ./videos
  python3 pull-oss-videos.py --in meta_..._1/video_segments.json --in meta_..._2/video_segments.json --out ./videos --jobs 4
  python3 pull-oss-videos.py --in video_segments.json --out ./videos --concat
  python3 pull-oss-videos.py --in meta_....jsonl --out ./videos --dry-run
  python3 pull-oss-videos.py --in meta_....zip --out ./videos --copy
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar


T = TypeVar("T")
R = TypeVar("R")
RECOMMENDED_CAMERA_IDS = ("cam0", "cam1", "cam2", "cam3", "cam5", "cam6")


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _read_jsonl_meta(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        first = f.readline()
    if not first.strip():
        raise SystemExit("jsonl 为空")
    row = json.loads(first)
    if not isinstance(row, dict):
        raise SystemExit("jsonl 首行不是对象")
    return row


def _normalize_zip_name(name: str) -> str:
    return str(name or "").replace("\\", "/").lstrip("./")


def _zip_bundle_prefixes(zf: zipfile.ZipFile) -> List[str]:
    """Return one prefix per subtask bundle inside a ZIP.

    A single-subtask ZIP may store files at the archive root or below one
    ``meta_<sub_task_id>/`` directory. An ``all.zip`` stores one such directory
    per subtask. If nested bundles exist, root-level metadata is ignored so it
    cannot be mistaken for an additional subtask.
    """
    prefixes = set()
    for info in zf.infolist():
        normalized = _normalize_zip_name(info.filename)
        if normalized.rsplit("/", 1)[-1] != "video_segments.json":
            continue
        if "/" in normalized:
            prefixes.add(normalized.rsplit("/", 1)[0] + "/")
    return sorted(prefixes) if prefixes else [""]


def _read_zip_json(
    zf: zipfile.ZipFile,
    name: str,
    prefix: str = "",
) -> Optional[Dict[str, Any]]:
    normalized_prefix = _normalize_zip_name(prefix)
    if normalized_prefix and not normalized_prefix.endswith("/"):
        normalized_prefix += "/"
    exact_name = normalized_prefix + name
    candidates = []
    for info in zf.infolist():
        normalized = _normalize_zip_name(info.filename)
        if normalized == exact_name:
            candidates.insert(0, info.filename)
        elif not normalized_prefix and normalized.rsplit("/", 1)[-1] == name:
            candidates.append(info.filename)
    for cand in candidates:
        raw = zf.read(cand)
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    return None


def _read_optional_json_object(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit("无法读取同目录元数据 %s: %s" % (path, e))
    if not isinstance(data, dict):
        raise SystemExit("同目录元数据不是 JSON 对象: %s" % path)
    return data


def _parse_windows(raw: Any) -> List[Tuple[float, float]]:
    windows: List[Tuple[float, float]] = []
    if not isinstance(raw, list):
        return windows
    for item in raw:
        if not isinstance(item, dict):
            continue
        a, b = _as_float(item.get("from")), _as_float(item.get("to"))
        if a is not None and b is not None and b > a:
            windows.append((a, b))
    return sorted(set(windows))


def _assemble_bundle(
    task: Dict[str, Any],
    export_meta: Dict[str, Any],
    video_meta: Dict[str, Any],
    segments: List[Dict[str, Any]],
    camera_positions: Dict[str, Any],
    task_id: str,
    sub_task_id: str,
) -> Dict[str, Any]:
    if not sub_task_id:
        sub = task.get("sub_task") if isinstance(task.get("sub_task"), dict) else {}
        sub_task_id = str(sub.get("sub_task_id") or "")
    if not sub_task_id and task_id:
        sub_task_id = "%s_1" % task_id
    if not task_id and sub_task_id:
        task_id = sub_task_id.rsplit("_", 1)[0]

    export_sub = export_meta.get("sub_task") if isinstance(export_meta.get("sub_task"), dict) else {}
    task_sub = task.get("sub_task") if isinstance(task.get("sub_task"), dict) else {}
    segment_windows = [
        item
        for seg in segments
        if isinstance(seg, dict)
        for item in (seg.get("keep_windows") or [])
    ]
    sources = (
        ("export_meta", export_meta.get("keep_windows")),
        ("export_sub_task", export_sub.get("keep_windows")),
        ("task_sub_task", task_sub.get("keep_windows")),
        ("video_segments", video_meta.get("keep_windows")),
        ("segments", segment_windows),
        # 仅无子任务作用域时，才允许使用整任务窗口，避免把其他子任务拼进来。
        ("task", task.get("keep_windows") if not task_sub else None),
    )
    keep: List[Tuple[float, float]] = []
    keep_source = "clip_bounds"
    for source, raw_windows in sources:
        keep = _parse_windows(raw_windows)
        if keep:
            keep_source = source
            break

    return {
        "task_id": task_id,
        "sub_task_id": sub_task_id,
        "task": task,
        "export_meta": export_meta,
        "segments": segments,
        "camera_positions": camera_positions,
        "keep_windows": keep,
        "keep_windows_source": keep_source,
    }


def _load_zip_bundle(zf: zipfile.ZipFile, prefix: str) -> Dict[str, Any]:
    video_meta = _read_zip_json(zf, "video_segments.json", prefix) or {}
    task = _read_zip_json(zf, "task.json", prefix) or {}
    export_meta = _read_zip_json(zf, "export_meta.json", prefix) or {}
    segments = list(video_meta.get("segments") or [])
    camera_positions = video_meta.get("camera_positions") or task.get("camera_positions") or {}
    task_id = str(video_meta.get("task_id") or task.get("task_id") or "")
    task_sub = task.get("sub_task") if isinstance(task.get("sub_task"), dict) else {}
    sub_task_id = str(video_meta.get("sub_task_id") or task_sub.get("sub_task_id") or "")
    bundle = _assemble_bundle(
        task,
        export_meta,
        video_meta,
        segments,
        camera_positions,
        task_id,
        sub_task_id,
    )
    bundle["zip_prefix"] = prefix
    return bundle


def _load_non_zip_bundle(abs_path: str) -> Dict[str, Any]:
    task: Dict[str, Any] = {}
    export_meta: Dict[str, Any] = {}
    video_meta: Dict[str, Any] = {}
    segments: List[Dict[str, Any]] = []
    camera_positions: Dict[str, Any] = {}
    sub_task_id = ""
    task_id = ""

    if abs_path.endswith(".jsonl"):
        row = _read_jsonl_meta(abs_path)
        task = row.get("_task") if isinstance(row.get("_task"), dict) else {}
        export_meta = row.get("_export") if isinstance(row.get("_export"), dict) else {}
        segments = list(row.get("_video_segments") or [])
        camera_positions = row.get("_camera_positions") or task.get("camera_positions") or {}
        task_id = str(task.get("task_id") or "")
        sub = task.get("sub_task") if isinstance(task.get("sub_task"), dict) else {}
        sub_task_id = str(sub.get("sub_task_id") or "")
    else:
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit("无法读取 JSON %s: %s" % (abs_path, e))
        if isinstance(raw, list):
            segments = raw
        elif isinstance(raw, dict):
            if isinstance(raw.get("segments"), list):
                video_meta = raw
                segments = raw["segments"]
                task_id = str(raw.get("task_id") or "")
                sub_task_id = str(raw.get("sub_task_id") or "")
                camera_positions = raw.get("camera_positions") or {}

                # download-task.sh 传入解压后的 video_segments.json。它的
                # keep_windows 在同目录 export_meta.json/task.json 中，必须一并读取。
                sibling_dir = os.path.dirname(abs_path)
                task = _read_optional_json_object(os.path.join(sibling_dir, "task.json"))
                export_meta = _read_optional_json_object(os.path.join(sibling_dir, "export_meta.json"))
                camera_positions = camera_positions or task.get("camera_positions") or {}
                task_id = str(task_id or task.get("task_id") or "")
            elif raw.get("task_id") and (raw.get("sub_tasks") or raw.get("keep_windows")):
                task = raw
                task_id = str(raw.get("task_id") or "")
            else:
                raise SystemExit("无法从 JSON 识别 video_segments")
        else:
            raise SystemExit("不支持的 JSON 形状")

    return _assemble_bundle(
        task,
        export_meta,
        video_meta,
        segments,
        camera_positions,
        task_id,
        sub_task_id,
    )


def load_bundles(path: str) -> List[Dict[str, Any]]:
    """读 jsonl / zip / video_segments.json / task.json；all.zip 返回每个子任务。"""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise SystemExit("找不到输入: %s" % abs_path)
    if zipfile.is_zipfile(abs_path):
        with zipfile.ZipFile(abs_path) as zf:
            return [_load_zip_bundle(zf, prefix) for prefix in _zip_bundle_prefixes(zf)]
    return [_load_non_zip_bundle(abs_path)]


def load_bundle(path: str) -> Dict[str, Any]:
    """兼容旧调用：单包返回一份；CLI 用 load_bundles 展开 all.zip。"""
    bundles = load_bundles(path)
    if not bundles:
        raise SystemExit("ZIP/JSON 里没有可读的任务包: %s" % path)
    return bundles[0]


def _ffmpeg() -> str:
    return os.environ.get("FFMPEG", "ffmpeg")


def _run_ffmpeg(args: List[str], timeout: int = 3600) -> None:
    cmd = [_ffmpeg(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + args
    try:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("找不到 ffmpeg。训练机请安装 ffmpeg，或设置 FFMPEG=/path/to/ffmpeg")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError("ffmpeg 失败: %s" % (err or e))


def _escape_concat_path(p: str) -> str:
    return p.replace("'", r"'\''")


def download_segment(url: str, dest: str, timeout: int = 60) -> int:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest_dir = os.path.dirname(os.path.abspath(dest)) or "."
        os.makedirs(dest_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".video-download-", suffix=".part", dir=dest_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
                size = f.tell()
            os.replace(temp_path, dest)
            return size
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def parse_seg_hw(seg: Dict[str, Any]) -> Optional[Tuple[float, float, Optional[int]]]:
    """读取可用于对齐的硬件时间范围；不为缺失的 t1_hw 伪造时长。"""
    t0 = _as_float(seg.get("t0_hw"))
    t1 = _as_float(seg.get("t1_hw"))
    frame_count = seg.get("frame_count")
    spec = seg.get("spec")
    spec_obj: Dict[str, Any] = {}
    if isinstance(spec, dict):
        spec_obj = spec
    elif isinstance(spec, str) and spec.strip().startswith("{"):
        try:
            parsed = json.loads(spec)
            if isinstance(parsed, dict):
                spec_obj = parsed
        except (TypeError, ValueError):
            pass
    if t0 is None:
        t0 = _as_float(spec_obj.get("t0_hw"))
    if t1 is None:
        t1 = _as_float(spec_obj.get("t1_hw"))
    if frame_count is None:
        frame_count = spec_obj.get("frame_count")
    if t0 is None or t1 is None or t0 <= 1e8 or t1 <= t0:
        return None
    try:
        count = int(frame_count) if frame_count is not None else None
    except (TypeError, ValueError):
        count = None
    return t0, t1, count if count and count > 0 else None


def choose_align_mode(segments: List[Dict[str, Any]]) -> str:
    # 必须全局所有相机/分段都有完整硬件时间，避免同一次输出混用两个时间基准。
    if segments and all(parse_seg_hw(seg) is not None for seg in segments):
        return "hw_ts"
    return "wall_clock"


def origin_of(seg: Dict[str, Any], mode: str) -> Optional[Tuple[float, float]]:
    if mode == "hw_ts":
        hw = parse_seg_hw(seg)
        return (hw[0], hw[1]) if hw else None
    t0 = _as_float(seg.get("start_ts"))
    if t0 is None:
        return None
    t1 = _as_float(seg.get("end_ts"))
    if t1 is None or t1 <= t0:
        t1 = t0 + 60.0
    return t0, t1


def keep_windows_of(bundle: Dict[str, Any], segments: List[Dict[str, Any]]) -> List[Tuple[float, float]]:
    keep = list(bundle.get("keep_windows") or [])
    if keep:
        return keep
    lower: Optional[float] = None
    upper: Optional[float] = None
    for seg in segments:
        start = _as_float(seg.get("clip_from_ts"))
        if start is None:
            start = _as_float(seg.get("start_ts"))
        end = _as_float(seg.get("clip_to_ts"))
        if end is None:
            end = _as_float(seg.get("end_ts"))
        if start is None or end is None or end <= start:
            continue
        lower = start if lower is None else min(lower, start)
        upper = end if upper is None else max(upper, end)
    if lower is None or upper is None or upper <= lower:
        return []
    return [(lower, upper)]


def pad_pieces_for_window(
    segments: List[Dict[str, Any]],
    window: Tuple[float, float],
    mode: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, float]], float, float]:
    """按 keep 窗铺设 content/black 时间片，重叠段只取时间轴上的第一份内容。"""
    window_from, window_to = window
    items: List[Dict[str, Any]] = []
    for seg in segments:
        origin = origin_of(seg, mode)
        if not origin:
            continue
        seg_from, seg_to = origin
        content_from = max(window_from, seg_from)
        content_to = min(window_to, seg_to)
        if content_to <= content_from:
            continue
        items.append({
            "seg": seg,
            "abs_start": content_from,
            "abs_end": content_to,
            "media_start": content_from - seg_from,
            "media_end": content_to - seg_from,
        })
    items.sort(key=lambda item: (item["abs_start"], item["abs_end"]))

    pieces: List[Dict[str, Any]] = []
    holes: List[Dict[str, float]] = []
    cursor = window_from
    epsilon = 1e-3
    content_seconds = 0.0

    def add_black(start: float, end: float) -> None:
        duration = end - start
        if duration <= epsilon:
            return
        holes.append({"from": start, "to": end, "duration_s": round(duration, 3)})
        pieces.append({"type": "black", "duration_s": duration, "abs_start": start})

    for item in items:
        if item["abs_start"] > cursor + epsilon:
            add_black(cursor, item["abs_start"])
        start = max(cursor, item["abs_start"])
        end = item["abs_end"]
        if end <= start + epsilon:
            continue
        duration = end - start
        pieces.append({
            "type": "content",
            "seg": item["seg"],
            "duration_s": duration,
            "media_start": item["media_start"] + (start - item["abs_start"]),
            "media_end": item["media_end"] - (item["abs_end"] - end),
            "abs_start": start,
        })
        content_seconds += duration
        cursor = end
    if cursor < window_to - epsilon:
        add_black(cursor, window_to)
    if not pieces and window_to - window_from > epsilon:
        add_black(window_from, window_to)
    return pieces, holes, round(content_seconds, 3), max(0.0, window_to - window_from)


def _ffprobe_shape(path: str) -> Tuple[int, int, float]:
    cmd = [
        os.environ.get("FFPROBE", "ffprobe"),
        "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", path,
    ]
    try:
        raw = subprocess.check_output(cmd, timeout=30)
        data = json.loads(raw.decode("utf-8"))
        stream = (data.get("streams") or [{}])[0]
        width = max(2, int(stream.get("width") or 640))
        height = max(2, int(stream.get("height") or 512))
        width += width % 2
        height += height % 2
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "15/1"
        fps = 15.0
        if isinstance(rate, str) and "/" in rate:
            numerator, denominator = rate.split("/", 1)
            if float(denominator) > 0:
                fps = float(numerator) / float(denominator)
        if not 5 <= fps <= 60:
            fps = 15.0
        return width, height, float(max(5, min(60, int(round(fps)))))
    except Exception:
        return 640, 512, 15.0


def _segment_path(segs_dir: str, camera: str, seg: Dict[str, Any], fallback: int) -> str:
    start = _as_float(seg.get("start_ts"))
    token = int(start) if start is not None else fallback
    return os.path.join(segs_dir, camera, "%s_%s.mp4" % (camera, token))


def align_concat_camera(
    camera: str,
    camera_segments: List[Dict[str, Any]],
    segs_dir: str,
    keep: List[Tuple[float, float]],
    out_mp4: str,
    work: str,
    mode: str,
) -> Dict[str, Any]:
    if not keep:
        return {
            "ok": False,
            "error": "no valid keep windows",
            "align_mode": mode,
            "holes": [],
            "window_s": 0.0,
            "content_s": 0.0,
        }

    path_of = {
        id(seg): _segment_path(segs_dir, camera, seg, index)
        for index, seg in enumerate(camera_segments)
    }
    available_segments = [
        seg for seg in camera_segments
        if os.path.isfile(path_of[id(seg)]) and os.path.getsize(path_of[id(seg)]) > 0
    ]
    missing_segments = len(camera_segments) - len(available_segments)
    shape_source = path_of[id(available_segments[0])] if available_segments else ""
    width, height, fps = _ffprobe_shape(shape_source) if shape_source else (640, 512, 15.0)

    all_holes: List[Dict[str, float]] = []
    window_seconds = sum(max(0.0, end - start) for start, end in keep)
    content_seconds = 0.0
    part_files: List[str] = []
    for window_index, window in enumerate(keep):
        pieces, holes, piece_content_seconds, _ = pad_pieces_for_window(
            available_segments, window, mode,
        )
        all_holes.extend(holes)
        content_seconds += piece_content_seconds
        inputs: List[str] = []
        input_of: Dict[int, int] = {}
        filters: List[str] = []
        labels: List[str] = []
        for piece_index, piece in enumerate(pieces):
            label = "p%d" % piece_index
            duration = piece["duration_s"]
            if piece["type"] == "black":
                filters.append(
                    "color=c=black:s=%dx%d:d=%.6f:r=%s,format=yuv420p,setsar=1[%s]"
                    % (width, height, duration, fps, label)
                )
            else:
                seg = piece["seg"]
                raw_path = path_of[id(seg)]
                if id(seg) not in input_of:
                    input_of[id(seg)] = len(inputs) // 2
                    inputs.extend(["-i", raw_path])
                input_index = input_of[id(seg)]
                filters.append(
                    "[%d:v]trim=start=%.6f:end=%.6f,setpts=PTS-STARTPTS,fps=%s,"
                    "scale=%d:%d:force_original_aspect_ratio=decrease,"
                    "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                    "tpad=stop_mode=clone:stop_duration=%.6f,trim=duration=%.6f,"
                    "setpts=PTS-STARTPTS[%s]"
                    % (
                        input_index,
                        piece["media_start"],
                        piece["media_end"],
                        fps,
                        width,
                        height,
                        width,
                        height,
                        duration,
                        duration,
                        label,
                    )
                )
            labels.append("[%s]" % label)
        if not labels:
            continue
        filters.append("%sconcat=n=%d:v=1:a=0[outv]" % ("".join(labels), len(labels)))
        part = os.path.join(work, "%s_keep_%02d.mp4" % (camera, window_index))
        _run_ffmpeg(inputs + [
            "-filter_complex", ";".join(filters),
            "-map", "[outv]",
            "-t", "%.6f" % (window[1] - window[0]),
            # ffmpeg 4.x（Orin）无 -fps_mode，但支持 -vsync。
            "-vsync", "cfr", "-r", str(int(fps)),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            part,
        ])
        part_files.append(part)

    if not part_files:
        return {
            "ok": False,
            "error": "no pieces",
            "align_mode": mode,
            "holes": all_holes,
            "window_s": window_seconds,
            "content_s": content_seconds,
            "missing_segments": missing_segments,
        }
    if len(part_files) == 1:
        os.makedirs(os.path.dirname(out_mp4) or ".", exist_ok=True)
        shutil.copy2(part_files[0], out_mp4)
    else:
        concat_paths(part_files, out_mp4, work)
    size = os.path.getsize(out_mp4) if os.path.isfile(out_mp4) else 0
    return {
        "ok": size > 0,
        "align_mode": mode,
        "holes": all_holes,
        "window_s": window_seconds,
        "content_s": round(content_seconds, 3),
        "missing_segments": missing_segments,
        "bytes": size,
        "mode": "align",
    }


def clip_segment(src: str, dest: str, start_ts: float, clip_from: float, clip_to: float) -> None:
    """按 clip_from/clip_to（unix 秒）相对段起点硬切。"""
    ss = max(0.0, clip_from - start_ts)
    dur = max(0.01, clip_to - clip_from)
    _run_ffmpeg([
        "-ss", "%.3f" % ss,
        "-i", src,
        "-t", "%.3f" % dur,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-an",
        dest,
    ])


def concat_paths(paths: List[str], out_path: str, work_dir: str) -> str:
    lst = os.path.join(work_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % _escape_concat_path(os.path.abspath(p)))
    try:
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", lst,
            "-c", "copy", "-movflags", "+faststart", "-an", out_path,
        ])
        return "copy"
    except RuntimeError:
        ts_files = []
        for i, p in enumerate(paths):
            ts = os.path.join(work_dir, "seg_%04d.ts" % i)
            _run_ffmpeg([
                "-i", p, "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", ts,
            ])
            ts_files.append(ts)
        lst2 = os.path.join(work_dir, "concat_ts.txt")
        with open(lst2, "w", encoding="utf-8") as f:
            for p in ts_files:
                f.write("file '%s'\n" % _escape_concat_path(p))
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", lst2,
            "-c", "copy", "-movflags", "+faststart", "-an", out_path,
        ])
        return "ts-remux"


def group_by_camera(segments: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for seg in segments:
        cam = str(seg.get("camera") or "unknown")
        out.setdefault(cam, []).append(seg)
    for cam in out:
        out[cam].sort(key=lambda seg: (
            _as_float(seg.get("clip_from_ts"))
            if _as_float(seg.get("clip_from_ts")) is not None
            else (_as_float(seg.get("start_ts")) or 0.0)
        ))
    return out


def _parallel_map_ordered(worker: Callable[[T], R], items: List[T], jobs: int) -> List[R]:
    """并发执行独立任务，并保持结果与输入顺序一致。"""
    if len(items) <= 1 or jobs <= 1:
        return [worker(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(items))) as executor:
        return list(executor.map(worker, items))


def _split_job_budget(task_count: int, jobs: int) -> Tuple[int, List[int]]:
    """把总并发预算分给子任务，再由各子任务用于相机并发。"""
    if task_count <= 0:
        return 0, []
    task_jobs = min(task_count, jobs)
    if task_count > jobs:
        return task_jobs, [1] * task_count
    base, extra = divmod(jobs, task_count)
    return task_jobs, [base + (index < extra) for index in range(task_count)]


def _cmd_run_bundle(
    args: argparse.Namespace,
    in_path: str,
    camera_jobs: int,
    bundle: Optional[Dict[str, Any]] = None,
) -> int:
    bundle = bundle or load_bundle(in_path)
    segs = bundle["segments"]
    if args.limit and args.limit > 0:
        segs = segs[: args.limit]
    sub_id = bundle["sub_task_id"] or "task_1"
    out_root = os.path.abspath(args.out)
    segs_dir = os.path.join(out_root, "segs")
    video_dir = os.path.join(out_root, "videos_%s" % sub_id)
    effective_keep = keep_windows_of(bundle, segs)

    print("task=%s sub_task=%s segments=%d keep_windows=%s source=%s" % (
        bundle["task_id"] or "—",
        sub_id,
        len(segs),
        effective_keep or "（无有效时间窗）",
        bundle["keep_windows_source"],
    ))
    if bundle["camera_positions"]:
        print("camera_positions:", json.dumps(bundle["camera_positions"], ensure_ascii=False))

    if not segs:
        print("没有 video_segments。请确认任务已「存储到云端」，并重新导出 JSONL/ZIP。", file=sys.stderr)
        return 1

    results = []
    ok = reused = skip = fail = 0
    grouped = group_by_camera(segs)
    align_mode = choose_align_mode(segs)

    for cam, cam_segs in grouped.items():
        for i, seg in enumerate(cam_segs):
            url = seg.get("url")
            dest = _segment_path(segs_dir, cam, seg, i)
            pos = ""
            pp = seg.get("physical_position") or (bundle["camera_positions"] or {}).get(cam) or {}
            if isinstance(pp, dict):
                pos = str(pp.get("position_zh") or pp.get("position_en") or "")
            if args.reuse_segments and os.path.isfile(dest) and os.path.getsize(dest) > 0:
                print("[reuse] %s %s" % (dest, pos))
                reused += 1
                continue
            if not url:
                print("[skip] %s 无 url %s（presigned 过期请重新导出）" % (cam, pos))
                skip += 1
                results.append({"camera": cam, "dest": dest, "ok": False, "error": "no url"})
                continue
            if args.dry_run:
                print("[dry-run] %s <- %s %s" % (dest, str(url)[:90], pos))
                ok += 1
                continue
            try:
                n = download_segment(str(url), dest)
                print("[ok] %s (%dB) %s" % (dest, n, pos))
                ok += 1
            except Exception as e:
                print("[fail] %s: %s" % (dest, e), file=sys.stderr)
                fail += 1
                results.append({"camera": cam, "dest": dest, "ok": False, "error": str(e)})

    concat_ok = 0
    if args.concat and not args.dry_run:
        os.makedirs(video_dir, exist_ok=True)
        camera_items = list(grouped.items())

        def concat_camera(
            item: Tuple[str, List[Dict[str, Any]]],
        ) -> Tuple[str, Optional[Dict[str, Any]]]:
            cam, cam_segs = item
            work = tempfile.mkdtemp(prefix="oss-vx-")
            out_mp4 = os.path.join(video_dir, "%s_continuous.mp4" % cam)
            try:
                if args.copy:
                    pieces = []
                    for i, seg in enumerate(cam_segs):
                        start = _as_float(seg.get("start_ts"))
                        raw = _segment_path(segs_dir, cam, seg, i)
                        if not os.path.isfile(raw) or os.path.getsize(raw) <= 0:
                            continue
                        clip_from = _as_float(seg.get("clip_from_ts"))
                        clip_to = _as_float(seg.get("clip_to_ts"))
                        if start is not None and clip_from is not None and clip_to is not None and clip_to > clip_from:
                            clipped = os.path.join(work, "%s_%04d.mp4" % (cam, i))
                            try:
                                clip_segment(raw, clipped, start, clip_from, clip_to)
                                pieces.append(clipped)
                            except RuntimeError as e:
                                print("[warn] %s clip 失败，改用整段: %s" % (cam, e), file=sys.stderr)
                                pieces.append(raw)
                        else:
                            pieces.append(raw)
                    if not pieces:
                        return cam, None
                    mode = concat_paths(pieces, out_mp4, work)
                    size = os.path.getsize(out_mp4) if os.path.isfile(out_mp4) else 0
                    return cam, {
                        "camera": cam,
                        "out": out_mp4,
                        "ok": size > 0,
                        "mode": mode,
                        "align_mode": "copy",
                        "bytes": size,
                        "segments": len(pieces),
                    }
                info = align_concat_camera(
                    cam, cam_segs, segs_dir, effective_keep, out_mp4, work, align_mode,
                )
                return cam, {"camera": cam, "out": out_mp4, **info}
            finally:
                shutil.rmtree(work, ignore_errors=True)

        for cam, info in _parallel_map_ordered(concat_camera, camera_items, camera_jobs):
            if info is None:
                print("[skip] %s 无可拼接段" % cam)
                continue
            size = int(info.get("bytes") or 0)
            if args.copy:
                print("[concat] %s → %s (%dB, copy 未对齐)" % (
                    cam, info["out"], size,
                ))
            else:
                print("[concat] %s → %s (%dB, align_mode=%s window=%.3fs)" % (
                    cam,
                    info["out"],
                    size,
                    info.get("align_mode"),
                    info.get("window_s") or 0,
                ))
            if info.get("ok"):
                concat_ok += 1
            results.append(info)
    elif args.concat and args.dry_run:
        for cam in grouped:
            print("[dry-run] concat → %s/videos_%s/%s_continuous.mp4" % (out_root, sub_id, cam))

    camera_count = len(grouped)
    concat_complete = not args.concat or concat_ok == camera_count
    manifest = {
        "kind": "oss_pull_concat",
        "task_id": bundle["task_id"],
        "sub_task_id": sub_id,
        "keep_windows": [{"from": a, "to": b} for a, b in effective_keep],
        "keep_windows_source": bundle["keep_windows_source"],
        "input": os.path.abspath(in_path),
        "out_dir": out_root,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "download_ok": ok,
        "download_reused": reused,
        "download_skip": skip,
        "download_fail": fail,
        "camera_count": camera_count,
        "concat_ok": concat_ok,
        "concat_complete": concat_complete,
        "align": args.concat and not args.copy,
        "align_mode": align_mode if args.concat and not args.copy else ("copy" if args.concat else "none"),
        "note": "默认按 keep 窗定长并对缺段补黑；--copy 是旧快路径，不视为已对齐。",
        "results": results,
    }
    if not args.dry_run:
        os.makedirs(out_root, exist_ok=True)
        man = os.path.join(video_dir if args.concat else out_root, "manifest.json")
        os.makedirs(os.path.dirname(man), exist_ok=True)
        with open(man, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print("manifest → %s" % man)

    print("done download ok=%d reused=%d skip=%d fail=%d concat=%d" % (
        ok, reused, skip, fail, concat_ok,
    ))
    if fail or skip:
        return 1
    if ok + reused == 0:
        return 1
    if args.concat and not args.dry_run and not concat_complete:
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    input_paths = list(args.in_paths)
    expanded = [
        (in_path, bundle)
        for in_path in input_paths
        for bundle in load_bundles(in_path)
    ]
    task_jobs, camera_jobs = _split_job_budget(len(expanded), args.jobs)
    work = [
        (in_path, bundle, bundle_jobs)
        for (in_path, bundle), bundle_jobs in zip(expanded, camera_jobs)
    ]

    def run_bundle(item: Tuple[str, Dict[str, Any], int]) -> int:
        return _cmd_run_bundle(args, item[0], item[2], item[1])

    return_codes = _parallel_map_ordered(run_bundle, work, task_jobs)
    warnings = []
    print("\n========== 视频下载最终汇总 ==========")
    for (_in_path, bundle), return_code in zip(expanded, return_codes):
        sub_task_id = str(bundle.get("sub_task_id") or "task_1")
        camera_ids = sorted(group_by_camera(list(bundle.get("segments") or ())))
        print(
            "sub_task=%s status=%s camera_count=%d cameras=[%s]"
            % (
                sub_task_id,
                "success" if return_code == 0 else "failed",
                len(camera_ids),
                ",".join(camera_ids),
            )
        )
        if tuple(camera_ids) != tuple(sorted(RECOMMENDED_CAMERA_IDS)):
            warnings.append(
                "%s 相机集合为 [%s]，不同于推荐集合 [%s]；仍按实际相机下载和拼接"
                % (
                    sub_task_id,
                    ",".join(camera_ids),
                    ",".join(RECOMMENDED_CAMERA_IDS),
                )
            )
    for warning in warnings:
        print("WARNING: " + warning)
    print("========== 汇总结束 ==========")
    return 1 if any(return_codes) else 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="从导出 JSONL/ZIP 拉 OSS 录像并按子任务规则拼接（训练机用）"
    )
    p.add_argument(
        "--in", dest="in_paths", action="append", required=True,
        help="meta_*.jsonl / meta_*.zip / meta_*_all.zip / video_segments.json；"
        "all.zip 自动按子任务展开，也可重复传入 --in",
    )
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--jobs", type=int, default=4, help="子任务与相机共享的总并发数（默认 4）")
    p.add_argument("--concat", dest="concat", action="store_true", default=True, help="按相机拼连续 MP4（默认开）")
    p.add_argument("--no-concat", dest="concat", action="store_false", help="只下载分段，不拼接")
    p.add_argument("--copy", action="store_true", help="旧 copy 快路径（不标成已对齐）")
    p.add_argument("--reuse-segments", action="store_true",
                   help="复用已有非空视频段，只下载缺失段，然后重新合并并覆盖旧结果")
    p.add_argument("--dry-run", action="store_true", help="只规划，不下载")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 段（调试）")
    args = p.parse_args(argv)
    if args.jobs < 1:
        p.error("--jobs 必须大于 0")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
