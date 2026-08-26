# VNav 数据预览

本地查看 `tmp_data/` 和 `/mnt/chengchangxu/data/visual_nav_mv/` 中的多视角视频、占据图、位姿、速度和最近 30 秒轨迹。

训练数据的读取、时间对齐和过滤约定见 [`readme_training.md`](./readme_training.md)。

## 开发准备

新用户只开发和预览已有数据时，需要准备：

- Node.js 20.19+（推荐使用当前 LTS）和 npm。
- 可供预览的数据目录；可放在项目的 `tmp_data/`，也可通过下文的 `VNAV_DATA_ROOT(S)` 指定。
- 如需使用视频裁剪功能，还需安装 `ffmpeg`。

首次启动：

```bash
git clone https://github.com/ccx1997/vnav_data_preview.git
cd vnav_data_preview
npm install
npm run dev
```

开发命令默认让 Vite 以 `500ms` 轮询检测前端文件变化，避免共享服务器上的 Codex、VS Code
等进程耗尽 inotify 实例后触发 `EMFILE: too many open files, watch`。轮询仅在开发模式启用；
`npm start` 生产服务不监听源码，也没有该额外开销。如确认服务器 inotify 资源充足，可直接执行
`NODE_ENV=development tsx server/index.ts` 使用原生文件事件。

如需从 skdos-live 下载并处理数据，还需要 Python 3、Bash、`unzip`、`ffmpeg`，以及访问
skdos-live 和 OSS 的网络权限。复制环境变量模板，并向管理员申请 API Key：

```bash
cp .env.example .env
# 编辑 .env，填写 SKDOS_API_KEY；不要提交 .env
set -a
source .env
set +a
```

API Key 仅通过 `SKDOS_API_KEY` 环境变量读取。真实 Key 不应写入代码、文档或命令行参数；
需要换环境时可同时修改本机 `.env` 中的 `SKDOS_LIVE_URL`。数据下载与处理命令见
[`processing/README.md`](processing/README.md)。

## 启动

```bash
npm install
npm run dev
```

然后访问 <http://127.0.0.1:5173>。页面会自动扫描平铺的成对目录：

```text
tmp_data/meta_<数据集ID>/
tmp_data/videos_<数据集ID>/
```

也支持批次目录中包含多个子数据集的结构：

```text
tmp_data/meta_<批次ID>/meta_<数据集ID>/
tmp_data/videos_<批次ID>/<数据集ID>/
```

批次目录中的视频元数据可由子数据集内的 `video_segments.json` 提供，视频文件按
`<相机ID>_continuous.mp4` 识别。

默认还会扫描已下载数据的目录结构：

```text
/mnt/chengchangxu/data/visual_nav_mv/<任务ID>/meta/unpacked/meta_<数据集ID>/
/mnt/chengchangxu/data/visual_nav_mv/<任务ID>/videos/videos_<数据集ID>/
```

如需覆盖默认目录，可设置单目录环境变量 `VNAV_DATA_ROOT`，或用
`VNAV_DATA_ROOTS=/目录一:/目录二` 指定多个扫描目录。多个目录出现相同数据集 ID 时，
优先使用排在前面的目录。

## 数据管理

数据集选择框右侧提供两项本地数据管理操作：

- 裁剪按钮：可在“时分秒”和“纯秒”两种时间格式间切换，填写希望保留的起点和终点。
  例如 `00:00:24` 到 `00:02:23` 表示保留这段区间。确认后会重新编码所有相机视频，
  同步过滤 `frames.jsonl`，删除保留区间外的占据图，并更新任务、导出和视频时间元数据。
- 垃圾桶按钮：二次确认后，永久删除当前数据集对应的完整 Meta 和视频目录；批次结构中
  只删除当前子数据集，不影响同批次的其他子数据集。

视频裁剪依赖本机 `ffmpeg`（可通过 `FFMPEG_PATH` 指定可执行文件）。两项操作均不可撤销，
建议只在确认当前选择和保留区间无误后执行。

## 远程低带宽预览

点击播放时，服务端会临时检查 NVIDIA GPU、NVENC 实际编码能力、GPU/编码器利用率、
剩余显存和已预约预览流数量。只有整组六路预览都能放入同一张空闲 GPU 时才启用
`480px / 10fps / 250kbps` 的实时分片 MP4；暂停、播放结束或切换数据集后立即终止 FFmpeg
并释放会话。输出只通过管道发送，不生成或保留代理视频文件。GPU 不支持 NVENC、正在执行
其他任务或资源不足时自动使用原始 MP4，不做 CPU 转码。

默认资源门槛可以通过环境变量调整：

```text
VNAV_PREVIEW_MAX_GPU_UTILIZATION=30
VNAV_PREVIEW_MAX_ENCODER_UTILIZATION=20
VNAV_PREVIEW_MIN_FREE_MEMORY_MIB=1024
VNAV_PREVIEW_MAX_STREAMS_PER_GPU=8
VNAV_PREVIEW_STOP_GPU_UTILIZATION=70
VNAV_PREVIEW_STOP_MIN_FREE_MEMORY_MIB=512
VNAV_PREVIEW_WIDTH=480
VNAV_PREVIEW_FPS=10
VNAV_PREVIEW_BITRATE_KBPS=250
```

GPU 能力不是根据型号或 FFmpeg 编码器列表猜测，而是对候选卡执行一次 64×64 单帧 NVENC
探针。例如 A100 虽然是 NVIDIA GPU且 FFmpeg 可能列出 `h264_nvenc`，实际不含 NVENC，
探针失败后会保持原始码率。多路播放采用整组缓冲屏障；任一路等待数据时暂停全部视角，
小于 500ms 的偏差通过轻微调整播放速率收敛，超过 500ms 才最多每秒硬校正一次。
占据图不再逐张请求 PNG，而是由服务端把播放方向上的 32 张原文件打成一次内存二进制响应，
浏览器解包并提前解码；剩余 12 张缓存时异步补下一批，从而把高延迟 SSH 链路上的 32 次往返
降为 1 次。缓存上限为 64 张或 64MiB，不写入浏览器持久缓存，也不在服务器生成预览文件。
页面只显示视频时间轴当前对应的占据图，不使用旧图掩盖缺帧；若随机跳转或网络抖动导致目标图
尚未就绪，则占据图加入多路播放缓冲屏障，整组视频短暂停住，解码完成后同步恢复。
实测在 Chromium 注入 `1000ms` RTT 后，以约 `5Hz` 连续推进 45 个真实占据图时间点，
`45/45` 显示时间戳与目标帧一致，未出现加载占位或旧帧错配，网络侧仅发送 2 个 32 帧批次。
实时预览期间每 10 秒复查 GPU；若其他任务使核心利用率达到 70% 或剩余显存低于 512MiB，
服务端会终止预览进程，页面自动回退原始视频。

## 验证

```bash
npm test
npm run build
```

生产方式运行时，先执行 `npm run build`，再执行 `npm start`。
