# 世界坐标转像素坐标（第四步）

复用已经生成的教师数据，独立导出像素 Manifest、手绘地图图像及审计文件。不启动教师、下载或
解包任务，也不改写采集目录、人工 `map_name`、视频裁剪或已有教师 run。依赖 Python 3.9+、
NumPy 和 Pillow；测试额外需要 pytest。

## 转换一个已有教师 run

在项目根目录运行，必须显式指定源 run 和**不存在的新输出目录**：

```bash
python3 processing/coordinate-converter/export_pixels.py \
  --run /path/to/existing/full/run_20260918_220806 \
  --output /path/to/pixel_exports/run_20260918_220806_pixels
```

默认按 `training-data-builder/config.json.map_mapping` 从
`/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server/model_server/maps/` 读取全局地图 NPZ。
可用 `--static-map-root /path/to/maps` 替换路径。仅加载样本实际使用的地图。

适配器只读成功 run 的 accepted case；核对 run 的接受数、标签/sample 身份与地图、JSON/NPZ
历史位姿和时间戳。不完整或不一致时，报错且不发布输出，不调用数据修复或生成流程。
它不重新执行质量筛选，也不自动应用外部无黑帧清单。

| 输出几何字段 | 当前教师数据来源 | 输出内容 |
| --- | --- | --- |
| `reference_pose` | `sample.json.grid_pose` | `[x_px,y_px,yaw_rad]` |
| `raw_poses` | `inputs.npz.teacher_history_pose` | 历史 `[x_px,y_px,yaw_rad]` |
| `route` | `inputs.npz.forward_route` | 当前向前路线 `[x_px,y_px]` |

历史时间戳保存为 `raw_pose_stamps_s`，速度指令为 `commands`。`initial_state`、RGB 历史引用及
`source_*` 溯源字段保留源值。局部 rollout 和占据张量仍在源 NPZ，不被当成全局世界坐标转换。
这是供后续训练读取的独立坐标数据集，不能替代原 run 给现有教师验证器或训练预览页使用。

## 转换参考脚本格式的 JSONL

输入每行必须有 `map_name`，及 `reference_pose`、`raw_poses`、`route` 中至少一个字段。
`reference_pose` 是一个 XY 或 XY-yaw 向量，后两者是向量列表，可为空。世界单位为米，yaw 为弧度。
可一次输入多个文件；输出保持文件名、行顺序及其他字段：

```bash
python3 processing/coordinate-converter/export_pixels.py \
  --input /path/to/world/train_manifest.jsonl /path/to/world/queries.jsonl \
  --output /path/to/pixel_exports/example \
  --maps-json /path/to/maps.json
```

`--maps-json` 是 `map_name -> NPZ 路径或元数据`，相对 NPZ 路径以配置文件目录为基准。例如：

```json
{
  "P_map": "/path/to/dufu_community_map.npz",
  "B10_map": {"width": 100, "height": 200, "resolution_m": 0.05, "origin_xy": [-1, -2]}
}
```

NPZ 必须有 `occupancy`、`origin_xy`、`resolution_m`。未知地图报错，不根据其他地图猜测坐标系。
已有 `coordinate_frame=cartesian_pixel` 或手绘字段的输入会被拒绝，防止重复缩放。
输出不能位于输入数据目录内；失败清理本次临时 `.building` 目录，已有输出不覆盖。

## 坐标约定和手绘映射

所有公开像素坐标均以**左下像素中心为原点，x 向右、y 向上**：

```text
x_px = (world_x - origin_x) / resolution_m - 0.5
y_px = (world_y - origin_y) / resolution_m - 0.5
image_column = x_px
image_row = image_height - 1 - y_px
```

`reference_pose/raw_poses/route` 属于各自全局栅格地图像素系；`P_map` 另外输出：

- `handdraw_pixel_xy`：当前点的手绘像素 XY。
- `handdraw_raw_pixel_xy`：历史位姿的手绘像素 XY 列表。
- `handdraw_route_pixel_xy`：前向路线的手绘像素 XY 列表。

仅为存在的输入字段添加对应手绘字段。其他地图的这些字段和 `handdraw_map` 为 `null`。
源 yaw **原值保留**（包括超过 `[-π,π)` 的值），不随手绘道路投影、旋转或缩放变换。
这与参考 `world_to_cartesian()` 的 yaw 归一化稍有不同；XY 映射和 `map_many()` 行为保持一致。

手绘转换沿用 `HanddrawMapper.map_many()`：最近道路中心线 → 端点吸附 → 对应道路弧长进度 →
原始 JPG 像素 → 旋转缩放后的像素。岔路口吸附 4 m、普通节点 1.5 m、入口 1 m，每端最多占
道路长度的 45%；近似等距时按道路标注顺序确定。包含 JPG `37–35`（P_map `38–36`），不含
JPG `35–47`（P_map `36–48`）。越界或离路较远的有限点也会投影，不新增隐式过滤门槛；审计
记录所有映射点（包含重复历史/路线点）的投影距离 count、median、p95、max，单位米。

原图 `1441×1079` 逆时针旋转 90° 后按**精确 1.5 倍**采样，输出 `1619×2162`。不能用取整后的
目标宽高做普通 resize，否则像素中心与坐标矩阵不一致。对应原始 JPG 栅格中心 `(u,v)`：

```text
handdraw_x = 1.5 * v + 0.25
handdraw_y = 1.5 * u + 0.75
```

`--handdraw-assets` 可指定完整资产目录（`metadata.json`、`original.jpg`、`data/roads.json`）；
资产哈希、原图尺寸、旋转缩放、矩阵和 P_map 原点/尺寸/分辨率必须匹配。当前仅支持上述手绘
资产约定，不是任意地图间的通用标定工具。占据图版本允许不同，几何参数必须相同。

## Python 接口

将 `processing/coordinate-converter` 加入 `PYTHONPATH` 后：

```python
from vnav_coordinates import HanddrawMapper, pmap_pixel_to_handdraw_pixel, world_to_cartesian

mapper = HanddrawMapper()
xy, distance_m = mapper.map_world([[0.0, 0.0], [1.0, 2.0]])
xy, distance_m = mapper.map_many([[843.0, 1546.0]])  # 输入 P_map 左下像素
x, y = pmap_pixel_to_handdraw_pixel(843.0, 1546.0)
```

## 输出与验证

```text
new_output/
  samples.jsonl                 # --run 模式；--input 则保留各输入文件名
  coordinate_schema.json        # 字段、单位、地图几何和手绘图像路径
  pixel_coordinate_audit.json   # 行数/地图计数、输入输出 SHA-256、投影距离
  handdraw_pmap/                 # 仅输入包含 P_map 时生成
    aligned.png
    aligned.jpg
    metadata.json
```

`source_*` 与 `inputs_npz_path` 指向原始资产，内容仍使用原坐标，不是像素化张量；输出没有伪装成
已转换的 NPZ 或可写源目录链接。导出前后校验实际读取文件的 SHA-256；审计不表示重新验证了源
run 的全部图片、视频或教师质量。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q processing/coordinate-converter/tests
```

测试覆盖道路吸附/弧长映射、道路编号、标量对照、图像与半像素约定、CLI、地图混合、yaw/指令
保持、损坏源与重复导出保护。迁移来源见 [assets/handdraw_pmap/README.md](assets/handdraw_pmap/README.md)。
