from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
import math
import subprocess

import numpy as np
from PIL import Image
from vnav_training.pipeline import _effective_video_windows
from validate_training_data import validate_run
from vnav_coordinates.export import VERSION
from vnav_coordinates.geometry import FRAME

from .common import PipelineError, digest, read_json, rows, write_json


def require(condition, message):
    if not condition:
        raise PipelineError(message)


def inside(root, path):
    path = (root / path).resolve()
    require(path.is_relative_to(root.resolve()), f"路径超出数据目录：{path}")
    return path


def validate_source(root, cloud=None):
    metas = sorted((root / "meta/unpacked").glob("meta_*"))
    require(bool(metas), "没有规范 Meta；保留已有现场，不自动刷新导出")
    maps = Counter()
    subtasks = []
    video_count = grid_count = row_count = 0
    for meta in metas:
        sid = meta.name.removeprefix("meta_")
        for name in ("frames.jsonl", "task.json", "video_segments.json"):
            require((meta / name).is_file(), f"缺少 {meta / name}")
        require(sid.startswith(root.name + "_"), f"错误子任务归属：{sid}")
        frames = list(rows(meta / "frames.jsonl"))
        stamps = [float(frame["ts"]) for frame in frames]
        require(stamps and all(math.isfinite(t) for t in stamps)
                and all(b > a for a,b in zip(stamps, stamps[1:])), f"空帧或非递增时间：{sid}")
        for frame in frames:
            require(frame.get("task_id") == root.name, f"帧 task_id 不匹配：{sid}")
            maps[str(frame.get("map_name"))] += 1
            if frame.get("grid_valid"):
                with Image.open(inside(meta, frame["grid_png"])) as image:
                    require(image.size == (frame["width"], frame["height"]), f"栅格尺寸不匹配：{sid}")
                    image.verify()
                grid_count += 1
        video_dir = root / "videos" / f"videos_{sid}"
        manifest = read_json(video_dir / "manifest.json")
        declared = read_json(meta / "video_segments.json")["segments"]
        cameras = sorted({item["camera"] for item in declared})
        results = manifest.get("results") or []
        require(cameras and manifest.get("sub_task_id") == sid
                and manifest.get("concat_complete") is True and manifest.get("align") is True
                and manifest.get("align_mode") in ("hw_ts", "wall_clock")
                and manifest.get("download_skip", 0) == manifest.get("download_fail", 0) == 0
                and manifest.get("download_ok", 0) + manifest.get("download_reused", 0) > 0
                and manifest.get("concat_ok") == manifest.get("camera_count") == len(cameras)
                and sorted(item.get("camera") for item in results if item.get("ok")) == cameras,
                f"视频清单不完整：{sid}；不会覆盖已有视频")
        windows, window_source = _effective_video_windows(manifest)
        duration = sum(float(w["to"]) - float(w["from"]) for w in windows)
        require(math.isfinite(duration) and duration > 0, f"无效视频保留窗口：{sid}")
        durations = []
        for camera in cameras:
            path = inside(video_dir, f"{camera}_continuous.mp4")
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration,nb_frames", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60)
            require(result.returncode == 0, f"视频探测失败：{path}")
            stream = json.loads(result.stdout)["streams"][0]
            seconds = float(stream["duration"])
            require(math.isfinite(seconds) and seconds > 0 and int(stream["nb_frames"]) > 0,
                    f"空视频：{path}")
            durations.append(seconds)
        require(max(durations)-min(durations) <= 0.1 and
                max(abs(value-duration) for value in durations) <= 0.1, f"视频时长未对齐：{sid}")
        if cloud:
            require(cameras == cloud["cameras_by_subtask"].get(sid), f"云端/导出相机集合不一致：{sid}")
        subtasks.append(dict(sub_task_id=sid, frames=len(frames), cameras=cameras,
                             align_mode=manifest["align_mode"], window_source=window_source,
                             holes=sum(len(item.get("holes", [])) for item in results)))
        video_count += len(cameras)
        row_count += len(frames)
    if cloud:
        require({item["sub_task_id"] for item in subtasks} == set(cloud["cameras_by_subtask"]),
                "云端/导出子任务集合不一致")
    return dict(passed=True, subtask_count=len(metas), row_count=row_count, grid_count=grid_count,
                video_count=video_count, map_names=dict(maps), subtasks=subtasks)


def check_run_source(run, source, assets, config):
    info = read_json(run / "run.json")
    require(info.get("status") == "success" and info.get("mode") == "full"
            and info.get("full_generation_authorized") is True and info.get("accepted_count", 0) > 0,
            "已有教师 run 不完整或没有接受样本；不自动重生成")
    for name in ("pipeline_version", "route_version", "state_sampler_version", "occupancy_fusion_version"):
        require(info.get(name) == config[name], f"已有 run 的 {name} 与当前配置不一致")
    for name in ("checkpoint_sha256", "teacher_config_sha256"):
        require(info.get(name) == assets[name], f"已有 run 的 {name} 与指定教师不一致；不自动重打标")
    require(Path(info["task_root"]).resolve() == source.resolve(), "已有 run 指向其他源目录")
    end = datetime.fromisoformat(info["completed_at"]).timestamp()
    current = {}
    for meta in sorted((source / "meta/unpacked").glob("meta_*")):
        sid = meta.name.removeprefix("meta_")
        for index, frame in enumerate(rows(meta / "frames.jsonl")):
            current[sid, index] = frame
        for path in [meta / "frames.jsonl", meta / "task.json",
                     source / "videos" / f"videos_{sid}" / "manifest.json"]:
            require(path.stat().st_mtime <= end + 1e-3, f"源标注/裁剪晚于教师 run：{path}；需明确授权重处理")
    seen = set()
    for item in rows(run / "source_manifest.jsonl"):
        key = item["source_sub_task_id"], item["row_index"]
        require(key in current and key not in seen, "已有 run 与源行集合不一致")
        frame = current[key]
        require(frame["ts"] == item["meta_ts"]
                and (frame.get("map_name") or frame.get("current_map")) == item.get("source_map_name"),
                f"源时间戳/人工 map_name 已改变：{key}；不自动重打标")
        seen.add(key)
    require(seen == set(current), "源记录行数与已有教师 run 不一致")
    return info


def validate_teacher(run, config, report, workers):
    validation = validate_run(run, config)
    write_json(report / "teacher_validation.json", validation)
    require(validation["validation"]["passed"], "教师逐样本验证失败；见 teacher_validation.json")
    samples = [read_json(path) for path in sorted((run / "cases").glob("*/sample.json"))]
    references = defaultdict(set)
    for sample in samples:
        for entries in sample["rgb_history"]["cameras"].values():
            for entry in entries:
                references[entry["filename"]].add(sample["case_id"])
        for camera in sample["camera_matches"]:
            references[f"cases/{sample['case_id']}/{camera}.png"].add(sample["case_id"])

    def inspect(filename):
        try:
            with Image.open(inside(run, filename)) as image:
                black = max(high for low, high in image.convert("RGB").getextrema()) <= 8
            return dict(filename=filename, decode_ok=True, black=black) if black else None
        except Exception as error:
            return dict(filename=filename, decode_ok=False, black=False, error=type(error).__name__)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        bad = [item for item in pool.map(inspect, references) if item is not None]
    affected = {case for item in bad for case in references[item["filename"]]}
    quality = dict(checked_image_paths=len(references), black_image_paths=sum(x["black"] for x in bad),
                   decode_failed_paths=sum(not x["decode_ok"] for x in bad), affected_case_count=len(affected),
                   without_black_frames_case_count=len(samples)-len(affected), images=bad,
                   black_threshold="all RGB channels/pixels <= 8", affected_cases=sorted(affected))
    write_json(report / "rgb_quality.json", quality)
    require(quality["decode_failed_paths"] == 0, "RGB 解码失败；见 rgb_quality.json")
    with (report / "samples_without_black_frames.jsonl").open("x") as stream:
        for sample in samples:
            if sample["case_id"] not in affected:
                stream.write(json.dumps(dict(run_dir=str(run), case_id=sample["case_id"],
                    sample_json=str(run / "cases" / sample["case_id"] / "sample.json"))) + "\n")
    return dict(candidate_count=validation["candidate_count"], accepted_count=validation["accepted_count"],
                rejected_count=validation["rejected_count"], validation=validation["validation"],
                without_black_frames_count=quality["without_black_frames_case_count"])


def validate_pixels(output, run, maps):
    audit = read_json(output / "pixel_coordinate_audit.json")
    schema = read_json(output / "coordinate_schema.json")
    require(audit.get("version") == schema.get("version") == VERSION
            and schema.get("coordinate_frame") == FRAME, "已有像素坐标版本/坐标系不匹配")
    require(audit.get("source_unchanged") is True, "像素审计缺少源保护成功标志")
    require(str((run / "run.json").resolve()) in audit["source_sha256"], "像素导出不属于指定教师 run")
    for path, expected in audit["source_sha256"].items():
        require(digest(path) == expected, f"像素源文件已改变：{path}")
    require({"samples.jsonl", "coordinate_schema.json"}.issubset(audit["output_sha256"]), "像素审计缺少核心文件")
    for relative, expected in audit["output_sha256"].items():
        require(digest(inside(output, relative)) == expected, f"已有像素文件已改变：{relative}")
    for name, meta in schema["maps"].items():
        require(Path(meta["path"]).resolve() == Path(maps[name]).resolve(), "像素导出地图与当前配置不一致")
    accepted = {item["case_id"] for item in rows(run / "teacher_labels.jsonl") if item.get("status") == "accepted"}
    seen = set()
    max_error = 0.0
    for row in rows(output / "samples.jsonl"):
        require(row.get("coordinate_frame") == FRAME and row.get("pixel_data_version") == VERSION,
                "像素行的坐标系/版本不匹配")
        case = row["case_id"]
        require(case in accepted and case not in seen, "像素样本身份重复或不属于接受样本")
        sample_path = run / "cases" / case / "sample.json"
        npz_path = sample_path.parent / "inputs.npz"
        require(row["source_run"] == str(run) and row["source_sample_json"] == str(sample_path)
                and row["inputs_npz_path"] == str(npz_path), "像素溯源路径不一致")
        sample = read_json(sample_path)
        require(row["map_name"] == sample["map_name"], "像素地图不一致")
        meta = schema["maps"][row["map_name"]]
        pose = sample["grid_pose"]
        with np.load(npz_path, allow_pickle=False) as arrays:
            fields = dict(reference_pose=np.asarray([pose["x"],pose["y"],pose["yaw_rad"]]),
                          raw_poses=arrays["teacher_history_pose"], route=arrays["forward_route"])
            require(np.array_equal(row["raw_pose_stamps_s"], arrays["teacher_history_stamp_s"]), "历史时间戳被改变")
            for key, world in fields.items():
                pixel = np.asarray(row[key])
                require(pixel.shape == world.shape and np.isfinite(pixel).all(), f"无效像素字段：{key}")
                back = (pixel[..., :2] + .5) * meta["resolution_m"] + np.asarray(meta["origin_xy"])
                error = float(np.max(np.abs(back-world[..., :2]))) if world.size else 0.0
                max_error = max(max_error, error)
                require(error < 1e-9, f"像素坐标往返失败：{key}")
                if world.shape[-1] == 3:
                    require(np.array_equal(pixel[..., 2], world[..., 2]), "yaw 被改变")
        require(row["commands"] == sample["teacher"]["commands"] and row["initial_state"] == sample["initial_state"]
                and row["source_rgb_history"] == sample["rgb_history"], "像素导出改变命令/初态/RGB 引用")
        seen.add(case)
    require(len(seen) == len(accepted) == read_json(run / "run.json")["accepted_count"]
            == audit["counts"]["samples.jsonl"]["total"], "像素数量与教师接受样本不一致")
    return dict(passed=True, row_count=len(seen), world_roundtrip_max_error_m=max_error,
                source_unchanged=True, output_hashes_verified=True)
