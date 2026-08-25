# 数据下载与处理

`algo-handoff-tools/download-task.sh` 用于按 task 下载结构化数据、栅格图和 OSS 视频，并按子任务把各相机合并为等长连续 MP4。默认按 keep 窗定长、缺段补黑；硬件时间完整时使用车上采集钟对齐，否则整批回退墙钟。

## 使用方法

依赖：Bash、Python 3、`unzip`、`ffmpeg`，并确保机器可以访问 skdos-live 和 OSS。运行前需配置 `SKDOS_LIVE_URL` 和 `SKDOS_API_KEY`；仓库不保存真实 Key。首次配置见项目根目录 README 的“开发准备”。

在项目根目录运行：

```bash
# 默认下载到 /mnt/chengchangxu/data/visual_nav_mv，保留 ZIP 和原始视频段
./processing/algo-handoff-tools/download-task.sh 20260820180215WDK

# 指定其他输出根目录
./processing/algo-handoff-tools/download-task.sh 20260820180215WDK /path/to/output

# 合并算法更新后，复用已有 segments 重新合并并覆盖旧结果
./processing/algo-handoff-tools/download-task.sh --remerge 20260820180215WDK

# 下载、解压和合并成功后删除 ZIP
./processing/algo-handoff-tools/download-task.sh --delete-zip 20260820180215WDK

# 合并成功后删除原始视频 segments
./processing/algo-handoff-tools/download-task.sh --delete-segments 20260820180215WDK

# 同时删除 ZIP 和原始视频 segments
./processing/algo-handoff-tools/download-task.sh \
  --delete-zip --delete-segments 20260820180215WDK
```

两个删除选项默认均不开启。只有所有子任务都满足下载无缺失、所有相机拼接成功且 manifest 标记为对齐完成时才会清理；任一检查失败都会保留 ZIP 和原始视频段。旧版或 `--copy` 生成的 manifest 不会被当作已对齐完成，重跑任务级命令会按新规则重新处理。

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
    meta_<task_id>_all.zip
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
