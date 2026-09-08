# 数据采集 Gate 接入说明

`data_collection_gate.py` 提供两个流式采集状态机：

- `TurnGate`：采集弧形转弯和原地转弯，恢复正常直线行驶或稳定停车后结束；
- `StraightGate`：正常直线行驶时按默认 1% 概率启动，采满随机的 6–12 s 后结束。

每次采集还有默认 `Tmax=45 s` 的硬截止。Gate 只给出录制器应该采取的动作及其准确时间节点，不直接启停录制、删除缓存或写文件。

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

## 流式控制器示例

```python
from data_collection_gate import StraightGate, TurnGate

turn_gate = TurnGate()
straight_gate = StraightGate()
collection_owner = None

# decision_timestamp_s 是滚动缓存内的历史锚点，不是墙上时钟的现在。
# 默认参数在 0.2 m/s 最低可靠识别速度下建议保留 6 s 判定延迟。
decision_timestamp_s = pose_cache[-1].timestamp_s - 6.0

if collection_owner is None:
    should_start, action_timestamp_s = turn_gate.start_collection(
        pose_cache, decision_timestamp_s
    )
    if should_start:
        recorder.resume_from_cache(action_timestamp_s)
        collection_owner = "turn"
    else:
        should_start, action_timestamp_s = straight_gate.start_collection(
            pose_cache, decision_timestamp_s
        )
        if should_start:
            recorder.resume_from_cache(action_timestamp_s)
            collection_owner = "straight"
elif collection_owner == "turn":
    should_end, action_timestamp_s = turn_gate.end_collection(
        pose_cache, decision_timestamp_s
    )
    if should_end:
        recorder.pause_after(action_timestamp_s)
        collection_owner = None
else:
    should_end, action_timestamp_s = straight_gate.end_collection(
        pose_cache, decision_timestamp_s
    )
    if should_end:
        recorder.pause_after(action_timestamp_s)
        collection_owner = None
```

控制器应按时间顺序传入 `decision_timestamp_s`，并为每个录制器长期保留同一组 Gate 实例。录制器被外部中止或重置时，调用对应 Gate 的 `reset()`；不要重新构造对象来代替正常的结束调用。

## 转弯 Gate

### 开始判别

本实现沿用训练数据后处理的 `is_turn_or_spin` 口径：

| 目标 | 在线判别 |
| --- | --- |
| 弧形转弯 | 锚点后的实际 XY 路线按 0.05 m 重采样；未来 1 m 相邻路线段局部曲率的中位数满足 `abs(curvature) >= 0.13962634 rad/m`，即 `8 deg/m`，包含边界 |
| 原地转弯 | 未来 2 s 最大平移 `<=0.10 m`，同时 `abs(yaw change) >=8 deg` |

离线后处理用教师动作 `SPIN_LEFT/SPIN_RIGHT` 判断原地转弯；采集侧没有教师标签，因此“低平移 + yaw 变化”是显式的在线代理。正曲率/yaw 为左转，负值为右转。

`start_collection()` 默认向后预看 1 s。如果这 1 s 内出现阳性转弯证据，返回 `(True, decision_timestamp_s)`，使片段包含首个阳性证据前的接近过程，同时记录活动状态。活动期间重复调用开始方法不会再次启动。

### 结束判别

结束不再以“某个锚点已经不是转弯”作为条件。转弯开始后，`end_collection()` 按流式历史依次检查：

1. **恢复直线**：动作节点之前连续 2 s 是正常直线运动（最大平移 `>=0.10 m`、yaw 变化 `<=3 deg`、路线曲率绝对值 `<8 deg/m`），并让该证据再持续 1 s；或
2. **稳定停车**：连续 3 s 内相对窗口起点的最大位置漂移 `<=0.03 m`，最大 yaw 漂移 `<=3 deg`；或
3. **硬截止**：从成功开始节点起达到 `Tmax=45 s`。

三个条件取最早出现的动作节点并返回 `(True, action_timestamp_s)`。新的转弯、自转、短暂停车、pose gap、pose jump 或证据不足都会清除“正在恢复直线”的候选状态；连续停车满 3 s 则通过独立的稳定停车分支结束。即使缓存已经不包含 `Tmax` 附近的位姿，硬截止仍返回准确的 `start + Tmax` 节点。结束定义是因果的，只用动作节点及其历史；未来缓存主要用于开始判别。

`Tmax` 由通用方法装饰器实现，不写入具体运动判断：

```python
from data_collection_gate import with_maximum_collection_interval

@with_maximum_collection_interval
def end_collection(self, pose_cache, decision_timestamp_s):
    # 只实现当前采集目标自己的自然结束条件。
    ...
```

使用该装饰器的 Gate 需要提供 `active_start_timestamp_s`、`config.maximum_collection_interval_s` 和 `reset()`。装饰器会先把自然结束逻辑检查到截止节点；如果历史中存在更早的恢复直线或稳定停车节点，仍返回较早节点，否则统一在 `start + Tmax` 结束。`functools.wraps` 保留原方法名称、docstring 和 `__wrapped__`。

`TurnGate` 是有状态对象，可用以下属性审计：

- `active_start_timestamp_s`：当前转弯片段的开始节点；
- `straight_recovery_start_timestamp_s`：当前持续直线恢复候选的起点；
- `evaluate(...) -> TurnEvidence`：单个时间节点的无副作用诊断结果。

### 默认参数

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `curve_lookahead_m` | 1.0 m | 弧线未来路线长度，与后处理一致 |
| `curvature_threshold_rad_per_m` | 0.13962634 rad/m | `8 deg/m`，包含边界 |
| `curve_minimum_path_m` | 0.30 m | 路径短于此值时证据不足 |
| `curve_resample_m` | 0.05 m | 路线重采样间距 |
| `curve_maximum_lookahead_s` | 15 s | 限制未来路线搜索时间 |
| `spin_measurement_window_s` | 2.0 s | 原地转弯观测窗 |
| `spin_maximum_translation_m` | 0.10 m | 原地转弯最大平移 |
| `spin_minimum_yaw_change_rad` | 8 deg | 原地转弯最小 yaw 变化 |
| `start_lookahead_s` | 1.0 s | 首个阳性证据前保留上下文 |
| `straight_recovery_window_s` | 2.0 s | 正常直线历史判别窗 |
| `straight_recovery_persistence_s` | 1.0 s | 正常直线判别成立后的持续确认时间 |
| `straight_recovery_minimum_translation_m` | 0.10 m | 恢复窗的最低平移，排除静止 |
| `straight_recovery_maximum_yaw_change_rad` | 3 deg | 恢复窗允许的最大 yaw 变化 |
| `stable_stop_window_s` | 3.0 s | 稳定停车需要连续满足的时间 |
| `stable_stop_maximum_translation_m` | 0.03 m | 停车窗允许的最大位置漂移 |
| `stable_stop_maximum_yaw_change_rad` | 3 deg | 停车窗允许的最大 yaw 漂移 |
| `maximum_collection_interval_s` | 45.0 s | 每次采集的硬截止 `Tmax` |
| `probe_interval_s` | 0.20 s | 补查两个流式调用之间历史节点的间隔 |
| `pose_smoothing_window_s` | 1.0 s | XY 中值平滑；yaw 展开后中值平滑 |
| `maximum_pose_gap_s` | 0.50 s | 超过则不跨 gap 判定 |
| `maximum_pose_jump_m` | 2.0 m | 位置跳变阈值 |
| `maximum_linear_speed_mps` | 3.0 m/s | 表观线速度阈值 |
| `maximum_yaw_step_rad` | 45 deg | yaw 单步跳变阈值 |
| `maximum_yaw_rate_rps` | 2.0 rad/s | yaw rate 阈值 |

参数通过 `TurnGateConfig(...)` 覆盖。

## 正常直线概率 Gate

`StraightGate` 只把以下情况当作合格开始机会：未来证据充分且非转弯、有有效路线曲率、未来 2 s 最大平移 `>=0.10 m`、yaw 变化 `<=3 deg`。静止、转弯、缓存不足和异常位姿不参与随机抽样。

每个合格调用以 `start_probability=0.01` 做一次伯努利抽样。1% 指“每次合格开始机会的命中概率”，不是最终采集时长占比；调用方必须固定并记录机会节拍，例如 1 Hz。改变调用频率会改变实际启动率。

开始命中后，从闭区间 `[6,12] s` 的连续均匀分布只抽一次时长并锁定截止节点。开始后即使停车或转弯也不提前结束；到达或越过截止时间后，结束方法返回准确截止节点，而不是较晚的检查节点。`StraightGateConfig.maximum_collection_interval_s` 同样默认为 45 s；默认随机上限只有 12 s，因此通常不会触发。如果自定义随机上限超过 `Tmax`，实际抽样上限取二者较小值。

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

可通过 `active_collection` 查看当前 `StraightCollection(start_timestamp_s, target_duration_s, end_timestamp_s)`，`active_start_timestamp_s` 为通用 `Tmax` 装饰器提供相同的开始节点接口；通过 `last_collection` 审计最近完成片段，`last_evidence` 保存最近一次开始尝试的运动证据。

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

开始判别所需延迟为：

```text
decision_delay >= start_lookahead
                  + max(2 s spin window,
                        1 m / 需要可靠识别的最低行驶速度)
```

默认 `start_lookahead=1 s`；最低可靠识别速度为 `0.2 m/s` 时，建议使用至少 6 s 延迟。最近 1 分钟缓存足以覆盖默认设置和多数低速场景。

## 失败安全

- yaw 计算差值前会跨 `+pi/-pi` 展开，避免 179° 到 -179° 被误判为 358°；
- 位姿 gap、位置/yaw 跳变会切断连续段，不会被当成转弯或恢复直行；
- 开始证据不足时不开始；结束证据不足时不触发运动结束条件，但到达 `Tmax` 仍会硬截止；
- Gate 只依赖位姿，不能发现“画面在动但定位冻结”，采集系统仍需跨模态冻结报警。

## 验证与视频回放

运行：

```bash
python3 -m unittest -v test_data_collection_gate.py
python3 -m py_compile data_collection_gate.py test_data_collection_gate.py
```

当前 19 个定向测试覆盖左右弧线、曲率边界、跨 yaw 环绕的原地转弯、输入异常、流式状态、恢复直线后结束、3 s 稳定停车结束、2 s 短暂停车不结束、二次转弯重置恢复、稀疏调用返回历史动作节点、默认 45 s 硬截止、1% 边界和 6–12 s 直线时长。

真实例子使用 `20260820180215WDK_1/cam0_continuous.mp4`。视频长 119.924 s，371 个有效 pose；历史 pose 约 3 Hz 且有 0.5–2.0 s 间隔，因此仅本次离线回放将 `maximum_pose_gap_s` 设为 2.0 s，实时高频缓存仍使用默认 0.5 s。其他设置为 60 s 缓存、6 s 判定延迟、转弯检查 0.2 s、直线机会 1 Hz、转弯优先、直线 seed 42。

| owner | 开始节点 | 结束节点 | 结果说明 |
| --- | ---: | ---: | --- |
| straight | 43.000 s | 53.835 s | 第 20 个合格机会命中；锁定时长 10.835 s |
| turn | 82.200 s | 94.600 s | 83.200 s 首次检测为右弧线；继续采到恢复直线后再结束 |
| turn | 102.200 s | 113.000 s | 后续左转保持为同一片段；连续稳定停车 3 s 后结束 |

旧的非状态式结束逻辑会把右转截成 82.200–85.400 s；新逻辑返回 82.200–94.600 s。旧逻辑还会把后段左转拆成 102.200–105.200 s 和 107.800–110.800 s；新逻辑保持为 102.200–113.000 s 的同一片段，在稳定停车后结束。本例所有片段都短于 45 s，因此 `Tmax` 没有介入。以上都是算法对历史 pose 的回放结果，没有人工修订边界。
