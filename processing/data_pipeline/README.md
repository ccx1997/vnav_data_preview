# 四阶段数据流水线

统一执行：下载与视频对齐 → 训练预处理 → Rule-10 教师轨迹 → 世界坐标转像素。
使用现有下载器、训练构建器和坐标转换器，不改变其采样、历史、路线或碰撞规则。

## 运行

从项目根目录运行（脚本本身也支持从其他工作目录调用）：

```bash
# 一个任务
./processing/run-data-pipeline.sh 20260920122356WIt

# 多个任务；顺序稳定去重
./processing/run-data-pipeline.sh --task-ids 20260920122356WIt 202609201240128Up

# 也支持 JSON 数组、逗号分隔，或者重复 --task-id
./processing/run-data-pipeline.sh --task-ids '["20260920122356WIt","202609201240128Up"]'

# 日期零点（包含）至当前时间；无时区时使用 Asia/Shanghai
./processing/run-data-pipeline.sh --since 2026-09-20
./processing/run-data-pipeline.sh 2026-09-20

# 可选结束日期/时间与机器人；仅用于日期模式
./processing/run-data-pipeline.sh --since '2026-09-20 12:00' --until '2026-09-20 18:00' --robot Robot-U2-V1

# 只做预检、日期发现与计划；不会启动教师或写采集/训练数据
./processing/run-data-pipeline.sh --since 2026-09-20 --dry-run

# stdout 只有一个 JSON 对象，进度在 stderr；同时可以另存 JSON
./processing/run-data-pipeline.sh --task-ids 20260920122356WIt 202609201240128Up \
  --result-json ./results.json > ./results_stdout.json
```

日期查询复用 `list-task-ids.py` 的 `/api/annotation-tasks/ids` 完整时间窗接口，不受看板最近
200 条限制。起止边界均包含；只给结束日期时包含当天结束。遇到 `truncated=true` 在下载前
停止，需缩小范围。查询为空时返回成功和空 `tasks`，不会处理整个本地数据目录。
脚本把日期解析为带时区的时间，再以 unix 秒调用接口；结果 JSON 保留 ISO 时间。
这是因为当前接口不接受带时区偏移的 ISO 字符串直接作为 `from/to`。

## 结果 JSON

每次非 dry-run 的报告和日志保存在：

```text
<output-root>/_pipeline_runs/<timestamp>_<unique-id>/
  result.json
  preflight.json
  pipeline_config.json
  <task-id>/
    result.json
    source_before.json
    source_validation.json
    source_protection.json
    teacher_validation.json
    rgb_quality.json
    pixel_validation.json
    samples_without_black_frames.jsonl
    ...各阶段日志、教师启动记录和通知结果
```

成功返回示例（路径均为绝对路径；其他验证/统计字段在此省略）：

```json
{
  "schema_version": "vnav_data_pipeline_v1",
  "status": "success",
  "result_json": "/mnt/chengchangxu/data/visual_nav_training/_pipeline_runs/<batch>/result.json",
  "counts": {"selected": 1, "completed": 1, "failed": 0},
  "tasks": [{
    "task_id": "20260920122356WIt",
    "status": "completed",
    "stage": "done",
    "source_data_dir": "/mnt/chengchangxu/data/visual_nav_mv/20260920122356WIt",
    "annotation_data_dir": "/mnt/chengchangxu/data/visual_nav_training/20260920122356WIt",
    "teacher_data_dir": "/mnt/chengchangxu/data/visual_nav_training/20260920122356WIt/vnav_teacher_rule10_history_v2/full/run_20260920_172826",
    "pixel_data_dir": "/mnt/chengchangxu/data/visual_nav_training/20260920122356WIt/pixel_exports/run_20260920_172826",
    "pixel_manifest": "<pixel_data_dir>/samples.jsonl",
    "teacher_labels": "<teacher_data_dir>/teacher_labels.jsonl",
    "samples_without_black_frames": "<task-report>/samples_without_black_frames.jsonl",
    "reused": {"source": true, "teacher": true, "pixels": true},
    "source_unchanged": true
  }]
}
```

- `source_data_dir` 包含 `meta/unpacked/meta_<subtask>/` 和 `videos/videos_<subtask>/`。
- `annotation_data_dir` 是该任务全部标注产物的根目录；实际本次使用的版本由 `teacher_data_dir`
  和 `pixel_data_dir` 精确定位。
- `teacher_data_dir` 保留世界坐标、教师动作、局部 rollout、当前/历史 RGB 和原 NPZ。
- `pixel_data_dir` 包含 `samples.jsonl`、`coordinate_schema.json`、`pixel_coordinate_audit.json`；
  `P_map` 另含 `handdraw_pmap/aligned.png/.jpg`。XY 为左下像素中心，yaw、命令和时间戳不变。
- `samples_without_black_frames` 是额外的样本路径清单；全黑阈值为所有 RGB 像素通道均 `<=8`。
  教师和像素主数据保留全部 accepted，不自动应用此筛选。未进行模糊、曝光、冻结等全面画质筛选。
- 单任务失败时记录 `status=failed`、`stage`、`error` 和 `report_dir`，随后继续其他任务；返回
  `partial_failure` 或 `failed`。只有 `status=completed` 的条目表示四步全部通过验证。
- 退出码：成功或 dry-run 为 `0`，部分/全部任务失败为 `1`，预检/参数错误为 `2`，中断为 `130`。
  中断时保留此前完成条目；尚未开始的任务为 `not_started`。`counts.failed` 表示未完成任务数。
  参数解析错误遵循 argparse 的 stderr 用法提示；运行时结果/错误输出为 JSON。

## 复用与保护

1. 已有采集目录只读验证，不调用下载器。不完整则失败保留现场，不刷新导出、解包、补下载、
   拼接或覆盖人工 `map_name` / 本地裁剪。新任务只在云端视频 ready、object_key 存在且修订一致后下载。
2. 最新成功 full run 存在时先核对教师权重/配置、源行时间和地图、标注/裁剪修改时间，再运行
   原 `validate_run` 和 RGB 解码检查。失效或损坏的成功 run 不回退旧版本，不自动重打标。
3. 同一 run 的像素目录存在时核对源/输出 SHA-256、地图来源、全部样本身份和坐标往返；损坏则
   报错，不覆盖。像素缺失时仅补充第四步。
4. `_pipeline_state/<task-id>/<run-name>.json` 在验证后保存源文件元信息/JSON 哈希与参数绑定。
   后续调用不匹配时停止。首次接入旧 run 时只能依据其现有 provenance、源行/地图/mtime 和深度
   验证核对；旧流水线未保存的历史源文件哈希无法追溯。状态文件保存的是首次核验时的基线。
5. 对选中任务检查源文件集合、大小、mtime、inode 与 JSON/JSONL SHA-256 前后不变。相同源根目录
   使用文件锁避免本入口并发执行。ZIP 和原始视频分片默认保留。

失败不会循环重试。首次下载失败留下的不完整源目录，下次也会停止并报告；修复/重新下载需要
明确范围并在本脚本外处理。教师生成失败的临时 run 保留供诊断；没有成功 run 时，下次调用可重试
该缺失阶段。成功结果不会因此重生成。

## 环境和选项

- 默认训练 Python：`/mnt/wlf/anaconda3/envs/deepseek_train/bin/python`。可通过
  `VNAV_PIPELINE_PYTHON=/path/to/python` 更改；需要 NumPy、Pillow、PyYAML，以及 `ffmpeg/ffprobe`。
  也可用具备这些依赖的 Python 直接调用 `processing/run_data_pipeline.py`。
- 鉴权从环境变量及项目 `.env` 读取 `SKDOS_API_KEY`、`SKDOS_LIVE_URL`；不写入结果 JSON。
  本地结果复用不请求云端接口。
- 默认教师为 `config_rule10_continuous_ep011200_sim.yaml`，必须有对应权重/地图；缺失时不回退
  ep023000。保持现有 `config.json` 参数、batch=32、20% 零速、历史与碰撞筛选规则。
- 自动启动从教师仓库根目录执行 `python -m model_server ... --batch`，显式禁止构建器自己启动。
  默认选择至少 8 GiB 空闲显存且负载较低的 GPU 和 8103–8119 的空闲端口；已有
  `CUDA_VISIBLE_DEVICES` 时遵守该限制。可传 `--teacher-device cuda:0` 或 `cpu`。
- `--teacher-url http://127.0.0.1:8103 --no-start-teacher` 复用已启动本地服务；核对 batch 能力、
  checkpoint 哈希和 `/proc/<pid>/cmdline` 中实际 YAML。不会停止预先存在的服务，只清理本次自启进程。
- 可设置 `--source-root`、`--output-root`、`--jobs`、`--teacher-config`、`--model-server-root`、
  `--static-map-root`、`--teacher-python`、`--teacher-start-timeout`。
- 默认在技能和 `FEISHU_WEBHOOK` 可用时逐任务发送结果通知；`--no-feishu` 关闭本次通知。
  通知失败记录在 JSON 中，不重试或改写数据处理状态。dry-run 不发送通知。
- `--result-json` 是额外输出路径，必须位于源/训练根目录之外，防止误覆盖数据；即使未指定，
  结果也始终通过 stdout 返回，非 dry-run 另有报告目录内的 `result.json`。

## 验证

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /mnt/wlf/anaconda3/envs/deepseek_train/bin/python \
  -m pytest -q processing/data_pipeline/tests
```
