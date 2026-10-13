# T1 局部更新报告（阶段A合成数据）

> 本报告由无大模型的手工 JSON 局部更新状态机生成，仅用于软件验收。

- case: `synthetic_military_ai`
- from snapshot: `SNAP-synthetic_military_ai-T0-3db2ee3ae0bf`
- to snapshot: `SNAP-synthetic_military_ai-T1-f97219be4909`
- updated viewpoints: `VP-OPS-B v2`, `VP-TECH-A v2`
- unchanged viewpoints: `VP-OPS-A`, `VP-TECH-B`

## 当前观点版本

### VP-OPS-A v1

- stance: `conditional`
- judgment: 若人机交接和任务编组能嵌入流程，受约束自治可能形成可用的体系化应用。
- evidence: `EV-T0-003:1`, `EV-T0-004:1`
- version reason: initial_version

### VP-OPS-B v2

- stance: `conditional`
- judgment: 新增采购要求把操作员负荷评估置于更宽授权之前，模块化辅助仍是当前基线，但体系化扩展存在条件性迹象。
- evidence: `EV-T0-004:1`, `EV-T0-003:1`, `EV-T0-001:1`
- version reason: T1 合成采购材料命中操作员负荷与授权监测指标，改变了对扩展条件的表述，但没有证明授权已经扩大。

- T1 trigger evidence: `EV-T1-002:1` limit → stance, judgment, uncertainties[0]

### VP-TECH-A v2

- stance: `conditional`
- judgment: 新增试验使受约束自治的可行性更有支撑，但授权和边缘情形仍需继续验证。
- evidence: `EV-T0-001:1`, `EV-T0-002:1`, `EV-T0-004:1`
- version reason: T1 合成试验命中受约束自治监测指标，补充了联合任务与人机交接信号，但尚未消除边缘情形和授权不确定性。

- T1 trigger evidence: `EV-T1-001:1` support → stance, judgment, key_assumptions[0]

### VP-TECH-B v1

- stance: `challenge`
- judgment: 边缘情形不稳定且治理约束尚未解决，使模块化辅助工具更可能先于稳定任务级自治扩展。
- evidence: `EV-T0-002:1`, `EV-T0-001:1`, `EV-T0-003:1`
- version reason: initial_version

## 字段级变化记录

- `VP-OPS-B` v1→v2: stance, judgment, uncertainties[0]; T1 合成采购材料命中操作员负荷与授权监测指标，改变了对扩展条件的表述，但没有证明授权已经扩大。
- `VP-TECH-A` v1→v2: stance, judgment, key_assumptions[0]; T1 合成试验命中受约束自治监测指标，补充了联合任务与人机交接信号，但尚未消除边缘情形和授权不确定性。

## 影响定位评估

- system candidates: `VP-OPS-A`, `VP-OPS-B`, `VP-TECH-A`, `VP-TECH-B`
- expert reference set: `VP-OPS-B`, `VP-TECH-A`
- intersection: `VP-OPS-B`, `VP-TECH-A`
- missed: none
- false positives: `VP-OPS-A`, `VP-TECH-B`

## 影响定位路径

- delta_evidence: `EV-T1-001:1`, `EV-T1-002:1`, `EV-T1-003:1`
- paths:VP-TECH-A: `EV-T1-001:1`
- paths:VP-TECH-B: `EV-T1-001:1`
- paths:VP-OPS-A: `EV-T1-001:1`, `EV-T1-002:1`
- paths:VP-OPS-B: `EV-T1-002:1`
