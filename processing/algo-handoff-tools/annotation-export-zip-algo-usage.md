# 标注任务导出 · 算法使用说明

看板默认交 **JSONL**（按子任务）。要占据 PNG 再导 ZIP。录像不在导出包里：车上回传到 OSS 后，训练机用 `pull-oss-videos.py` 拉段并拼接。

训练机也可以**不打开看板**，用工具包里的 `pull-task-export.py` 直拉同一 ZIP。

```http
GET /api/annotation-tasks/{task_id}/export?format=jsonl&grids=meta&pack=algo&sub_task=1
GET /api/annotation-tasks/{task_id}/export?format=zip&grids=png&pack=algo&sub_task=1
GET /api/annotation-tasks/{task_id}/export?format=zip&grids=png&pack=algo&sub_task=all
```

看板：选子任务 → **导出 JSONL**（单子任务）/ **导出 ZIP（算法包）**。需登录或 `X-API-Key`。

训练机运行前需通过环境变量 `SKDOS_API_KEY` 配置 Key。仓库不保存真实 Key，申请和加载方式见工具包 README“鉴权”。

```bash
python3 pull-task-export.py --task 20260820180215WDK --out ./meta
python3 pull-task-export.py --task 20260820180215WDK --sub-task 7 --out ./meta
```

## 命名

`task_id` = 上海时间 `YYYYMMDDHHMMSS` + 3 位 `[0-9A-Za-z]`，例 `20260819143022aB3`。
`sub_task_id` = `{task_id}_{i}`，`i` 对应 pause/resume 的采集段（从 1），**不是**排除切开的碎段。
排除只缩短该子任务，不改 `_2` 编号。改过后必须重新导出。

| 产物 | 文件名 |
|------|--------|
| 单子任务 JSONL / ZIP | `meta_{task_id}_{i}.jsonl` / `meta_{task_id}_{i}.zip` |
| 全部子任务 ZIP | `meta_{task_id}_all.zip`（内含 `meta_{task_id}_{i}/…`） |
| 训练机拼好的视频 | `videos_{task_id}_{i}/camX_continuous.mp4` |

历史任务可能仍是 `at-…`，前缀规则相同。

---

## 默认包（`pack=algo`）

**JSONL**（看板主入口，必须指定单个子任务）：

```text
首行  { _task, _export, _kind:"frames", _video_segments, _camera_positions }
中间  一帧一行（pose + actual_vel + 占据元数据，无 cells / 无 PNG）
末行  { _export_complete, sample_count, ... }
```

**ZIP**（要 PNG 时）：

```text
meta_{task_id}_{i}.zip
├── task.json              # 含 sub_task / sub_tasks / keep_windows / excluded_ranges
├── export_meta.json
├── video_segments.json    # 该子任务 keep 窗内 OSS 段（presigned url + clip_from/to）
├── frames.jsonl
└── grids/{ts_ms}.png
```

不含 mp4 二进制、不含 `cells`。`task.json` 另有 `collect_video`、`video_cameras`、`video_quality`（上传规格，默认 `640x512`）。

### 审计全量（可选）

```http
GET .../export?format=zip&grids=png&pack=full
# 或 raw=1
```

额外包含 `signals.jsonl`，且 occupancy payload **保留 cells**。

---

## 关键文件说明

### 1. `task.json`（任务级）

| 字段 | 说明 |
|------|------|
| `task_id` / `robot_id` / `session_id` | 标识 |
| `mode` | `navigation` \| `teleop` |
| `start` / `end` | `{x,y,yaw,graph_name,building_id,floor_id,...}`，yaw **度** |
| `planned_route` | 全局规划；**权威 `segments[]`**（每段 `graph_name` + `path` + **`node_labels`**），另附 `maps`/`source`/`node_labels`。**不含** `planner_response`、`timeline`、`node_path`/`node_tags` |

`planned_route` 与实测 pose **不一定同一坐标系**，对比路径前先确认 frame。

```json
"planned_route": {
  "source": "orin_global_planner",
  "maps": ["B10_map_..."],
  "segments": [
    {
      "graph_name": "B10_map_...",
      "map_type": "indoor",
      "path": [[x, y], ...],
      "distance": 12.3,
      "node_labels": [ { "node_id": "...", "label": "...", "acceleration": -2 }, ... ]
    }
  ],
  "node_labels": [ "...各段拼接, 可选顶层聚合..." ]
}
```

- **`node_labels`**：需要保留（电梯/按钮/加速度等动作语义；与 `path` 等长）
- **`node_path` / `node_tags`**：导出不保留
- 跨图时 `segments.length > 1`；单图通常 **1 段**

### 2. `frames.jsonl`（算法主入口）

每行示例：

```json
{
  "ts": 1784623469.294,
  "ts_ms": 1784623469294,
  "stamp_ms": 1784623469282,
  "datetime_utc": "2026-07-21T12:44:29.294Z",
  "datetime_local": "2026-07-21 20:44:29.294",
  "tz": "Asia/Shanghai",
  "task_id": "20260817153045aB3",
  "session_id": "20260817153045aB3-...",
  "pose": {
    "x": 0.384,
    "y": -0.01,
    "yaw": 179.09,
    "valid": true,
    "pose_age_s": 0.02
  },
  "actual_vel": {
    "vx": 0.12,
    "vy": 0,
    "wz": 0.01,
    "linear_vel_mps": 0.12,
    "angular_vel_rads": 0.01,
    "src": "odom",
    "valid": true
  },
  "grid_valid": true,
  "width": 200,
  "height": 200,
  "resolution": 0.05,
  "origin_x": -5,
  "origin_y": -5,
  "frame_id": "body",
  "y_axis": "image",
  "cells_count": 602,
  "grid_pose": {
    "x": 0.384,
    "y": -0.01,
    "yaw": 179.09,
    "yaw_rad": 3.125,
    "valid": true,
    "source_ts": 1784623469.294,
    "match": "ros_grid_sync",
    "match_dt_ms": 0,
    "pose_src": "ros_grid_sync"
  },
  "grid_png": "grids/1784623469294.png"
}
```

| 字段 | 说明 |
|------|------|
| `ts` / `ts_ms` | 采样对齐键（与同批 pose/vel/grid 一致） |
| `datetime_utc` | ISO-8601 UTC |
| `datetime_local` | 墙钟 `YYYY-MM-DD HH:mm:ss.SSS`，时区见 `tz`（固定 Asia/Shanghai） |
| `tz` | `"Asia/Shanghai"` |
| `pose` | 独立 `type=pose` 流摘要（轨迹监督）；yaw **度**；**不是**栅格对齐权威 |
| `actual_vel` | `vx,vy` 线速度 m/s，`wz` 角速度 rad/s；别名 `linear_vel_mps`=`vx`、`angular_vel_rads`=`wz`；`src`=`odom`（底盘/odom）或 `pose_diff*`（位姿差分兜底） |
| `grid_valid` | 该帧是否有有效占据图 |
| `frame_id` | 通常 **`body`**（车体近场 ±5m） |
| `y_axis` | 固定 `"image"`：行 0 在图顶，入库已 flipud |
| `cells_count` | 占用格子数（**无 cells 数组**） |
| `grid_pose` | **栅格对齐位姿**：栅格帧内 ROS 同步（`payload.grid_pose`，兼容历史 `robot_pose`）；无则 `null` 或 `match=none` |
| `grid_png` | 相对 ZIP 根路径；无图时为 `null` |

**位姿（勿混用）：**

1. **栅格对齐 / 占据监督** → 只用 `grid_pose`，且 `match == "ros_grid_sync"`
2. **轨迹 / 速度监督** → 同行 `pose` + `actual_vel`（独立 5Hz 信号流）
3. **不要**把 `pose` 当栅格对齐位姿

**过滤建议：** 训占据时要求 `grid_valid == true` 且 `grid_pose.valid == true` 且 `match == "ros_grid_sync"`。
无 grid 的样本仍可能有 `pose` / `actual_vel`（轨迹/速度序列）；此时 `grid_pose` 为 `null`。

**速度 `src`：**

| src | 含义 |
|-----|------|
| `odom` | 底盘/odom 服务 twist（优先） |
| `pose_diff` / `pose_diff_fallback` | 相邻定位差分估计（兜底，非底盘原始） |

### 3. `grids/{ts_ms}.png`

- 8-bit **灰度**，默认约 **200×200**
- **黑(0)=障碍物**，**白(255)=非障碍物**
- 由库内稀疏 `cells` 在**导出时**合成，非车端原始 PNG
- **Y 轴已在入库时 `flipud`**（与 navi `data_collector` 一致）：`y_axis=image`，PNG **行 0 = 图顶**（世界 y 最大）。算法不要再翻。
- 像素 `(ix, iy)` → 车体坐标（米）：

```text
x = origin_x + (ix + 0.5) * resolution
y = origin_y + (height - 1 - iy + 0.5) * resolution
```

---

## 算法最小用法（Python）

```python
import json, zipfile
import numpy as np
from PIL import Image

def load_zip(path: str):
    z = zipfile.ZipFile(path)
    task = json.loads(z.read("task.json"))
    frames = []
    with z.open("frames.jsonl") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            frames.append(json.loads(line))
    return z, task, frames

z, task, frames = load_zip("meta_20260817153045aB3.zip")

for fr in frames:
    if not fr.get("grid_valid"):
        continue
    rp = fr.get("grid_pose") or {}
    if not rp.get("valid") or rp.get("match") != "ros_grid_sync":
        continue
    png_name = fr.get("grid_png")
    if not png_name:
        continue

    img = np.array(Image.open(z.open(png_name)))  # (H,W) uint8
    x = (img.astype(np.float32) / 255.0)[None, None, ...]

    robot_xy = (rp["x"], rp["y"])
    yaw_deg = rp["yaw"]
    # 轨迹/速度用独立 pose 流, 不是 grid_pose
    traj_pose = fr.get("pose") or {}
    vel = fr.get("actual_vel") or {}
    # 训练 step ...
```

---

## 时间与对齐

| 时间 | 用途 |
|------|------|
| `ts` / `ts_ms` | **主对齐键** |
| `stamp_ms` | 栅格源时钟，可与 `ts_ms` 差几～几十 ms |
| `ts_cloud` | 入库时刻，**不要**用于同步 |

同一次 5Hz 采样：`pose` / `actual_vel` / `occupancy_grid` 共享同一 `ts`，已在 `frames.jsonl` 聚成一行。

---

## 坐标系注意

| 数据 | frame |
|------|--------|
| 近场 grid / PNG | **`body`** 车体 |
| `grid_pose` / `pose` | 定位系（map/odom）；`grid_pose` 与 grid 同 ROS 帧 |
| `planned_route.path` | 对应 `graph_name` 的地图系 |

grid 与 path **不要**直接叠在同一张全局图上，除非已有 TF。

---

## 其它导出格式

| URL | 内容 |
|-----|------|
| `?format=zip&grids=png` | **默认算法包**（同上） |
| `?format=zip&grids=png&pack=full` | + `signals.jsonl` + cells |
| `?format=jsonl&pack=algo` | 仅 frames 行（无 PNG 文件） |
| `?format=jsonl&grids=png&pack=algo` | frames + 内嵌 base64（体积大，不推荐） |
| `?format=jsonl&pack=full` | 原始 signals 行 |

---

## 质量门槛（建议）

1. 丢弃 `grid_valid=false` 或 `grid_pose.valid=false` / `match!="ros_grid_sync"`（若只训占据）
2. 轨迹序列可用 `pose`/`actual_vel`，即使该行无 grid
3. 速度：优先 `src=="odom"`（`linear_vel_mps`/`vx`、`angular_vel_rads`/`wz`）；`pose_diff*` 为兜底
4. 跨图：按 `planned_route.segments[].graph_name` 分段

---

## 实现

- `skdos-live` `export?format=zip` + `occupancy-export.ts`
- 采集：edge 5Hz → `/api/ingest` → ClickHouse 稀疏 JSON；PNG **仅导出合成**

---

## 相机物理位置（算法判断）

方位名称 **跨机器人统一**；`camN` 编号 **按车映射**（实现 `src/camera-positions.ts`）。导出必须结合任务 `robot_id`。

统一方位：

| position_zh | position_en |
|-------------|-------------|
| 前下 | front_bottom |
| 右 | right |
| 前上 | front_top |
| 左 | left |
| 前广 | front_wide |
| 后上 | rear_top |

Robot-U2-V1：`cam0` 前下、`cam1` 右、`cam2` 前上、`cam3` 左、`cam5` 前广、`cam6` 后上。

- `task.json.camera_positions`：`{ "cam0": { "position_zh", "position_en", "body_frame", "unified_key" }, ... }`
- `video_segments.json.camera_positions`：同上
- `segments[].physical_position`：`{ position_zh, position_en, unified_key }`

---

## 训练机：直拉任务数据 ZIP

与看板“任务数据导出 → 导出 ZIP”使用同一接口。运行前必须通过环境变量 `SKDOS_API_KEY` 配置 Key。

```bash
export SKDOS_LIVE_URL=https://skdos-live-test.uniubi.com
export SKDOS_API_KEY='从管理员处获得的Key'
python3 pull-task-export.py --list --robot Robot-U2-V1
python3 pull-task-export.py --task 20260820180215WDK --out ./meta          # 全部子任务 ZIP
python3 pull-task-export.py --task 20260820180215WDK --sub-task 7 --out ./meta
python3 pull-task-export.py --task 20260820180215WDK --format jsonl --sub-task 1 --out ./meta
```

## 训练机：拉 OSS 并拼接（推荐）

车上「存储到云端」之后，JSONL 首行 `_video_segments`（或 ZIP 里 `video_segments.json`）是段索引，**不含 mp4**。

每段字段：`camera`、`start_ts`/`end_ts`、`clip_from_ts`/`clip_to_ts`（keep∩段墙钟，无 hw 回退）、`url`（presigned，约 1h）、`object_key`、`physical_position`、`spec`；有车上采集钟时另有 `t0_hw`/`t1_hw`/`frame_count`（无则省略，不填假值）。

```bash
python3 pull-oss-videos.py --in meta_20260819143022aB3_1.jsonl --out ./videos
python3 pull-oss-videos.py --in meta_20260819143022aB3_1.zip --out ./videos
python3 pull-oss-videos.py --in meta_....jsonl --out ./videos --dry-run
python3 pull-oss-videos.py --in meta_....zip --out ./videos --copy   # 旧 copy，不标成已对齐
```

- 按 **子任务** 输出 `videos_{sub_task_id}/camX_continuous.mp4`
- 默认按 `keep_windows` 建立定长时间轴，缺段补黑；各路输出时长互差目标 **<100ms**
- 所有分段都有完整 `t0_hw`/`t1_hw` → `align_mode=hw_ts`；否则整批回退 `wall_clock`
- 相位 <100ms 依赖车上采集钟，不使用 shm `%019d` / jpegenc 文件名推断时间
- `--copy` 保留旧关键帧 copy 快路径，不视为已对齐
- `url` 过期：重新导出 JSONL/ZIP

只下载不分段拼接：`node download-video-segments.mjs --in meta_....jsonl --out ./vids`

车上直接读本机盘（不经 OSS，可选）：`export-local-continuous-video.py --task-json task.json`

## 交给算法的最小包

1. 本工具包 + `X-API-Key`
2. `python3 pull-task-export.py --task <id> --out ./meta` → `meta_{task_id}_all.zip`（含 PNG）
3. 车上已「存储到云端」后：`python3 pull-oss-videos.py --in meta_…_all.zip --out ./videos`
4. 仍可用看板「导出 JSONL / ZIP」代替第 2 步
