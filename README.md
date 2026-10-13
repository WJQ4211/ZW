# 分歧驱动的持续研判原型：阶段 A+

这是《分歧驱动的国防科技情报持续研判：最小可运行研究原型实现设计》的阶段 A+ 加固实现。

阶段 A/A+ 只做以下事情：

- 用合成证据导入并冻结 T0/T1 证据快照；
- 用手工 JSON 冻结 2 条竞争性假设；
- 用手工 JSON 写入 4 个观点、2 个分歧和监测计划；
- 通过证据依赖、触发器和关键词兜底扫描定位 T1 受影响观点；
- 只为受影响观点追加新版本，保留未受影响观点的原版本；
- 生成可反查证据与版本的 Markdown 报告和运行清单；
- 用 pytest 验证快照不可变、引用完整、版本追加和局部更新；
- 将审核对象绑定到案例、gate、run、文件路径、文件哈希和创建时间；
- 按 case_id 隔离证据、快照、观点、分歧和监测计划；
- 通过 SQLite 触发器和存储层双重约束实现快照冻结及证据/观点历史追加式存储；
- 将 T1 触发证据、关系类型和字段级变化写入 VP 新版本依赖；
- 保留原始系统候选集，单独记录专家参考集、漏检和误报；
- 保存输入、配置、快照和代码版本哈希，并分离 pipeline_stage 与 model_call 日志。

本阶段不导入真实案例、不联网、不调用大模型，也不实现前端。`synthetic: true` 是示例对象的强制标记，示例输出不应作为论文案例结论使用。

## 快速运行

在本目录执行：

```text
python -m src.pipeline run-synthetic
pytest
```

等价脚本入口：`python scripts/01_init_db.py` 建库/迁移，`python scripts/02_run_phase_a_demo.py` 运行合成验收流程。

演示命令使用 `fixtures/approvals/` 中预置的合成审核内容作为模板。运行时仍会先在 `outputs/<run_id>/reviews/` 生成带绑定字段的 pending 文件，再由模板填充并通过绑定校验；这些模板不代表真实专家签发。

每个 pending 文件必须保持以下绑定字段原样不变：`case_id`、`gate`、`run_id`、`artifact_path`、`artifact_hash`、`created_at`。文件内容或审核对象发生变化时，应开启新的运行，不应复用旧审核。

也可以按设计文档中的命令逐步运行：

```text
python -m src.pipeline init-case --config config/case_synthetic.yaml
python -m src.pipeline ingest --case synthetic_military_ai --phase t0 --input data/raw/t0
python -m src.pipeline freeze --case synthetic_military_ai --phase t0 --approve-file fixtures/approvals/g1_t0.json
python -m src.pipeline hypotheses --case synthetic_military_ai --snapshot <T0快照ID> --input fixtures/hypotheses_t0.json --approve-file fixtures/approvals/g2.json
python -m src.pipeline analyze-t0 --case synthetic_military_ai --snapshot <T0快照ID> --input-dir fixtures/analysis_t0 --approve-monitor-file fixtures/approvals/g3.json --approve-report-file fixtures/approvals/g4_t0.json
python -m src.pipeline ingest --case synthetic_military_ai --phase t1 --input data/raw/t1
python -m src.pipeline freeze --case synthetic_military_ai --phase t1 --approve-file fixtures/approvals/g1_t1.json
python -m src.pipeline update-t1 --case synthetic_military_ai --from <T0快照ID> --to <T1快照ID> --input fixtures/update_t1.json --approve-affected-file fixtures/approvals/affected_t1.json --approve-report-file fixtures/approvals/g4_t1.json
```

默认数据库为 `data/research.sqlite3`，运行产物位于 `outputs/<run_id>/`。可以用 `--workspace` 指定另一份工作目录。旧版阶段 A 数据库首次打开时会迁移 evidence、snapshot_items 和 evidence_dependency 的 case-scoped 外键结构，并回填新增的审计字段。

## 协议第 12 节 P0

P0 已建立 `config/case_us_military_ai.yaml`、`data/curated/` 下的 source manifest/search log 空模板、`prompts/` 下的 Luna 资料搜集和证据抽取模板，以及 `data/raw/t1_sealed/` 的读取保护。T1 封存目录在 G4-T0 审核记录写入前不可读取。P0 只建立结构和合成测试，不填入真实材料、竞争性假设或专家结论。

## 明确未实现内容

阶段 B 的 `ModelClient`、提示模板、模型调用、结构化输出重试、真实材料抽取和语义检索均未实现。完成阶段 A 验收前不会接入这些内容。
