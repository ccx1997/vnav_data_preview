# VNav 训练数据读取与处理约定

本文记录本项目已经确认的训练数据读取、同步、过滤和处理方式。后续对话或代码改动只要涉及训练数据读取、同步、采样、过滤、裁剪、导出或预处理，都必须同步更新本文，记录新增结论、参数和验证结果。

## 2026-09-20 新增统一四阶段脚本

- 入口：[`processing/run-data-pipeline.sh`](processing/run-data-pipeline.sh)，Python 入口为
  [`processing/run_data_pipeline.py`](processing/run_data_pipeline.py)。支持单个 ID、位置参数列表、
  重复 `--task-id`、`--task-ids` 空格/逗号/JSON 数组，以及 `--since/--date` 或单个日期位置参数。
  ID 稳定去重；不同选择模式互斥。完整选项和输出示例见
  [`processing/data_pipeline/README.md`](processing/data_pipeline/README.md)。
- 日期模式包含起始日期零点，默认查询至当前 `Asia/Shanghai` 时间，可指定 `--until` 和 `--robot`。
  复用 `list-task-ids.py` 完整时间窗接口；查询截断时在下载前失败，空查询不会误处理所有本地任务。
  实测接口拒绝带 `+08:00` 的 ISO 字符串（HTTP 400），故在解析时区后传 Unix 秒，JSON 中仍保存
  ISO 时间。修正后 `2026-09-20 00:00` 至本次执行时刻成功发现 WIt/8Up 两个任务。
- 四阶段复用现有下载器、训练构建器和像素转换器；默认 ep011200、Rule-10 v2 与既有 config.json，
  不修改同步、路线、历史、20% 零速、batch=32、碰撞或坐标规则。源目录和输出根目录可配置，
  必须互不包含。依赖预检先于云端查询/下载，不回退旧教师。
- 新任务先检查云端停止采集、视频 ready、object_key 和修订号，再首次下载；已有源目录只读验证，
  完整则复用，不完整则保留现场报错。已有成功教师与像素结果分别验证复用，只补缺失阶段；
  不自动重打成功标签或覆盖损坏像素。人工地图、裁剪或源行变化触发失败。
- 独立 `_pipeline_state/<task>/<run>.json` 绑定源文件集合/元信息/JSON 哈希和配置。首次接入旧 run
  依据原 provenance、时间/地图/mtime、逐样本校验建立基线，不能追溯旧流水线没有保存的历史源哈希。
  默认选空余 GPU/端口，显式以 `--batch` 启动教师，构建器使用 `auto_start_teacher=False`；
  复用服务时核对 batch 能力、checkpoint 哈希与真实启动 YAML，只停止本次自启进程。
- stdout 仅输出一个 `vnav_data_pipeline_v1` JSON，进度写 stderr。每任务返回 `source_data_dir`、
  `annotation_data_dir`（任务标注根目录）、`teacher_data_dir`、`pixel_data_dir`、标签/像素 Manifest、
  无黑帧清单、计数、验证结果、复用标记及报告路径；失败包含 `stage/error` 并继续后续任务。
  非 dry-run 另存 `_pipeline_runs/<batch>/result.json`；`--result-json` 可另存到数据根目录之外。
  成功/预览退出 0、任务失败退出 1、预检/参数错误退出 2、中断退出 130；中断保留完成条目和现场。
- 新脚本的 RGB 检查覆盖历史引用和每个 case 的当前图像，部分路径为指向同一图像内容的硬链接，
  因而路径检查数量可能高于先前仅检查历史引用的脚本；黑帧阈值仍为所有通道均 `<=8`。
  主教师/像素数据保留全部 accepted，额外无黑帧清单需显式使用。默认技能和 webhook 可用时逐任务
  通知飞书，支持 `--no-feishu`；dry-run 不通知。通知失败不触发数据重试。
- 验证：新入口 **28 项** + 原训练 **21 项** + 像素转换 **29 项**，合计 **78/78 通过**，覆盖输入、
  时区/日期查询、截断/空集合、缺失依赖、云端修订、复用、源地图/裁剪保护、部分失败、中断与锁、
  教师清理、像素身份/哈希/坐标往返和纯 JSON stdout。
- 真实复用验证指定 WIt/8Up，两个任务均 `source/teacher/pixels=true`，完整验证 835/940 个接受样本
  和对应像素，额外无黑帧清单 791/891 条，源文件前后未变。未下载、重生成教师或重导出这两份数据。
  新数据执行分支使用隔离测试替身验证调用顺序、授权范围、失败处理和资源清理，未另拉真实新任务。
- [真实复用 JSON](reports/data_pipeline_20260920/reuse_result.json)、
  [日期查询计划](reports/data_pipeline_20260920/date_plan_fixed.json)、
  [78 项回归日志](reports/data_pipeline_20260920/regression_final.log)。
- 提交前复验：使用 `deepseek_train` 环境并设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，运行
  `processing/data_pipeline/tests`、`processing/coordinate-converter/tests` 和
  `processing/training-data-builder/tests`，**78/78 通过（4.01 s）**；本次仅运行隔离回归测试。

## 2026-09-20 指定两份采集数据的完整处理

- 范围仅限 `20260920122356WIt`、`202609201240128Up`；开始前两任务的采集和训练目录均不存在。
  首次下载使用 `download-task.sh --jobs 4`，保留 ZIP 与原始视频分片；既有任务不下载、不解包、
  不重新处理，保护本地人工 `map_name` 和裁剪。报告各自保存于
  `/mnt/chengchangxu/data/visual_nav_training/_task_reports/<task_id>/`。
- 沿用 `vnav_teacher_rule10_history_v2` 和现有 `config.json`，显式指定 ep011200 教师与
  `--batch`：batch=32、20% 零速重复、0.5 s pose 估速、1.5 s 教师历史、1.0 s RGB 历史且最高
  10 Hz；同步门槛 50 ms / 0.05 m / 3 deg。限定 task ID 构建，只停止本次自启教师。
- 第四步对成功 run 的全部 accepted case 使用 `coordinate-converter/export_pixels.py`，
  输出到训练任务目录下独立的 `pixel_exports/<run_name>/`；采用左下像素中心坐标及既有 P_map
  手绘映射约定，保留世界坐标源 run、yaw、速度指令与时间戳。
- 验证计划：云端完整性、源 Meta/PNG/连续视频、逐样本 `validate_run`、RGB 解码与全黑检查、
  像素转换审计和源文件前后保护核验。无黑帧清单单独保存，不隐式缩减教师或像素导出范围。
- 云端 350/350 路视频就绪后，两任务首次下载与对齐均成功。源数据验证共 70 个子任务、
  2,882 行（全部 `P_map`）、2,880 张有效栅格、350 路视频，全部使用 `hw_ts`；保留视频清单
  中的 24/26 个覆盖空隙及既有定长补黑规则。已保存 312 份既有 frames/视频 manifest 的保护快照。
- `WIt` 已于 17:41 完成四步：`run_20260920_172826` 共 852 个候选、835 个接受、17 个碰撞拒绝；
  逐样本教师验证 0 错误，唯一 warning 为实际五路相机。RGB 解码失败 0，79 个全黑图像路径影响
  44 个样本，无黑帧清单 791 条。独立像素目录
  `/mnt/chengchangxu/data/visual_nav_training/20260920122356WIt/pixel_exports/run_20260920_172826/`
  包含全部 835 条 accepted；坐标往返最大误差 `4.2633e-14 m`，yaw/命令/时间戳及源文件未变。
- `8Up` 已于 18:00 完成四步：`run_20260920_174149` 共 954 个候选、940 个接受、14 个碰撞拒绝；
  逐样本教师验证 0 错误，唯一 warning 同为实际五路相机。RGB 解码失败 0，48 个全黑图像路径
  影响 49 个样本，无黑帧清单 891 条。独立像素目录
  `/mnt/chengchangxu/data/visual_nav_training/202609201240128Up/pixel_exports/run_20260920_174149/`
  包含全部 940 条 accepted；坐标往返最大误差同为 `4.2633e-14 m`。
- 两任务合计 **1,806 个候选、1,775 个接受、31 个碰撞拒绝、1,775 条像素记录**；57 次 teacher
  batch 均仅执行一次 forward。另有 530/546 行源记录被原有路线、历史、同步等预处理门槛拒绝；
  `8Up_19` 的 20 行均未成为候选，因此接受样本实际使用该任务其余 32 个子任务的 160 路视频。
  这不影响完整下载和验证全部 33 个子任务的 165 路源视频。
- 共检查 32,607 个 RGB 当前/历史引用路径，解码失败 0；全黑阈值为所有 RGB 通道像素均 `<=8`。
  93 个样本涉及全黑帧，额外无黑帧清单共 **1,682 条**，分别位于各任务报告目录的
  `samples_without_black_frames.jsonl`。它们没有自动过滤原教师 run 或像素导出，也不代表已做
  模糊、曝光、冻结等完整画质筛选。
- 像素验证覆盖全部接受样本：几何坐标往返、yaw/命令/时间戳/RGB 引用保持、导出文件 SHA-256
  均通过。手绘投影距离统计含重复历史和路线点，WIt 的 p95/max 为 `3.9879/6.5906 m`，8Up 为
  `4.1101/4.9027 m`；沿用既有道路投影规则，不新增离路过滤。
- 312 份既有 frames/视频 manifest 与两任务全部 3,669 个源文件保护核验通过；像素转换读取的
  教师源文件前后哈希一致。两个本次自启 teacher 均已停止；各任务限定 dry-run 均为
  `pending=0 / already_completed=1 / incomplete=0`。源数据验收和最终结果已分别成功同步飞书。
- 报告：
  [WIt 完整报告](/mnt/chengchangxu/data/visual_nav_training/_task_reports/20260920122356WIt/report.md)、
  [8Up 完整报告](/mnt/chengchangxu/data/visual_nav_training/_task_reports/202609201240128Up/report.md)。
  报告目录保存 preflight、pipeline_config、源保护快照、教师验证、RGB 质量、像素验证与全部执行日志。

## 2026-09-20 新增第四步：世界坐标导出为指定地图像素

- 新入口：[`processing/coordinate-converter/export_pixels.py`](processing/coordinate-converter/export_pixels.py)。
  `--run` 显式读取一个已有成功教师 run 的 accepted case，或 `--input` 读取一个或多个世界坐标
  JSONL；`--output` 必须是源目录外的新目录。独立导出，不自动接入下载或教师重跑，不覆盖已有
  采集数据、人工 `map_name`、裁剪结果或教师 run。不自动应用外部无黑帧清单；保持调用者选择的
  输入范围，不能将像素导出当成重新筛选或教师质量验证。
- 世界坐标转换公式：`xy_px=(xy_world-origin_xy)/resolution_m-0.5`，左下像素中心为原点，
  x 向右、y 向上；读取图像用 `column=x_px,row=height-1-y_px`。各地图参数默认由原训练配置的
  `map_mapping` 对应全局 NPZ 读取，可用 `--static-map-root` 或 `--maps-json` 指定。
  未知地图/非有限坐标/错误形状/已转换数据报错，不猜测坐标系。
- 教师 run 适配：`reference_pose ← sample.grid_pose`，`raw_poses ← inputs.npz.teacher_history_pose`，
  `route ← inputs.npz.forward_route`；历史时间戳另存 `raw_pose_stamps_s`。源 yaw 弧度值原样保留，
  不旋转、不归一化；命令保留 m/s、rad/s 和秒。只转换这三组全局位置，局部 teacher rollout、
  占据图和原 NPZ 保留源含义。此输出为独立坐标 Manifest，不能替换原 run 用于旧预览/验证器。
- `P_map` 增加 `handdraw_pixel_xy / handdraw_raw_pixel_xy / handdraw_route_pixel_xy`，字段按
  实际输入几何字段生成；其他地图对应手绘字段与 `handdraw_map` 为 `null`。复用用户指定的
  `visual_navigation_e2e_bak` 道路配对和 `HanddrawMapper.map_many()`：最近道路中心线 →
  岔路口 4 m/普通节点 1.5 m/入口 1 m 吸附（每端最多 45% 道路长度）→ 对应道路弧长进度 →
  JPG → 对齐后手绘像素。保留 JPG `37–35`（P_map `38–36`），排除 JPG `35–47`（P_map `36–48`）。
  越界/离路点仍按参考逻辑投影，不新增过滤阈值；输出全体映射点（含重复点）的距离统计，单位米。
- 原始图像、道路标注及元数据已原样迁入本项目，运行不依赖备份仓库。检查资产哈希和 P_map
  尺寸/原点/分辨率一致性；原图 `1441×1079` 逆时针 90°、精确 1.5 倍采样，输出 `1619×2162`。
  原 JPG 像素 `(u,v)` 对应 `handdraw_xy=(1.5*v+0.25,1.5*u+0.75)`，不能改成取整尺寸后的 resize。
- 输出：`samples.jsonl`（或原输入 JSONL 文件名）、`coordinate_schema.json`、
  `pixel_coordinate_audit.json`，涉及 P_map 时另写 `handdraw_pmap/aligned.png/.jpg/metadata.json`。
  `source_*`/`inputs_npz_path` 仅引用原坐标资产；源数据不复制改写或建立可写目录链接。
  校验实际读取文件的前后 SHA-256，检查接受数量、样本身份/地图、JSON/NPZ 历史一致性；失败
  不发布部分结果、不自动修复源数据，既有输出拒绝覆盖。
- 验证：新增 29 项 + 原训练流水线 21 项，共 **50/50 通过**。2,048 个固定随机测试点与参考实现
  的 XY/投影距离最大误差均为 **0**；生成图与参考 `aligned.png` 逐像素一致。只读抽查任务
  `20260918200506GCy` 已有 run 前 12 个 accepted case，共 **1,111 个映射点**，最大误差 **0**，
  涉及 30 个源/地图/资产文件的 SHA-256 前后一致。未进行已有任务全量导出或重新处理。
  首轮 2 个测试断言修正了 JPG/P_map 编号差异和独立插值轴交换的舍入容差；迁移图像本身与参考
  图逐像素相同。验证结果已通过 `send-feishu-experiment` 通知飞书。
- [使用说明与命令](processing/coordinate-converter/README.md)、
  [验证报告及源文件哈希](reports/coordinate_converter_20260920.json)。


<!-- task:20260918200506GCy:start -->
## 2026-09-18 指定任务 `20260918200506GCy` 下载、预处理与教师轨迹

- 当前状态：**completed**；更新时间 `2026-09-18T22:27:01.294814+08:00`。
- 完成并验证通过：1750 个候选、1727 个接受、23 个拒绝；无黑帧清单 1619 条，源文件保护通过。
- 本机后台轮询间隔为 1,200 秒；仅在全部云端视频 ready、有 object_key、修订号一致且子任务/相机集合完整后开始。初次见到 54 个子任务、270 路视频；缺少推荐 cam5 不阻断实际五路处理。
- 仅首次下载本任务，调用 `download-task.sh --jobs 4`；保留 ZIP/原始分片。已有任务复用；本地不完整现场停止并报告，不刷新、重新解包或覆盖 frames/map_name/裁剪。
- 教师显式使用 ep011200、`--batch`、`vnav_teacher_rule10_history_v2`。batch=32、20% 零速重复、0.5 s pose 估速、1.5 s 教师历史、1.0 s RGB 历史/最高 10 Hz；同步门槛 50 ms/0.05 m/3 deg。运行时选空余 GPU 和空闲端口，结束只停止本次自启服务。
- 验证顺序：源 Meta/PNG/全部连续 MP4 → 原项目逐样本 validate_run → RGB 解码与全黑检查（所有通道均 <=8）→ 源文件保护核验。完整标签保留，额外生成 `samples_without_black_frames.jsonl`；训练需显式采用该清单。
- 预检已核对教师配置/权重 SHA-256 与上次成功批次一致；检查脚本、cloud readiness 判定及源数据保护快照记录在报告目录。未就绪期间不启动教师，不写源采集目录；完成或需要人工处理时通过 send-feishu-experiment 通知飞书。
- 后台编排验证：4 份脚本语法通过，9/9 就绪判断检查通过（覆盖未就绪、无对象键、旧修订、缺相机、重复项、采集中、空集合和截断）；已有采集数据的 204 份 frames/视频 manifest 已保存大小、mtime、inode 与 SHA-256 保护快照。这些是编排预检，尚不代表目标数据验收完成。
- 报告目录：`/mnt/chengchangxu/data/visual_nav_training/_task_reports/20260918200506GCy`；实时状态 `state.json`、轮询历史 `events.jsonl`、后台日志 `worker.log`。
- 成功产物：`/mnt/chengchangxu/data/visual_nav_training/20260918200506GCy/vnav_teacher_rule10_history_v2/full/run_20260918_220806`；候选 1750，接受 1727，拒绝 23。
- 验证：`{"passed": true, "error_count": 0, "warning_count": 1, "errors": [], "warnings": [{"scope": "run", "reason": "video_camera_set_nonstandard", "actual": ["cam0", "cam1", "cam2", "cam3", "cam6"], "recommended": ["cam0", "cam1", "cam2", "cam3", "cam5", "cam6"]}]}`；源文件未变 `True`；无黑帧清单 1619 条。

<!-- task:20260918200506GCy:end -->

## 2026-09-18 Gate precision 与 recall 联合修正（当前规则）

- 撤销会降低召回的 2 秒弧线硬上限，恢复原有最多 15 秒、最多 1 米的曲率观测范围，
  可用缓存内至少 0.30 米路径即可判别。6 秒延迟、60 秒缓存、0.2 秒检查节拍保持不变。
  附近 2 秒只检查是否超过既有停车漂移容差 3 cm，完整曲线位移要求超过 10 cm，
  原地 yaw 转弯规则不变；这取代上一版附近 2 秒必须移动超过 10 cm 的条件。
- 曲率仍取实际采样顶点的三点圆曲率，锚点插值只补回边界路程，不加入曲率顶点。
  因此有限时长的稀疏、错相位圆弧不再少算支持路程。自然结束等待初始阳性证据的
  实际末端，达到路线长度即结束观察，停车尾部不会延长证据窗。
- 保存统一要求精确 action 时间差 `>=4.0 s`。提前硬停止若截断启动证据，仅用最终
  窗口内的实际顶点检查是否存在达到转弯曲率阈值的弯折，或满足原有判据的原地转弯；
  不重新套用 0.30 米启动门槛，
  避免误丢已发生的短真弧。复用既有 2 cm 路线去重和曲率阈值，没有新增数值参数。
  结果通过 `last_completed_collection.target_confirmed` 与 `should_save` 交给采集端。
  **真实录制控制器须在永久保存/提交窗口前消费 `should_save`，本仓库不包含该端实现。**
- 验证：42 项定向回归、Python 编译与 diff 空白检查通过；17 例与 Git HEAD 的默认 Gate 对比，双方都应用 4 秒政策，
  当前版实际消费保存标记，保留的真实转弯覆盖时间全部一致。覆盖低速、紧弯、停走、
  2 Hz 错相位与有限短弯，以及硬停止前不足 0.30 米的真实弯道。6 个 precision 场景的
  无转弯保存片段由 4 个降至 0 个，真实转弯区间完整保留。四个用户原例的 action 时长
  均小于 4 秒，统一保存规则会拒绝这些时长；缺少原始连续缓存，不能声称精确线上重放。
- Astra 已先后发现有限稀疏弯道漏采、4.2 秒空片段、短真弧误丢和浮点插值越界，均已修复
  并加入回归；随后补齐 6/7 秒延迟下提前硬停止仍保留真实原地转弯，以及停车补查的浮点
  区间边界。Astra 最终定向复审 **accept**，独立确认两个延迟下均保存 `[0,5.4]`、
  `[5.6,12.8]`，20° 原地转弯保留，8.6 秒检查不再崩溃；该结论限于已发现反例与修正。
  阶段测试及最终结果已按 `send-feishu-experiment` 成功通知飞书。
  提交前将诊断与对比脚本的旧版基线固定为 `048c0f6b6b9123b12ad5ce09e63adc17deea778f`，
  避免提交后 HEAD 改变导致复现失效；未改变 Gate 算法。
  详细规则、验证命令和审阅结论见[联合修正记录](reports/gate_0917_diagnosis_20260918/precision_recall_repair.md)。
  这些定向测试不能证明生产全部场景 recall 不变；未下载、解包、重处理或修改任何已有
  采集数据、人工 map_name 和裁剪结果。以下同日小节保留此前诊断和改动历史。

## 2026-09-18 Gate precision 改动的 recall 复核

- 用户澄清重点是提高 precision；本轮只审计，不修改 Gate 或已有数据。新旧默认 Gate
  在同一 6 秒延迟、60 秒缓存、0.2 秒检查节拍下比较，旧版从 Git HEAD 只读载入，双方
  都按 4 秒最短保留规则评价，排除最短时长要求本身的影响。
- **确认存在额外 recall 损失**：10 Hz 下 0.06/0.10/0.14/0.15 m/s 的 20 秒真实弧线、
  走走停停弯道、4 秒低速转弯事件，以及 2 Hz/0.20 m/s 错相位圆弧，旧版能保留大于
  4 秒窗口而新版完全不启动。0.08 m/s 紧弯仅保存最后约 2.2 秒真实转弯。
- 主因是 2 秒硬窗口与最低 0.30 米路径引入约 0.15 m/s 速度门槛，实际顶点采样会进一步
  缩短有效路径；低速弧线平移超过 spin 的 0.10 米门槛时，原地转弯分支无法补齐盲区。
- Astra 再次独立复核确认下降。先前 34 项回归及 1,680 组合的 accept 不代表 recall 不变；
  该几何扫描仅覆盖速度至少 0.20 m/s、3–20 Hz、恒速圆弧，未覆盖本轮边界。
- 本次 11 例为定向合成反例，不作为生产整体 precision/recall 估计。建议后续将短窗运动
  确认与可延长的有限曲率观测窗分离，并分别处理曲率顶点与时间/路径边界；本轮未实施。
  完整[召回复核](reports/gate_0917_diagnosis_20260918/recall_audit.md)含脚本、机器证据和参数。
  本轮结论及 Astra 独立复核结果已通过 `send-feishu-experiment` 成功发送飞书。

## 2026-09-18 Gate 时间范围与 4 秒保留规则优化

- 基于同日 0917-auto 诊断，弧线证据改为未来最多 2 秒、最多 1 米，至少 0.30 米路径，
  且附近 2 秒最大平移 `>0.10 m`；原地转弯继续使用低平移和 `>=8 deg` yaw 变化。
  外层锚点预看仍为 1 秒，默认总观察跨度最多 3 秒；现有 6 秒判定延迟可继续使用。
  与离线未来 1 米标签保留相同曲率阈值，但在线因时间限制不保证逐点一致。
- 自然直行恢复等待启动阳性证据的观测窗结束，防止转弯前的历史直行提前结束；跳变、
  3 秒稳定停车及 45 秒 Tmax 的硬停止继续优先。0.2 m/s 低速弧线验证通过，极低速不足
  0.30 米路径时不延长观测窗攒距离。
- 两类 Gate 每次结束统一发布 `last_completed_collection`，`should_save` 按实际 action
  时间差 `>=4.0 s` 判定。短片段照常结束/复位，恰好 4 秒保留；二元启停接口兼容。
  **真实采集端需同步消费该标记并在永久保存/提交 collect_windows 前丢弃短片段**；
  当前仓库无该端实现，已更新 `readme_gate.md` 的接入示例，未声称线上已部署。
- Astra 首轮审阅发现稀疏轨迹插值造成曲率漏判/误判，已改为实际顶点抽稀后的有符号
  三点圆曲率中位数；开始与恢复直行均不把边界插值点用于曲率，原参数 `curve_resample_m`
  现在表示实际顶点的最小抽稀路程间隔。左右 7.9/8.0/8.1 deg/m 边界及 4/10 Hz 稀疏
  采样、不同速度和相位的 144 组单点/24 组启动组合验证通过。
  Straight 显式要求曲率低于配置阈值，避免将因平移不足拒绝的弯道/抖动当作直行。
  基线 22/22，当前 34/34 定向回归、Python 编译和 diff 空白检查通过；初轮失败及修正过程、
  Astra 审阅结果见[优化记录](reports/gate_0917_diagnosis_20260918/optimization.md)。
  Astra 独立 1,680 组几何检查和新增 Straight 定向复核通过，最终结论 **accept**，无阻断项。
- 未修改任何已有采集数据、人工 map_name、裁剪、教师数据或下载产物。诊断与验证阶段
  状态及最终 Astra accept 结论均已通过 `send-feishu-experiment` 成功发送飞书。

## 2026-09-18 0917-auto 停车与短片段只读诊断

- 按用户“先解释原因”的范围，复用 `20260917174539CCx` 的现有 Meta 和视频，重点检查
  43、53、20、14。未改 Gate、源 `frames.jsonl`、人工地图标注、视频和裁剪结果，未重新下载、
  解包或生成教师数据；当前缺失的相邻片段 54 规范 Meta 只记录，未恢复。
- 以原始采集 `from/to` 为时长依据；四例均由 TurnGate 采集。43、53 各为 3.000 s，
  保存位姿相对首点最大平移分别 6.600/7.169 mm，展开后的 yaw 极差 0.291/0.199 deg，支持
  基本静止判断；20、14 分别 2.078450/1.806082 s。20 路视频均无 Manifest 覆盖缺口或缺失
  分片，ffprobe 时长分别为 3.000/3.000/2.067/1.800 s，短窗口在采集记录中已经存在。
- 当前 Gate 默认配置的两个合成机制复现通过：6 秒才起步转弯时可提前保存 `[2,5]` 秒纯
  静止片段；4.5 秒才转弯时可因转弯前历史直行结束，保存 `[3,4.2]` 秒片段。使用 0.1 s pose、
  6 s 判定延迟、60 s 滚动缓存、0.2 s 结束检查。根因机制为未来路线开始证据与历史结束证据
  的时间范围冲突，并缺少统一最短保存时长。缺少线上完整缓存、配置/版本和 reason 日志，
  上述机制不能视为四个实片的精确触发回放。
- 记录后续要求（本轮未实现）：所有采集类型按精确 action 时间差 `<4.0 s` 丢弃，恰好
  4 秒允许保存；在结束后的保存层处理，不延迟硬停止来凑时长，不自动删除已有数据。
  弧线开始需要约束到实际运动附近，同时保留原地 yaw 转弯；历史直行结束应关联已发生转弯。
- 验证：四例元数据与视频时长核对完成，两个机制复现断言通过；本次读取的 36 个源文件大小、
  mtime、SHA-256 前后一致。完整证据和复现脚本见
  [诊断报告](reports/gate_0917_diagnosis_20260918/report.md)。
  诊断验证结论已通过 `send-feishu-experiment` 成功发送飞书。

## 2026-09-17 指定采集任务下载、预处理与教师轨迹

- 范围仅为用户指定的 `20260917174539CCx`；开始前采集目录和训练目录均不存在，执行首次下载。
  使用 `download-task.sh --jobs 4` 完成 Meta 归属校验、规范解包和实际相机视频对齐，保留 ZIP
  与原始视频分片。已有采集任务全部复用，既有 `frames.jsonl` 和视频 manifest 已保存保护快照。
- 采集根目录 `/mnt/chengchangxu/data/visual_nav_mv`，训练输出根目录
  `/mnt/chengchangxu/data/visual_nav_training`；报告和执行日志位于后者的
  `_task_reports/20260917174539CCx/`。
- 显式使用 `config_rule10_continuous_ep011200_sim.yaml`，配置 SHA-256
  `212caf60c9a4969a9861ac0dee8911994ac996b0033e88592f56b0d126489246`，checkpoint SHA-256
  `2f372ee3f204f2978d1e3466ee2742011cbaab1d66aa79861bebdfa875f13028`，预检均与最近成功批次一致。
  使用 `vnav_teacher_rule10_history_v2` 与现有 `config.json`：batch=32、20% 精确零速重复、
  0.5 s pose 估速、1.5 s 教师历史、1.0 s RGB 历史且不超过 10 Hz；同步门槛保持
  50 ms / 0.05 m / 3 deg，按逐帧地图切段并保留碰撞拒绝记录。
- 下载与源数据验证通过：58 个规范 Meta、2,363 行（全部 `P_map`）、2,362 张有效栅格、290 路
  连续视频；58 份 manifest 均完整且采用 `hw_ts`，总窗口 495.000351 s。实际相机集合统一为
  `cam0/cam1/cam2/cam3/cam6`，缺少推荐 `cam5` 仅记 warning；最大跨相机时长差
  0.066667 s、最大窗口时长误差 0.066437 s。清单没有缺失分片，但按既有定长补黑策略记录了
  46 个视频覆盖空隙，保留在 manifest 中。全部 290 路通过 ffprobe 时长/帧数检查。
- 一键入口首次启动未传 model server 新接口所需的 `--batch`，在教师推理前报
  `teacher service does not expose /teacher/infer_batch`，无已接受样本；该服务已自动停止，
  失败批次和日志保留。随后通过报告目录的 `generate_teacher.py` 显式用 GPU 3、8103 端口、
  `--batch` 启动教师，再以 `--no-start-teacher --task-id 20260917174539CCx` 调用原构建器。
  健康检查确认 epoch 11200、独立历史 batch 接口启用、`curvature_only=false`。
- 教师已完成：成功 run 为
  `/mnt/chengchangxu/data/visual_nav_training/20260917174539CCx/vnav_teacher_rule10_history_v2/full/run_20260917_221344/`。
  2,363 行中 1,261 行因路线/历史/同步等原有门槛被拒绝；1,102 个候选进入教师，接受 1,063 个，
  39 个全部因 `teacher_rollout_collision` 拒绝。接受样本来自 31 个子任务，包含 866 个动态历史
  和 197 个零速重复；35 次 batch 全部只执行一次 forward。原项目 `validate_run` 通过，0 错误，
  1 个实际五路相机集合 warning。
- 补充逐图像检查覆盖 18,585 个当前/历史 RGB 引用路径，解码失败为 0；发现 173 个整帧黑屏路径，
  影响 87 个样本（31 个涉及当前帧，另外 56 个仅历史帧涉及）。黑屏阈值为 RGB 全图所有通道
  像素均 `<=8`。保留原始 1,063 个 accepted 教师结果，同时在报告目录生成
  `samples_without_black_frames.jsonl`，明确列出排除这 87 个样本后的 **976** 个 `sample_json`
  路径；训练时应显式使用该清单，现有加载器/预览计数不会自动应用它。此检查不代表完成模糊、
  曝光、冻结等所有视觉质量筛选。
- 保护验证通过：原有 132 份 frames/视频 manifest 的大小、mtime 和内容哈希未变；本任务
  3,036 个源文件的大小/mtime/inode、JSON 内容哈希及文件集合均未变。再次限定扫描结果为
  `pending=0 / already_completed=1 / incomplete=0`。本次启动的 GPU 3 teacher 已停止，8103 释放。
- 完整报告与验证产物：
  `/mnt/chengchangxu/data/visual_nav_training/_task_reports/20260917174539CCx/report.md`。
  源数据验证、教师启动修复状态与最终验证结果均已通过 `send-feishu-experiment` 成功发送飞书。

## 2026-09-14 仿真视觉数据只读统计与抽检

- 2026-09-18 归档前复核：已有 JSON/JSONL/CSV 可解析，两个逐轨迹清单和 CSV 均为
  5,416 行，索引快照 SHA-256 与报告一致，20 张派生图像校验通过；未重新处理源数据。

- 输入：`/mnt/wlf/projects/e2evnav/hybrid25d_renderer/outputs/visual_dataset/index.jsonl`。
  数据持续生产，以北京时间 `2026-09-14 21:44:13` 的索引快照固定本次口径；
  按 `generation_settings_version` 区分 `legacy_v1` 与 `v2_unique_obstacles`。
  历史 NPZ 元数据缺失该字段时按源格式约定回退旧版，索引版本为统计分组依据。
- 快照共 **5,416 条**：旧版 **3,394**（室内 1,610 / 室外 1,784），新版 **2,022**
  （室内 956 / 室外 1,066）。旧版比用户描述的约 3,400 少 6 条，现有快照不能解释原因。
  合计 **923,832 个同步时刻 / 2,771,496 张三相机图像 / 127.8621 小时**。
  时长累加 `duration_s`，不以帧数除以采样率替代完整积分终止时间。
- 数据为 2 Hz、三相机 `front/rear_left/rear_right`；RGB 为
  `T×3×512×640×3 uint8`。本次仅复用现有 NPZ、JPEG，不接入真实采集数据处理流程，
  不重新下载、解包、仿真、渲染或覆盖源数据，也不修改任何 `frames.jsonl`。
- 对全量 `rollout.npz` 的 `obstacle_ids`、`obstacle_kinds` 分别检查轨迹内唯一性：
  旧版 **3,298/3,394（97.17%）**有资产重复，新版 **0/2,022**有 ID/资产重复。
  独立资产数均值 **3.47 → 8.54**，约 2.46 倍；这不表示跨轨迹或语义类别去重。
- 抽取 9 例：新版室内 3、室外 3，旧版室内重复、室外重复、室外无重复各 1。
  按分层内路线长度、障碍数、累计航向变化的分位数确定性选样（详情见选择清单），
  不把覆盖性选样当随机质量评估。每例时间前/中/后三段各选一帧，相邻至少间隔总帧数的 12%；
  前相机 ±44°、障碍距离 0.7–8m、原始占据视线与高度/距离得分用于突出障碍，
  仅作为选帧启发式。三相机共用帧号；俯视图按保存的原点、分辨率及最大 Y 对应第 0 行绘制。
- 验证：5,416 条索引、所需文件存在性、所读 NPZ 元数据/小数组、跨文件位姿/时间戳一致性、
  2 Hz 时间轴、RGB 数组头/声明大小均未发现问题；sample_id/seed/source_scene_id 无重复。
  9 例共 81 张 JPEG 实际解码成功（640×512），18 张派生 PNG 重新打开成功。
  未全量解压 RGB 像素或做全库 CRC/画质/碰撞复评；`success=true` 仅为生成器记录。
- [完整报告与 9 例大图](reports/sim_visual_dataset_20260914_214413/report.md)、
  [分组统计](reports/sim_visual_dataset_20260914_214413/statistics.json)、
  [逐轨迹 CSV](reports/sim_visual_dataset_20260914_214413/samples.csv)、
  [选择清单](reports/sim_visual_dataset_20260914_214413/selected_cases.json)、
  [检查结果](reports/sim_visual_dataset_20260914_214413/validation.json)。
  报告目录保留快照 SHA256、只读统计和绘图脚本，便于复核。

## 核心原则

- 已下载并处理的采集任务直接复用，不自动重新下载、解包或处理；本地人工 `map_name` 和裁剪结果
  必须保留。已有任务未通过完整性检查时停止并报告，不能用上游重新导出来覆盖现场。
- 预览与训练使用不同的匹配策略。预览优先连续、易检查；训练优先时间同步和标签可信度。
- 训练样本应以有效占据图所在的 Meta 行为锚点，再从各路视频中检索最近的实际 PTS 帧。
- 不要从连续视频的任意时刻反查并复用旧占据图，否则会产生重复或过时的训练标签。
- 当前 Meta 中的 `actual_vel` 不准确，训练读取和同步过滤均忽略速度字段。
- 位姿可以在可靠的局部邻域内插值或短距离外推；占据图不能插值，也不能用上一张图填补断档。

## 权威字段

训练主入口是每个数据集的 `frames.jsonl`：

- `ts` / `ts_ms`：Meta、位姿、占据图与视频时间轴的主对齐键。
- `pose`：独立位姿流，用于轨迹和估算相邻时刻的平移、航向变化。
- `grid_valid`：该 Meta 行是否包含有效占据图。
- `grid_png`：占据图相对路径。
- `grid_pose`：占据图对应的 ROS 同步位姿。
- `map_name`：小车在该帧所在的地图，值域固定为 `P_map`、`B9_map`、`B10_map`、
  `Lift_map`。人工标注的单图数据集可以全文件统一写入；上游导出的跨图数据则以逐帧
  `map_name` 时间线为权威，相邻值改变是训练分段边界。未标注、枚举外值或时间线缺口
  不得在未明确回退来源的情况下进入依赖地图条件的训练流程。

## 任务发现与多子任务导出

2026-08-28 同步算法交接工具时确认：

- 完整时间窗的任务发现使用
  `GET /api/annotation-tasks/ids?from=<start>&to=<end>&robot=<optional>`。`from` / `to` 可为 unix 秒或
  日期时间；未写时区按 `Asia/Shanghai`，只给日期时包含当天整天。响应
  `truncated=true` 时必须缩小时间窗，不得把截断结果当作全量。
- 导出的 `meta_<task_id>_all.zip` 是多 bundle 容器；读取时必须按包内
  `meta_<task_id>_<i>/video_segments.json` 分别建立子任务，并为每份使用同目录
  `export_meta.json` / `task.json` 的 `keep_windows`，不得按 basename 只取第一份。
- `pull-oss-videos.py` 展开 `all.zip` 后，所有子任务与相机仍共享一个
  `--jobs` 总并发预算（默认 4）。现有全子任务时钟策略保持不变：所有段均有完整
  `t0_hw` / `t1_hw` 才使用 `hw_ts`，否则整个子任务回退 `wall_clock`，不引入 mixed 时基。
- 下载仍以缺 URL、下载失败或任一相机未完成拼接为整体失败；保留原子文件替换、
  `--reuse-segments`、`concat_complete` 及严格 manifest 完整性契约。
- 鉴权只从本机环境变量 `SKDOS_API_KEY` 读取，仓库、命令行默认值和文档中均不得保存明文 Key。

2026-08-31 起补充统一规范：

- 导出文件名必须采用 `pull-task-export.py` 最后一行的 `EXPORT_PATH`，不得假定
  `sub_task=all` 一定返回 `_all.zip`。单子任务响应允许返回 `_1.zip`，也允许核心文件直接位于 ZIP
  根目录。
- 每个 ZIP 在使用前由 `normalize-meta-export.py` 交叉校验目录名、`task.json`、
  `export_meta.json`、`video_segments.json` 中的 task/subtask 归属；根级单 bundle 与嵌套多 bundle
  一律规范到 `meta/unpacked/meta_<sub_task_id>/`。缺核心文件、空 `frames.jsonl` 或归属冲突时不得
  覆盖已有规范目录。
- 相机集合以各子任务实际 `video_segments.json` 和完成 Manifest 为准，不再把六路作为下载和训练
  发现的硬条件。配置 `camera_ids` 仅作为 Robot-U2-V1 推荐集合及稳定展示顺序；实际相机全部下载、
  拼接成功即可进入训练。集合不一致时必须在下载最终汇总、batch/run 文档和验证报告中记录
  `video_camera_set_nonstandard` warning，不得用补黑、复制旧帧或假视频伪造缺失相机。
- `download-task.sh` 无论成功失败都必须在日志最后输出最终汇总，集中列出实际归档、规范 Meta、
  视频目录及所有 warning，避免根级 ZIP 或非推荐相机集合只出现在中间日志而被忽略。

同步验证结果：

- 算法工具 Python 回归 21/21 通过，其中包含 `all.zip` 两子任务展开、完整
  FFmpeg / manifest 输出、环境变量鉴权、URL 编码和 `ids-only` 输出。
- 真实 `meta_20260820180215WDK_all.zip` 正确展开 5 个非空子任务，每份均命中
  各自 `export_meta` 时间窗；`meta_20260827180935lmd_all.zip` 正确展开 2 份，
  其 `video_segments.json` 源数据本身为空，读取器仍保留了子任务身份和时间窗。
- Node 语法、Bash 语法、CLI dry-run、明文 Key 扫描和 diff 空白检查通过；项目
  Vitest 53/53 通过，TypeScript 检查与 Vite 生产构建通过。

占据图训练样本至少要求：

```text
grid_valid == true
grid_png != null
grid_pose.valid == true
grid_pose.match == "ros_grid_sync"
```

占据图对齐只使用 `grid_pose`；不要用独立 `pose` 替代 `grid_pose`。独立 `pose` 仅用于轨迹或时间差对应的运动量估算。

## 从占据图检索视频帧

对每一条有效占据图 Meta，使用其 `ts` 计算视频本地时间：

```text
video_time = meta.ts - keep_window.from + camera_offset
```

然后在每路相机视频中按实际 PTS 选择时间差最小的帧。不要只用 PNG 文件名、`stamp_ms`、固定帧号或 `ts_cloud` 代替 `ts`。

当前连续视频通常为 15 FPS，时间轴量化误差理论上不超过约 33.3 ms。实现仍应读取实际视频 FPS、起始 PTS 和帧 PTS，不应永久写死为 15 FPS。

每个输出训练样本建议保存：

```text
dataset_id
meta_ts / meta_ts_ms
grid_png
grid_pose
camera_id -> video_path / frame_index / pts / delta_ms / camera_offset_ms
estimated_position_delta_m
estimated_yaw_delta_deg
filter_result / reject_reason
```

## 当前推荐过滤参数

完全忽略 `actual_vel`，通过时间差和相邻 `pose` 变化过滤：

```text
abs(video_pts_absolute - meta.ts) <= 0.05 s
estimated_position_delta <= 0.05 m
estimated_yaw_delta <= 3 deg
local_pose_neighbor_gap <= 0.5 s
```

位姿变化估算优先使用视频时刻方向上的相邻有效 `pose`；若该方向缺失或断档，可使用另一侧相邻位姿做很短距离的线性外推。没有可靠相邻位姿时丢弃样本。

不要为了增加样本量而放宽并复用占据图。Meta 断档、无效 `grid_pose`、缺失 PNG 或视频帧无法可靠对齐时，直接丢弃并记录原因。

## 训练数据清洗分层

训练清洗不直接破坏原始下载数据，而是生成带版本号的样本 manifest。每个候选样本保留原始数据集、时间戳、清洗配置版本、质量指标、最终决策和原因。清洗分为三层：

1. **硬过滤**：数据已经无法形成可信监督，例如文件损坏、时间不同步、位姿断档或跳变、标定不一致、占据图无效或陈旧、未来轨迹标签不完整。
2. **软质量标记**：样本仍有训练价值，但存在模糊、曝光、局部遮挡、单路相机降质等问题。默认保留质量分数供采样或损失加权，不因单个启发式指标直接删除困难场景。
3. **采样去偏**：对连续近重复直行帧、长时间静止帧和高频常见场景降采样；对转弯、窄通道、动态障碍、启停和恢复等稀有但有效样本提高采样权重。

建议至少使用以下 `reject_reason`，并分别统计数量和时长：

```text
schema_invalid / timestamp_non_monotonic / duplicate_timestamp
video_missing / video_corrupt / video_gap / video_frozen / video_sync_error
pose_invalid / pose_gap / pose_jump / pose_frame_mismatch
grid_missing / grid_corrupt / grid_stale / grid_schema_mismatch
calibration_missing / calibration_mismatch
target_horizon_incomplete / boundary_idle_trimmed / unsafe_demonstration
```

## 首尾静止段裁剪

只裁剪任务边界处明确属于采集冗余的连续静止段，不自动删除任务中间的停车、避障等待或重新起步。端到端策略仍需要学习减速和停车，因此保留进入静止状态前的刹停过程、重新起步过程，以及少量带明确 `stop` 语义的静止样本。

静止检测使用独立 `pose` 的时间差分，不使用当前不准确的 `actual_vel`。第一版建议参数如下，后续应按不同定位系统的静态噪声重新标定：

```text
pose_smoothing_window = 1.0 s
motion_measurement_window = 2.0 s
stationary_position_delta <= 0.05 m
stationary_yaw_delta <= 1 deg
moving_position_delta >= 0.10 m OR moving_yaw_delta >= 3 deg
stationary_persistence >= 3.0 s
moving_persistence >= 1.0 s
transition_context_to_keep = 1.0 s
```

- 对位置使用滑窗中值或鲁棒拟合，对 yaw 先按角度环绕展开；单点定位毛刺不能改变状态。
- 静止与运动阈值之间是“不确定区”，沿用上一状态形成迟滞，避免在阈值附近反复切换。
- 只有从数据起点连续到首次可靠运动、或从最后一次可靠运动连续到数据终点的静止状态才可裁剪。
- 边界静止不足 3 秒时不裁剪；检测到静止后再次运动时，之前的等待不能作为尾部冗余删除。
- 整段都静止时不直接当普通负样本批量加入训练，应标记为无任务运动并进入人工抽检或整段拒绝。
- 位姿不动但多相机有一致光流时，优先判断为定位冻结而不是真实停车；位姿在动但某路画面或占据图完全不变时，分别标记相机冻结或占据图陈旧。

对于需要输出动作或未来轨迹的模型，建议每个真实停车事件至少保留停止前 1 秒、停止后 1–2 秒，并对更长的内部静止段降到 1 Hz 或设置每个事件的最大样本数。这样既去掉冗余，又不会让模型缺失“应该停”的监督。

## 其他必须检查的质量维度

### 时间、完整性与标定

- 检查 Meta 时间戳严格递增、重复时间戳、异常间隔和各路视频 PTS 的连续性；跨断档不能插值成连续轨迹。
- 固定多视角模型默认要求所有必需相机在同一锚点均有可靠帧。缺失视角不能静默补黑或复用旧帧；若模型明确支持 `view_mask` 和缺视角增强，才可带掩码保留。
- 除单帧 `50 ms` 同步门限外，还要估计每路相机的固定时钟偏移和漂移。墙钟数据在完成事件相关性或硬件时间校正前应保留同步风险标记。
- 每个数据片段记录相机内参、外参、图像方向、裁剪缩放方式和版本。标定版本变化时分段，不能把未知或不一致标定混在一个样本中。

### 图像与视频

- 硬过滤解码失败、整帧黑屏/花屏、长时间重复帧和明显错误分辨率；冻结检测应使用帧哈希或图像差异并结合车辆运动判断。
- 为模糊、过曝、欠曝、眩光、雨滴/污渍、镜头遮挡和严重压缩建立每路相机质量分数。单路轻度问题通常做软标记，多路同时失效才硬过滤。
- 所有视角必须执行同一时刻和几何一致的数据增强；会改变左右语义、相机顺序或轨迹坐标的增强必须同步修改标签。

### 位姿与轨迹监督

- 检查有限数值、坐标系和单位，并用相邻位姿计算速度、角速度、加速度和曲率的物理上限。定位重定位造成的位置/yaw 跳变应切段，不能平滑成真实运动。
- 若监督目标是未来轨迹，先把未来位姿变换到当前车体坐标系，再检查完整预测时域。片段尾部不足目标 horizon 的样本直接丢弃，不用末值填充。
- 相同视觉场景在不同目标下可能对应相反动作。模型必须有目标点、路线或高层指令作为条件；若数据没有该条件，应避免把动作含糊样本当成唯一正确标签。
- 碰撞、人工接管、卡住、错误路线和明显振荡要单独标记。行为克隆的正样本默认排除不安全动作，但可留作失败检测、对比学习或恢复策略数据，不能与正常专家行为混为一类。

### 占据图

- 除 `grid_valid/grid_pose` 外，验证 PNG 可解码、尺寸、分辨率、原点、坐标方向、像素类别和值域一致。
- 对连续占据图做哈希/差异检测。车辆明显运动而占据图长时间完全相同，或 `cells_count`、占用率、未知区域比例出现异常突变时，标记 `grid_stale` 或异常样本。
- 不因障碍密集、动态物体多或视野困难而简单过滤；这些通常是有价值的长尾场景。只有确认标签错误或传感器失效时才硬过滤。

### 去重、采样与数据集划分

- 先按时间邻近、图像感知哈希、位姿邻近和占据图哈希识别近重复样本，再按运动类型和场景分层采样，避免直行或静止数据压倒转弯和避障数据。
- 统计每路相机、速度/角速度区间、曲率、障碍密度、昼夜/天气、场地和任务类型的覆盖率；清洗后仍要检查长尾是否被误删。
- 训练/验证/测试必须按任务、连续片段、路线或场地分组切分，不能随机打散相邻帧，否则近重复画面会造成数据泄漏和虚高指标。
- 自动清洗后抽检“保留、硬拒绝、阈值附近”三类样本，并输出每个原因的数量、时长、场景分布和阈值敏感性。只有总体保留率不能证明清洗正确。

## 雷达教师伪标签处理约定与 Pilot 验证

2026-08-26 核对当前真实导出、地图资产和速度模型服务后，确定第一版处理约定如下；同日已在
`processing/training-data-builder/` 实现，并完成 12-case pilot。全量生成仍需等待人工验收满意。

### 路线来源与稀疏化

- 对 `planned_route` 为空的 teleop 数据，按时间顺序使用独立 `pose` 流生成实测全局路线；
  `grid_pose` 仍只负责当前占据图和教师请求位姿的对齐，二者不得互换。
- 在子任务、地图/标定变化、时间不单调、可靠位姿断档或方向含糊的倒车处切段，不跨断点插值或
  连线。对短时间相邻位姿的坐标跳变采用更严格策略：在 `2 s` 配对窗内，平移超过 `2 m`、表观速度
  超过 `3 m/s`、yaw 单步超过 `45 deg` 或 yaw rate 超过 `2 rad/s` 时，保留跳变前一帧，并以
  `pose_jump_tail_discarded` 丢弃跳变帧及该子任务所有后续帧，禁止把坐标系重置后的数据作为新路线。
  长时间数据缺口只触发普通切段，不单凭跨缺口位移触发整段尾删。内部静止等待可以保留为样本，
  但重复静止位姿不作为路线密集点。
- 第一版先对有限 XY/yaw 做局部鲁棒平滑和 yaw 展开，再去掉静止重复点；用 RDP
  `epsilon=0.10 m` 简化路线。以 1 m 距离窗内航向变化 `>=8 deg` 标记转弯，转弯前后各
  1 m 内最大点距 `0.4 m`，近直线最大点距 `2.0 m`。简化后相对稠密路径的横向误差应
  `<=0.10 m`，局部切向误差应 `<=5 deg`；切向误差使用有序局部投影和 `1.5 m` 切向窗，
  排除因未来路线不足而不会生成训练锚点的末尾 `2.0 m`。路线级任一门槛不通过时，以
  `route_simplification_error` 拒绝该路线候选；参数与结果写入版本化路线 manifest。
- 一条连续片段生成一条稳定的稀疏有序路线。每个训练样本和路线图只使用当前位置及其后的
  forward suffix，不把已走过的前缀画入条件。教师服务按同一路线的时间顺序调用，避免自交路线处
  无序投影。当前 `grid_pose` 到稀疏路线的投影距离超过 `0.30 m` 时拒绝打标。

### 地图与路线图

- 当前教师静态地图映射固定为：`P_map -> dufu_community_map`、
  `B9_map -> 2_lifts_2_units`、`B10_map -> 2_lifts_6_units`。
- 当前教师没有 `Lift_map` 静态地图或别名。第一版必须以 `teacher_map_unsupported` 明确拒绝，
  不能静默映射成 B9/B10，也不能默认切换到只使用局部占据图的兼容模式。
- 路线图使用与教师相同版本的静态地图和路线，建议保存以 `grid_pose` 为中心、
  `20 m x 20 m`、`0.05 m/cell` 的机器人对齐裁剪。占据 mask、forward-route mask、
  世界坐标到裁剪坐标的仿射变换分别无损保存；RGB、缩放和“车头朝上”的预览都是可重建派生物，
  不作为唯一权威数据。教师内部约定为车体前方 `+x` 指向图像右侧；若学生输入使用车头朝上，
  必须用一个固定额外旋转并把该约定写入 preprocessing version。
- 训练权威裁剪直接使用教师 `model_server/maps/*.npz`，不要用 PCD 预览图或经过 LANCZOS 缩放的
  渲染结果。对输出像素 `(row,col)`，教师坐标公式为：

```text
body_x = (col - 200) * 0.05
body_y = -(row - 200) * 0.05
world_x = pose_x + cos(yaw)*body_x - sin(yaw)*body_y
world_y = pose_y + sin(yaw)*body_x + cos(yaw)*body_y
map_col = floor((world_x - map_origin_x) / map_resolution + 1e-7)
map_row = map_height - 1 - floor((world_y - map_origin_y) / map_resolution + 1e-7)
```

  地图外区域按障碍处理。路线先按 0.05 m 等弧长加密，再用逆位姿变换：

```text
local_x =  cos(yaw)*(route_x-pose_x) + sin(yaw)*(route_y-pose_y)
local_y = -sin(yaw)*(route_x-pose_x) + cos(yaw)*(route_y-pose_y)
col = round(200 + local_x / 0.05)
row = round(200 - local_y / 0.05)
```

  只保留单调路线投影进度之后的点，并按当前 `route_width_cells=3` 扩宽。这样生成的两通道
  `static_occupancy + binary_forward_suffix` 与教师输入一致；车头朝上的查看图是在该权威结果上
  固定逆时针旋转 90°，不能另写一套坐标换算。
- 实际教师局部占据输入还要执行 `local_static_obstacle_union_v1`：从 `400×400` 静态裁剪中只取中心
  与原始局部观测对应的 `200×200` 区域，将两者按障碍并集融合。静态地图只能在这 `10 m×10 m`
  局部范围内补入人工预标障碍，不得扩大局部观测范围。落盘同时保留 `raw_local_occupancy`、
  `static_local_occupancy` 和权威融合结果 `local_occupancy`；预览用橙色标出仅由静态地图新增的障碍。

### 初始线角速度状态

> 本小节以下内容记录的是已淘汰的 `vnav_teacher_v1 / Rule-7` 方案。2026-08-31 起，正式训练数据改用
> 后文的 `vnav_teacher_rule10_history_v2`：20% 离散零速，其余样本从实际 pose 变化估速；旧版随机
> 曲率限速初态不得再用于训练或续做标签。

- 每个有效视觉/占据图锚点生成一组可复现的随机初始状态。随机种子由
  `dataset_id + meta_ts + state_sampler_version + teacher_version` 派生，重复导出必须得到相同状态。
- 在教师按 0.05 m 加密后的 forward route 上，用当前位置之后 1 m 内的
  `heading_delta / arc_length` 鲁棒中位数估计有符号曲率 `kappa0`，并限制到当前 Rule-7 输入域
  `[-2,2]`。直行时 `kappa0` 近似 0，左/右转保留正负号。
- 全局最大线速度为 `0.75 m/s`，最大角速度为 `0.5 rad/s`。教师当前配置还会把室内 profile
  最大线速度限制为 `0.50 m/s`。按部署 `curvature_only` 分支计算：

```text
v_profile = 0.75 (室外) 或 0.50 (教师判定室内)
v_curve = v_profile / (1 + 1.2 * max(0, abs(kappa0) - 0.2))
v_allowed = min(v_curve, 0.5 / max(abs(kappa0), epsilon))
```

- 以 20% 概率令 `v0=v_allowed`；其余 80% 从 `[0,v_allowed]` 均匀采样。然后设置
  `w0=kappa0*v0`，仅为数值安全裁剪到 `[-0.5,0.5] rad/s`。不要独立随机线速度和角速度，
  否则会生成转弯方向相反或曲率不一致的状态。
- 保存 `initial_linear_mps / initial_angular_rps / initial_curvature /`
  `allowed_linear_mps / sampling_branch / rng_seed / state_sampler_version`。连续均匀分布几乎不会
  产生精确 `v0=0`；如果后续要求固定比例的完全静止状态，应新增带版本的离散零速分支，不能
  静默改变上述 20/80 分布。
- 当前指定 YAML 使用 PyTorch `curvature_only` Rule-7：网络读取 `reference_curvature`，但
  `current_linear_mps/current_angular_rps` 在该分支只被记录，不直接改变绝对速度预测。因此同一
  `kappa0` 下不同 `v0` 会得到相同原始教师绝对速度块，但相对初态的 `delta_v/delta_w` 不同。
  如果需要“绝对教师输出随当前速度变化”，必须改用真正消费 `(v,w,curvature)` 的 Rule-8/Rule-10
  backend 或修改教师，数据处理层不能虚构这种依赖。

### 教师请求

- 导出 PNG 是 `0=障碍物、255=非障碍物`；原始局部图必须先转换为 `int8` 的
  `100=障碍物、0=可通行`，且不得再次翻转 Y 轴，再与同值域的静态中心裁剪做障碍并集。教师、
  输入 hash、确定性复查和碰撞回放必须使用同一张融合局部图。
- `GridMeta` 使用原帧的 `200 x 200 / 0.05 m / origin=(-5,-5)` 等字段并声明
  `robot_aligned`；教师位姿只使用 `grid_pose.x/y/yaw_rad`，全局路线使用上述独立 pose
  生成的同坐标系有序 XY。
- 没有经过坐标对齐验证的 planner `node_labels` 时，`accelerate` 与路线等长并全部置零。
  当前 teleop 数据的 `planned_route` 为空，不从其它字段猜测加速事件。
- 不使用已知不准确的 `actual_vel`。教师请求的
  `current_linear_mps/current_angular_rps/reference_curvature` 使用上述可复现随机初态；独立 pose
  估计的真实运动只保存为审计字段，用于检查采集行为和伪标签差异。

### Rule-10 动态多帧教师接口核对（2026-08-31）

新 checkpoint `checkpoint_ep023000.pt` 必须通过无状态
`POST /teacher/infer_batch` 重新生成标签。请求体不是 JSON，而是
`Content-Type: application/octet-stream` 的 protocol-v1 二进制 batch；调用方应直接使用
`TeacherHistoryObservation`、`TeacherInferenceSample` 和
`ModelClient.infer_teacher_batch()` 封包，不自行拼协议。

每个样本实际提交的当前观测为：

- `occupancy`：`int8[200,200]` 车体对齐局部占据；服务内部按“非零即障碍”二值化。
  `GridMeta` 必须声明 `alignment=robot_aligned`；Rule-10 的历史对齐和模型网格实际固定为
  `0.05 m/cell`，因此仍使用 `resolution=0.05, origin=(-5,-5)`，不得仅因协议未显式拒绝
  其它 resolution 就改变网格定义。`grid.stamp_s` 是当前帧时间。
- `pose=(x,y,yaw)`：当前 `grid_pose`，须与路线、历史 pose 使用同一世界坐标系。
- `global_path`：至少 2 个不重合的有序世界 XY；`accelerate` 必须与路线等长。
  当前 Rule-10 网络的路线通道不直接消费 `accelerate`，但它仍是协议和路线预处理的必填字段；
  无可靠节点标注时保持全 0。
- `metadata`：`sample_id` 用于响应回填，`graph_name` 选择静态地图，
  `current_linear_mps/current_angular_rps/reference_curvature` 是当前执行状态；速度字段也兼容
  `current_v_mps/current_w_rps`，缺失时默认 0。`initial_indoor` 的布尔值不决定室内外；
  室内外由服务结合静态地图和当前 pose 重新判定。只有特殊字符串
  `initial_indoor="local_occupancy_static"` 会改为用请求局部图构造静态画布。
- `history`：0–32 个 `TeacherHistoryObservation`，每个只有历史
  `occupancy int8[200,200] + pose(x,y,yaw) + stamp_s`，不提交历史速度。时间戳必须严格递增
  且全部早于当前 `grid.stamp_s`；历史和当前图要使用相同的值域、融合和坐标预处理版本。
  重标注时只能取同一连续路线/地图/坐标段的前帧，建议保留至少覆盖当前前
  `1.0 s` 的 5 Hz 序列，不跨断点补历史。

服务不会将所有历史帧直接堆为网络通道。它先为每个样本创建独立队列，以当前时间为基准分别选择最接近
`1.0 s` 前和 `0.5 s` 前的历史帧，再用历史/当前绝对 pose 做最近邻刚体对齐。
序列中相邻 pose 平移跳变超过 `2.0 m` 或 yaw 跳变超过 `1.0 rad` 会清空该样本已注册的旧历史。
最终一次 forward 的真实输入形状为：

```text
far   [B,2,200,200] = 400x400 静态地图 2x 平均下采样 + 有方向的 forward route
local [B,4,200,200] = 当前局部占据 + 1.0s 对齐历史 + 0.5s 对齐历史
                        + 由当前 (v,w) 积分的未来 3s 车体 footprint
state [B,3]         = current_linear_mps, current_angular_rps, reference_curvature
```

这一版模型确实消费初始线角速度：它们同时进入 `state` 和 future-footprint 通道。
室内且当前状态被分类为 ARC 时，服务在进模型前会将 `v/w` 同时除以 `2/3`，
恢复到训练速度域；响应中的 `raw_prediction.model_input_execution` 才是网络实际收到的
`v/w/curvature`，必须随标签保存。

顶层成功响应包含 `status/batch_request_id/model_id/sample_count/forward_passes/`
`inference_ms/predictions`。每个 prediction 包含输入顺序 `index`、`sample_id`、
`commands` 和完整 `diagnostics`。continuous Rule-10 的 `raw_prediction` 中：

- `raw_curve_values` 和 `projected_curve_values` 均为 5 个三元槽；
  `selected_action` 取 `CURVE/SPIN_LEFT/SPIN_RIGHT`，`action_logits=null`。
- `CURVE` 返回 5 条速度命令；室外每槽 `0.2 s`，室内执行 `v/w` 同时缩放为 `2/3`
  且每槽 `0.3 s`。自转返回 1 条 `(v=0,w=±0.5,duration=1.0 s)`。
- `occupancy_history` 返回提交数、含当前帧队列深度、两个实际选中 age、reset 原因和
  pose 对齐标志。`execution_constraints_applied=false` 表示教师输出未套用在线五步执行锁或
  pending-spin 约束。

默认服务上限为 batch `64`、每样本历史 `32`、请求体 `256 MiB`；客户端协议编码器的绝对 batch 上限虽是 256，当前 YAML 会在服务端按 64 拒绝更大请求。
HTTP/协议/尺寸错误返回非 2xx JSON，核心字段为 `status=error`、`error`、`detail`。

2026-08-31 实测以 CUDA:0 启动指定配置，官方 4-sample 请求得到
`sample_count=4 / forward_passes=1 / inference_ms=81.987`；随后 `/health.teacher_batch` 为
`batch_request_count=1 / sample_count=4 / forward_count=1`，而在线 execution/occupancy history 深度仍为 0，
pending spin 仍为 null。在同一 batch 内保持占据、路线、pose、历史和曲率完全一致，仅将
`(v,w)` 从 `(0.1,0.025)` 改为 `(0.4,0.1)` 时，室内恢复后的模型状态分别为
`(0.15,0.0375,0.25)` 和 `(0.6,0.15,0.25)`，首槽 raw 预测由
`(-1.975444,0.012879,-0.495054)` 变为 `(-1.999992,0.000288,-0.499997)`，证明速度条件实际生效。
测试完成后 8103 已关闭。

本轮重标注已将流水线升版为 `vnav_teacher_rule10_history_v2`，实现约定如下：

- 同一地图/连续路线段内保留当前帧之前最多 `1.5 s / 32` 帧，并要求能在 `±0.25 s` 内分别匹配
  教师实际消费的 `1.0 s` 和 `0.5 s` 历史；同时最老帧 age 必须 `>=1.0 s`。仅有 0.75–1.0 s
  之间的近邻仍不合格，因为教师 `_select()` 在 1.0 s 目标早于队首时会把 older 通道置零，而不是
  选择较年轻近邻。时间戳严格递增且早于当前帧。当前与每一张动态历史图
  都按各自 `grid_pose` 重新做 `local_static_obstacle_union_history_v2`，不能把当前静态裁剪复用到旧位姿。
- 初态使用 `pose_velocity_zero20_v2`。由稳定种子确定的 20% `static_repeat` 样本令
  `v=w=0`，把当前融合占据和当前 pose 按历史时间戳重复提交；其余 `dynamic_history` 样本使用当前
  前 `0.5 s` 内实际 pose 差分的鲁棒中位数估计前向线速度、角速度和横向速度，并将教师实际输入的
  `v/w` 限制到模型 profile 范围。原始估计、裁剪值、是否触发裁剪和分支均写入样本。
- RGB 以教师网络的 `1.0 s` 实际历史窗口为准。动态分支按视频真实 PTS 从该窗口选择已有帧，任意
  相邻帧间隔不少于 `0.1 s`（频率不超过 10 Hz）；Manifest 只索引实际图片文件、PTS 与相对时间。
  零速分支用当前 RGB 单图按 10 Hz 的名义时间点重复索引，所有实际相机遵循同一规则。
- 标签按最多 32 个样本组成一次 `/teacher/infer_batch`，一次请求必须报告
  `forward_passes=1`；单个坏样本只在整批失败时降级为单样本重试。服务在线 `/infer` 的历史、执行
  状态和 pending spin 不参与也不受污染。
- `inputs.npz` 增加 `teacher_history_occupancy / teacher_history_pose /
  teacher_history_stamp_s`；`sample.json` 保存历史文件/hash/pose/时间戳、实际 pose 估速、batch 请求
  provenance 和完整教师 diagnostics；run 级 `rgb_history_manifest.jsonl` 保存实际相机 RGB 历史索引。
- 历史融合使用最多 256 项的有界只读缓存，键包含原始栅格路径、历史 pose、静态地图 hash 和融合
  版本，既避免同一历史帧跨样本重复计算，也避免全量任务随样本数无界占用内存。

修正版 12-case pilot 对这一门槛做了真实服务复核：`older_selected_age_s=0.914–1.150 s`、
`recent_selected_age_s=0.404–0.733 s`，两通道空值均为 0；12/12 标签确定性匹配、每样本
`forward_passes=1`，深度验证错误/警告均为 0。流水线回归为 19/19。

### Rule-10 v2 全量重生成结果（2026-08-31）

首批 7 个任务生成后，依赖任务完成 Meta 规范解包、视频恢复及可变相机集合支持；随后只对新就绪目录
补生成。当前 11 个采集目录中有 9 个成功 v2 full run，正式结果：

```text
task                  candidate  accepted  collision_rejected
20260818193938Axg          2150      2148                   2
20260819203318qFl          1491      1464                  27
20260820180215WDK          1921      1920                   1
20260821114524n4Q           302       302                   0
20260824161657Li2            93        93                   0
202608251919001QS           926       867                  59
20260826154539RI5            33        33                   0
20260827180935lmd            79        79                   0
20260828154654XXz          1416      1374                  42
TOTAL                      8411      8280                 131
```

- 1,686 个 `static_repeat`（20.36%）、6,594 个 `dynamic_history`。另有 10 个动态样本的真实 pose
  估速自然为精确 `v=w=0`，故全数据精确零速为 1,696（20.48%）。动态分支线/角速度裁剪比例为
  36.11% / 0.409%。地图覆盖 `P_map=7901 / B10_map=379`。
- 268 次 batch 覆盖 8,411 个槽位，全部 `forward_passes=1`，没有 batch 失败降级。接受样本的
  older/recent 历史通道空值均为 0；age 分别为 `0.750–1.249 s / 0.275–0.750 s`。教师历史
  3–7 帧，中位数 5；窗口 `1.000–1.500 s`，中位数 1.360 s。
- 标准六路 accepted 6,906 个，`20260828154654XXz` 使用声明完整的五路
  `cam0/cam1/cam2/cam3/cam6` accepted 1,374 个；后者明确保留
  `video_camera_set_nonstandard` warning，没有伪造 `cam5`。共 48,306 组样本/相机 RGB 历史，
  每组 8–11 个索引；动态窗口 0.933–1.000 s，最高约 7.5 Hz。同步/估算位姿差最大为
  `33.330 ms / 0.04987 m / 1.4789 deg`，通过既有硬门槛。
- 深度验证逐个打开 8,280 个 sample/NPZ/RGB 历史并重算占据并集、历史/零速/估速/碰撞约束，
  9/9 run、0 错误；唯一 warning 是上述五路非推荐相机集合。当前训练构建回归 21/21 通过。权威统计位于
  `/mnt/chengchangxu/data/visual_nav_training/vnav_teacher_rule10_history_v2_validation.json`，详细报告见
  `processing/training-data-builder/rule10_history_v2_report_20260831.md`。
- 旧 `vnav_teacher_v1`、旧汇总目录和旧批次日志共删除 16,101,846,275 bytes，不可恢复。采集源
  删除时曾验证当时采集源聚合 hash 不变；之后用户授权的目录规范化/视频恢复使 source 快照更新为
  11 个任务、26,165 文件、3,646,659,601 bytes。本轮正式补生成前后，四个处理目录各自的文件数、
  字节数和逐文件聚合 SHA-256 完全一致，训练生成没有改动采集源。
- 最终 dry-run 为 `discovered=11 / already_completed=9 / pending=1 / incomplete=1 / failed=0`。
  `20260819181014HWI` 布局完整但 862 行经质量过滤后 0 candidate；`202608181754378l1` 只有规范
  Meta、没有视频 Manifest/MP4。两项都未伪造输入或放宽门槛。

#### 四个未就绪采集目录根因复核与修复前状态（2026-08-31）

- `meta_unpacked_missing` 是批处理布局检查的汇总状态：只有
  `meta/unpacked/meta_*/frames.jsonl` 至少存在一份时才算 Meta 就绪，并不等价于远端或 ZIP 中没有
  Meta。`202608181754378l1` 与 `20260824161657Li2` 各自保留了一份完整、`unzip -t` 通过的
  `meta_<task_id>_1.zip`，分别包含 `346 / 1,979` 条 frame、`345 / 1,979` 张有效栅格及六路
  `cam0/cam1/cam2/cam3/cam5/cam6` 视频段；但本地 `unpacked/` 和 `videos/` 为空。
- 上述两项由单子任务导出的文件名/布局契约不一致造成：导出器服从 HTTP
  `Content-Disposition`，把 `sub_task=all` 的单子任务响应保存为 `_1.zip`，ZIP 内容位于根目录；
  `download-task.sh` 却固定查找 `_all.zip` 并假定其中已有 `meta_<sub_task_id>/` 层级，因此在解压前
  就退出。修复时必须同时采用实际返回文件名，并把单子任务根目录内容规范化到
  `unpacked/meta_<sub_task_id>/`，只改其中一项仍不能满足训练发现契约。
- `20260819181014HWI` 的两个 Meta、grids 和两个视频目录当前全为空。历史只读清单曾记录两段
  `frames.jsonl/task.json/video_segments.json` 完整存在，之后本地内容被清空；当前重新导出的
  `_all.zip` 已通过完整性检查，`_1/_2` 分别有 `459 / 403` 条 frame，且两段均声明完整六路相机。
  因而这不是采集端缺 Meta，而是本地落盘内容曾被删除或清理。现有目录和训练批次日志没有记录
  操作者/进程，不能可靠归因到预览页删除、人工命令或其他清理程序。
- `20260828154654XXz` 是真实的采集规格不匹配：全部 8 个子任务的 `task.json`、
  `video_segments.json` 与视频 Manifest 都只包含
  `cam0/cam1/cam2/cam3/cam6`，每段 `camera_count=concat_ok=5`、五路拼接均成功，但统一缺少
  训练配置要求的前广角 `cam5`。错误只显示 `_1` 是因为布局检查遇到第一个不满足六路约束的子任务
  后立即返回，并不表示 `_2.._8` 合格。
- 当前看板再次核对时，`20260819181014HWI` 与 `20260824161657Li2` 的连续视频分别为
  `12/12`、`6/6` ready，可在修复单子任务解包契约后刷新导出并重新下载。
  `202608181754378l1` 已不在任务列表且导出接口返回 404；其旧 ZIP 仍可恢复 Meta，但其中约一小时
  有效的视频签名 URL 已过期，标准下载链路还需要恢复任务记录或重新取得 OSS 授权，不能仅解压 ZIP
  就宣称六路训练源完整。
- 后续规范已调整为“实际相机集合完整即可处理，非推荐集合显式告警”。因此
  `20260828154654XXz` 声明且完整下载的五路 `cam0/cam1/cam2/cam3/cam6` 不再被当作下载不完整，
  训练同步、解帧、预览和验证均按实际集合动态执行；缺少推荐 `cam5` 会保留 warning。旧结论中的
  “六路硬契约”到此废止，但仍禁止复制画面、空白图或静默补路数。
- 本次实现增加 Meta 规范化器、实际导出路径协议、下载末尾汇总、动态相机批扫描/同步/解帧/预览/
  验证以及对应回归。45/45 Python 回归与 Python/Bash 语法检查通过。
- `20260819181014HWI` 已刷新并规范出 `_1/_2` 两个 Meta bundle，实际六路视频 `12/12` 拼接完成；
  `20260824161657Li2` 的服务端实际响应仍为根级 `_1.zip`，已规范出一个 bundle，六路视频 `6/6`
  拼接完成。两者 ZIP 和原始 segments 均按默认策略保留。
- `202608181754378l1` 的旧 `_1.zip` 已恢复为
  `unpacked/meta_202608181754378l1_1/`；标准入口再次确认远端导出为
  `404 annotation task not found`，因此仍缺视频 Manifest。日志末尾明确记录“规范 Meta=1、视频=0、
  视频尚未下载或拼接”，没有修改旧 ZIP，也没有伪造视频。
- `20260828154654XXz` 的八个五路子任务现均为 pending，各自带
  `video_camera_set_nonstandard` warning。实际只读上下文构建成功得到 `8` 个 context、`5,022` 行
  audit、`1,416` 个候选，相机集合统一为 `cam0/cam1/cam2/cam3/cam6`，证明同步和候选阶段不再暗含
  六路索引。
- 四目录最终限定 dry-run 为
  `discovered=4 / pending=3 / already_completed=0 / incomplete=1 / generated=0 / failed=0`；唯一
  incomplete 是 `202608181754378l1` 的 `video_manifest_missing`。本次未启动教师、未生成训练样本。

#### 四目录修复后的正式补生成结果（2026-08-31）

- 等待依赖任务完成后，限定正式批次只处理 HWI、Li2、XXz。`Li2` 生成 93/93 accepted；`XXz`
  生成 1,416 个候选、1,374 accepted、42 个 `teacher_rollout_collision`，五路相机 warning 保留。
  两个成功 run 分别位于 `run_20260831_214112 / run_20260831_214306`。
- `HWI` 布局检查虽为 pending，但上下文构建得到 2 个 B9 context、862 行、0 条满足最短 2 m 的可靠
  路线和 0 candidate，因此教师未被调用。首失败原因为
  `boundary_idle_trimmed=392 / route_gap=210 / pose_jump_tail_discarded=259 / grid_missing=1`；其中 `_1`
  row 200 在 0.2018 s 内跳动 0.804 m、表观速度 3.986 m/s，触发尾段保护。`_2` 经静止边界和路线
  鲁棒化后仍不足 2 m。该任务不能通过放宽门槛或复制路线凑标签。
- `8l1` 当前 346 行规范 Meta 和栅格完整，但 `videos/` 没有 Manifest/MP4，仍不能构造 RGB 观测。
- 全量验证更新为 9/9 run 通过、8,411 个候选、8,280 accepted、131 碰撞拒绝、错误 0；唯一 warning
  是 `XXz` 五路非推荐相机集合。训练构建回归 21/21；最终全目录 dry-run 为
  `already_completed=9 / pending=1 / incomplete=1 / failed=0`。
- 四个处理目录在正式生成前后逐任务的文件数、字节数和逐文件聚合 SHA-256 全部一致；详细指纹与
  全局特征见 `processing/training-data-builder/rule10_history_v2_report_20260831.md`。

#### 首批七任务四个千行级 source 拒绝原因复核

七个就绪任务的 source Manifest 共 17,209 行，其中 10,307 行被 source 硬过滤。拒绝原因采用
“首个失败门槛”互斥记账：跳变尾删早于路线检查，`route_insufficient` 早于视频/pose 同步检查，
`pose_gap` 又早于教师历史检查。因此下列数字不能理解为四种独立缺陷，也不能用它们反推缺陷交集；
四项合计 8,713 行，占全部拒绝的 84.53%、全部 source 行的 50.63%。

| 原因 | 行数 | 占全部拒绝 | 判定与解释 |
| --- | ---: | ---: | --- |
| `pose_jump_tail_discarded` | 3,091 | 29.99% | 在相邻有效 pose 的时间差不超过 2 s 时，平移超过 2 m、表观速度超过 3 m/s、yaw 单步超过 45 deg 或 yaw rate 超过 2 rad/s，即认为定位坐标系可能重置；保留跳变前一行，跳变行及同一 map 段后续全部作废。3,091 行实际只由 4 次首次跳变产生：`WDK_6=112`、`WDK_7=1535`、`n4Q_2=1067`、`1QS_7=377`。其中 `WDK_7/n4Q_2` 是约 9.8 m 的位置跳变，另两次主要是 65.67/47.86 deg 的 yaw 跳变。整尾删除是为了避免把重定位后的新坐标系与跳变前路线、速度和历史直接相连。 |
| `teacher_history_insufficient` | 2,699 | 26.19% | 当前帧之前只保留 1.5 s 内、有效且文件存在、`grid_pose.match=ros_grid_sync` 的占据帧；最老实际帧 age 必须至少 1.0 s，且 1.0 s older 与 0.5 s recent 两个目标都要在 ±0.25 s 内有可选帧。复算细分为：最老历史不足 1.0 s 1,721 行、1.0 s 附近无帧 445 行、0.5 s 附近无帧 480 行、1.5 s 内完全没有有效先前占据帧 53 行。这个过滤防止教师 older/recent 通道因缺帧变成零图或错时图。 |
| `pose_gap` | 1,502 | 14.57% | RGB 先按视频 PTS 匹配到当前 Meta 时刻；随后必须从当前独立 pose 向匹配时间方向找到下一条有效 pose，并用它估算视频与 Meta 间的真实位姿差。若该方向没有有效邻居，或邻居与当前 pose 的时间间隔超过 0.5 s，就无法可靠验证 `50 ms / 5 cm / 3 deg` 同步门槛，故拒绝。它不同于 `route_gap`：当前行可以已经有路线和投影，但缺少足够近的 pose 邻居来证明 RGB 同步可靠。 |
| `route_insufficient` | 1,421 | 13.79% | 当前 `grid_pose` 已能投影到有效稀疏路线，但投影点之后的真实 forward suffix 小于 2.0 m。常见于每条路线/断段的末尾和末端停留帧；不能复制终点或拼接断点后的路线凑长度，否则教师会在缺少真实未来路径的条件下输出伪标签。 |

当前实现先统一检查真实教师历史，再决定样本进入 20% `static_repeat` 还是 80% `dynamic_history`；所以
2,699 行历史不足样本没有机会因抽中零速单图重复而被保留。这是当前流水线的保守前置门槛，不是
教师 HTTP 响应拒绝。若希望只挽救其中确定落入 `static_repeat` 的行，需要调整采样与过滤顺序、升版
数据口径并重新生成，不能直接修改现有 Manifest 统计。

逐任务分布如下；可见跳变尾删集中于 3 个任务，而另外三类是七个任务普遍存在的时间历史、pose
邻接和路线末端约束。

```text
task                  jump_tail  teacher_history  pose_gap  route_insufficient
20260818193938Axg              0              993       598                 240
20260819203318qFl              0              681       341                 398
20260820180215WDK           1647              625       336                 345
20260821114524n4Q           1067               87        47                  22
202608251919001QS            377              277       167                 280
20260826154539RI5              0               17         6                 115
20260827180935lmd              0               19         7                  21
TOTAL                       3091             2699      1502                1421
```

### 标签、过滤与落盘

- 原样保存教师返回的完整命令块 `[(linear_mps, angular_rps, duration_s), ...]`，不写死命令
  数量或室内/室外时长。同时保存相对随机初态的 `delta_v/delta_w`；相对位姿 action chunk 可以
  用精确单轮车积分派生，但两者都不能替代原始绝对命令。
- 每条标签同时记录教师 health、模型/配置/checkpoint 标识与 hash、静态地图 hash、请求输入 hash、
  路线/预处理版本、完整 diagnostics 和请求失败原因，保证可复现且不会混用不同教师版本。
- `request_input_sha256` 覆盖包括 `recording_session_id` 在内的完整请求；另存
  `sample_content_sha256`，排除仅用于记录隔离的 session ID，用于跨 review 验证同一训练内容。
- 除既有同步硬过滤外，新增：`teacher_map_unsupported / route_gap / route_insufficient /`
  `route_projection_error / route_behind_only / teacher_request_failed / teacher_output_nonfinite /`
  `teacher_output_out_of_bounds / teacher_rollout_collision / pose_jump_tail_discarded`。片段尾部没有完整
  未来路线时直接拒绝，不复制终点凑足路径或标签时域。
- 教师命令与实测运动不一致、相邻命令突变默认作为软审计信号，不直接删除，因为教师标签本来就是
  对真实遥操作的反事实改进；只有结合地图、占据图或回放碰撞证据确认错误时才硬拒绝。
- 先为全部有效锚点打标，再通过 manifest 对近重复直行/静止样本降权，对转弯、障碍、启停样本
  提权；“路线点稀疏化”与“训练时间采样”是两个独立步骤。train/validation/test 仍按完整任务或
  连续路线分组切分，切分后再物化多视角图片、路线 mask 和标签 shard。

### 第一阶段验收

- 先选择一段 P_map 的直行+转弯和一段 B10_map 室内路线做 pilot，不直接全量打标。
- 输出保留、拒绝和阈值附近样本的多视角/占据图/地图路线/教师 rollout 联合预览。
- 验收至少检查：地图与路线坐标正确、稀疏路线误差门限通过、当前点只看到 forward suffix、
  教师重复请求在抽样子集上确定一致、命令回放无高置信碰撞、数据划分零泄漏，以及按地图、曲率、
  障碍密度、运动类型和命令区间统计的保留/拒绝分布。

> 以下从“旧 v1 Pilot”到“主项目路径修复后的追加批次”记录 2026-08-27 的
> `vnav_teacher_v1 / Rule-7` 历史结果。相关训练目录已在 2026-08-31 验证新 v2 后删除，路径不再
> 存在；数字仅用于审计旧处理决策，不代表当前可训练数据。

### 旧 v1 12-case Pilot 历史结果（产物已删除）

2026-08-27 使用独立 `http://127.0.0.1:8103` 教师完成调整后的 `review_005`，产物位于：

```text
/mnt/chengchangxu/data/visual_nav_training/20260820180215WDK/
  vnav_teacher_v1/pilot/review_005/
```

- 首次跳变检测在 `_6` 的 row 959 发现 `0.013 m / 65.673 deg` 航向坐标重置，丢弃后续 112 行；
  在 `_7` 的 row 1608 发现 `9.791 m / 98.636 deg` 坐标重置，丢弃后续 1535 行。`_1/_3/_5` 无跳变。
  五段有效候选数更新为 `294 / 35 / 392 / 757 / 1077`，每段只保留一条跳变前路线。
- 仍按 `_1/_3/_5/_6/_7 = 2/1/2/3/4` 生成 12 个单锚点 case；12/12 位于首次跳变前，教师阶段
  12/12 接受。`case_012` 已改为 `_7` 跳变前 row 1063 的有效室外路线，不再使用旧坐标原点附近片段。
- 教师请求使用原始局部占据与静态中心 `200×200` 裁剪的障碍并集。12 个 case 每例新增
  `862–17,925` 个静态障碍格，总计 50,612 格；逐样本重算的并集、中心裁剪、PNG 和 hash 全部一致，
  12 个融合输入 hash 互不重复。
- 六路实际 PTS 最大绝对同步误差 `32.073 ms`；12 个样本重复教师请求的五步命令全部一致。
  教师输出最大绝对线速度 `0.75 m/s`、最大绝对角速度 `0.456988 rad/s`，均在配置域内。
- 精确单轮车积分与 footprint 回放碰撞为 `0/12`。5 条有效路线最大横向误差 `0.097398 m`、
  最大有序局部切向误差 `4.767978 deg`，通过 `0.10 m / 5 deg` 硬门槛。
- 单元测试扩充为 9 项并全部通过；`overview.png` 为 `1860×2488`，逐 case 联合图为 `2160×1920`。
  `run.json` 仍为 `full_generation_authorized=false`，尚未执行批量生成。

### 旧 v1 全量生成历史结果（产物已删除）

用户明确确认 `review_005` 满意后，2026-08-27 完成授权全量运行：

```text
/mnt/chengchangxu/data/visual_nav_training/20260820180215WDK/
  vnav_teacher_v1/full/run_20260827_012110/
```

- 全量扫描 2555 个有效候选，接受并物化 2543 个训练 case；子片段接受数
  `_1/_3/_5/_6/_7 = 294/35/382/757/1075`。另外 12 个样本因 `teacher_rollout_collision` 明确拒绝，
  其中 `_5` 为 10 个、`_7` 为 2 个；拒绝标签和碰撞 state index 均保留在 `teacher_labels.jsonl`。
- `source_manifest.jsonl / routes.jsonl / teacher_labels.jsonl` 分别为 `5280 / 5 / 2555` 行，最终目录
  约 4.3 GB，运行耗时 `774.596 s`。所有 2543 个 case 均包含六路相机 PNG、原始/融合栅格、权威
  `inputs.npz` 和 `sample.json`，逐样本文件检查无缺失或空文件。
- 对全部已接受样本重算确认：教师融合数组为 `int8 {0,100}`，展示用融合 PNG 为黑白 `{0,255}`；
  静态中心裁剪和障碍并集逐项一致，融合 hash 可复算。2555 个 `sample_content_sha256` 全部唯一；
  静态地图为每个候选补入 `0–19,144` 个障碍格，总计 7,820,911 格。
- 六路最大绝对同步误差 `33.332 ms`；教师命令均为 5 步，最大绝对线速度 `0.75 m/s`、最大绝对
  角速度 `0.474678 rad/s`。5 条路线最大横向/切向误差仍为 `0.097398 m / 4.767978 deg`。
- `run.json` 为 `status=success`、`full_generation_authorized=true`。独立 8103 教师完成 2555 次请求后
  已停止；全量过程中未覆盖任何 Pilot 或原始数据。

### 旧 v1 训练数据检查网页契约

2026-08-27 在现有本地预览服务中新增只读训练数据检查页，默认入口为
`http://127.0.0.1:5173/#training`，默认产物根目录为
`/mnt/chengchangxu/data/visual_nav_training/`，可用 `VNAV_TRAINING_ROOT` 显式覆盖。

- 服务端只从 `<task>/<pipeline>/{pilot,full}/<run>/` 中发现同时具有 `run.json` 和
  `teacher_labels.jsonl` 的 run；摘要读取 `source_manifest.jsonl`、`routes.jsonl` 和标签 Manifest，
  不修改任何训练产物。媒体接口仅允许已接受、已物化 case 下的六路相机、`grid.png` 和
  `fused_grid.png`，拒绝任意路径和非白名单文件。
- 页面支持切换 run，按 `accepted/rejected/all`、子片段和关键词过滤，并以 30 条分页。接受样本展示
  六路实际帧、同步误差、原始/融合占据图、静态障碍补入计数、五步教师命令、grid pose、路线投影、
  初态和推理耗时；相机复用采集预览的六个方位槽位，当前 Robot-U2-V1 顺序为
  `后上/前上/前广 · 左/前下/右`。拒绝样本只展示保留在 Manifest 中的拒绝原因、碰撞 state 和诊断，
  不伪造媒体。
- 检查页直接展示落盘黑白 PNG：原始与融合预览值域均为 `{0,255}`；文案同时明确其对应教师权威
  `local_occupancy` 为 `int8 {0,100}`。页面不添加橙色差异层，橙色仍只用于 Pilot 联合预览。
- 全局教师地图本体不逐 case 复制；`sample.json.provenance.static_map/static_map_sha256` 保存源 NPZ
  路径与内容 hash。case 的 `inputs.npz` 保存以当前 `grid_pose` 为中心、车体对齐的
  `static_occupancy (400×400 int8)`、`forward_route_mask (400×400 uint8)`、
  `world_to_pixel (3×3 float64)`、`sparse_route/forward_route (N×2 world XY)` 和
  `teacher_rollout (N×4)`。网页按需从这些权威数组生成车头向上的“静态地图 + forward route +
  rollout”PNG，只放在 128 项内存缓存中，不新增或覆盖训练产物。该图是全局静态图的 20m 局部裁剪，
  不是整张全局地图的副本。
- 自动化验证为 `53/53` 通过，TypeScript 检查和 Vite 生产构建通过。对真实全量 run 的 Chromium
  验收确认首页为 `2555/2543/12`，可见 6 路 `640px` 相机、2 张 `200px` 占据图和 5 步动作；
  拒绝筛选精确返回 12 条且详情图片为 0，第二页为 `31–60 / 2543`，末例搜索精确命中
  `sample_0002543`。`1440×1000` 与 `390×844` 均无横向溢出，控制台错误、页面异常和失败请求均为 0。
- 地图路线浏览器验收对两个连续 case 的派生 PNG 请求均返回 200；自然尺寸为 `400×400`，首例逐像素
  检出静态障碍 18,917、forward route 1,921、rollout 65、当前车位 81 个着色像素，四层均非空。
  接受样本切换时 URL 与图像同步更新，拒绝样本地图面板数量为 0；390px 下地图宽 364px 且保持单栏。
- 网页实现最终合并到正式项目 `/mnt/chengchangxu/projects/vnav_data_preview`，没有以
  `.codex/worktrees/` 作为交付或运行目录；迁移时保留了主项目中更新的批量训练、跨地图切分和追加批次
  改动。正式目录 53/53 测试与生产构建通过，5182 监听进程 cwd 已通过 `/proc` 确认；浏览器读取最新
  `run_20260827_172812` 为 `54/54/0`，六路顺序、400×400 地图和网络/控制台状态均通过。

### 旧 v1 可训练样本计数复核（已淘汰）

2026-08-27 以“每个任务最新一个 `status=success`、`mode=full`、
`full_generation_authorized=true` 的 run”为去重口径，排除 pilot、失败 run 和旧版重复 run。当前共有
6 个任务、9,543 个教师候选，其中 9,415 个 accepted case 已物化，可直接作为训练样本；另有 128 个
碰撞拒绝标签，不计入训练样本数。

```text
task                  candidate  accepted  rejected
20260818193938Axg          3156      3147         9
20260819203318qFl          2180      2159        21
20260820180215WDK          2555      2543        12
20260821114524n4Q           389       384         5
202608251919001QS          1209      1128        81
20260826154539RI5            54        54         0
TOTAL                      9543      9415       128
```

逐 run 交叉检查 `run.json.accepted_count`、`teacher_labels.jsonl` 中 accepted/rejected 行数和
`cases/` 一级目录数，6/6 全部一致；因此 9,415 不是仅依赖声明字段的估算值。

### 旧 v1 多任务一键补齐与跨地图切分历史

2026-08-27 新增：

```bash
./processing/training-data-builder/build_pending_training_data.sh
```

该入口扫描 `/mnt/chengchangxu/data/visual_nav_mv/*`，只为未出现有效成功 full run 的就绪任务生成
训练集。有效完成标志要求同一 `pipeline_version` 下 `run.json` 同时满足 `mode=full`、
`status=success`、`full_generation_authorized=true`、`accepted_count>0`，且 `cases/`、
`source_manifest.jsonl`、`teacher_labels.jsonl` 存在。残留 `.in_progress`、失败记录和 0 样本 run 不作为
完成标志。下载目录必须有已解压 `frames.jsonl`、每个源子任务的成功拼接 Manifest 和六路非空 MP4；
其余记为 `incomplete`，下次执行重新发现。批次使用文件锁、逐任务失败隔离，并在
`visual_nav_training/_batch_runs/run_*/batch_run.json` 增量落盘；默认自动拉起/关闭自己拥有的 8103
教师，健康的外部 8103 只复用不关闭。

地图与视频对齐新增以下硬规则：

- 逐帧 `map_name/current_map` 归一化为配置中的地图名；同一源子任务内相邻帧地图改变时，以新地图
  首帧时间切成新的内部子任务。各地图段独立做路线、边界清理、位姿跳变截断和教师请求，绝不连接
  两个地图坐标系；run 记录源子任务、段序号、源行区间和地图时间区间。
- 所有帧缺地图时，只允许 `task.json` 中所有 `current_map` 快照归一化后唯一时回退。若快照跨地图
  而缺少逐帧切换时刻，明确报 `map_timeline_incomplete`，不猜测切点。
- 视频 Manifest 有合法 `local_trim.from/to` 时优先使用该窗口，否则才使用 `keep_windows`。实测
  `20260821114524n4Q_3` 的 MP4 为 151 秒本地裁剪，但 Manifest 旧 `keep_windows` 为 483.731 秒；
  使用旧窗会让 436 行全部变成 `video_sync_error`。切换到 `local_trim`
  `[1787284923.863153, 1787285074.863153]` 后恢复 389 个候选，最大 PTS 误差 `33.271 ms`，无需放宽
  `50 ms / 5 cm / 3 deg / 0.5 s` 门槛。

首次批处理先生成两个任务，第三个因上述陈旧窗口被“至少一个有效样本”保护明确判失败；修复后只重跑
第三个任务。最终成功产物：

```text
/mnt/chengchangxu/data/visual_nav_training/20260818193938Axg/
  vnav_teacher_v1/full/run_20260827_153033/  # 3156 / 3147 / 9，约 4.2 GB
/mnt/chengchangxu/data/visual_nav_training/20260819203318qFl/
  vnav_teacher_v1/full/run_20260827_154522/  # 2180 / 2159 / 21，约 2.8 GB
/mnt/chengchangxu/data/visual_nav_training/20260821114524n4Q/
  vnav_teacher_v1/full/run_20260827_160016/  # 389 / 384 / 5，约 795 MB
```

- 共 5725 个候选、5690 个接受样本、35 个拒绝；拒绝全部为 `teacher_rollout_collision` 并保留标签。
  三个 run 的 source 行数为 `4120 / 3056 / 1637`，路线数为 `10 / 15 / 1`；地图覆盖 P_map 和
  B10_map。
- 对 5690 个接受样本逐一检查六路图片、原始/融合栅格、NPZ 和 sample JSON，缺失/空文件为 0；
  教师融合数组全部为 `int8 {0,100}`，静态障碍并集、融合 hash 和展示 PNG `{0,255}` 全部可复算。
  静态地图共新增 23,414,483 个局部障碍格。
- 三个 run 最大同步误差 `33.324 ms`；路线最大横向/切向误差
  `0.099208 m / 4.553904 deg`，通过 `0.10 m / 5 deg`。单元测试为 16/16。
- 二次一键只读扫描得到 `discovered=6 / pending=0 / already_completed=4 / incomplete=2`，8103 已停止。
  当前未就绪的是 `202608181754378l1`、`20260819181014HWI`，均因无解压 Meta；以后下载完整后再次
  执行同一命令即可自动补做。

#### 主项目路径修复后的追加批次

确认会话工作树与用户实际运行的 `/mnt/chengchangxu/projects/vnav_data_preview` 不同后，将批处理入口、
跨地图/`local_trim` 实现、测试和文档逐字节同步到主项目。主项目验证 Shell 语法、16/16 单元测试和
`--dry-run` 均通过，随后同一命令发现并完成两个后来下载的新任务：

```text
/mnt/chengchangxu/data/visual_nav_training/202608251919001QS/
  vnav_teacher_v1/full/run_20260827_172222/  # 1209 / 1128 / 81，约 1.4 GB
/mnt/chengchangxu/data/visual_nav_training/20260826154539RI5/
  vnav_teacher_v1/full/run_20260827_172812/  # 54 / 54 / 0，约 93 MB
```

- 本批共有 1263 个候选、1182 个接受、81 个拒绝；拒绝全部为 `teacher_rollout_collision`。接受样本
  六路相机、原始/融合栅格、NPZ、JSON 均完整非空，教师融合数组 `int8 {0,100}`、静态障碍并集、
  hash 和 PNG `{0,255}` 逐样本复算错误为 0；静态地图新增 5,734,242 个局部障碍格。
- 最大同步误差为 `33.330 ms`。`202608251919001QS_6:route_000` 的全路线切向误差
  `176.046 deg`，没有任何接受标签引用；该路线相关 679 行在 source Manifest 中明确标为
  `route_simplification_error`。实际进入训练集的路线最大横向/切向误差为
  `0.098927 m / 3.465017 deg`，门槛通过。
- 此批由主项目入口 `/mnt/chengchangxu/projects/vnav_data_preview/processing/training-data-builder/`
  运行，批次报告位于 `visual_nav_training/_batch_runs/run_20260827_172216_2696213/`，失败任务为 0，
  8103 已自动停止。
- 累计 5 个新增任务共有 6988 个候选、6872 个接受样本、116 个碰撞拒绝；连同原基准任务，当前
  有 6 个成功 full 任务。下载根目录现有 9 个任务，最终应为
  `pending=0 / already_completed=6 / incomplete=3`；未就绪任务是 `202608181754378l1`、
  `20260819181014HWI`、`20260824161657Li2`，均缺解压 Meta。

### 旧 Pilot review_004（已淘汰）

2026-08-26 使用独立 `http://127.0.0.1:8103` 教师完成 `review_004`，产物位于：

```text
/mnt/chengchangxu/data/visual_nav_training/20260820180215WDK/
  vnav_teacher_v1/pilot/review_004/
```

- 按 `_1/_3/_5/_6/_7 = 2/1/2/3/4` 生成 12 个单锚点 case；12/12 接受，教师阶段无替换或拒绝，
  六路图像、占据图、权威 NPZ 地图、forward-route mask、五步动作与联合 PNG 均完整。
- 五段离线合格候选数为 `294 / 35 / 392 / 791 / 1133`。边界静止尾部裁剪约为
  `0 / 10.345 / 12.984 / 4.346 / 438.816 s`；原始文件未修改。
- 六路实际 PTS 最大绝对同步误差 `32.714 ms`，低于 `50 ms` 门槛；所有样本的位姿运动量和
  邻帧间隔门槛同时通过。
- 12 个样本重复教师请求的五步命令全部一致；`sample_content_sha256` 12/12 可从落盘输入重算且
  互不重复。教师输出最大绝对线速度 `0.75 m/s`、最大绝对角速度 `0.31645 rad/s`，均在配置域内。
- 精确单轮车积分和 `0.70 m x 0.60 m` footprint 回放碰撞为 `0/12`。7 条路线最大横向误差
  `0.097398 m`、最大有序局部切向误差 `4.767978 deg`，均通过 `0.10 m / 5 deg` 硬门槛。
- `overview.png` 为 `1860 x 2488`，每个 case 的高清联合图为 `2160 x 1920`；已人工检查 B10 室内
  case 和 P_map 高曲率 case，车头朝上、路线方向、rollout 叠加与动作文本无裁切。
- `run.json` 明确记录 `full_generation_authorized=false`。当前只完成 pilot，尚未执行批量生成。

该版本的 `case_012` 位于 `_7` 坐标系跳变后的无效片段，且教师尚未融合静态障碍；只保留作审计，
不得作为全量生成验收依据。

## OSS 已有任务发现口径

2026-08-27 使用 `processing/algo-handoff-tools/pull-task-export.py` 和本机 `.env` 中已有的
`SKDOS_LIVE_URL / SKDOS_API_KEY` 做了只读验证：

- `python3 processing/algo-handoff-tools/pull-task-export.py --list` 能从
  `GET /api/annotation-tasks` 列出 28 个看板任务，但它不是 OSS bucket 列表，不能直接把 28 个
  `task_id` 都视为已有云端视频。
- 当前本机没有 OSS AccessKey 或 `ossutil`，`X-API-Key` 也不能用于遍历 bucket；因此无法发现未登记
  到看板数据库的孤立 OSS 对象。训练数据入口应优先使用看板 API 返回的已登记对象信息，不猜测
  bucket 内容。
- “OSS 上明确已有连续视频”的保守判定为：任务的 `video_continuous.items` 至少存在一项
  `status == "ready"`、`object_key` 非空。对象键实测遵循
  `videos/{task_id}/{sub_task_id}/cam*_continuous.mp4`。不能只看 `video_upload.status`，因为状态可能
  长时间停留在 `uploading`。
- 按上述判定，本次 28 个任务中有 13 个明确包含 OSS 连续视频对象，共 `232/248` 个 item 为
  `ready`：

```text
20260826154539RI5       4/12
20260824161657Li2       2/6
202608251919001QS      26/30
20260821114524n4Q      18/18
20260820180215WDK      30/30
20260819203318qFl      48/48
20260819181014HWI      12/12
at-tiu0i0.5aifo-15e337a0  5/5
at-tiu0sl.y20l-848f3637  5/5
at-tixpiy.wbt3-c9d3dc79  5/5
at-tj10s8.5m6c-d3c4226e  5/5
202608181754378l1       6/6
20260818193938Axg      66/66
```

- 反例验证：`20260821142720ScN`、`202608211447512nb`、`20260821182052Sv7` 都返回
  `video_upload.status=uploading`，但逐一对其全部子任务请求
  `format=jsonl&grids=none&pack=algo` 后，首行 `_video_segments` 均为 0，且没有 ready 的连续视频
  `object_key`；这 3 个任务不能纳入“OSS 已有任务”列表。
- 后续若需要严格等价于 bucket 的完整枚举，必须由服务端增加只读列表接口，或单独提供仅含
  `ListObjectsV2` 权限的 OSS 凭据，并按 `videos/` prefix 分页、从对象键第二级去重 `task_id`。

本次验证只读取任务元数据和 JSONL 首行，未下载视频、未生成训练样本，也未修改任何 OSS 对象。

## 首尾静止只读初检

2026-08-25 对任务 `20260820180215WDK` 的 5 个非空片段使用独立 `pose` 做了首尾运动粗扫。粗扫用 2 秒前向窗口，`0.10 m OR 3 deg` 作为运动证据；这是阈值设计验证，尚未执行文件裁剪：

- 5 个片段均在开头很快出现运动证据，没有发现适合自动删除的长静止前缀。
- 片段 `_1/_3/_5/_6/_7` 的最后运动证据距数据结尾约 `1.45 / 11.14 / 12.68 / 4.80 / 439.31 s`。
- `_1` 和 `_6` 在接近结尾时又发生运动，说明不能把较早出现的停车一直裁到结尾；应用 3 秒静止持续时间与 1 秒上下文后，`_1` 不应自动裁剪。
- `_7` 存在约 7.3 分钟的明显末尾静止冗余，是首尾静止清洗的高收益样本；正式裁剪前仍应结合六路视频光流确认是真实停车而非定位冻结。

本次只读检查未改动任何原始 Meta、视频或占据图。正式实现时需先输出候选区间和预览供抽检，再由独立导出流程生成训练 manifest。

## 已下载数据统计

2026-08-25 对任务 `20260820180215WDK` 的 5 个非空片段统计：

- Meta 共 5,280 条，位姿均有效。
- 原始有效且 PNG 存在的占据图共 5,277 张；另有 3 条 `grid_valid=false`。
- 采用 `50 ms / 5 cm / 3 deg / 0.5 s` 参数后，保留 5,277 条 Meta，最终得到 5,274 组带有效占据图的六路视频训练样本。
- 训练过滤额外丢弃 3 条 Meta/占据图：1 条位于视频尾部、时间差约 89 ms；2 条缺少 0.5 秒内可用于估算变化的相邻位姿。

参数敏感性结论：

- 时间阈值低于一半视频帧间隔会大量误删；30 ms 在当前数据上会丢弃约 450 条 Meta。
- 时间阈值 50 ms 已覆盖除一个视频尾帧外的全部名义 PTS 匹配。
- 平移阈值从 5 cm 放宽到 10 cm 没有增加保留量。
- 航向阈值从 1° 放宽到 3° 可多保留 11 条 Meta。
- 局部位姿邻帧间隔从 0.3 s 放宽到 0.5 s 可多保留 15 条 Meta；继续放宽到 1 s 没有收益。

## 视频时钟限制

当前已下载视频的 `align_mode` 为 `wall_clock`。当前统计只能验证声明时间轴上的 PTS 差，无法发现相机与 Meta 之间潜在的固定相位偏移。

- 当前数据应为每路相机标定并保存 `camera_offset`。
- 可以利用车辆启停、转弯等明显事件，将图像运动与位姿变化做时间相关性搜索。
- 后续采集应优先保留完整的 `t0_hw` / `t1_hw`，使用 `hw_ts` 对齐。
- 放宽匹配窗口不能修复固定相位偏移；必须先校正时间偏移，再取最近 PTS 帧。

## 预览界面约定

预览页面不直接代表训练样本过滤结果：

- 位姿/实时数据使用当前视频时刻前后 1 秒内最近的 Meta。
- 实时数据的位姿区并列显示当前数据集统一的 `map_name`；该标签来自整份 `frames.jsonl` 的
  一致值，不跟随近邻帧切换。未标注或文件内标签不一致时显示 `—`，不得据此推断地图。
- 占据图独立从有效 `grid_valid + grid_png` 帧中选择前后 1 秒内最近项，避免某一 Meta 没有 grid 时让预览中断。
- 预览状态显示“近邻预览”，表示数据可能不是严格同步训练对。
- 严格训练匹配仍使用前述 `50 ms / 5 cm / 3 deg / 0.5 s` 规则。
- 远程预览允许在 GPU 资源空闲且实际支持 NVENC 时实时降为 `480px / 10fps / 250kbps`；
  该流只用于人工查看，不落盘，也不得作为训练视频或改变训练帧匹配参数。
- 多路预览任一路缓冲时整组暂停；小偏差使用播放速率收敛，硬 seek 的触发差值为 `500ms`、
  冷却时间为 `1s`。这些只是浏览器播放策略，不改变原视频 PTS 或训练同步结论。
- GPU 预览期间每 10 秒复查资源；核心利用率达到 70% 或剩余显存低于 512MiB 时主动停止，
  回退原始视频，避免预览继续占用新启动训练任务所需的 GPU。
- 占据图按播放方向以 32 张原始 PNG 为一批进行内存传输，缓存剩余 12 张时补下一批；浏览器
  缓存上限为 64 张或 64MiB。服务端不生成批次文件或代理图，批次响应和解码缓存均为临时内存。
- 缓存淘汰时保护当前帧及播放方向上的后续 12 帧，避免第三个批次进入缓存时把尚未播放的预取帧
  当作普通 LRU 提前淘汰；反向预览使用同一规则。保护窗口最多 13 张，32/12/64 与 64MiB 参数不变。
- 开发模式的 React StrictMode 会额外重放一次 effect setup/cleanup。占据图资源生命周期必须在每次
  setup 时恢复可用状态，在 cleanup 时终止批请求、失效异步结果并释放 Object URL，不能让首次模拟
  cleanup 永久关闭本次挂载的缓存。该约束只影响预览资源管理，不改变占据图选择或训练样本时间轴。
- 页面只显示视频时间轴当前匹配的占据图，不复用旧图冒充当前帧。目标图未就绪时，占据图与
  多路视频共用缓冲屏障，短暂停止时间轴并在目标图解码后同步恢复；随机 seek 也遵循该规则。
  这只是预览传输与播放策略，不改变训练数据文件、时间戳或训练匹配参数。
- 2026-08-25 验证：片段 7 的 3140 张占据图总计 4.9MiB、平均 1.6KiB，最短帧间隔约
  200ms；浏览器注入 1000ms RTT 后按约 5Hz 连续检查 45 个真实时间点，45/45 显示帧时间戳
  精确匹配，0 次占位或旧帧错配，只产生 2 个 32 帧批请求。该验证只检查预览同步，不改变
  严格训练匹配阈值。
- 2026-08-26 验证：片段 7 在 5173 开发模式、React StrictMode 下预热首批后，以约 60Hz 连续推进
  时间轴 20 秒，只产生播放窗口所需的 2 个 32 帧预取批次；“正在同步占据图”切换 0 次、直接 PNG
  回退 0 次、浏览器长任务 0 次、控制台错误 0 次，渲染保持 60–62fps。自动化 Chromium 不含 H.264，
  因此本次直接验收占据图请求、解码缓存和缓冲屏障，不宣称已在该浏览器中验证六路视频解码性能。

## 按日期自动拉取与可选教师构建

2026-09-01 新增全局 skill `vnav-oss-training-data`，用于从必填起始日期开始自动发现、下载并处理
已登记的 OSS 任务，并按显式选项调用教师制作训练数据。统一约定如下：

- 起始日期/时间为必填闭区间下界；未带时区时由服务端按 `Asia/Shanghai` 解析。结束时间默认取运行时
  的上海当前时间，也可显式指定；可选 `robot` 过滤。
- 任务发现只使用 `list-task-ids.py --from ... --to ... --json`，响应
  `truncated=true` 时必须在任何下载前停止并缩小日期窗，不能静默处理不完整列表。
- 每个 task 均由 `download-task.sh --jobs <N> <task_id> <source_root>` 完成导出、规范解包、OSS 视频
  下载、对齐和完整性判定；单任务失败不阻止后续任务，但失败任务不得进入教师批次。
- 教师默认关闭。只有显式 `--with-teacher` 或提供 `--teacher-config` 时，才把下载成功任务传给
  `build_pending_training_data.sh`；未提供模型配置时使用当前 Rule-10
  `config_rule10_continuous_ep011200_sim.yaml`。请求教师时必须在任务发现和下载前检查该配置、地图、
  model server 和 Python runtime；ep011200 资源缺失时直接停止，不得回退 ep023000。教师批次成功后必须运行
  `validate_training_data.py`，输出继续使用版本化 full run 和全局 validation JSON。
- `--dry-run` 仍会只读查询真实任务列表，以便检查范围和截断状态，但不下载、不启动教师、不写训练
  样本。鉴权继续只从本机环境或项目 `.env` 读取，不写入命令、日志或仓库。

入口：

```bash
python3 /mnt/chengchangxu/.codex/skills/vnav-oss-training-data/scripts/run_pipeline.py \
  --since '2026-08-31' [--with-teacher]
```

创建时只做离线安全校验，未查询真实日期范围、未下载 OSS、未启动教师，也未生成训练样本：
`quick_validate.py` 通过；更新后编排脚本 4/4 单测通过，覆盖 ep011200 默认配置、必填起始日期、任务 ID
稳定去重和截断响应在处理前失败。当前本机尚未同步 ep011200 YAML，带教师的 dry-run 在真实任务查询前
按预期以退出码 2 报告缺失路径；因此没有回退旧模型，也没有产生半途下载。

### 2026-08-31 日期窗口教师生成结果

2026-09-01 使用 `--since '2026-08-31' --with-teacher` 完成首次真实窗口运行。实际闭区间为
`2026-08-31` 至 `2026-09-01 11:37:11`（`Asia/Shanghai`），未指定 robot 过滤，任务发现未截断。

- 发现 2 个任务：`20260831152735OM4` 和 `2026083119533604q`。两者都通过
  `download-task.sh --jobs 4` 最终汇总，下载成功/失败为 `2/0`；每个任务均规范出 5 个子任务，
  10/10 子任务都是 `cam0,cam1,cam2,cam3,cam5,cam6` 六路完整视频、`hw_ts` 对齐。
  下载和规范化 warning 为 0，没有 `video_camera_set_nonstandard`。
- 使用默认 `config_rule10_continuous_ep023000_sim.yaml`、`cuda:0` 生成
  `vnav_teacher_rule10_history_v2` full 数据；教师为 `Rule10ContinuousPolicyNet`、epoch 23000，
  checkpoint 为 `checkpoint_ep023000.pt`，路线编码为 `rule10_directional_visible_progress_history`。
- `20260831152735OM4` 产物为 `run_20260901_114408`，候选/接受/拒绝为 `279/270/9`；
  9 个拒绝均为 `teacher_rollout_collision`。`2026083119533604q` 产物为
  `run_20260901_114847`，候选/接受/拒绝为 `58/58/0`。本批合计 `337/328/9`，两个 run 的
  warning 均为 0。
- 批次报告为
  `/mnt/chengchangxu/data/visual_nav_training/_batch_runs/run_20260901_114403_2814505/batch_run.json`，
  最终 `generated=2 / failed=0`。全局校验产物为
  `/mnt/chengchangxu/data/visual_nav_training/vnav_teacher_rule10_history_v2_validation.json`：11 个 run、
  8748 个候选、8608 个接受、140 个拒绝，`all_passed=true`；新增两个 run 均为
  `validation.passed=true`、错误 0、warning 0。

### 2026-09-01/02 ep011200 全量重生成结果

本轮按用户要求不做日期发现、不下载 OSS，也不设置日期窗口或 robot 过滤，直接复用
`/mnt/chengchangxu/data/visual_nav_mv` 下已有的 12 个任务重新打教师标签。生成前删除
`/mnt/chengchangxu/data/visual_nav_training` 下原有的 16 个一级条目、330,060 个旧训练文件，包括
历史 full run、legacy 数据、batch、snapshot 和 validation；删除不可恢复，仅保留空的
`_batch_runs/batch.lock` 作为并发锁。采集输入删除前后均为 35,012 个文件、5,399,830,812 字节，
路径/类型/大小/mtime/mode 元信息摘要均为
`435c51806304cdef8773e13e166f66bfc213f076ce72f3219eee5e19399897bc`，确认源数据未被修改。

教师使用 `/mnt/chengchangxu/projects/navi_sys_odo/dev/model_server/model_server/`
`config_rule10_continuous_ep011200_sim.yaml`，配置 SHA-256 为
`212caf60c9a4969a9861ac0dee8911994ac996b0033e88592f56b0d126489246`。checkpoint 为
`rule10_history_20260831/rule6_safe08_softbypass_temporal_ep3500_optimized_continuation_19p27h/`
`checkpoint_ep011200.pt`，SHA-256 为
`2f372ee3f204f2978d1e3466ee2742011cbaab1d66aa79861bebdfa875f13028`；健康检查确认
`Rule10ContinuousPolicyNet`、epoch 11200、`cuda:0`、
`rule10_directional_visible_progress_history` 和 batch 教师端点均就绪。服务按 skill 规定从仓库根目录
以 `--batch` 启动，只由本轮复用，生成后已停止。

批次报告为
`/mnt/chengchangxu/data/visual_nav_training/_batch_runs/run_20260901_220250_2671100/batch_run.json`，
运行区间为 `2026-09-01 22:02:50` 至 `23:59:58`（Asia/Shanghai），耗时 7,028.258 秒。

| task | 候选 | 接受 | 碰撞拒绝 | 结果 |
| --- | ---: | ---: | ---: | --- |
| `20260818193938Axg` | 2,150 | 2,148 | 2 | generated |
| `20260819181014HWI` | 0 | 0 | 0 | failed：质量过滤后无合格候选 |
| `20260819203318qFl` | 1,491 | 1,467 | 24 | generated |
| `20260820180215WDK` | 1,921 | 1,920 | 1 | generated |
| `20260821114524n4Q` | 302 | 300 | 2 | generated |
| `20260824161657Li2` | 93 | 89 | 4 | generated |
| `202608251919001QS` | 926 | 867 | 59 | generated |
| `20260826154539RI5` | 33 | 33 | 0 | generated |
| `20260827180935lmd` | 79 | 79 | 0 | generated |
| `20260828154654XXz` | 1,416 | 1,382 | 34 | generated；8 个五路子任务 warning |
| `20260831152735OM4` | 279 | 269 | 10 | generated |
| `2026083119533604q` | 58 | 58 | 0 | generated |

批次最终为 `generated=11 / failed=1`，因此状态为 `partial_failure`；唯一失败的 HWI 是历史已知的
0-candidate 任务，本轮未重试、未伪造样本。11 个成功 run 合计 8,748 个候选、8,612 个接受、136 个
`teacher_rollout_collision` 拒绝，接受率 98.4454%。教师共执行 279 个 batch，8,748 个 sample slot，
每个 batch 都恰好一次 forward。`XXz` 的 8 个子任务继续使用 Manifest 声明完整的
`cam0/cam1/cam2/cam3/cam6`，未补造推荐集合中的 `cam5`。

全量验证产物为
`/mnt/chengchangxu/data/visual_nav_training/vnav_teacher_rule10_history_v2_validation.json`：
11/11 run 均通过、逐样本错误 0、`all_passed=true`；唯一聚合 warning 为 `XXz` 的非推荐五路相机集合。
静态重复分支为 20.0302%，初始精确零速为 20.1463%。验证后 dry-run 为
`discovered=12 / already_completed=11 / pending=1 / incomplete=0`，pending 仅 HWI，证明所有非空成功
run 均已被完成标志识别。HWI 保留一个仅含 273 字节 `run_failed.json` 的
`.run_20260901_223127.in_progress_2671100` 诊断目录，明确记录本次 0-candidate 失败；它不属于成功训练
数据，后续仍会按 pending 处理，本轮不做循环重试。

### 2026-09-02 ep011200 Rule-10 数据与动态学生 v0 训练约定

数据产物目录仍为
`/mnt/chengchangxu/data/visual_nav_training/dynamic_teacher_v2_cam0_2hz_ep011200`。
其中 V2 是 Rule-10 数据契约版本，不是模型版本；当前动态学生和训练方法版本统一规定为 **v0**，
revision 为 `ep011200_joint_decoder_fusion_v0`。不得与旧
`dynamic_teacher_v1_cam0_2hz` 混用，也不得复用旧动态 checkpoint 或 gate。

来源教师 `checkpoint_ep011200.pt` 的 SHA-256 为
`2f372ee3f204f2978d1e3466ee2742011cbaab1d66aa79861bebdfa875f13028`；全局验证报告 SHA-256 为
`26404cac8abefb228ba08ece5e7d26d7ec138ee36ff7917c8c05a52b473d1044`，必须满足
`all_passed=true`。0-candidate 的 HWI 不重试、不伪造样本。

训练/测试物理切分和身份：

- train 7,654 条，manifest SHA-256 为
  `cdf6094f3b8d69db4c90f284c35f1d03936bd2bf1dbbf1c88f173b4e045e3897`；三折 route-group 数量
  `2,556 / 2,549 / 2,549`，相同 `sub_task_id+route_id` 不得跨折。
- locked test 958 条，manifest SHA-256 为
  `5e4f90e300627f0cd13b02ae07c49a6de24f07f52ab5e163c76496f978d621e0`。训练、调参、preview、
  cache 和 OOF 不得评估其标签；仅允许通过全部门槛后的 `final_ema.pt` 原子 claim 一次。
- config SHA-256 为
  `79641c3059e1b6ed6f0bdd3d119afaee78b71849cbb9b8edab5427507f77a904`。
  实际有 22,962 个 cam0 引用、17,312 张唯一图片；所有身份和计数必须从 manifest 推导。
- float16 DINO cache 形状为 `[7654,3,196,384]`，reference identity SHA-256 为
  `6abc4577728bb564ecdecfa9f7b6b41205f9dc24e55f1872cb93a023fc4afc46`。只有逐引用 SHA、PTS、
  offset、frame index、checkpoint、预处理、形状和计数全部相同，才允许安全重绑 manifest hash。

manifest 字段约定：

- `reference_curvature`：未来 1 m forward-route 航向变化除以弧长，单位 rad/m。
- `is_turn_or_spin`：`abs(reference_curvature) >= 0.13962634 rad/m`（8 deg/m，包含边界），
  或教师动作是 `SPIN_LEFT/SPIN_RIGHT`。train 中 475 条，占 6.21%。
- `sample_weight`：转弯/自转为 **2.0**，其余为 1.0；权重和 8,129，转弯/自转贡献
  `950/8129 = 11.69%` 有效训练质量。loss 必须逐样本计算后按
  `sum(sample_weight * loss) / sum(sample_weight)` 聚合；验证、OOF 和测试始终无权统计。
- `history_change_cells`：复现教师 1.0/0.5 s 历史选择与 pose 对齐，移除静态占据，只在局部
  forward-route 周围 0.30 m 扩张区域统计历史/当前 XOR cells。
- `history_relevant`：任一历史 XOR 数量 `>=8`。train 中 1,588 条；1,531 条
  `static_repeat` 必须全部为 false、maximum change 为 0。它只用于审计、preview、切片评估和
  扰动门槛，不得作为学生输入或额外权重。

九样本 preview 必须覆盖直行、左右路线转弯、左右自转、history-relevant、static-repeat、室内和转弯
阈值边界，并绑定上述 config hash。部署输入仍只有 cam0 三帧、静态/forward-route/validity 地图和
`[0,0,v,w]`，输出五个 200 ms `[dv,dw]`。

v0 模型使用 `joint_temporal_decoder_memory_chunk`：每个时间步由 4 个 visual、4 个 map/route 和
1 个 state token 组成，经同一 streaming causal Transformer；当前 9 个路线条件编码 token 再与
64 个 instant visual token、1 个 instant state token 拼成 74-token memory，只调用一次最终 causal
decoder。诊断只能在同一联合路径上 mask 或 donor-shuffle 输入。DINO、trajectory、STOP、critic 冻结。
结构图：
`/mnt/chengchangxu/projects/visual_navigation_e2e/docs/architecture/dynamic_joint_fusion_v0.svg`。

本轮正式训练使用 seed 42、GPU 0、物理/有效 batch 64，从静态 Stage-D checkpoint 初始化：

- 64 样本 1,000-step overfit：loss `1.06377468 -> 0.01492075`，下降 98.597%，冻结哈希不变，
  NaN/Inf 为 0。
- 1× 控制组三折 joint 最佳 epoch/loss：
  `14/0.06908086`、`13/0.06676986`、`15/0.05614251`。
- 2× 实验组三折 joint 最佳 epoch/loss：
  `6/0.07362567`、`15/0.06375739`、`20/0.05815774`。
- 2× pooled 路线打乱恶化 54.18%、当前帧打乱恶化 11.50%，均通过；但
  `history_relevant` 相对 T=1 改善仅 0.835%，95% CI `[-3.43%,4.83%]`，未显著；
  last-frame-repeat 恶化 0.136%，95% CI `[-0.0529%,0.329%]`，未显著。
- 2× 相对 1× 的 turn/spin delta-loss 改善 0.446%，28 个 turn-bearing route group、10,000 次
  bootstrap 的 95% CI 为 `[-4.31%,6.56%]`，下界不大于 0；整体退化 1.756%、straight 退化
  1.904%，均超过 1%。

因此 v0/2× pooled gates 为 false。用户复核该结论后明确选择 1×，并人工放行 1× OOF 中唯一失败的
`history_relevant_last_frame_repeat` 门槛。放行仅允许 `sample_weight_mode=uniform`，绑定 1× 三折 summary
SHA-256 `ea95f446820954ecde489bd03209676c4fac24a06487e4f3e08a61f98c514aca`、overfit report SHA-256
`708d50751177ac9649be4b1ab3e4a2af2b746cbbc8d66868f20620dd61a0ac77` 和人工原因；默认自动门禁未放宽。
manifest 继续保留 2× 数据实验元信息，但此次全量训练对全部样本使用有效权重 1.0。

1× 全量训练从静态 checkpoint 重新初始化，在 7,654 条 train 上使用三折最佳 epoch 中位数
`10/14/14/14/14`。最后 joint step 的无权 train loss/delta loss 为 `0.03363017/0.03235007`；转弯
路线打乱使 delta loss 恶化 108.085%，当前帧打乱使整体恶化 15.751%。这些是 in-sample 诊断，不能
替代失败的 OOF history gate。full summary SHA-256 为
`0ca7808b2f264231ab952c192ab94640383d3537d2a326191045ecc9a234d140`。

958 条 locked test 已按 checkpoint/test-manifest hash 原子 claim 并完成唯一一次评估，但被评估的
`final_ema.pt`（SHA-256 `53dcc1f7a4ec17148cac16cc37a86c2636e36f57fec7eeb7945efab01e40a598`）
存在 EMA 实现缺陷：旧逻辑把 map encoder 的 BatchNorm `running_mean/running_var` 也做了 EMA。
记录值 overall loss/delta loss 为 `0.16665519/0.16294142`、turn/spin delta 为 `0.16210522`、straight
delta 为 `0.16301761`；但同一 checkpoint 在 train 上为 `0.20438212/0.20011523`，而最后非 EMA 模型
为 `0.03363017/0.03235007`。64 样本交叉替换进一步确认“EMA 参数 + 最后 buffers”恢复到
`0.03447116`，“最后参数 + EMA buffers”恶化到 `0.24334800`。因此上述 locked-test 数值只保留审计，
不得解释为训练模型的泛化性能；消费标记不得删除或绕过。

EMA 已改为只平均 named parameters，并直接保留最后模型 buffers。原 test-bound checkpoint 不覆盖；
恢复 checkpoint `final_ema_parameter_only_recovered.pt` 的 SHA-256 为
`9fcabe8352cd2e69e803dfc6e00b6c31fe2561cfa9efce7ef7ca32b169ac9df4`，全 train
loss/delta loss 为 `0.04149504/0.04006865`，且明确标记 `locked_test_evaluated=false`。不会对它重开
locked test。修复后的全仓回归为 `126 passed, 14 warnings`。

## 2026-09-08 转弯过滤式采集 Gate 约定

新增根目录 `data_collection_gate.py`，供采集系统利用最近 1 分钟位姿缓存延迟判断转弯片段的开始和结束；完整集成说明见 `readme_gate.md`。该 Gate 不改变既有训练 manifest，只把训练后处理的转弯定义前移到采集阶段，以减少后续直行/静止样本筛选。

- 弧形转弯继续使用未来 1 m 路线曲率，阈值保持 `abs(curvature) >= 0.13962634 rad/m`（`8 deg/m`，包含边界）。在线路线由未来实际 XY 位姿构成，经 1 s 中值平滑并按约 0.05 m 路程重采样；与现有 `estimate_route_curvature()` 一致，对相邻路线段的局部曲率取中位数。
- 离线 `is_turn_or_spin` 中的自转来自教师 `SPIN_LEFT/SPIN_RIGHT`；采集侧没有该标签，因此明确改用未来 2 s 内最大平移 `<=0.10 m` 且 `abs(yaw change)>=8 deg` 的可观测代理。yaw 先跨环绕展开，正/负分别标记左/右。
- 开始判定仍预看 1 s。Turn 的自然结束条件是正常直线证据（历史 2 s 平移 `>=0.10 m`、yaw 变化 `<=3 deg`、`abs(curvature)<8 deg/m`）再持续 1 s。所有 Gate 额外执行统一硬停止：连续 3 s 几乎不动（最大位置漂移 `<=0.03 m`、最大 yaw 漂移 `<=3 deg`）、位姿跳变，或从开始节点起达到默认 `Tmax=45 s`，取最早节点。位姿跳变结束点取跳变前最后一个有效 pose；即使两帧超过普通 pose gap，只要绝对位置/yaw 跳变量越界仍硬停。yaw 先展开，正常 `+pi/-pi` 环绕不触发；短暂停车、证据不足和只有时间缺口而无绝对跳变的普通 pose gap 不硬停。位置跳变 `2 m`、表观速度 `3 m/s`、yaw 单步 `45 deg`、yaw rate `2 rad/s` 和相邻 pose gap `0.5 s` 沿用现有训练切段/对齐门槛。
- 采集系统必须传入缓存中的历史锚点，并长期复用同一个 Gate 实例。推荐开始判定延迟至少为 `start_lookahead + max(2 s, 1 m / 最低可靠识别速度)`；默认预看为 1 s，若最低速度为 `0.2 m/s`，应使用至少 6 s。当前导出 `pose.yaw` 为度，接入时必须显式转 rad，或使用 mapping 的 `yaw_deg` 字段。
- 四个控制方法统一返回 `(should_act: bool, action_timestamp_s: float)`。true 时第二项是应开始/结束的准确缓存节点；false 时不采取动作。稀疏调用可以返回早于本次检查时刻的历史动作节点。调用方必须先解包，不能直接对 tuple 做 `if` 判断。
- 全局硬停止统一由 `@with_hard_stop_conditions` 方法装饰器负责，具体 Gate 的 `end_collection()` 只保留各自自然结束条件。装饰器依赖统一的 `active_start_timestamp_s`、`config.maximum_collection_interval_s`、`reset()` 和位姿硬停判定契约，比较自然结束、位姿跳变、稳定停车和 `Tmax` 后返回最早节点；`with_maximum_collection_interval` 仅作为兼容别名保留。
- 定向验证命令为 `python3 -m unittest -v test_data_collection_gate.py` 及 `python3 -m py_compile data_collection_gate.py test_data_collection_gate.py`。2026-09-13 当前结果为 22/22 通过，除原有曲率、自转、缓存和输入契约外，覆盖流式活动状态、恢复直线后结束、3 s 稳定停车硬停止、2 s 短暂停车不结束、再次转弯重置恢复、位姿跳变前停止、稀疏调用返回准确历史节点，以及 Turn/Straight 两类的硬停止和审计状态。60 s、20 Hz、1201 pose 上按 5 Hz 顺序执行 190 次结束检查，Turn 平均/95 分位/最大为 `20.0/29.3/37.9 ms`，Straight 为 `10.6/12.9/19.6 ms`，均低于 200 ms 调用周期。

同日新增 `StraightGate`，保留少量正常直线行驶数据：

- 合格直线要求 `TurnGate` 未来证据充分且非转弯、未来 2 s 最大平移 `>=0.10 m`、yaw 变化 `<=3 deg`，并且已有足够实际路线估计曲率；完全静止、缓存不足、位姿异常不参与随机抽样。
- 每个由采集控制器按固定节拍提供的合格开始机会，以默认 `0.01` 做一次伯努利抽样。该 1% 是每机会启动概率，不是最终数据时长占比；机会节拍必须作为采集配置记录，改变调用频率时必须重新评估实际采集率。
- 命中后从连续均匀分布 `[6,12] s` 抽取一次目标时长并锁定。活动期间不重抽、不延长，转弯本身不提前结束；但位姿跳变和连续 3 s 稳定停车是贯穿所有 Gate 的硬停止，会提前结束。两类 Gate 均配置默认 `maximum_collection_interval_s=45 s`；Straight 默认随机上限 12 s 已更严格，自定义随机上限超过 `Tmax` 时取较小的有效上限。即使结束检查晚于截止时间，返回的 float 仍是准确截止节点。控制器必须记录片段 owner，不能用 `TurnGate` 的自然停止条件关闭 `StraightGate` 启动的片段。
- 直线 Gate 用例覆盖默认 1% 边界、静止/转弯拒绝、活动期不重启、固定时长截止和 6–12 s 取值范围。

真实视频回放验证使用 `20260820180215WDK_1/cam0_continuous.mp4`，视频长 `119.924 s`、manifest 无 hole、对应 371 个有效 pose。由于历史导出 pose 约 3 Hz 且存在 `0.5–2.0 s` 稀疏间隔，本次回放单独将 `maximum_pose_gap_s` 设为训练路线切段所用的 `2.0 s`；线上高频缓存默认 `0.5 s` 不变。其余设置为 60 s 缓存、6 s 判定延迟、转弯 0.2 s 节拍、直线机会 1 Hz、转弯优先、直线 seed 42。输出为直线 `[43.000,53.835] s`，时长 `10.834916 s`；右转 `[82.200,94.600] s`，在恢复直线后结束；后段左转合并为 `[102.200,113.000] s`，在连续 3 s 稳定停车后结束。旧逻辑把后段拆成 `[102.200,105.200] s` 与 `[107.800,110.800] s`，上一版流式逻辑因没有恢复直线而保持未结束；新增停车分支后得到明确结束节点。本例所有片段均短于 45 s，`Tmax` 未触发。

## 2026-09-09：2026-09-01 起 OSS 采集数据同步与处理

本轮使用统一 `vnav-oss-training-data` 编排器，按 `Asia/Shanghai` 时间从
`2026-09-01 00:00:00`（含起点）发现到 `2026-09-09 12:10:07`，不设置 robot 过滤，下载并处理到
`/mnt/chengchangxu/data/visual_nav_mv`，视频下载共享 `--jobs 4`。服务端返回计数和实际 ID 数均为 6，
发现结果未截断：

- `20260901185939a1N`：成功；3 个子任务，实际六路
  `[cam0,cam1,cam2,cam3,cam5,cam6]` 全部按 `hw_ts` 对齐并拼接。
- `20260902153110uGf`：首次因
  `cam3_1788336233.mp4` 为 partial file、H.264 NAL 损坏而失败；恢复时先将坏文件暂存为
  `cam3_1788336233.mp4.corrupt-20260909`，刷新导出后仅补下该分片并重新合并，最终 10/10 个子任务
  严格成功。补下文件为 3,544,459 bytes，整段 FFmpeg 解码无报错；原坏文件为 1,303,620 bytes，
  在后续获授权的 segments 清理中与其他原始分片一并删除。
  10 个子任务实际均为五路 `[cam0,cam1,cam2,cam3,cam6]`，缺少推荐集合中的 `cam5`，后续只能按
  实际相机集合消费。
- `20260903154146O1X`：成功；2 个子任务、75 个视频分片，实际五路
  `[cam0,cam1,cam2,cam3,cam6]`，全部按 `hw_ts` 对齐并拼接；同样记录非标准相机集合 warning。
- `20260904155611MuZ`：任务级失败；2 个规范 Meta 子任务实际均只有 `[cam0,cam2]`。子任务 1 的
  3/3 分片和两路拼接严格成功；子任务 2 的 24 个可用分片已下载并生成两路连续视频，但另有
  `cam0`、`cam2` 各 1 个分片在首次导出和一次刷新导出中均无 URL，manifest 为
  `download_skip=2`，因此该子任务不可视为完整数据，不得直接进入训练。
- `20260907162109nzz`：失败；首次和一次重试均在 Meta ZIP 的 chunked HTTP 响应中断，稳定复现
  `IncompleteRead(11945 bytes read)`；没有规范 Meta 或视频 manifest，停止继续重试。
- `20260908171756w8w`：失败；首次和一次重试均由导出服务返回 HTTP 502；没有规范 Meta 或视频
  manifest，停止继续重试。

最终只读核验共发现 17 个非空规范 `frames.jsonl` 子任务和 17 份视频 manifest；16/17 份通过
`download_ok + download_reused > 0`、`download_skip=0`、`download_fail=0`、全部实际相机拼接成功、
`concat_complete=true`、`align=true` 的严格条件。唯一未通过的是
`20260904155611MuZ_2` 的 2 个缺 URL 分片；已有 manifest 的输出全部采用 `hw_ts` 对齐。任务级最终为
3/6 完整成功、3/6 受上游数据或导出服务阻塞。未请求也未运行 teacher 生成，teacher 配置和训练数据
验证均为不适用。

随后按用户要求，对 3 个完整任务分别调用
`download-task.sh --delete-zip --delete-segments <task_id> /mnt/chengchangxu/data/visual_nav_mv`。脚本均命中
“任务已完整处理，仅执行清理”保护分支，没有重新下载或合并；共删除 3 个 Meta ZIP 和 3 个
`videos/segs` 目录，释放 913,803,113 bytes。清理后逐任务确认 ZIP 与分片目录均不存在，同时仍保留
15 个非空规范 Meta、15 份成功 manifest 和 78 个非空连续视频。最终核验时，先前失败的
`20260904155611MuZ`、`20260907162109nzz`、`20260908171756w8w` 任务目录也已不在输出根目录；本轮
3 条清理命令仅以成功任务为目标，没有删除这 3 个失败任务目录，也未尝试恢复其来源。

### 指定 3 个任务的 Rule-10 训练数据生成

同日用户明确授权为 `20260901185939a1N`、`20260902153110uGf`、`20260903154146O1X` 生成 full
训练数据。本轮按显式 task ID 扫描，不重新按日期发现，也不设置 robot 过滤；只读预检确认 3/3 均为
pending、没有已有成功 full run。源根目录为 `/mnt/chengchangxu/data/visual_nav_mv`，输出根目录为
`/mnt/chengchangxu/data/visual_nav_training`，流水线配置为
`processing/training-data-builder/config.json`。

teacher 使用默认
`model_server/config_rule10_continuous_ep011200_sim.yaml`，以 `cuda:0` batch 模式启动并由构建器通过
`--no-start-teacher` 复用；健康检查确认 `Rule10ContinuousPolicyNet` epoch 11200、
`route_encoding=rule10_directional_visible_progress_history`、`curvature_only=false`。teacher 配置 SHA-256
为 `212caf60c9a4969a9861ac0dee8911994ac996b0033e88592f56b0d126489246`，checkpoint SHA-256 为
`2f372ee3f204f2978d1e3466ee2742011cbaab1d66aa79861bebdfa875f13028`。生成版本固定为：

```text
pipeline_version = vnav_teacher_rule10_history_v2
route_version = pose_truncate_rdp_turn_v2
state_sampler_version = pose_velocity_zero20_v2
occupancy_fusion_version = local_static_obstacle_union_history_v2
```

三个任务均成功原子提交：

- `20260901185939a1N`：`run_20260909_144652`，27 个候选、22 个接受、5 个
  `teacher_rollout_collision` 拒绝；1 次 batch 请求，逐 run 验证错误 0、warning 0。
- `20260902153110uGf`：`run_20260909_144758`，1,384 个候选、1,376 个接受、8 个
  `teacher_rollout_collision` 拒绝；44 次 batch 请求。10 个子任务均按实际五路
  `[cam0,cam1,cam2,cam3,cam6]` 生成，逐 run 验证通过、错误 0，并保留 1 个聚合
  `video_camera_set_nonstandard` warning。
- `20260903154146O1X`：`run_20260909_150420`，1,373 个候选全部接受、拒绝 0；43 次 batch 请求。
  2 个子任务均按实际五路 `[cam0,cam1,cam2,cam3,cam6]` 生成，逐 run 验证通过、错误 0，并保留
  1 个聚合 `video_camera_set_nonstandard` warning。

本批合计 2,784 个候选、2,771 个接受、13 个碰撞回放拒绝，接受率 99.533%；共 88 次 teacher batch
请求，所有请求均只执行一次 forward。生成完成后只停止本轮启动的 teacher，8103 端口已关闭。

随后用权威 `validate_training_data.py` 对整个训练根目录重新验证并更新
`/mnt/chengchangxu/data/visual_nav_training/vnav_teacher_rule10_history_v2_validation.json`：当前 14 个 full
run、11,532 个候选、11,383 个接受、149 个拒绝，`all_passed=true`。验证报告 SHA-256 为
`29938d8ec42fc2287749f24138dd4ea9cc91e39bc95717160ee0b29afeb10ca5`。

## 2026-09-12：人工地图标签丢失排查与已有采集数据保护

用户反馈 0826 及以前人工标注的地图标签缺失，并明确要求已下载处理的数据不得再重新下载。
本轮只读检查真实采集目录、代码与历史执行记录，没有下载 OSS、重新解包、重新合并、启动教师，
也没有回写或推断补填任何地图标签。

已确认的代码缺陷：预览页 `setMapName` 将人工标注直接原子写入本地 `frames.jsonl`，没有独立的
持久标注副本。旧 `download-task.sh` 仅在带 `--delete-zip` 或 `--delete-segments` 时跳过完整任务，
普通重跑仍会刷新导出；旧规范化器随后替换整个同名 Meta 目录，并删除临时旧目录。上游文件没有
人工标签时，这条路径会丢失本地标签，也会覆盖本地裁剪结果。

实际证据与边界（时间均为 Asia/Shanghai）：

- 8 月 31 日任务 `01a057db-1440-7b60-b121-4d074f7faca7` 的执行记录确认，21:18:55 曾对
  `20260819181014HWI`、`20260824161657Li2` 调用普通 `download-task.sh --jobs 4`。
  HWI 的两份 Meta 落盘于 21:19:02/05，Li2 的其他 Meta 文件落盘于 21:19:06。那次是在修复
  缺失/根级 Meta 布局和恢复视频；记录不能证明此前每份人工标签的存在与具体丢失时间。
- 该轮检查时，0826 及以前共 32 个规范 `frames.jsonl`、19,922 行，其中 28 个文件有完整有效标签。
  `20260819181014HWI_1/_2`（459/403 行）与 `20260826154539RI5_1/_2`（121/89 行）仍无
  `map_name`。不能把这四份未标注数据自动按任务首尾快照补标。
- 用户当天 17:35–17:36 已重新标注 `Li2_1` 和 `1QS_2/_5/_6/_7/_8`，六份文件目前标签完整。
  `1QS` 的其他 Meta 保留 8 月 27 日 16:56 的导出/落盘时间；RI5 的未标注 frames ctime 为
  8 月 27 日 16:52。Axg、qFl、WDK、n4Q 的多份人工标签仍保留 8 月 26/27 日修改时间。
  因此没有证据支持“最近把所有 0826 以前数据重新下载覆盖”的判断。

修复后的执行约定：

- 默认入口对完整任务无条件跳过，不依赖 ZIP/segments 是否保留；只有显式清理选项才清理中间产物。
- 已有非空任务目录若未通过完整性检查，失败退出并保留现场，不自动重下、刷新 Meta 或覆盖视频。
- 显式 `--remerge` 只复用完整本地 segments；缺段时停止，不再自动刷新导出或补下载。
- 规范化器在发布任何 bundle 前检查全部目标目录；发现已有同名目录则拒绝覆盖，保护标签和裁剪。
  需要对比新版导出时使用独立输出目录，不能替换原采集输入。
- 本要求同时写入项目 `AGENTS.md`，约束后续采集与训练任务。

验证：在临时目录验证普通重跑、清理过中间产物后的重跑、不完整任务、失败任务的清理请求和
缺段 `--remerge`，均未触发下载/合并入口，原有文件内容、mtime、inode 保持不变；有效但同名的
新 ZIP 也不能覆盖人工标签或裁剪，且多 bundle 冲突会在发布新 bundle 前拒绝。
`test_normalize_meta_export.py` 与 `test_pull_oss_videos.py` 共 19/19 回归通过，包含现有本地
FFmpeg 拼接验证；Bash 语法和 `git diff --check` 通过。所有写入测试均使用临时数据。

## 2026-09-12：按人工地图修改时间定向重打标签

用户依据任务 `01a09444-95b6-7eb3-b147-48607e7d0d52` 的第 279 条地图问题，授权先检查
`map_name`，再仅对修改过地图字段的采集数据重新生成标签。此次不触发下载、解包、视频拼接或
源文件回写。审计和执行记录目录为
`/mnt/chengchangxu/data/visual_nav_training/_repairs/map_name_20260912_175553/`。

- 全量检查 15 个现存采集任务、66 份规范 `frames.jsonl`、41,484 行：所有行都有 `map_name`
  属性，但只有 64 份、40,622 行为有效枚举；`20260819181014HWI_1/_2` 的 459/403 行仍为
  `null`。HWI 没有成功 full run，本轮不自动推断地图、不生成标签，等待明确人工归属。
- 筛选同时要求 `frames.jsonl` 的 mtime 晚于该任务最新成功 full run 的 `completed_at`，且与旧
  `source_manifest.jsonl` 按子任务和行号连接后，源 `map_name` 确实改变；另核对行数及逐行时间戳
  没有变化。不是只因目录时间变新就重跑，也不把 ZIP 保留的 1979 年 mtime 当作人工修改。
- 命中 8 份、4,967 行：`20260824161657Li2_1`；`202608251919001QS_2/_5/_6/_7/_8`；
  `20260826154539RI5_1/_2`。mtime 均为本日 17:35:03–17:44:54，原始地图字段均从 null 补为人工
  枚举。三任务当前全部子任务恰好都命中，所以可以调用原有 full 构建器而不扩大重打标范围。
- `1QS_5` 的 169 行从旧快照回退的 `P_map` 更正为 `B10_map / 2_lifts_6_units`；其余七份的
  人工地图与旧回退值一致。重新生成的来源必须全部是 `frames.map_name`，本轮禁止快照回退。
- 显式使用 `config_rule10_continuous_ep011200_sim.yaml`，配置 SHA-256
  `212caf60c9a4969a9861ac0dee8911994ac996b0033e88592f56b0d126489246`，checkpoint SHA-256
  `2f372ee3f204f2978d1e3466ee2742011cbaab1d66aa79861bebdfa875f13028`，均与旧 run 一致。
  保持 `vnav_teacher_rule10_history_v2`、原路线、过滤、20% 零速分支、历史融合及 batch=32 配置。
- 使用独立新 run 保存当前/历史雷达静态地图融合、teacher 动作、NPZ 和 manifest；旧 run 保留。
  `sample_mapping.json` 按源子任务和原始 `meta_ts` 连接新旧候选，供后续显式迁移下游成员；不能
  假设新旧 `sample_XXXXXXX` 编号相同。已有 Streaming VIB 冻结清单、checkpoint 和评估不在本轮
  重打标范围内，不会自动改写。
- 源数据保护核验通过：42,818 个文件的大小/mtime/inode 与所有源 JSON 哈希前后相同；全部
  14 份旧成功 full run 的权威 manifest 大小/mtime/哈希保持一致。未修改人工地图或裁剪。

执行于 18:04:40–18:20:18（Asia/Shanghai），三份新 run 均已原子发布并通过权威
`validate_run`；逐帧地图/来源/静态地图 provenance 和新旧候选身份的补充核验也通过：

| 任务 | 新 run | 候选 | 接受 | 碰撞拒绝 | 验证 |
| --- | --- | ---: | ---: | ---: | --- |
| `20260824161657Li2` | `run_20260912_180447` | 93 | 89 | 4 | 0 错误、0 警告 |
| `202608251919001QS` | `run_20260912_180638` | 926 | 904 | 22 | 0 错误、0 警告 |
| `20260826154539RI5` | `run_20260912_181915` | 33 | 33 | 0 | 0 错误、0 警告 |

- 合计 1,052 个候选、1,026 个接受、26 个碰撞拒绝；34 次 batch 请求全部只执行一次 forward。
  本轮自启的 GPU 5 teacher 已停止，8103 端口释放。
- 新旧 1,052 个候选的源子任务和时间戳一一对应；970 个有效地图没有改变的候选，teacher
  动作命令逐值完全一致。Li2 的原 89 个训练样本与 RI5 的 33 个样本保持原动作。
- `1QS_5` 共 82 个候选改用 B10，其中原 43 个接受全部保留、37 个旧碰撞拒绝改为接受、2 个
  仍碰撞拒绝；该子任务现有 80 个接受样本。动作差值统计仅比较命令形状相同的 78 个候选，另
  4 个命令长度变化单独记录，不能把停止命令与五步命令直接广播比较。
- 原第 279 条按 `202608251919001QS_5 + meta_ts=1787658079.5927436` 找到新
  `sample_0000279`，地图为 `B10_map / 2_lifts_6_units`。原始雷达、历史位姿/时间、路线和坐标
  变换完全一致；静态地图、当前及历史融合、teacher rollout 与动作均已重算。
- 修订后的最新标签库存为 14 个任务、11,532 个候选、11,420 个接受、112 个拒绝。
  `current_runs_manifest.json` 列出明确 run 路径及验证来源：本轮验证三份新 run，另外 11 份复用
  原有验证并核对旧 manifest 未变；没有重打标或重复全量验证未修改任务。原全局验证 JSON
  SHA-256 `29938d8ec42fc2287749f24138dd4ea9cc91e39bc95717160ee0b29afeb10ca5` 保留为历史记录。
- 完整报告：
  `/mnt/chengchangxu/data/visual_nav_training/_repairs/map_name_20260912_175553/report.md`；同目录
  保存 `audit.json`、三份 `*_validation.json`、`map_validation.json`、`sample_mapping.json`、
  `current_runs_manifest.json`、源文件保护快照和执行日志。修复前检查及三任务验证结论均已同步飞书。

## 更新记录

- 2026-09-12：按 frames 修改时间及旧 source_manifest 确认，仅对 Li2/1QS/RI5 的 8 份人工补标
  数据重打 ep011200 标签。3/3 新 run 验证通过，接受 1,026、碰撞拒绝 26；1QS_5 更正为 B10
  后新增 37 个接受样本，其余 970 个候选动作完全一致。源数据与旧 manifest 校验不变，HWI 的
  两份 862 行仍为 null，未自动补标。保留旧模型与 7861 冻结输入；新旧成员对照与修订清单已落盘。

- 2026-09-12：修复已有采集任务普通重跑刷新 Meta 的风险；完整任务默认跳过、不完整任务停止，
  规范化器拒绝覆盖已有 Meta。只读核查保留了用户本日补标的六份文件，四份旧 Meta 仍无地图标签；
  未对真实数据重新下载、处理或自动回填标签。

- 2026-09-09：使用 ep011200 Rule-10 batch teacher 为 0901、0902、0903 三个指定任务生成 full 训练
  数据；3/3 原子提交成功，共 2,784 个候选、2,771 个接受、13 个碰撞拒绝。0902/0903 按实际五路
  相机生成并保留非标准集合 warning。全局 14 个 run、11,383 个接受样本验证 `all_passed=true`，
  验证报告 SHA-256 为 `29938d8ec42fc2287749f24138dd4ea9cc91e39bc95717160ee0b29afeb10ca5`；本轮
  teacher 已停止。
- 2026-09-09：完整发现并处理 2026-09-01 起的 6 个登记 OSS 采集任务；一次性修复 0902 任务的损坏
  视频分片后，3 个任务完整成功。0904 任务仍缺 2 个源 URL，0907 Meta 导出稳定断流，0908 导出服务
  稳定返回 502；三者停止重试，首轮结束时保留诊断。最终 17 份 manifest 中 16 份通过严格完整性
  核验，未运行 teacher 生成。随后用 `download-task.sh --delete-zip --delete-segments` 清理 3 个完整任务的 ZIP 和
  原始视频分片，释放 913,803,113 bytes；清理后 15 个规范 Meta、15 份成功 manifest 和 78 个连续
  视频保持存在。最终核验时三个失败任务目录已在输出根目录中消失，但不属于本轮清理命令的目标。

- 2026-09-08：新增转弯过滤式采集 Gate 和独立接入文档；沿用未来 1 m、`8 deg/m` 弧线口径，
  以未来 2 s 低平移/高 yaw 变化代理在线自转，并增加开始预看、结束迟滞及位姿异常失败安全。
  9/9 定向测试和 Python 语法编译通过；60 s、20 Hz 缓存下 `end` 单次调用由约 103 ms 优化到
  11.3 ms；不修改既有训练数据与 manifest。
- 2026-09-08：新增正常直线概率采集 `StraightGate`；默认每个固定节拍的合格机会以 1% 启动，
  每次锁定均匀抽取的 8–20 s 时长，并严格按时长结束。正常直线要求 2 s 平移 `>=0.10 m`、
  yaw 变化 `<=3 deg` 且无转弯证据；全套定向测试更新为 13/13 通过。
- 2026-09-08：对真实前视视频 `20260820180215WDK_1` 完成 Gate 回放及代表帧目视复核；固定 seed 42
  在当前 6–12 s 设置下得到 `[43.000,53.835] s` 直线区间；流式转弯 Gate 将右转从旧的
  `[82.200,85.400] s` 延长为 `[82.200,94.600] s`，并且不拆分视频尾部未确认恢复的连续左转。
  控制方法返回 `(bool, float)`，后者是准确动作节点；历史稀疏 pose 回放使用 2.0 s gap，线上默认
  0.5 s 不变。
- 2026-09-08：将直线默认片段从 8–20 s 缩短为 6–12 s；将 `TurnGate` 升级为结合当前缓存与
  历史活动/恢复状态的流式状态机，正常直线持续确认后才结束。全套定向测试更新为 16/16 通过。
- 2026-09-08：增加连续 3 s 稳定停车结束条件，以及两类 Gate 通用的默认 `Tmax=45 s` 硬截止；
  同一 WDK_1 视频尾部左转由“未结束”更新为 `[102.200,113.000] s`，结束原因为稳定停车。定向
  测试更新为 19/19 通过。
- 2026-09-08：将 `Tmax` 硬截止重构为通用 `@with_maximum_collection_interval` 方法装饰器，移除
  `TurnGate.end_collection()` 内的超时分支，并为 `StraightGate` 补齐统一开始节点属性。19/19 回归通过；
  WDK_1 三个输出区间逐项一致，未改变采集边界。
- 2026-09-13：将位姿跳变和连续 3 s 几乎不动提升为贯穿所有 Gate 的硬停止，并将装饰器扩展为
  `@with_hard_stop_conditions`；跳变结束点取跳变前最后有效 pose，稳定停车阈值为 3 cm/3 deg，且
  已验证 1 cm/0.5 deg 静态噪声及跨 pose-gap 的绝对跳变边界。
  Turn/Straight 定向回归 22/22 通过。只读复用现有 WDK_1 数据回放，三个区间逐项不变；未下载、
  解包、重处理或覆盖本地 `frames.jsonl`。
- 2026-09-02：动态学生方法版本统一为 v0，采用 joint temporal/decoder memory 融合；重建并绑定
  ep011200 Rule-10 数据。完成 overfit、1×/2× 三折和 bootstrap；2× 未通过后，用户明确选择 1× 并
  人工放行唯一失败的 OOF history-repeat 门槛，完成 7,654 条全量训练和 958 条一次性 locked test。
  随后确认被测 `final_ema.pt` 因错误平均 BatchNorm buffers 而失效；保留原始 test 审计，修复 EMA 为
  parameter-only，恢复 checkpoint 仅验证 train，不重开 locked test。
- 2026-09-02：删除全部旧训练产物后，使用 ep011200 batch 教师对 12 个已有采集任务全量重生成；
  11 个任务成功得到 8,612 个接受样本，HWI 因 0 合格候选失败且未重试。11/11 run 深度验证通过、
  错误 0，源数据文件数、字节数和元信息摘要前后完全一致；8103 教师已停止。
- 2026-09-01：收到“跳过 OSS 下载、删除旧训练产物并用
  `model_server/config_rule10_continuous_ep011200_sim.yaml` 全量重生成”的要求。预检确认保留输入根目录
  `/mnt/chengchangxu/data/visual_nav_mv`（12 个已有采集任务），拟清理范围仅为
  `/mnt/chengchangxu/data/visual_nav_training` 下的历史训练 run、batch 与 validation 产物；不设日期窗口和
  robot 过滤。当前指定配置及其 ep011200 checkpoint 均未同步到默认 model-server 树，预检失败，按
  “缺少 ep011200 资产不得回退 ep023000”约束停止；尚未删除旧数据、启动教师、生成或运行全量验证。
- 2026-09-01：对 2026-08-31 起的真实窗口完成 2/2 任务下载复核和 Rule-10 epoch 23000
  教师 full 生成，新增 328 个接受样本、9 个碰撞拒绝；全局 11 个 run 校验
  `all_passed=true`。
- 2026-09-01：新增 `vnav-oss-training-data` skill 和编排脚本，固定“日期发现 → 逐 task 下载/规范处理
  → 可选教师 full → 全局验证”边界；默认打标配置按最新指定切换为 ep011200，缺少配置时在下载前
  阻断且不回退 ep023000。创建校验未触发真实数据或教师运行。
- 2026-08-31：完成 Rule-10 无状态多帧重标注流水线与全部就绪采集任务的正式重生成；20% 零速
  教师/RGB 单图重复，其余按 pose 实际估速并保存真实历史。修复“最老帧不足 1.0 s 时 older 通道
  填零”后，目录修复前首批 6,813 个样本、修复后累计 8,280 个样本的两个历史通道空值均为 0；
  当前 9/9 run 深度验证、21/21 训练构建测试通过。`Li2` 与五路 `XXz` 已补生成；`HWI` 因 0 个
  合格候选、`8l1` 因无视频仍未生成。历史上删除 16.10 GB 旧 v1 数据；本轮四个处理目录的逐任务
  聚合 SHA-256 在生成前后完全一致。
- 2026-08-28：选择性同步算法交接工具；新增时间窗任务发现和 `all.zip` 多子任务
  自动展开，保留全子任务时钟回退、共享 `--jobs 4`、断点复用、原子下载和严格
  manifest 契约。鉴权仍仅来自环境变量；21/21 工具回归、两份真实 `all.zip`、
  53/53 项目测试和生产构建已验证。
- 2026-08-27：按每任务最新成功授权 full run 去重，复核当前 6 个任务共有 9,415 个已物化训练样本，
  另有 128 个碰撞拒绝标签；run 声明、标签行数和 case 目录数 6/6 一致。
- 2026-08-27：训练检查页相机复用采集预览的方位槽位；新增只读 run/case API，以及由
  `inputs.npz` 权威数组按需派生的静态地图裁剪、forward route、rollout 叠加图和保存格式说明。
  预览只做内存缓存，不写回训练 case；53 项测试与真实全量 run 桌面/390px 验收通过，地图四层
  均非空且浏览器无错误或失败请求。实现已迁入正式项目目录并从该目录启动 5182，不依赖 Codex worktree。
- 2026-08-27：只读验证 OSS 已有任务发现方式。看板 API 共返回 28 个任务，其中按 ready 连续视频
  `object_key` 保守确认 13 个任务、`232/248` 个对象已就绪；确认 `video_upload=uploading` 不能作为
  已有判据，3 个反例的全部子任务均无 OSS 段。当前凭据不能遍历 bucket，无法发现未登记孤立对象。
- 2026-08-27：用户确认 `review_005` 满意后完成 `run_20260827_012110` 全量生成；2555 个候选中
  2543 个接受、12 个碰撞回放拒绝。4.3 GB 产物的六路文件、融合二值值域、静态障碍并集、hash、
  同步、路线与动作范围逐样本验收通过；拒绝原因完整保留，8103 已停止。
- 2026-08-27：路线版本更新为 `pose_truncate_rdp_turn_v2`，首次短时间位姿跳变后硬丢弃整个尾段；
  教师局部输入更新为 `local_static_obstacle_union_v1`。新增 2 项核心测试并完成 `review_005` 12-case
  Pilot：12/12 位于跳变前、融合重算一致、同步/路线/确定性/速度边界通过、0 碰撞；全量仍未授权。
- 2026-08-26：新增 `processing/training-data-builder/` 训练数据流水线与 7 项核心测试；完成
  `review_004` 的 12-case pilot。12/12 标签、同步、确定性、稳定内容 hash、碰撞回放和路线
  `0.10 m / 5 deg` 门槛均通过；生成 overview 与逐 case 高清联合图。全量安全门保持关闭，等待人工验收。
- 2026-08-26：记录并细化真实多视角数据使用雷达端到端速度服务生成伪标签的处理方案；确定 pose
  路线稀疏化、教师 NPZ 地图按 `grid_pose` 旋转裁剪和 forward-route 栅格化公式、20/80 初始状态
  采样、Rule-6 曲率限速、PNG 值域转换、地图别名、原始/相对初态 action chunk 与教师 provenance
  落盘、质量门槛及 pilot 验收。确认当前 `curvature_only` Rule-7 不消费线角速度幅值；需要速度条件
  教师时必须更换 backend。当前仅完成接口核对和方案设计，尚未生成伪标签；`Lift_map` 因教师缺少
  静态地图明确列为第一版不支持。
- 2026-08-26：实时数据位姿区新增第四个 `map_name` 值卡片，展示数据集级一致标签；未标注或
  标签不一致时显示 `—`。完整测试 47/47 通过，TypeScript 检查与 Vite 生产构建通过；不改变
  `frames.jsonl` 写入格式、训练同步或过滤参数。Chromium 验证最长标签 `Lift_map` 在桌面与
  390px 窄屏均保持四卡同排、无文本或页面横向溢出，控制台错误和页面异常均为 0。
- 2026-08-26：5173 数据预览页新增数据集级地图标签确认；服务端仅接受四个固定枚举值，
  校验 `frames.jsonl` 全部非空行后原子写入/覆盖每行 `map_name`。单元测试验证 4/4 行写入、
  标签回读、非法枚举拒绝及坏 JSON 时文件保持不变；隔离数据集的 Chromium 端到端验证确认
  2/2 行写入、刷新后标签回读、390px 窄屏无横向溢出，且无控制台错误、页面异常或失败请求。
  训练同步、过滤和采样参数不变。
- 2026-08-26：修复 React StrictMode 首次 effect 清理后占据图缓存永久失效的问题；LRU 增加当前方向
  13 帧保护窗口，并用前向/反向三批次压力测试和 20 秒浏览器时间轴验证确认不再周期性进入同步占位。
  预览批次、缓存上限、训练时间匹配和过滤参数均保持不变。
- 2026-08-25：占据图改为 32 帧内存批传输、12 帧低水位补批和 64 张/64MiB 有界缓存；移除旧图掩盖策略，缺帧时与视频同步停启；1000ms RTT 下连续 45 帧同步验证通过。
- 2026-08-25：占据图预览改为方向感知预取、后台预解码和原子切换，消除逐帧加载闪烁；训练对齐规则不变。
- 2026-08-25：记录远程预览实时 GPU 降码率、整组缓冲同步与占据图有界内存预取约定；明确预览产物不落盘且不进入训练。
- 2026-08-25：补充训练清洗分层、首尾静止迟滞检测参数、跨模态冻结判定、图像/位姿/占据图质量检查、去重采样与防数据泄漏策略；记录 5 个片段的首尾静止只读初检结果。
- 2026-08-25：建立本文；确认占据图锚定的视频检索方向、忽略不准确速度、推荐同步过滤参数、当前数据统计、`wall_clock` 限制，以及预览与训练匹配分离策略。
