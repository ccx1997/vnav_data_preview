# 手绘 P_map 资产来源

2026-09-20 从用户指定的本机项目迁移：

- `/mnt/chengchangxu/projects/visual_navigation_e2e_bak/assets/handdraw_pmap/`：
  `original.jpg`、`data/roads.json`、`metadata.json` 原样保存。
- 同目录 `pmap_to_jpg.py` 原样保存到 `../../vnav_coordinates/_paired_roads.py`，作为内部配对道路
  内核使用，不使用它另行归一化的 world/pixel 公共接口。
- `src/visual_navigation_e2e/pixel_coordinates.py` 的 `HanddrawMapper.map_many()` 与
  `scripts/build_pose_history_v4_pixels.py` 的 `prepare_image()` 迁移到 `geometry.py`。
  批次大小 128、最近道路决策、弧长/吸附、Pillow 图像旋转缩放和像素矩阵保持一致。

运行时不导入备份仓库。`metadata.json` 内原训练地图路径只保留历史溯源意义，转换使用显式配置
的地图元数据；原图/道路哈希在加载时校验。派生 aligned 图片只生成到用户指定的新输出目录。

| 文件 | SHA-256 |
| --- | --- |
| `original.jpg` | `1a2bead0f9c4c6c056d12e7d01c9341dd8f03571027479f56e3151fba0090668` |
| `data/roads.json` | `30199849f179a04772d6daf25b7cfca8d590bfef70e88a45c3b68b98de51e507` |

本地实现对非有限值/错误形状/不匹配的地图元数据显式报错，空列表返回空数组；yaw 原值保留，
不执行参考 world_to_cartesian 中的区间归一化。资产是项目已有标注，不是自动生成或重新标注。
