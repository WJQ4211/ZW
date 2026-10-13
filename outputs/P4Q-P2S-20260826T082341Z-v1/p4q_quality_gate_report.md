# P4Q冻结前门禁审计报告

- canonical逻辑Evidence：60
- SQLite物理T0 Evidence记录：61
- SQLite最大版本逻辑Evidence：60
- canonical与最大版本字段差异：0
- 正式证据来源数：9
- A级片段：31；B级片段：29；D级片段：0
- A级片段占比：31/60=0.516667
- published_at晚于2024-12-31：0
- snapshot数量：0；snapshot_items数量：0

## 门禁结论

- p4_gate_open=false
- 阻断原因：A级正式证据片段占比51.67%，低于60%
- 分母保持为正式Evidence片段总数60；未使用A级来源文件数替代片段分母。

## P2S准备边界

- 本运行仅归档5份原始网页及正文文本，未抽取Evidence、未修改Evidence、未调用ModelClient/API。
- P2S材料仍为补充候选，p2s_review_decision保持pending_author_review，不自动进入正式证据池。
