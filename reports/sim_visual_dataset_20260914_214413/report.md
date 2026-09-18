# 仿真视觉数据统计与 9 个可视化 case

统计快照：**2026-09-14 21:44:13（北京时间）**。源目录：`/mnt/wlf/projects/e2evnav/hybrid25d_renderer/outputs/visual_dataset`。

**结论：共 5,416 条，旧版 3,394 条、新版 2,022 条；旧版 97.17% 的轨迹存在同轨迹内障碍资产重复，新版 2,022 条均未发现 ID 或资产名重复。**

数据持续生产，本报告固定索引快照，不代表打开报告时的实时数量。旧版实际索引数比用户给出的约 3,400 少 6 条；未从现有快照推断其原因。新版已超过先前约 900 条。

## 数据规模

| 指标 | 旧版 legacy_v1 | 新版 v2_unique_obstacles | 合计 |
|---|---:|---:|---:|
| 轨迹数 | 3,394 | 2,022 | 5,416 |
| 室内轨迹 | 1,610 | 956 | 2,566 |
| 室外轨迹 | 1,784 | 1,066 | 2,850 |
| 同步时刻 / 每相机帧数 | 576,584 | 347,248 | 923,832 |
| 三相机图像总数 | 1,729,752 | 1,041,744 | 2,771,496 |
| 轨迹总时长（小时） | 79.80 | 48.06 | 127.86 |
| 全局路线总长（km） | 181.40 | 110.16 | 291.56 |
| 障碍物实例数 | 28,894 | 17,259 | 46,153 |
| 两个 NPZ 文件合计（GiB） | 359.51 | 216.84 | 576.35 |

三路相机顺序为 `front / rear_left / rear_right`，RGB 数组形状为 `T×3×512×640×3`、`uint8`，采样率 2 Hz。图像总数是同步时刻数的 3 倍；时长取索引 `duration_s` 之和，保留完整积分轨迹终止时刻，不用 `帧数 / 2` 代替。NPZ 大小不含 JPEG 预览、地图 PNG 和其他文件。

![统计图](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/statistics.png)

## 时长、路线和场景分布

| 版本 / 环境 | 条数 | 时长：中位 / P95（s） | 路线：中位 / P95（m） | 障碍数：均值 / 范围 | 障碍密度（个/10m） |
|---|---:|---:|---:|---:|---:|
| 旧版室内 | 1,610 | 70.2 / 124.5 | 31.3 / 57.0 | 10.12 / 5–20 | 2.97 |
| 旧版室外 | 1,784 | 90.5 / 156.0 | 67.9 / 120.4 | 7.06 / 2–17 | 1.00 |
| 新版室内 | 956 | 67.5 / 124.6 | 30.2 / 56.5 | 9.91 / 5–20 | 2.95 |
| 新版室外 | 1,066 | 93.9 / 167.0 | 70.9 / 126.9 | 7.30 / 2–21 | 1.00 |

- 全库单轨迹时长 13.5–241.2 s，全局路线长 16.18–203.35 m。路线长度不等于实际 rollout 行驶里程。
- 室外靠右轨迹：旧版 865/1,784（48.49%）；新版 560/1,066（52.53%）。
- 两个版本都覆盖 664 种障碍资产；室内 534 种、室外 163 种，二者有重叠，不能直接相加。
- 两个版本材质覆盖一致：室内地面 20、墙体 10、天花板 3；室外地面 17、占据区域 7。
- 室内已成功发布样本的场景生成尝试次数中位数为 6，室外为 1（新旧一致）；这里只统计已发布样本，不能据此推算整个生产任务的成功率。

## 障碍物去重效果

| 指标 | 旧版 | 新版 |
|---|---:|---:|
| 含重复资产的轨迹 | 3,298/3,394（97.17%） | 0/2,022（0%） |
| 含重复 ID 的轨迹 | 3,298 | 0 |
| 每条轨迹独立资产数：均值 | 3.47 | 8.54 |
| 每条轨迹独立资产数：范围 | 1–4 | 2–21 |
| 轨迹内重复放置的额外实例 | 17,106（59.20%） | 0 |

重复按每条 `rollout.npz` 的 `obstacle_ids` 和 `obstacle_kinds` 分别计算；额外实例数定义为每条 `K − unique(K)` 后求和，比例分母是该版本所有障碍实例数。这是 ID / atlas 资产名唯一性，不代表跨轨迹去重或语义类别唯一。新版平均单轨迹资产多样性约为旧版 **2.46 倍**，每轨迹障碍物总数均值基本相近（8.51 → 8.54）。

旧版室内 1,610/1,610 条有重复；旧版室外 1,688/1,784 条有重复，另外 96 条无重复。因此旧版规则是允许重复，不是每条必然重复。

## 9 个 case

前 6 例是新版（室内 3、室外 3），后 3 例是旧版对照。按模式和路线长度、障碍数量、累计航向变化的分位数确定性选样，平局按 sample_id 排序；属于覆盖性抽检，不是随机质量评测。每例从时间轴前、中、后三段各取一帧，优先显示障碍物，相邻关键帧至少相隔总帧数的 12%。

选帧使用已保存前相机位姿、±44° 水平角、0.7–8 m 距离、原始占据图视线检查及障碍高度/距离得分，仅用于挑选预览，不是可见性真值。三相机复用同一帧号。直接读取已有 JPEG；地图从保存的占据数组和路线绘制，未重新仿真或渲染源数据。

![9 个 case 总览](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/overview_9_cases.jpg)

| Case | 内容 | 轨迹 ID | 时长 / 路线 | 障碍实例 / 独立资产 | 三个时刻（s） |
|---|---|---|---:|---:|---|
| [01](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_01.png) | 新版室内：典型路线长度 | `indoor-cf402de9a274` | 50.7s / 30.2m | 8 / 8 | 9.0 / 25.5 / 34.5 |
| [02](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_02.png) | 新版室内：高障碍数量 | `indoor-00ac8d79d643` | 132.0s / 58.5m | 17 / 17 | 30.0 / 79.5 / 101.0 |
| [03](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_03.png) | 新版室内：大累计转向 | `indoor-7679a6baac38` | 132.3s / 56.3m | 16 / 16 | 21.0 / 67.0 / 93.5 |
| [04](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_04.png) | 新版室外：中线行驶 | `outdoor-3ece8904abc1` | 89.4s / 65.4m | 7 / 7 | 14.5 / 42.5 / 79.0 |
| [05](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_05.png) | 新版室外：靠右行驶 | `outdoor-344afbb80c42` | 108.0s / 77.1m | 8 / 8 | 23.5 / 71.5 / 93.5 |
| [06](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_06.png) | 新版室外：长路线 | `outdoor-7bc9db3a2aa6` | 158.4s / 127.1m | 12 / 12 | 52.5 / 78.5 / 121.5 |
| [07](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_07.png) | 旧版室内：重复障碍物对照 | `indoor-0f137463f455` | 108.9s / 50.7m | 16 / 4 | 30.0 / 54.0 / 76.5 |
| [08](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_08.png) | 旧版室外：重复障碍物对照 | `outdoor-045fa7be67d9` | 132.6s / 100.2m | 11 / 4 | 9.0 / 86.5 / 103.5 |
| [09](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_09.png) | 旧版室外：无重复对照 | `outdoor-018f4f7d7a73` | 47.4s / 35.1m | 3 / 3 | 15.0 / 31.5 / 42.0 |

读图：青色虚线为全局路线，橙色为实际 rollout；绿色点为起点，红色星为终点，A/B/C 对应三行相机时刻；地图中相同资产使用相同颜色，数字为该例障碍物序号。室内部分障碍物很小，地图标注可帮助定位。

## 检查范围与限制

- 5,416/5,416 条索引 JSON 解析成功；sample_id、seed、source_scene_id 均未发现重复。
- 所有索引项的 rollout.npz、visual.npz、map.png、map_original.png、publication.json 均存在且非空。
- 全量读取所需 NPZ 元数据和小数组，核对版本、样本 ID、障碍数量、路线长度、时长、位姿有限值及严格 2 Hz 时间轴；两个 NPZ 的时间戳与位姿完全一致。
- 全量核对 RGB 的 NPY 头（帧数、维度、dtype、顺序）和 ZIP 成员声明大小；这些检查没有发现异常。**未对全库 RGB 像素做完整解压、CRC 或逐帧画质检测，也未全量解码地图/JPEG。**
- 9 例共 81 张已有 JPEG 已实际解码，尺寸均为 640×512；18 张派生 PNG 已重新打开验证。
- 5,416 条元数据均记录 `success=true`；这是生成器记录，未在本次重跑碰撞或导航成功条件。
- 本次所有产物写入当前报告目录；源数据只读，不重新下载、解包、处理、裁剪或覆盖任何数据。

## 可复核产物

- [原始索引快照](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/index.snapshot.jsonl)
- [快照时间与 SHA256](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/snapshot.json)
- [完整分组统计、分位数、资产和材质计数](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/statistics.json)
- [逐轨迹 CSV](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/samples.csv)
- [逐轨迹审计及障碍资产明细](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/audit_records.jsonl)
- [9 例选择参数、帧号和源图路径](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/selected_cases.json)
- [检查结果](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/validation.json)
- [只读统计脚本](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/analyze.py)
- [可视化脚本](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/visualize.py)

索引 SHA256：`dbf105c0f541cfb7ae8c62af46a4b4fd2988cd6142bc4733a1a8046dc6eaf56c`。

## 单例大图

### 01 · 新版室内：典型路线长度

![新版室内：典型路线长度](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_01.png)

### 02 · 新版室内：高障碍数量

![新版室内：高障碍数量](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_02.png)

### 03 · 新版室内：大累计转向

![新版室内：大累计转向](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_03.png)

### 04 · 新版室外：中线行驶

![新版室外：中线行驶](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_04.png)

### 05 · 新版室外：靠右行驶

![新版室外：靠右行驶](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_05.png)

### 06 · 新版室外：长路线

![新版室外：长路线](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_06.png)

### 07 · 旧版室内：重复障碍物对照

![旧版室内：重复障碍物对照](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_07.png)

### 08 · 旧版室外：重复障碍物对照

![旧版室外：重复障碍物对照](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_08.png)

### 09 · 旧版室外：无重复对照

![旧版室外：无重复对照](/mnt/chengchangxu/projects/vnav_data_preview/reports/sim_visual_dataset_20260914_214413/case_09.png)
