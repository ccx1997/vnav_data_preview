# 数据下载与处理

## 四步一键处理

统一入口 [`run-data-pipeline.sh`](run-data-pipeline.sh) 接受任务 ID、ID 列表或起始日期，完成下载、
预处理、ep011200 教师轨迹及像素坐标导出。完整结果复用，已有损坏数据只报告；stdout 返回包含
源目录、标注根目录、教师 run 和像素目录的 JSON，日志/进度写入独立报告目录及 stderr。

```bash
./processing/run-data-pipeline.sh --task-ids 20260920122356WIt 202609201240128Up
./processing/run-data-pipeline.sh --since 2026-09-20 --result-json ./results.json
```

日期边界、输出字段、环境选项和复用规则见 [`data_pipeline/README.md`](data_pipeline/README.md)。

## 单独下载和对齐

`algo-handoff-tools/download-task.sh` 用于按 task 下载结构化数据、栅格图和 OSS 视频，校验 Meta 的
task/subtask 归属并统一解压到 `meta/unpacked/meta_<sub_task_id>/`，再按子任务把实际存在的各相机
合并为等长连续 MP4。默认按 keep 窗定长、缺段补黑；硬件时间完整时使用车上采集钟对齐，否则整批
回退墙钟。相机不固定为六路；偏离推荐相机集合时任务仍可完成，但日志末尾会明确输出 `WARNING`。

## 世界坐标转像素坐标

[`coordinate-converter/`](coordinate-converter/) 是独立第四步：读取已完成教师 run 或世界坐标
JSONL，输出各地图栅格像素及 `P_map` 对应的手绘像素，并生成对齐图像、坐标约定与哈希审计。
输出必须是新目录，复用已有教师结果和采集数据。单点和批量接口、地图参数及命令示例见其 README。

```bash
python3 processing/coordinate-converter/export_pixels.py \
  --run /path/to/existing/run \
  --output /path/to/pixel_exports/new_run
```

新版灰线地图使用 `--handdraw-map source-map`：仅导出 P_map，排除 B10 等无配准地图；
剔除当前/历史落在未绘制道路的样本，路线在断口或
进楼处截断，并独立保存审计。对已有任务批量映射可用 `coordinate-converter/remap_existing.py`，
复用每任务最新成功 full run；参数及 2026-09-24 已完成结果见坐标转换器 README。

## 训练数据制作

[`training-data-builder/`](training-data-builder/) 提供多视角训练样本、稀疏路线、地图裁剪和雷达教师
伪标签的 `pilot/full` 流水线。原始下载数据保持只读，生成物默认写到
`/mnt/chengchangxu/data/visual_nav_training/`。

当前版本是 `vnav_teacher_rule10_history_v2`，通过无状态 `/teacher/infer_batch` 同时提交 batch 内每个
样本的独立占据历史。初态有 20% 稳定随机分支为精确零速，教师占据和学生 RGB 均重复当前单图；
其余样本用当前前 0.5 s 的实际 pose 变化估算线角速度，并使用真实多帧历史。动态 RGB 按 MP4 实际
PTS 索引 1.0 s 窗口，频率不超过 10 Hz。

修正版 12-case pilot 已验证 older/recent 两个教师通道均非空、确定性一致、每次
`forward_passes=1`，逐样本深度校验错误为 0。详细参数和输出契约见
[`training-data-builder/README.md`](training-data-builder/README.md)。

手工生成一个 pilot：

```bash
/mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  processing/training-data-builder/build_training_data.py pilot \
  --revision 1
```

正式输出位于
`/mnt/chengchangxu/data/visual_nav_training/<task_id>/vnav_teacher_rule10_history_v2/full/run_*/`。
源采集根目录 `/mnt/chengchangxu/data/visual_nav_mv/` 始终只读。

### 自动补齐未生成任务

已提供包含教师生命周期的一键入口：

```bash
# 自动扫描、跳过已成功任务并生成所有当前就绪任务
./processing/training-data-builder/build_pending_training_data.sh

# 只查看 pending / already_completed / incomplete 分类
./processing/training-data-builder/build_pending_training_data.sh --dry-run
```

成功完成的判定不是“目录存在”，而是同一 pipeline 下 full `run.json` 成功、已授权、接受样本数大于
0，且 cases 与 source、教师标签、RGB 历史三份核心 Manifest 存在。失败或 0 样本任务会在下次运行重试；未规范解压 Meta、缺子任务
Manifest 或 Manifest 所声明的实际相机视频不齐会标为 `incomplete`，待下载完整后自动进入下一批。
实际相机集合偏离配置中的推荐集合只记 warning，不阻止进入 pending。批次状态位于
`/mnt/chengchangxu/data/visual_nav_training/_batch_runs/run_*/batch_run.json`，并用文件锁避免两个批次
同时运行。

跨地图源子任务按逐帧 `map_name` 的变化时刻切成独立地图段，不跨坐标系构造路线；若逐帧地图缺失，
仅允许 `task.json` 快照地图唯一时回退。Manifest 同时存在 `local_trim` 与旧 `keep_windows` 时，训练
同步优先使用实际 MP4 对应的 `local_trim`。

2026-08-31 Rule-10 v2 正式批次完成 7 个就绪任务，`6902` 个候选中接受 `6813` 个，`89` 个拒绝
全部为碰撞回放；220 次 batch 均为一次 forward。20% 零速重复分支实际为 `1408/6813=20.67%`，
动态 RGB 实际约 7.5 Hz。逐样本深度验证 7/7 run、0 错误、0 警告，19/19 回归通过。4 个 source
不完整任务保持未处理；删除旧 v1 后 dry-run 为 `pending=0 / already_completed=7 / incomplete=4`。
旧数据共删除 16.10 GB，采集源 22,831 个文件的内容聚合 SHA-256 删除前后相同。详细统计见
[`training-data-builder/rule10_history_v2_report_20260831.md`](training-data-builder/rule10_history_v2_report_20260831.md)。

同日按新 Meta/相机规范修复四个历史目录后，限定 dry-run 更新为
`pending=3 / incomplete=1`：`20260819181014HWI` 与 `20260824161657Li2` 已规范解压并完整下载，
五路的 `20260828154654XXz` 以 warning 进入 pending；`202608181754378l1` 已恢复 Meta，但远端任务
返回 404，仍缺视频 Manifest。本次只修复下载源并验证发现/同步，没有启动教师或生成训练样本。

以下 2026-08-27 数字只保留为旧 `vnav_teacher_v1` 的历史审计记录；该版本没有正确动态初态和显式
教师历史，已被 Rule-10 v2 取代，不得继续作为当前训练集。

2026-08-27 首次自动批处理新增完成 3 个任务：

```text
20260818193938Axg  3156 候选 / 3147 接受 /  9 拒绝  4.2 GB
20260819203318qFl  2180 候选 / 2159 接受 / 21 拒绝  2.8 GB
20260821114524n4Q   389 候选 /  384 接受 /  5 拒绝  795 MB
```

共新增 5690 个完整样本，逐样本六路文件、融合二值值域/并集/hash、同步和路线门槛验收错误为 0；
二次扫描为 `pending=0 / already_completed=4 / incomplete=2`，证明成功任务不会重复生成。

随后下载目录新增任务后，从主项目再次运行同一入口，又自动完成：

```text
202608251919001QS  1209 候选 / 1128 接受 / 81 拒绝  1.4 GB
20260826154539RI5    54 候选 /   54 接受 /  0 拒绝   93 MB
```

本轮新增 1182 个样本，逐样本验收错误仍为 0；81 个拒绝均为碰撞回放。全路线清单中的一条
`176.046 deg` 坏路线未被接受样本引用，其 679 行由 `route_simplification_error` 拒绝；实际训练路线
最大横向/切向误差为 `0.098927 m / 3.465017 deg`。截至本轮，自动入口累计完成 5 个新任务、生成
6872 个样本；当前全部 9 个目录应分类为 `pending=0 / already_completed=6 / incomplete=3`。

## 使用方法

依赖：Bash、Python 3、`unzip`、`ffmpeg`，并确保机器可以访问 skdos-live 和 OSS。运行前需配置 `SKDOS_LIVE_URL` 和 `SKDOS_API_KEY`；仓库不保存真实 Key。首次配置见项目根目录 README 的“开发准备”。

在项目根目录运行：

```bash
# 默认下载到 /mnt/chengchangxu/data/visual_nav_mv，保留 ZIP 和原始视频段
./processing/algo-handoff-tools/download-task.sh 20260820180215WDK

# 指定其他输出根目录
./processing/algo-handoff-tools/download-task.sh 20260820180215WDK /path/to/output

# “合并算法”更新后，复用已有 segments 重新合并并覆盖旧结果
./processing/algo-handoff-tools/download-task.sh --remerge 20260820180215WDK

# 下载、解压和合并成功后删除 ZIP
./processing/algo-handoff-tools/download-task.sh --delete-zip 20260820180215WDK

# 合并成功后删除原始视频 segments
./processing/algo-handoff-tools/download-task.sh --delete-segments 20260820180215WDK

# 同时删除 ZIP 和原始视频 segments
./processing/algo-handoff-tools/download-task.sh \
  --delete-zip --delete-segments 20260820180215WDK
```

两个删除选项默认均不开启。只有所有子任务都满足下载无缺失、Manifest 声明的实际相机全部拼接成功
且标记为对齐完成时才会清理；任一检查失败都会保留 ZIP 和原始视频段。相机数不是固定六路。
旧版或 `--copy` 生成的 manifest 不会被当作已对齐完成，重跑任务级命令会按新规则重新处理。

`--remerge` 用于合并算法更新后的重新处理：

- segments 完整时不访问下载接口，直接用最新算法重新合并，并覆盖旧 MP4 和 manifest。
- segments 缺失或不完整时，先刷新任务导出以获得新的 OSS URL，只补下载缺失片段，然后重新合并。
- 可以和 `--delete-zip`、`--delete-segments` 组合；清理仍只在所有子任务重新合并成功后执行。

```bash
# 重新合并，完成后继续保留 segments
./processing/algo-handoff-tools/download-task.sh --remerge 20260820180215WDK

# 重新合并成功后删除 segments
./processing/algo-handoff-tools/download-task.sh \
  --remerge --delete-segments 20260820180215WDK
```

如果任务之前已下载并合并完成，之后再带删除选项运行同一命令，脚本会根据解压内容和各子任务成功的 `manifest.json` 判断任务已完成，随后只执行指定清理，不重复下载或合并。例如：

```bash
# 初次运行时保留全部中间文件
./processing/algo-handoff-tools/download-task.sh 20260820180215WDK

# 后续仅删除该任务的 ZIP 和原始视频段
./processing/algo-handoff-tools/download-task.sh \
  --delete-zip --delete-segments 20260820180215WDK
```

## 输出结构

```text
/mnt/chengchangxu/data/visual_nav_mv/<task_id>/
  meta/
    meta_<task_id>_all.zip 或 meta_<task_id>_<i>.zip
    unpacked/meta_<sub_task_id>/
      frames.jsonl
      grids/*.png
      task.json
      video_segments.json
  videos/
    segs/cam*/                  # OSS 原始视频段
    videos_<sub_task_id>/
      cam*_continuous.mp4
      manifest.json
```

字段、坐标系及单独下载命令见 [`algo-handoff-tools/README.md`](algo-handoff-tools/README.md)。
