# 算法交接工具包

车上采完 → 训练机用本包两步拉齐：**任务数据 ZIP**（pose / 速度 / 占据 PNG）和 **连续视频**。

不必打开看板点「导出 ZIP」。训练机用 `X-API-Key` 直拉即可。

## 鉴权 `SKDOS_API_KEY`

导出接口要带 `X-API-Key`。脚本只从环境变量 `SKDOS_API_KEY` 读取，仓库不保存真实 Key。

向管理员申请 Key 后，在本机配置：

```bash
export SKDOS_LIVE_URL=https://skdos-live-test.uniubi.com
export SKDOS_API_KEY='从管理员处获得的Key'
python3 pull-task-export.py --list --robot Robot-U2-V1
python3 pull-task-export.py --task 20260820180215WDK --out ./meta
```

也可以从项目根目录复制 `.env.example` 为 `.env`，填写后通过 `source .env` 加载；`.env` 已被 Git 忽略。管理员可在 https://skdos-live-test.uniubi.com/admin/keys 创建或轮换 test Key（明文只显示一次）。报 `401` 表示 Key 错误或已吊销。

## 包内文件

| 文件 | 用途 |
|------|------|
| `README.md` | 本说明 |
| `annotation-export-zip-algo-usage.md` | JSONL/ZIP 字段、坐标系、`grid_pose` |
| **`download-task.sh`** | **项目入口：按 task 完成导出、解压、视频下载、对齐与可选安全清理** |
| **`list-task-ids.py`** | **按起止时间列出全部 task_id**（不受看板 200 条限制） |
| **`pull-task-export.py`** | **训练机直拉「任务数据导出」ZIP / JSONL** |
| **`normalize-meta-export.py`** | **校验 task/subtask 归属并规范解压到 `unpacked/meta_<sub_task_id>/`** |
| **`pull-oss-videos.py`** | 从刚拉到的 ZIP/JSONL 拉 OSS 段 + 按 keep_windows 拼连续 MP4 |
| `download-video-segments.mjs` | 可选：只下载分段（Node 18+） |
| `export-local-continuous-video.py` | 可选：车上读本机盘拼接（不经 OSS） |
| `test_pull_oss_videos.py` | sibling 元数据、时间轴、FFmpeg 输出及任务清理回归测试 |

## 命名

| 项 | 规则 | 例 |
|----|------|----|
| `task_id` | 上海时间 `YYYYMMDDHHMMSS` + 3 位 `[0-9A-Za-z]` | `20260819143022aB3` |
| `sub_task_id` | `{task_id}_{i}`，`i` 是 pause/resume 的采集段（从 1） | `20260819143022aB3_1` |
| 任务数据 | `meta_{sub_task_id}.jsonl` / `.zip`；全部子任务 `meta_{task_id}_all.zip` | `meta_20260819143022aB3_1.jsonl` |
| 连续视频 | `videos_{sub_task_id}/camX_continuous.mp4` | `videos_20260819143022aB3_1/cam0_continuous.mp4` |

排除片段只缩短该子任务内容，**不改** `_2` 编号。改过排除必须重新导出。

历史任务可能仍是 `at-…`，前缀规则一样。

## 按时间批量查 task_id

看板任务列表只加载最近 200 条。训练机需要完整时间窗时，使用：

```http
GET /api/annotation-tasks/ids?from=2026-08-26&to=2026-08-26&robot=Robot-U2-V1
```

- `from` / `to` 必填，可为 unix 秒或 `YYYY-MM-DD` / `YYYY-MM-DD HH:MM[:SS]`。
- 未写时区按 `Asia/Shanghai`；只给日期时，`from` 从当天 00:00:00 开始，`to` 包含当天结束。
- 筛选采集开始时间 `started_ts`；未启动草稿使用 `created_ts`。可选 `robot`。
- 服务端最多返回 50000 条；响应 `truncated=true` 时应缩小时间窗。
- 鉴权与导出一致，只从环境变量 `SKDOS_API_KEY` 读取。

```bash
# 某一整天（上海时间）
python3 list-task-ids.py --from 2026-08-26 --to 2026-08-26

# 指定机器人和时段
python3 list-task-ids.py \
  --from '2026-08-26 15:00' --to '2026-08-26 20:00' \
  --robot Robot-U2-V1

# 仅输出 ID，便于批量导出
python3 list-task-ids.py --from 2026-08-26 --to 2026-08-27 --ids-only > ids.txt
while read -r task_id; do
  python3 pull-task-export.py --task "$task_id" --out ./meta
done < ids.txt
```

`pull-task-export.py --list` 仍走看板最近任务列表，不可用于完整时间窗查询。

## 推荐流程

```text
车上采集（edge 5Hz + 本机录像）
    ↓ 看板「存储到云端」（只要视频）
OSS 分段 mp4
    ↓ 训练机
python3 pull-task-export.py --task <task_id> --out ./meta
    ↓
meta_<task_id>_all.zip 或 meta_<task_id>_1.zip
    ↓ 校验归属并规范解压
unpacked/meta_<sub_task_id>/    # pose / vel / grids PNG / video_segments
    ↓
python3 pull-oss-videos.py --in <实际下载 ZIP> --out ./videos
videos_<sub_task_id>/cam*_continuous.mp4
```

导出器以服务端实际文件名为准：单子任务的 `sub_task=all` 响应可能是 `_1.zip`，且包内文件可能直接
位于根目录。`download-task.sh` 读取 `EXPORT_PATH`，再由 `normalize-meta-export.py` 校验 ZIP 内声明的
task/subtask 归属；根级单 bundle 和多目录 bundle 最终都统一写到
`meta/unpacked/meta_<sub_task_id>/`。归属不一致、缺少核心文件或空 `frames.jsonl` 时不覆盖现有规范目录。

本地已下载处理的任务使用 `download-task.sh <task_id> [output_root]` 会直接跳过，ZIP 或原始
segments 已清理也不会触发重新下载。附带清理选项时只清理指定中间产物。
已有但未通过完整性检查的任务会停止并保留现场，不自动刷新导出、解包或覆盖视频。
`--remerge` 仅允许复用完整本地 segments；缺段时停止，不补下载或刷新 Meta。
`normalize-meta-export.py` 拒绝覆盖任何已有同名 Meta 目录，避免丢失本地 `map_name` 人工标注和
裁剪结果；需要读取新版导出时，应使用独立输出目录，不把上游导出覆盖回已处理采集目录。

`meta_<task_id>_all.zip` 可直接传给 `pull-oss-videos.py`；脚本会按包内
`meta_<task_id>_<i>/` 自动展开子任务。展开后与重复传入多个 `--in` 使用同一个
`--jobs` 总并发预算。

默认拼接按子任务 `keep_windows` 建立定长时间轴，缺段补黑；15fps 下各路输出时长互差目标 **<100ms**（≤1 帧）。所有分段都有完整车上采集钟 `t0_hw`/`t1_hw` 时使用 `align_mode=hw_ts`；任一分段缺失时整批回退 `wall_clock`，避免混用时间基准。仅 `--copy` 走旧关键帧 copy 快路径，不视为已对齐。

相位 <100ms 依赖车上采集钟进库，不要把 shm `%019d` JPEG 文件名当作时间戳。

### 1) 拉任务数据 ZIP（结构化 + 占据图）

看板同一接口：`GET /api/annotation-tasks/{id}/export?format=zip&grids=png&pack=algo&sub_task=all`

```bash
# 默认连接 skdos-live-test，运行前需配置 SKDOS_API_KEY

# 列出某车任务
python3 pull-task-export.py --list --robot Robot-U2-V1

# 拉全部子任务 ZIP（含 grids/*.png）—— 对应看板「导出 ZIP」选「全部」
python3 pull-task-export.py --task 20260820180215WDK --out ./meta

# 只拉某一个子任务（更快）
python3 pull-task-export.py --task 20260820180215WDK --sub-task 7 --out ./meta

# 只要 JSONL（无 PNG 文件；必须指定单个子任务）
python3 pull-task-export.py --task 20260820180215WDK --format jsonl --sub-task 1 --out ./meta

# 只看将请求的 URL
python3 pull-task-export.py --task 20260820180215WDK --out ./meta --dry-run
```

输出文件名采用 HTTP `Content-Disposition` 的实际值：通常为 `meta_{task_id}_all.zip` 或
`meta_{task_id}_{i}.zip`；脚本最后一行稳定输出 `EXPORT_PATH=<绝对路径>`，调用方不得自行猜文件名。

ZIP 内：

```text
meta_{task_id}_{i}/          # sub_task=all 时每个子任务一个目录
  task.json
  export_meta.json
  video_segments.json        # OSS 段索引（presigned url，约 1h）
  frames.jsonl               # pose + actual_vel + 占据元数据
  grids/{ts_ms}.png
```

字段、坐标系、`grid_pose` 见 `annotation-export-zip-algo-usage.md`。
**栅格对齐只用 `grid_pose` 且 `match == "ros_grid_sync"`。** 独立 `pose` 只作轨迹/速度。

### 2) 拉视频并拼接

```bash
# 要 ffmpeg
./download-task.sh --jobs 4 20260820180215WDK
python3 pull-oss-videos.py --in meta_20260820180215WDK_all.zip --out ./videos
python3 pull-oss-videos.py --in meta_20260820180215WDK_7.zip --out ./videos
# 多个子任务与各相机共享 4 个并发槽；单子任务时 4 个槽全部用于相机
python3 pull-oss-videos.py --in meta_..._1/video_segments.json --in meta_..._2/video_segments.json --out ./videos --jobs 4
python3 pull-oss-videos.py --in meta_....jsonl --out ./videos --dry-run
python3 pull-oss-videos.py --in meta_....zip --out ./videos --copy
# 复用已有非空分段，只下载缺失分段，并覆盖连续视频
python3 pull-oss-videos.py --in meta_....jsonl --out ./videos --reuse-segments
```

输出：

```text
videos/
  segs/cam0/cam0_<start_ts>.mp4     # 原始 OSS 段
  videos_20260819143022aB3_1/
    cam0_continuous.mp4             # 该子任务、该路连续片
    cam1_continuous.mp4
    manifest.json
```

拼接规则：

- 按 **子任务** 一条连续片，不是整次任务一条
- `all.zip` 按包内目录自动展开 `_1` / `_2` / …，不会只读第一份 `video_segments.json`
- 默认 `--jobs 4`；展开的子任务和多个 `--in` 并行处理，并在每个子任务内并行处理不同相机，总并发不超过该值
- 默认按该子任务 `keep_windows` 定长补黑；全段硬件时间完整时用 `hw_ts`，否则整批用 `wall_clock`
- 直接传 `video_segments.json` 时会自动读取同目录 `export_meta.json` / `task.json` 的子任务窗口
- `--copy` 使用段上的 `clip_from_ts` / `clip_to_ts` 走旧关键帧 copy 快路径，不标成已对齐
- presigned `url` 有时效（约 1 小时）；过期重新跑 `pull-task-export.py`
- 相机路数和 ID 以各子任务 `video_segments.json` 的实际内容为准，不要求固定六路；只要实际各路均
  下载、拼接成功，Manifest 即为完成。与 Robot-U2-V1 推荐集合
  `cam0/cam1/cam2/cam3/cam5/cam6` 不一致时，命令末尾会输出 `WARNING`，但不会把完整数据判失败

`download-task.sh` 无论成功失败都会在日志最后输出“下载任务最终汇总”，列出实际 ZIP、规范 Meta、
视频子任务和全部 `WARNING`。以后出现单子任务根级 ZIP、非推荐相机集合或不完整 Manifest 时，应以
这个末尾汇总为准，不能只看中间下载日志。

方位名称统一：前下 / 右 / 前上 / 左 / 前广 / 后上。cam 编号按车不同。Robot-U2-V1：`cam0` 前下、`cam1` 右、`cam2` 前上、`cam3` 左、`cam5` 前广、`cam6` 后上。

## 可选

只下分段、不拼接：

```bash
node download-video-segments.mjs --in meta_<sub_task_id>.jsonl --out ./vids
```

Node 脚本定位为单子任务下载；若输入 `all.zip`，它会警告并仅处理第一份。
多子任务必须使用 Python `pull-oss-videos.py`。

车上直接读本机盘（不经 OSS）：

```bash
python3 export-local-continuous-video.py --task-json task.json --out ./local_vx
```
