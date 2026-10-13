# G1-T0-C1数据库/权威导出一致性对比

- 纠正后物理T0 Evidence行数：61（预期61）
- 逻辑Evidence ID数：60（预期60）
- 最新版本分布：{'1': 59, '2': 1}（预期v1=59、v2=1）
- 目标Evidence版本：[(1,), (2,)]（预期[1, 2]）
- v1 content_hash纠正前/后：90a27931067c9d49bd7623994eeb9e2117b99814fbe2b1c60a9bb20a95c7220c / 90a27931067c9d49bd7623994eeb9e2117b99814fbe2b1c60a9bb20a95c7220c；未变化=True
- v2性质：resource_input
- v2维度：['engineering_resource_conditions', 'application_maturity']
- v2声明：REMA项目首个开发周期于2023年12月启动。
- canonical JSONL直接读取数据库每个evidence_id的最大version，并按evidence_id排序。
- created_at直接保留数据库payload值，未重新生成。
- exclude项和split父项未进入最新Evidence集合。
