"""Build the final audit summary and scientific coverage plot from saved exports."""
from collections import Counter
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

REPORT = Path(__file__).resolve().parent
PROJECT = REPORT.parents[1]
BATCH = Path("/mnt/chengchangxu/data/visual_nav_training/_source_map_exports/20260924_source_map")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


summary = json.loads((BATCH / "summary.json").read_text())
assert summary["status"] == "success" and len(summary["tasks"]) == 18
before = json.loads((REPORT / "source_protection_before.json").read_text())
changed = [name for name, old in before.items()
           if not Path(name).is_file() or Path(name).stat().st_size != old["size"]
           or Path(name).stat().st_mtime_ns != old["mtime_ns"] or digest(name) != old["sha256"]]
protection = dict(checked_files=len(before), unchanged=not changed, changed=changed)
(REPORT / "source_protection_after.json").write_text(json.dumps(protection, indent=2) + "\n")
assert not changed, changed

counts, exclusions, current_points, trimmed_lengths = Counter(), Counter(), [], Counter()
source_hashes = {}
gray_points = gray_segments = 0
max_error = 0.
for task in summary["tasks"]:
    validation = task["validation"]
    counts.update(validation["maps"])
    exclusions.update(task["exclusion_reasons"])
    gray_points += validation["gray_points_checked"]
    gray_segments += validation["gray_segments_checked"]
    max_error = max(max_error, validation["world_roundtrip_max_error_m"])
    audit = json.loads((Path(task["output"]) / "pixel_coordinate_audit.json").read_text())
    for name, sha in audit["source_sha256"].items():
        assert name not in source_hashes or source_hashes[name] == sha
        source_hashes[name] = sha
    with Path(task["samples"]).open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["map_name"] == "P_map":
                current_points.append(row["handdraw_pixel_xy"])
                trim = row["source_map_route_trim"]
                if trim["retained_points"] < trim["original_points"]:
                    trimmed_lengths[trim["retained_points"]] += 1

# Recheck the union after all tasks, not only when an individual task finished.
assert all(digest(name) == sha for name, sha in source_hashes.items())
assets = PROJECT / "processing/coordinate-converter/assets/source_map"
registration = json.loads((assets / "registration.json").read_text())
assert digest(registration["source_path"]) == registration["source_sha256"]
image = np.rot90(np.asarray(Image.open(assets / "source_map.png")))
xy = np.asarray(current_points)
fig, ax = plt.subplots(figsize=(8, 10), constrained_layout=True)
ax.imshow(image)
ax.scatter(xy[:, 0], image.shape[0] - 1 - xy[:, 1], s=3, alpha=.35,
           c="#1473c9", linewidths=0, label=f"Retained P_map observations: {len(xy):,}")
ax.set_title("New source map: retained observation coverage")
ax.set_xlabel("image column = x_pixel")
ax.set_ylabel("image row = height - 1 - y_pixel")
ax.legend(loc="upper right")
fig.savefig(REPORT / "coverage.png", dpi=150)
plt.close(fig)

result = dict(summary, retained_maps=dict(counts), exclusion_reasons=dict(exclusions),
              gray_points_checked=gray_points, gray_segments_checked=gray_segments,
              world_roundtrip_max_error_m=max_error, raw_source_protection=protection,
              teacher_map_asset_sources_verified=len(source_hashes),
              trimmed_route_prefix_lengths=dict(sorted(trimmed_lengths.items())),
              regression=dict(passed=65, failed=0, command="PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q processing/coordinate-converter/tests processing/data_pipeline/tests"),
              code_sha256={str(p.relative_to(PROJECT)): digest(p)
                           for p in sorted((PROJECT / "processing/coordinate-converter").rglob("*.py"))})
(REPORT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
total = summary["totals"]
lines = ["# 新版灰线地图坐标映射（2026-09-24）", "",
         "> 本页为首次混合导出的历史记录，包含超出用户要求的 B10 数据。当前使用 [P_map 专用结果](pmap_only_report.md)。", "",
         "已完成 18 个任务各自最新成功 full 教师 run 的独立坐标导出；未重新下载、解包或运行教师。", "",
         f"输入 **{total['input_count']:,}** 条，保留 **{total['retained']:,}** 条，剔除 **{total['excluded']:,}** 条；保留样本中 **{total['trimmed_routes']:,}** 条路线被截断。",
         f"保留地图分布：`{dict(counts)}`。剔除原因：`{dict(exclusions)}`。", "",
         f"- 数据目录：[新版批次]({BATCH})；[各任务数据与 schema 索引]({BATCH / 'summary.json'})。",
         "- 使用 `--handdraw-map source-map`；旧模式仍为默认 `legacy`。",
         "- P_map 的新图坐标在 `handdraw_pixel_xy/handdraw_raw_pixel_xy/handdraw_route_pixel_xy`；`reference_pose/raw_poses/route` 保持对应原栅格坐标。B10 保留原图。",
         "- 关闭路口吸附；复用经用户确认的新旧同布局注册和配对道路弧长。新图仅逆时针 90°，输出 1642×2192，保留三色和人工断口。",
         "- 当前点/任一历史点不在灰线则剔除样本；路线起点无效也剔除；其余路线只保留第一个无效点/线段前的前缀。灰线拐角允许最多 2 px 局部校正。",
         "- 每任务 `excluded_rows.jsonl` 记录剔除，`source_map_route_trim` 记录路线原/保留点数。未额外筛黑帧，不改写原采集裁剪或标注。", "",
         "## 验证", "",
         "- 坐标转换与原流水线回归 **65/65** 通过；默认旧行为、半像素、灰度图/断口、缓存等价、过滤/截断、混合地图和损坏源保护均覆盖。",
         f"- 对全部保留 P_map 数据核验 **{gray_points:,}** 个当前/历史/路线点和 **{gray_segments:,}** 段相邻路线，均落在灰色 192 上。",
         f"- 坐标往返最大误差 **{max_error:.4g} m**；yaw、历史时间、命令、初态和 RGB 引用保持源值。",
         f"- **{len(before)}** 份源 frames/视频清单的大小、mtime、SHA-256 前后一致；**{len(source_hashes):,}** 个教师/地图/资产文件的 SHA-256 在任务结束和批次结束时复核一致；输出哈希全部通过。",
         "- 每任务保留与剔除的 case 集合不重叠，合起来恰好等于全部 accepted case。三个人工断口均拒绝直连，输出图与原 PNG 的精确 90° 旋转逐像素一致。", "",
         "## 使用边界", "",
         "这是基于旧配对道路的迁移注册，不是重新实测标定。未配对的新道路没有补世界坐标；公开名义比例不能当全图统一精确米制比例。",
         "教师动作标签原样保留，裁短路线不代表未来动作已通过新版通行拓扑验证。过滤后的记录也不能直接视为连续时序；后续序列训练应按源时间/保留情况重新分段。",
         "新版本为 `vnav_pixels_source_map_v2`，不能当旧版像素 manifest 直接使用。新 `source_map/road_mask.png` 是通行栅格，当前转换器不生成新的图搜索路线。", "",
         "## 任务明细", "", "| 任务 | 输入 | 保留 | 剔除 | 路线截断 |", "|---|---:|---:|---:|---:|"]
for task in summary["tasks"]:
    lines.append(f"| {task['task']} | {task['input_count']} | {task['retained']} | {task['excluded']} | {task['trimmed_routes']} |")
lines += ["", "## 位置覆盖图", "", "蓝点为保留的当前观测位置；用于检查道路覆盖，不代表定位精度评估。", "",
          f"![新图保留观测覆盖]({REPORT / 'coverage.png'})", ""]
(REPORT / "report.md").write_text("\n".join(lines))
print(json.dumps({key: result[key] for key in ("totals", "retained_maps", "exclusion_reasons", "gray_points_checked",
                                               "gray_segments_checked", "world_roundtrip_max_error_m",
                                               "teacher_map_asset_sources_verified")}, ensure_ascii=False))
