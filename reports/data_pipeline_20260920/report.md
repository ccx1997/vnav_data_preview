# 四阶段数据流水线脚本验证

新增 `processing/run-data-pipeline.sh`，接收任务 ID/列表或日期，返回源数据与标注目录 JSON。

- 78/78 回归通过，其中新编排 28 项、训练构建 21 项、像素转换 29 项。
- WIt/8Up 真实复用均成功：835/940 条教师和像素记录，791/891 条无黑帧清单。三阶段复用标志全部为 true，源文件前后未变。
- 验证期间没有下载、重跑教师或重导出已有像素数据；新建/失败/中断分支使用隔离替身验证。
- 日期接口首次收到带时区 ISO 时间返回 HTTP 400；改为 Unix 秒后，2026-09-20 起的真实只读查询成功发现两个任务。
- stdout 能直接解析为单个 JSON，内容与 --result-json 文件一致；所有返回目录均存在。

证据：`reuse_result.json`、`date_plan_fixed.json`、`regression_final.log`、`validation_summary.json`。首次日期失败记录和修复前回归日志保留用于审计。
