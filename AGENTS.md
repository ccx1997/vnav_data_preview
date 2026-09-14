# AGENTS.md

- Whenever a conversation or task involves training-data reading, synchronization, sampling, filtering, alignment, trimming, export, or preprocessing, update `readme_training.md` in the same change with the new decisions, parameters, and validation results.
- 已下载并处理的采集任务必须复用，禁止自动重新下载、重新解包或重新处理。保护本地 `frames.jsonl` 中人工标注的 `map_name` 和裁剪结果；已有数据未通过完整性检查时只报告问题，不得通过刷新导出覆盖现场。只有用户明确指定重处理范围时才执行对应操作。

After each experiment or test, please send the status, conclusions or analysis to Feishu message by the skill `send-feishu-experiment`, if available.
