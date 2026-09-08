# VNav Rule-10 历史训练数据制作

本目录把只读采集数据中的占据图、实际相机视频真实 PTS、独立 pose 路线和 Rule-10 教师输出组织为
可追溯训练样本。当前流水线版本为 `vnav_teacher_rule10_history_v2`，默认输出到
`/mnt/chengchangxu/data/visual_nav_training/`；源数据
`/mnt/chengchangxu/data/visual_nav_mv/` 不会被修改。

## 依赖和教师服务

- 数据处理：`/mnt/wlf/anaconda3/envs/deepseek_train/bin/python`
- 教师客户端/服务：`/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server`
- 教师运行时：`/mnt/zrh/miniconda3/envs/navrl_try/bin/python`
- 配置：`model_server/config_rule10_continuous_ep023000_sim.yaml`
- `ffprobe`、OpenCV、NumPy、Pillow、Matplotlib

手工启动服务：

```bash
cd /mnt/chengchangxu/projects/navi_sys_odo
PYTHONPATH=dev/model_server \
/mnt/zrh/miniconda3/envs/navrl_try/bin/python -m model_server \
  --config dev/model_server/model_server/config_rule10_continuous_ep023000_sim.yaml \
  --host 127.0.0.1 \
  --port 8103 \
  --device cuda:0
```

批处理在 8103 空闲时会自动启动并只关闭自己拥有的服务；若端口已有同模型的健康服务，则复用但
不关闭。`--no-start-teacher` 可要求必须复用已启动服务。

## 一键生成所有就绪任务

```bash
./processing/training-data-builder/build_pending_training_data.sh

# 只发现，不启动教师或写训练样本
./processing/training-data-builder/build_pending_training_data.sh --dry-run

# 限定一个任务
./processing/training-data-builder/build_pending_training_data.sh \
  --task-id 20260827180935lmd
```

入口扫描所有采集任务，仅处理当前 `pipeline_version` 尚无成功 full run 的任务。完成标志同时要求
`run.json` 为授权成功的 full、接受样本数大于 0，并且 cases、source/标签/RGB 历史 Manifest 都
存在。残留 `.in_progress`、失败和 0 样本 run 会在下一次重试。

只有以下源数据完整性条件全部满足的任务才会进入 pending：Meta 已按
`meta/unpacked/meta_<sub_task_id>/` 规范解压，每个源子任务都有成功视频 Manifest，且 Manifest 声明的
实际相机连续 MP4 数量一致、文件非空。相机不固定为六路；配置中的 `camera_ids` 是推荐集合与展示
顺序，实际集合不同时保留该子任务并在 batch/run/验证报告中写入 `video_camera_set_nonstandard`
warning。真实下载或拼接缺失仍标为 incomplete，不用复制、黑图或旧帧伪造缺失相机。
批次状态持续写入
`/mnt/chengchangxu/data/visual_nav_training/_batch_runs/run_*/batch_run.json`，文件锁阻止并发重复生成；
单任务失败不会回滚此前完成项，也不会阻止后续任务。

## 当前初态与历史约定

- 初态分支由数据集、时间戳、采样器版本和教师版本组成的稳定种子确定。20% 为
  `static_repeat`：`v=w=0`，教师占据历史重复当前融合图和当前 pose，RGB 历史重复当前单图。
- 其余为 `dynamic_history`：在当前前 `0.5 s` 的实际 pose 序列上，对逐段车体前向速度、角速度和
  横向速度取鲁棒中位数；教师输入的 `v/w` 裁剪到模型 profile 范围，raw/clipped 值和裁剪标志都
  写入样本。
- 教师历史只取同一地图和连续路线段内、严格早于当前帧的最多 32 帧，保留窗为 1.5 s；要求在
  `1.0 s / 0.5 s` 目标附近各存在一帧（容差 0.25 s），而且最老帧必须真正覆盖到 1.0 s 之前，
  防止教师 older 通道填零。当前帧和每个历史帧都按各自 pose 执行
  `local_static_obstacle_union_history_v2`。
- RGB 动态历史覆盖教师网络实际消费的 1.0 s 窗口，按 MP4 真实 PTS 选择，相邻帧至少间隔 0.1 s，
  即不超过 10 Hz；Manifest 索引图片名、PTS、frame index 和相对时间。零速分支保存 11 个名义
  时间点，但都索引同一当前 RGB 文件。
- 每 32 个样本合并为一次 `/teacher/infer_batch`，正常响应必须是一次 forward。batch 内每个样本有
  独立显式历史，不使用或污染在线 `/infer` 的缓存、执行历史和 pending spin。

## 输出结构

```text
/mnt/chengchangxu/data/visual_nav_training/<task_id>/
  vnav_teacher_rule10_history_v2/full/run_<timestamp>/
    run.json
    source_manifest.jsonl
    routes.jsonl
    teacher_labels.jsonl
    rgb_history_manifest.jsonl
    rgb_frames/<source_sub_task_id>/<camera>/<frame_index>.png
    cases/sample_0000001/
      cam*.png                  # 仅实际相机集合
      grid.png
      fused_grid.png
      inputs.npz
      sample.json
```

`rgb_frames/` 按视频帧去重保存历史图；case 下各实际相机当前图优先使用同 run 内硬链接，避免重复占用数据
块。`inputs.npz` 的权威数组包括原始/融合局部占据、静态地图与中心裁剪、forward route、rollout，
以及 `teacher_history_occupancy / teacher_history_pose / teacher_history_stamp_s`。`sample.json` 保存实际
pose 估速、初态分支、完整教师命令/diagnostics、batch provenance、历史 hash 与所有处理版本。

## Pilot、Full 和验证

```bash
# 固定 12-case 审阅；不会覆盖同 revision
/mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  processing/training-data-builder/build_training_data.py pilot \
  --revision 1

# Full 安全门
/mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  processing/training-data-builder/build_training_data.py full \
  --confirm-full SATISFIED

# 校验每个任务最新成功的 v2 full run，并输出全局特征统计
/mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  processing/training-data-builder/validate_training_data.py
```

验证器逐样本读取 JSON 和 NPZ，重算静态障碍并集，检查二值值域、历史时间戳/形状、零速重复、动态
pose 估速一致性、RGB 文件与 PTS 频率、教师 forward 次数、命令有限性和碰撞回放，同时统计地图、
子任务、动作、速度/角速度/曲率、历史窗口、RGB 窗口、障碍格、同步误差及所有过滤原因。

2026-08-31 可变相机修复验证：45/45 下载与训练 Python 回归通过；真实五路任务
`20260828154654XXz` 成功构建 8 个 context、5,022 行 audit 和 1,416 个候选，未启动教师、未写训练
产物。四个历史目录 dry-run 为 `pending=3 / incomplete=1`，五路任务的八个子任务均保留明确
`video_camera_set_nonstandard` warning。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
/mnt/wlf/anaconda3/envs/deepseek_train/bin/python -m pytest -q \
  processing/training-data-builder/tests
```

当前 21 项测试覆盖实际 pose 估速、强制零速/动态分支、RGB 真实 PTS 限频、Rule-10 完整 1.0 s 历史和有界缓存，
以及原有跨地图、路线、视频裁剪、任务完成标志与 pending 发现规则。

## 2026-08-31 正式结果

当前 9 个任务有成功 full run，8,411 个候选生成 8,280 个 accepted case，131 个拒绝全部为碰撞回放；
268 次 batch 全部一次 forward。零速重复分支 1,686 个（20.36%），动态历史 6,594 个；动态 RGB
实际最高约 7.5 Hz。逐样本验证 9/9 run、0 错误；唯一 warning 是 `20260828154654XXz` 使用声明完整
的五路 `cam0/cam1/cam2/cam3/cam6`，缺少推荐 `cam5`。`20260819181014HWI` 虽布局完整但 862 行
经质量过滤后 0 candidate；`202608181754378l1` 仍缺视频 Manifest/MP4。完整统计、过滤分布、采集内容
保护和旧数据删除记录见 [`rule10_history_v2_report_20260831.md`](rule10_history_v2_report_20260831.md)。
