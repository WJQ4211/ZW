# T0 条件化研判报告（阶段A合成数据）

> 本报告由无大模型的手工 JSON 状态机生成，仅用于软件验收，不代表真实案例结论。

- case: `synthetic_military_ai`
- snapshot: `SNAP-synthetic_military_ai-T0-3db2ee3ae0bf`
- cutoff: `2025-12-31T23:59:59+00:00`
- evidence manifest: `3db2ee3ae0bf9f1c223ff20c03297e26bac5bd80ee25067e1bab482131c1bf7b`
- 主判断签发：待 G4 人工填写；本阶段不自动替代专家判断。

## 冻结假设

### HYP-A

能力建设首先形成受约束、可审计的任务级自治，并在有限任务中逐步扩展。

- 可观察含义：出现受约束自治试验；人机交接与测试评价同步制度化
- 可证伪条件：多个项目在无约束条件下直接形成稳定通用自治；受约束任务的重复测试长期无法通过

### HYP-B

能力建设主要停留在模块化辅助工具与人工监督集成，短期内不会形成稳定的任务级自治。

- 可观察含义：采购和部署集中于辅助模块；授权要求持续把关键决策留在人类操作员手中
- 可证伪条件：受约束自治在多项任务中完成可重复的端到端测试；正式项目文件明确扩大任务级授权

## 观点与证据

### VP-TECH-A v1（technical/HYP-A）

- stance: `support`；status: `admitted`
- judgment: 受约束自治已有技术试验与名义条件测试支撑，但边界条件仍限制外推。
- evidence: `EV-T0-001:1`, `EV-T0-002:1`, `EV-T0-004:1`
- key assumptions: bounded autonomy can preserve operator handoff under edge cases
- boundaries: not generalizable to unrestricted autonomy or untested environments
- falsifiers: repeated edge-case failures block the constrained task

### VP-TECH-B v1（technical/HYP-B）

- stance: `challenge`；status: `conditional`
- judgment: 边缘情形不稳定且治理约束尚未解决，使模块化辅助工具更可能先于稳定任务级自治扩展。
- evidence: `EV-T0-002:1`, `EV-T0-001:1`, `EV-T0-003:1`
- key assumptions: edge-case instability remains material for deployment authorization
- boundaries: does not deny bounded pilots or later transition
- falsifiers: repeatable field trials reduce edge-case instability and obtain authorization

### VP-OPS-A v1（operations/HYP-A）

- stance: `conditional`；status: `conditional`
- judgment: 若人机交接和任务编组能嵌入流程，受约束自治可能形成可用的体系化应用。
- evidence: `EV-T0-003:1`, `EV-T0-004:1`
- key assumptions: operator handoff is accepted in the target task workflow
- boundaries: not applicable where authorization cannot be delegated
- falsifiers: operator workload or authorization rules prevent routine handoff

### VP-OPS-B v1（operations/HYP-B）

- stance: `support`；status: `admitted`
- judgment: 治理和授权要求使人工监督下的模块化辅助更符合当前体系运用条件。
- evidence: `EV-T0-004:1`, `EV-T0-003:1`, `EV-T0-001:1`
- key assumptions: authorization remains contingent on operator workload and assurance
- boundaries: does not cover a formally approved autonomous task sequence
- falsifiers: official authorization expands after a successful operator workload evaluation

## 未决分歧

- **DIS-001** `assumption` / `high`：边缘情形下受约束自治能否保持可审计的人机交接；处置：条件保留并监测可重复的受约束自治试验。
  - A: 受约束任务的测试链足以支持逐步扩展
  - B: 边缘情形不稳定会使模块化辅助长期占主导
- **DIS-002** `mechanism` / `high`：人机交接是体系化采用的扩展机制还是持续的授权瓶颈；处置：条件保留并跟踪操作员负荷评估与正式授权变化。
  - A: 人机交接嵌入任务流程后可推动体系化采用
  - B: 授权和操作员负荷约束会把应用锁定在辅助工具

## 判别性证据与监测计划

- **NEED-001**：后续受约束自治试验是否在联合任务序列中完成可重复的人机交接？；判别性评分：5。
- **NEED-002**：操作员负荷评估和授权文件是否允许把人机交接嵌入例行任务流程？；判别性评分：4。
  - 指标 `IND-001`：受约束自治联合任务试验；A方向：出现可重复的人机交接和任务边界扩大；B方向：出现边缘情形失败或逐项人工授权。
  - 指标 `IND-002`：操作员负荷与授权变化；A方向：负荷评估通过且授权扩大；B方向：负荷或授权约束保持不变。
  - 触发器 `TRG-001`：event_match `{"event_terms": ["bounded autonomy", "joint task integration"]}`；动作：review_viewpoints。
  - 触发器 `TRG-002`：event_match `{"event_terms": ["operator workload", "authorization review"]}`；动作：review_viewpoints。

## 审计回链

报告中的证据标识均以 `evidence_id:version` 形式引用；观点版本、证据依赖和人工审核记录保存在 SQLite 与运行产物中。
