# P_map 专用坐标数据（2026-09-24 范围纠正）

用户提供的 `source_map.png` 只对应 P_map。首次导出误保留了 1,184 条使用原栅格坐标的 B10 样本；
它们没有套用简笔地图，但不应进入本次数据集。新版代码已限制 `source-map` 只导出 P_map。

当前入口：[P_map 数据索引](/mnt/chengchangxu/data/visual_nav_training/_source_map_exports/20260924_pmap_only/summary.json)。

| 项目 | 数量 |
|---|---:|
| 范围内 P_map 输入 | 14,796 |
| 保留 P_map 样本 | 10,245 |
| 灰线外当前点/历史筛选剔除 | 4,551 |
| 保留样本中路线截断 | 5,785 |
| 范围外其他地图，不进入数据集 | 1,184 |

13 个任务含 P_map 输入，其中一个任务的 P_map 数据全部因灰线筛选而剔除；5 个纯非 P_map 任务
从当前索引中跳过。保留样本的 `map_name` 全部是 `P_map`，`handdraw_map` 全部是 `source_map`，
每份 schema 的 `map_scope` 和地图元数据也只包含 P_map。

本次直接从上次成功产物中选择 P_map 行：每份保留 manifest 都与上游 P_map 行拼接结果逐字节一致。
当前点、历史、路线、yaw、时间戳、RGB 引用和教师命令均未重新计算。先核验上游全部输出哈希，
再复核实际读取的 112 个导出文件前后 SHA-256 一致；每任务另有 `pixel_coordinate_audit.json`，
记录上游来源、范围排除和新输出哈希。原采集数据、教师 run、旧导出均未改写。

代码回归 **65/65 通过**，覆盖非 P_map 在地图加载前排除、直接转换接口拒绝非 P_map、纯其他地图输入
产生可审计空输出，以及旧 `legacy` 多地图模式保持。保留 P_map 几何没有变化，沿用此前全部
526,256 个灰线点、446,973 段路线检查结果；[覆盖图](coverage.png)也仍对应这 10,245 条 P_map 样本。

今后 `--handdraw-map source-map` 会在转换前排除非 P_map 行，并以
`map_not_supported_by_source_map` 记录到排除清单；`legacy` 的通用多地图导出行为不变。
旧混合目录 `20260924_source_map` 仅用于追溯，不应再作为这张简笔地图的数据入口。
