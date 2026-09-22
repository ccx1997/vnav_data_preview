# 数据采集 Gate 接入说明

`data_collection_gate.py` 提供两个流式采集状态机：

- `TurnGate`：采集弧形转弯和原地转弯，恢复正常直线行驶或稳定停车后结束；
- `StraightGate`：正常直线行驶时按默认 1% 概率启动，采满随机的 6–12 s 后结束。

所有 Gate 都执行硬停止：位姿跳变、连续 3 s 几乎不变，以及默认 `Tmax=45 s`。普通 pose gap 只切断几何证据，不单独结束已有录制窗口。所有结束片段统一要求至少 4 s 才能保留。Gate 给出启停节点与保留判定，不直接操作录制器或写文件。

历史背景：2026-09-18 [召回复核](reports/gate_0917_diagnosis_20260918/recall_audit.md)发现早先 2 秒弧线窗口漏采后，已恢复按距离积累证据，时间上限沿用原来的 15 秒。当时修正之后，2026-09-21 又按持续转向语义完成精度与上下文优化，当前实现见下文；[联合修正记录](reports/gate_0917_diagnosis_20260918/precision_recall_repair.md)记录新旧保存窗口对比和验证范围。

## 统一返回格式

四个控制方法都返回 `GateResult`，即 `tuple[bool, float]`：

```python
TurnGate.start_collection(pose_cache, decision_timestamp_s) -> tuple[bool, float]
TurnGate.end_collection(pose_cache, decision_timestamp_s) -> tuple[bool, float]
StraightGate.start_collection(pose_cache, decision_timestamp_s) -> tuple[bool, float]
StraightGate.end_collection(pose_cache, decision_timestamp_s) -> tuple[bool, float]
```

返回值应解包为：

```python
should_act, action_timestamp_s = gate.start_collection(
    pose_cache,
    decision_timestamp_s,
)
```

- `should_act=True`：在 `action_timestamp_s` 执行本次开始或结束动作；
- `should_act=False`：本次不切换状态，第二个值只是本次检查的时间节点，不应操作录制器；
- `action_timestamp_s` 一定是有限的 `float`；方法即使在较晚时刻才被调用，也可以从缓存和内部状态返回更早的准确动作节点。

不要写成 `if gate.start_collection(...):`。Python 中非空 tuple 即使内容为 `(False, timestamp)` 也是真值，必须先解包再判断 bool。

每次 `end_collection()` 返回 `True` 后，读取 `gate.last_completed_collection`：

- `start_timestamp_s` / `end_timestamp_s`：最终动作窗口，结束时间可能早于本次调用时间；
- `duration_s`：窗口的精确时长；
- `target_confirmed`：转弯最终窗口内是否有确认的转向目标；正常、硬停止和 EOF 均验收，直行 Gate 为 `True`；
- `should_save`：时长 `>=4.0 s` 且 `target_confirmed=True` 才保留，恰好 4 秒满足时长要求。

该属性初始为 `None`，完成后一直保留最近一次结果，包括 `reset()` 和下一次开始；只在本次结束返回 `True` 时消费它。短片段照常结束并清除活动状态，不能通过返回 `False` 或推迟硬停止来凑满 4 秒。外部强制截断不属于 Gate 正常结束：调用方需确认实际窗口内的目标，再用 `CompletedCollection(start_s, end_s, target_confirmed=...).should_save` 判断，随后调用 `reset()`；只传起止时间只能检查时长。

**采集端需要同步接入 `should_save`**：旧的二元启停接口兼容，但旧调用方如果仍然结束就保存，不会自动丢弃短片段或被硬停止截断的无目标片段。应在提交 `collect_windows` 和永久保存/上传之前执行以下分支。本仓库仅包含 Gate 和接入示例，不包含真实采集系统的录制控制器。

## 流式控制器示例

```python
from data_collection_gate import StraightGate, TurnGate

gates = {"turn": TurnGate(), "straight": StraightGate()}
collection_owner = None

# decision_timestamp_s 是媒体时间线上的延迟锚点，不是墙上时钟的现在。
# 沿用 6 s 延迟；证据只使用缓存中实际已有的连续历史和未来。
# 与 pose 同一时间基准的单调媒体进度；pose 断流期间也继续推进。
decision_timestamp_s = latest_media_timestamp_s - 6.0

if collection_owner is None:
    for owner, gate in gates.items():  # 转弯优先
        should_start, action_timestamp_s = gate.start_collection(
            pose_cache, decision_timestamp_s
        )
        if should_start:
            recorder.resume_from_cache(action_timestamp_s)
            collection_owner = owner
            break
else:
    gate = gates[collection_owner]
    should_end, action_timestamp_s = gate.end_collection(
        pose_cache, decision_timestamp_s
    )
    if should_end:
        recorder.pause_after(action_timestamp_s)
        clip = gate.last_completed_collection
        assert clip is not None
        if clip.should_save:
            recorder.commit_window(clip.start_timestamp_s, clip.end_timestamp_s)
        else:
            recorder.discard_current_clip()
        collection_owner = None
```

`recorder` 的方法为集成伪代码：`resume/pause` 操作待定缓存，`commit_window` 才提交保留窗口及输出，`discard_current_clip` 丢弃本次待定片段。请映射到实际录制器，不要让 `pause_after` 提前永久保存。控制器应按时间顺序传入 `decision_timestamp_s`，并为每个录制器长期保留同一组 Gate 实例。录制器被外部中止或重置时，调用对应 Gate 的 `reset()`；不要重新构造对象来代替正常的结束调用。

`latest_media_timestamp_s` 必须与 pose 时间戳同域，不能直接混用系统墙钟。旧示例使用 `pose_cache[-1].timestamp_s - 6`，pose 断流时会停表，导致 `Tmax` 也无法及时推进；采集端需同步改为媒体进度驱动。断流期间保留已有 60 s 历史缓存，不要把“没有新 pose”转换成空缓存；开始检查至少需要两个有效样本。媒体也停止时，用真实媒体末端执行 EOF 排空和收尾。

## 转弯 Gate

### 开始确认与诊断

2026-09-21 改为识别持续转向：局部三点高曲率只保留作诊断，不能单独启动 TurnGate。`0902-auto2` 19/20 的厘米级摆动由此排除。算法先展开 yaw、做中值去噪，再用 2° 回转死区划分方向波段；不会累加逐帧绝对转角，也不会让 S 弯的左右转向在首尾相减时抵消。

| 分支 | 默认确认条件 |
| --- | --- |
| 常规曲线 | 同一方向波段累计 yaw ≥8°，路径 ≥0.15 m；距离加权稳健拟合的入口/出口路线方向变化 ≥3°，且与 yaw 同向 |
| 短慢弯 | yaw ≥4°，路径 0.08–0.80 m；5%–95% 转角的形成时间 ≥1.5 s、净角度/绝对变化 ≥0.85、XY 拟合一致且残差足够小；还须看到方向反转闭合，或波段极值之后至少 0.5 s 航向稳定 |
| 原地转向 | 保留未来 2 s 最大平移 ≤0.10 m、yaw 变化 ≥8° 的判据 |

短慢弯几何门槛为 `max(2°, 0.4×yaw)` 至 `2×yaw+3°`，入口/出口拟合的 80% 分位垂直残差不超过 `max(3 mm, 路程×1%)`。不足以确认的小角度转向返回 `reason="uncertain_turn"`、`sufficient_data=False`，不自动进入正式转弯样本；未闭合的小角度前缀不能被当成完整短弯。约 0.8 s 的局部纠偏不能靠后续停车凑满持续时间。

每次证据的 yaw、路径、方向拟合、持续性和**平滑输入**都来自同一连续、最多 15 s 的实际观测窗；不跨 pose gap、跳变或稳定停车借证据。XY 方向用实际路线独立展开，支持大于 180° 的弯道；没有 yaw 转动的前进/倒退不会成为掉头。yaw 与 XY 来自同一定位源，不能视为两套独立传感器的验证。

`start_collection()` 保留未来 1 s 的锚点探测，并在需要时复查一个有界历史锚点，使 6 s 调度延迟仍可积累更慢转向。历史复查同样受 15 s、连续区间及已完成窗口约束。`evaluate()` 本身无状态，只诊断指定锚点，不承担这些控制器历史复查。

`TurnEvidence` 提供以下可审计字段：

- `core_start_timestamp_s` / `core_end_timestamp_s`：触发确认的方向波段，非整场景的人工精确边界；
- `observation_end_timestamp_s`：包含闭合确认及平滑支持的观测终点；
- `yaw_excursion_rad` / `route_heading_change_rad`：该波段的 yaw 与 XY 方向变化；
- `reason`：`curve_turn`、`short_slow_turn`、`spin_turn`、`uncertain_turn`、`straight` 或证据不足原因；
- 原有 `reference_curvature_rad_per_m` / `future_path_m`：最多 1 m、至少 0.30 m 路线的旧曲率诊断，不再单独决定 TurnGate。

### 场景上下文、结束和续段

默认 `pre_context_s=4`：保存起点回溯到核心之前 4 s，受当前连续缓存起点限制。返回的动作时间可以早于、但不能晚于本次 `decision_timestamp_s`，录制器必须使用返回值。如果预看命中了尚晚于 decision 的新连续段，先不启动，等 decision 到达该段；不会把起点强行放进缺失 pose 区间。`active_start_timestamp_s` 是保存起点；`core_start_timestamp_s` 是动作核心起点。停车硬停止从核心起点之后检查，避免新增的停车接近画面把真正转弯截掉。

自然结束需先越过启动证据观测终点，再观察到连续 2 s 正常直行（最大平移 ≥0.10 m、yaw 极差 ≤3°、净位移/路径 ≥0.95、稳健直线拟合的 80% 分位残差 ≤3 cm），并持续 `max(straight_recovery_persistence_s, post_context_s)`。默认后者为 4 s，即**确认恢复直行后再保留 4 s**，通常带来约 5–6 s 的驶离画面，并非从人工精确弯道终点机械切 4 s。另看最近 4 s 的同向趋势：转角 ≥2°、同向一致性 ≥0.85 且主要转角持续 ≥1.5 s 时不视为恢复，防止持续宽弧尾部漏采。重新出现转向或恢复证据不足会清除恢复候选。

以下硬边界仍优先：

- 位姿跳变，动作节点为断点前的最后 pose；正常 yaw 跨 ±π 不算跳变。跨 gap 端点位置变化 >2 m 或 yaw 变化 >45° 仍保守截断，因为连续性无法确认；较长空洞中的正常运动也可能超过这些阈值，不能断言一定发生了定位跳变；
- 核心开始后，连续 3 s 最大位置漂移 ≤3 cm、最大 yaw 漂移 ≤3°；因此停车尾部可以不足 4 s；
- 自保存起点起最多 45 s，上下文计入此上限；
- 外部媒体/文件结束，调用下述 `finish_collection()`。

2026-09-22 修正普通 gap 的处理：`maximum_pose_gap_s=0.5` 仍严格切开几何证据，不跨洞平滑、插值或累计转角；但 gap 本身不再触发录制硬停止。已有转弯窗口可以包含定位未知的媒体上下文，恢复直行与停车必须重新取得连续证据，空洞时间不能算作直行或停车。训练侧仍按原有 pose 有效性/断段规则过滤，跨洞保存视频不代表缺失区间有可用轨迹标签。

Tmax 切开仍在持续的弯道时，Gate 会携带跨过切点、已完整确认的有界核心证据继续下一段，默认向前重叠 4 s；剩余短尾不必重新独立转够 8°。没有跨切点的实际核心则不生成纯上下文续段。正常结束后抑制已完成窗口的重复触发；外部 `reset()` 清空活动、去重及续段状态。

所有正常结束、硬停止与 EOF 都执行最终目标验收。完整证据及其平滑支持已经落在最终保存窗内时可复用确认；提前截断则只用实际窗口重新确认，不能退回“任意三个点曲率高就保存”。已确认长事件的续段可复用跨切点的核心证书。`>=4 s` 只决定最短保存时长，不能替代目标确认。

```python
# EOF：先按原有时间顺序排空最后 6 s 尚未检查的真实节点，
# 每次完成结果都立即消费，允许发生多段；不能补造未来 pose。
# 再关闭仍然活动的最后一段：
should_end, end_s = turn_gate.finish_collection(pose_cache, actual_media_end_s)
if should_end:
    completed = turn_gate.last_completed_collection
    # 根据 completed.should_save 保存或丢弃，随后停止本任务的调度。
```

`finish_collection()` 只关闭活动段，**不会替调用方扫描最后 6 s 的新事件**；重复调用是幂等的。实际媒体终点应同时满足目标相机的覆盖范围。`last_completed_collection` 的消费方式及二元返回 API 均保持不变。

### 参数与限制

上述新增参数均可通过 `TurnGateConfig` 覆盖。其余默认值仍为：中值平滑 1 s、探测步长 0.2 s、最小路线点距 2 cm、pose 最大间隔 0.5 s、绝对跳变 2 m / 45°、表观线速度上限 3 m/s、角速度上限 2 rad/s。`pre_context_s=0, post_context_s=0` 可关闭额外上下文；`straight_recovery_persistence_s` 仍默认 1 s。

慢速原地转动未扩大到 15 s 累积：低角速度与当前 3 s/3° 停车容差存在冲突，暂为能力边界。极浅弯、定位误差及小幅绕行仍可能处于不确定区间，不能把当前几何抽查结果当作完整线上 precision/recall。

[本次独立盲评、历史回放及验证报告](reports/gate_method_revision_20260921/report.md)。只复用已下载数据；已有裁剪片段缺失的接近/驶离画面无法补回。

## 正常直线概率 Gate

`StraightGate` 只把以下情况当作合格开始机会：未来证据充分且非转弯（uncertain 不合格）、有效路线曲率绝对值小于配置的转弯阈值（默认 8 deg/m）、未来 2 s 最大平移 `>=0.10 m`、yaw 变化 `<=3 deg`。不能仅凭“未触发转弯”认定直线；因平移门槛被拒绝的弯曲/抖动路线也不参与随机抽样。

每个合格调用以 `start_probability=0.01` 做一次伯努利抽样。1% 指“每次合格开始机会的命中概率”，不是最终采集时长占比；调用方必须固定并记录机会节拍，例如 1 Hz。改变调用频率会改变实际启动率。

开始命中后，从闭区间 `[6,12] s` 的连续均匀分布只抽一次时长并锁定截止节点。转弯本身不提前结束直线片段，但位姿跳变或连续 3 s 稳定停车属于全局硬停止，会提前结束；普通 pose gap 不单独结束录制。否则达到随机截止时间时结束。`StraightGateConfig.maximum_collection_interval_s` 同样默认为 45 s；默认随机上限只有 12 s，因此通常不会触发。如果自定义随机上限超过 `Tmax`，实际抽样上限取二者较小值。

```python
import random

from data_collection_gate import StraightGate, StraightGateConfig

straight_gate = StraightGate(
    StraightGateConfig(
        start_probability=0.01,
        minimum_collection_duration_s=6.0,
        maximum_collection_duration_s=12.0,
        maximum_collection_interval_s=45.0,
    ),
    rng=random.Random(20260908),  # 仅回放/测试固定 seed
)
```

可通过 `active_collection` 查看当前 `StraightCollection(start_timestamp_s, target_duration_s, end_timestamp_s)`，`active_start_timestamp_s` 为通用硬停止装饰器提供相同的开始节点接口。硬停止后，`last_collection` 记录实际结束节点和实际时长；`last_evidence` 保存最近一次开始尝试的运动证据。

## 输入与延迟契约

缓存元素支持：

- `PoseSample(timestamp_s, x_m, y_m, yaw_rad)`；
- 四元组 `(timestamp_s, x_m, y_m, yaw_rad)`；
- mapping：时间字段为 `timestamp_s`、`timestamp` 或 `ts`，位姿字段可在本层或 `pose` 层，角度必须显式命名为 `yaw_rad` 或 `yaw_deg`。

当前数据导出的 `pose.yaw` 是度，不能直接作为 rad 传入：

```python
sample = PoseSample(
    timestamp_s=row["ts"],
    x_m=row["pose"]["x"],
    y_m=row["pose"]["y"],
    yaw_rad=math.radians(row["pose"]["yaw"]),
)
```

所有数值必须有限，时间戳必须严格递增；schema 错误或重复/逆序时间戳会抛出 `ValueError`，调用方应报警。

默认沿用 **6 s 调度延迟、60 s pose 缓存**。无需等满 15 s 才能判断：常规转弯可由当前缓存确认；较慢转向还会使用有界历史复查，最终使用的单次原始证据窗口仍不超过 15 s。启动返回的保存起点可能比历史决策节点更早，不能假设就是 `decision_timestamp_s`。

`start_collection()` 返回 True 时保证 `action_timestamp_s <= decision_timestamp_s`。活动窗口的 `end_collection()` 收到早于保存起点的时刻，返回 `(False, 原时刻)`，不抛异常、不改变活动状态或扫描进度；`TurnGate.finish_collection()` 对早期边界同样无操作。有限性、schema 和实际解析缓存时的时间戳校验仍保留。调用方继续使用返回的启停动作时间，不应自行替换为当前时刻。

媒体缓存也必须覆盖确认延迟、回看及前置上下文：默认最坏回填可超过 20 s，建议和 pose 一样预留 60 s。**60 s pose 缓存不能证明视频缓存存在**。活动片段须锁定媒体或写入临时文件，不能采到 45 s 时把开头覆盖。每个相机的真实覆盖不足时应报告实际可用上下文，禁止外推/拼接缺帧。

示例控制器不包含真实录制器。若 StraightGate 活动期间完全停用转弯检测，仍可能漏掉直行随机片段期间开始的转向；生产控制器需持续检测并统一处理重叠窗口。本仓库本次没有部署或修改远端录制器，也没有重导出历史片段。

## 失败安全

- yaw 计算差值前会跨 `+pi/-pi` 展开，避免 179° 到 -179° 被误判为 358°；
- 位姿 gap 切断几何连续段；普通时间空洞保留活动录制窗，端点变化越界仍在断点之前封口；
- 开始证据不足时不开始；结束证据不足时不触发运动结束条件，但到达 `Tmax` 仍会硬截止；
- Gate 只依赖位姿，不能发现“画面在动但定位冻结”，采集系统仍需跨模态冻结报警。

## 验证与视频回放

2026-09-22：67 个单测、68 组 6 s 延迟/1–8 s 空洞回放、54 次既有几何回归通过；159 个已有片段仍保留 32/32 明确转弯、排除 69/69 明确近直线，既有盲标 29/29 一致。拟保存窗由 87 减至 84，640 个源文件指纹未变。见[延迟契约与碎片化修复报告](reports/gate_delayed_gap_20260922/report.md)。

2026-09-21 的 60 项测试未覆盖新段起点晚于 decision，也曾把普通 gap 封口作为预期；该语义现已按录制与证据分离修正。[当日报告](reports/gate_method_revision_20260921/report.md)作为历史记录保留。旧启停精确时间用显式 `pre_context_s=0, post_context_s=0` 检查；默认扩展上下文另有独立测试。

运行：

```bash
python3 -m unittest -v test_data_collection_gate.py
python3 -m py_compile data_collection_gate.py test_data_collection_gate.py
```

2026-09-18 历史版本的 42 个定向测试通过，包含低速/停走弯道、有限时长的 2 Hz 错相位圆弧、曲率边界、停车与跳变硬停止、4 秒精确保留边界，以及提前硬停止后的短真弧/原地转弯保留。17 个新旧默认 Gate 对比场景，双方应用相同 4 秒门槛，当前版额外实际消费 `should_save`；真实转弯覆盖时间均未下降。6 个误采验证场景中，无转弯的保存片段由旧版的 4 个降至 0 个，所有真实转弯区间完整保留。以上为定向合成验证，不是生产整体 precision/recall 估计。Python 编译与 diff 空白检查通过，复现命令和专家审阅见[联合修正记录](reports/gate_0917_diagnosis_20260918/precision_recall_repair.md)。

以下性能数字和真实回放为 2026-09-13 旧版本历史结果，不代表当前版本：60 s、20 Hz、1201 个合成 pose 上按 5 Hz 顺序执行 190 次结束检查，Turn 平均/95 分位/最大耗时为 `20.0/29.3/37.9 ms`，Straight 为 `10.6/12.9/19.6 ms`，均低于 5 Hz 的 200 ms 调用周期。该结果只代表当时环境和负载。

真实例子使用 `20260820180215WDK_1/cam0_continuous.mp4`。视频长 119.924 s，371 个有效 pose；历史 pose 约 3 Hz 且有 0.5–2.0 s 间隔，因此仅本次离线回放将 `maximum_pose_gap_s` 设为 2.0 s，实时高频缓存仍使用默认 0.5 s。其他设置为 60 s 缓存、6 s 判定延迟、转弯检查 0.2 s、直线机会 1 Hz、转弯优先、直线 seed 42。

| owner | 开始节点 | 结束节点 | 结果说明 |
| --- | ---: | ---: | --- |
| straight | 43.000 s | 53.835 s | 第 20 个合格机会命中；锁定时长 10.835 s |
| turn | 82.200 s | 94.600 s | 83.200 s 首次检测为右弧线；继续采到恢复直线后再结束 |
| turn | 102.200 s | 113.000 s | 后续左转保持为同一片段；连续稳定停车 3 s 后结束 |

旧的非状态式结束逻辑会把右转截成 82.200–85.400 s；新逻辑返回 82.200–94.600 s。旧逻辑还会把后段左转拆成 102.200–105.200 s 和 107.800–110.800 s；新逻辑保持为 102.200–113.000 s 的同一片段，在稳定停车后结束。本例所有片段都短于 45 s，因此 `Tmax` 没有介入。以上都是算法对历史 pose 的回放结果，没有人工修订边界。
