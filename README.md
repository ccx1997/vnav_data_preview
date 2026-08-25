# VNav 数据预览

本地查看 `tmp_data/` 和 `/mnt/chengchangxu/data/visual_nav_mv/` 中的多视角视频、占据图、位姿、速度和最近 30 秒轨迹。

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

## 验证

```bash
npm test
npm run build
```

生产方式运行时，先执行 `npm run build`，再执行 `npm start`。
