# P7R–P9R质量报告

本运行是对旧P7–P9结果的追加式恢复，不重跑P4、P5、G2，不重新收集Evidence。
旧运行的12个观点均为suspended，且没有依赖边；旧manifest关于field-level dependencies的complete标记已在本运行中更正。

## P7R/P8R观点

- 最新观点：15；admitted/conditional：15；suspended：0。
- 各视角准入数：{'ENG': 3, 'OPS': 3, 'STRAT': 3, 'TECH': 3, 'TRUST': 3}。
- 新增版本观点：12；STRAT v1：3。
- 字段级Evidence依赖边：94；依赖边随观点版本追加写入。

## P9R真实分歧

- 通过确定性分歧门禁：3；拒绝：0。
- 高影响且当前未决：1。措辞相近但没有实质相反分支的内容不建立分歧。
- 每条保留具体观点字段、分支和Evidence引用，供后续G3审核。

## 监测链条

- 判别性需求：0；指标：0；触发规则：0。
- 监测模型输出只在高影响、不可即时消除且resolution_action为conditional_retain/monitor时进入G3准备。
- 本轮被拒绝或不进入监测候选的记录：21；原因保存在P9R/monitoring_rejections.jsonl。
- 本轮监测模型原始输出含5项需求、8项指标和8项触发规则，但其分歧ID与通过门禁的DISAG-T0-*不匹配，确定性门禁全部拒绝；未进行自动改写或补造监测链。
- 若当前没有符合条件的高影响未决分歧，G3监测表保持空，不以模型数量凑出监测链条。

## 边界

- 本轮不写入正式disagreement或monitoring表，不执行正式G3/G4，不生成最终主判断，不读取T1。
