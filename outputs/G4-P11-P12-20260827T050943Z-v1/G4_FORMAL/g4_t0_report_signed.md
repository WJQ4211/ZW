# 美国军用人工智能T0案例报告（v2待签发草案）

状态：`signed`。本稿基于G3 v5的G4草案修订，原稿SHA-256为`15dd27078f2289217cdbb9ad72a46eee5fdceb1c364b21432cb08bd25fbd48b4`；本稿已由作者整体签核并正式签发G4-T0。

## 1. 问题与证据边界

研究问题是：截至2024年12月31日，公开、非密材料所呈现的美国军用人工智能能力建设处于何种主要演进状态。T0时间边界为2022年1月1日至2024年12月31日。本文只使用已冻结的T0证据快照、正式G2假设、已完成的正式G3观点与分歧治理结果，不读取T1、不联网、不调用模型/API。`excerpt_original`是事实核查的首要字段；来源等级表示来源质量，不等于证据方向。

## 2. 共享事实基线

共享基线是：截至2024年底，美国军用人工智能已在若干特定任务中由技术原型走向试验验证和有限部署，但尚未形成成熟的跨军种、跨任务通用能力体系。该基线属于共同事实层，不是竞争性假设。治理文件、试验活动、局部部署、预算投入和约束材料必须按各自性质解释，不能互相替代。

## 3. 代表性案例事实

### 3.1 战略治理与共用底座

- **EV-T0-0A73030BBF2649F9（pdf_page_17）**：表明CDAO承担国防部AI政策实施与监督协调，属于治理职责事实。 原文：As outlined in the RAl Memo, the Office of the Chief Digital and Artificial Intelligence Officer (CDAO), as the successor organization to the Joint Artificial Imelligence Center (JAJC), serves as the Depaitment's lead for coordinating the implementation and oversight of guidance and policy on Al, including RAT and the DoD A l Ethical Principles.
- **EV-T0-7E37FBD511FC654E（pdf_page_45）**：表明JCF的目标是整合不同AI应用，属于整合意图或计划目标。 原文：According to DOD documentation, the Joint Common Foundation will bring disparate AI applications under a single platform and scale AI across the department.
- **EV-T0-51D65DEC82584BCD（pdf_page_82）**：表明预算、合同和疫情因素推迟JCF全面作战能力，限制“整合已经实现”的判断。 原文：According to a JAIC official and DOD documentation, budgetary and contracting issues (e.g., the cancellation of the Joint Enterprise Defense Infrastructure cloud contract), as well as the impacts of COVID-19, have delayed the Joint Common Foundation reaching full operational capability and an estimated date for that milestone has not been determined.
- **EV-T0-A515E3B9391C51B4（pdf_page_83）**：表明JCF路线图曾出现延迟，同样限制把治理目标直接写成运行结果。 原文：According to DOD officials, DOD planned to finalize a roadmap or high-level plan with milestones for the Joint Common Foundation by August 2021, but has experienced multiple delays, and as of November 2021 the roadmap was not finalized.

前两项支持“治理架构和整合方向已经形成”的事实判断，后两项支持“实际整合效果仍需验证”的限定判断。它们合在一起并不构成JCF已完成跨军种运行整合的证据。

### 3.2 技术试验与任务项目

- **EV-T0-DB4536B8270ECF58（web_paragraph_0009）**：记录ACE完成AI自主驾驶F-16与有人驾驶F-16的视距内空战测试，属于试验活动/结果。 原文：DARPA’s Air Combat Evolution (ACE) program has achieved the first-ever in-air tests of AI algorithms autonomously flying an F-16 against a human-piloted F-16 in within-visual-range combat scenarios (sometimes referred to as “dogfighting”).
- **EV-T0-DDA30BF3C0C38B0C（web_paragraph_0004）**：记录RACER实验完成55次以上无人运行、246英里和24.6小时，属于试验结果。 原文：During the most recent experiment, teams completed more than 55 driverless runs of between roughly four and 11 miles each, reaching speeds of about 25 miles per hour. The performers completed 246 miles over 24.6 total hours on course with a robotic fleet of 12 RFVs.
- **EV-T0-9C79EF2837BC80A2（web_paragraph_0009）**：记录REMA在失去操作员连接后自主继续预定义任务的目标，属于项目目标。 原文：DARPA’s Rapid Experimental Missionized Autonomy (REMA) program aims to enable a drone to autonomously continue its predefined mission when connection to the operator is lost.

ACE和RACER可以支持技术试验已经发生以及部分观察结果，REMA只支持项目目标。三者均不能直接改写为规模化采办、列装或跨军种通用能力；试验完成也不等于作战效能已经被普遍验证。

### 3.3 局部部署与任务应用

- **EV-T0-A8459E6E8776A15D（web_paragraph_0006）**：陆军官方称AIDP在不到12个月内向优先战区部署并投入使用，属于官方部署事件。 原文：The Army deployed and operationalized AIDP rapidly to units in all priority theaters in under 12 months, showing just how important this new and improved intel tool is to Soldiers and DoD.
- **EV-T0-F0773C8E2ECB5C8D（web_paragraph_0007）**：记录IMSC海上演习整合无人系统和人工智能，属于演习事件。 原文：The International Maritime Security Construct (IMSC) completed a three-day maritime exercise in the Arabian Gulf, Jan. 9, integrating unmanned systems and artificial intelligence during a naval drill for the second time in six months.

AIDP是重要的官方局部部署案例，但不代表全部军种具有相同成熟度；IMSC说明演习中的系统整合发生过，但不等于实战效能验证。上述差异正是共享基线中“特定任务、有限部署”的具体表现。

### 3.4 资源投入与约束

- **EV-T0-AD4CCAC18E1BB26C（web_paragraph_0012）**：记录机载AI受到处理速度限制，属于工程约束。 原文：Onboard AI systems are constrained for several reasons, but the primary factor is processing speed.
- **EV-T0-62B1ECDA7DF87F1A（pdf_page_25）**：记录缺乏基于测试标准的组织化方法会影响数据质量，属于资源/治理约束。 原文：Absent an organized approach based on tested criteria, Components will face significant challenges developing the level of data quality required to address analytics and artificial intelligence requirements.
- **EV-T0-8E35B508DF5C2E65（pdf_page_2）**：记录全部门AI采办指导方针缺少具体计划和时间表，属于治理与采办约束。 原文：DOD is in the process of planning to develop such guidance, but it has not defined concrete plans and has no timeline to do so.
- **EV-T0-0405886E04C205A6（pdf_page_27）**：记录2023财年核心AI预算申请11亿美元，属于资源投入。 原文：given that DOD is investing considerable effort and funds toward developing and acquiring AI tools and capabilities and requested $1.1 billion for core AI in fiscal year 2023
- **EV-T0-FB39056D1BA1F579（web_paragraph_0008）**：记录DARPA收集超过100TB传感器数据，属于数据资源投入。 原文：To further support software development, DARPA has also collected over 100 terabytes of RFV-based sensor data from more than 500 kilometers of terrain in the Mid-Atlantic and West Coast.

预算和数据投入不能直接推出瓶颈已经解决，也不能从投入规模直接推出能力成熟度。约束证据与投入证据必须并列呈现，由后续材料检验投入是否产生了工程和应用变化。

## 4. 三项竞争性假设

HYP-T0-01（体系整合牵引型）认为，部门级统一治理、共用能力平台和标准化流程正在形成体系整合过渡期；HYP-T0-02（任务分布牵引型）认为，实际能力形成仍由具体任务需求牵引，通过分散试验、技术演示和局部部署逐步扩展；HYP-T0-03（约束锁定渐进型）认为，数据、算力、基础设施、采办和测试评估约束共同造成进展缓慢及应用扩展不均衡。三项假设绑定同一快照，均不是已经证实的最终结论。

## 5. 五视角评价结果

T0观点生成与治理阶段采用STRAT、TECH、ENG、OPS、TRUST五个专业视角，对每项假设各形成一个最新观点，共15条。视角只是一种分析组织方式，不等同于真实专家访谈；观点中的支持、条件和不确定性必须回到具体Evidence。结构上，HYP-T0-01有5个conditional，HYP-T0-02有4个support和1个conditional，HYP-T0-03有4个support和1个conditional。这些数量说明观点结构和条件化程度，不构成投票、排名或多数表决。

## 6. 证据指向式质疑如何改变观点

P8R的真实修订记录提供了可追溯的纠偏路径。VP-T0-STRAT-H01将“治理架构已建立”收窄为“治理架构处于规划建设阶段、实际牵引效果待验证”；VP-T0-STRAT-H02将“整合目标未产生影响”改为“现有证据不足以证明已产生实质影响”；VP-T0-TECH-H02将“未发生跨项目复用”改为“现有公开证据未显示复用，不能排除未公开机制”；VP-T0-TECH-H01重新处理JCF延迟，将其作为支持工程实现滞后的限制性事实，而非简单的体系整合反证。每个实例都保留原稳定ID、修订动作和字段级依赖，避免把证据缺失误写成事件不存在。

## 7. 分歧与伪分歧治理

正式G3保留两项条件性分歧：DISAG-T0-01讨论治理领先信号与跨军种实际复用的关系；DISAG-T0-02讨论资源和治理瓶颈与任务差异常态之间的相对解释力。DISAG-T0-03通过“治理架构建立—运行整合实现”的范围拆分解决。范围拆分避免把制度建立和运行效果混在同一个事实命题中；监测建议则通过确定性映射把重复内容归并为三项判别性需求，原始P9R模型响应未修改。

## 8. 条件化综合判断

当前证据支持“任务驱动的分散试验、演示和局部部署”为主要可观察形态；资源和治理约束解释其发展不均衡；体系整合是具有制度与规划依据、但尚待跨军种实际复用验证的备择方向。HYP-T0-02构成当前状态判断的主要解释，HYP-T0-03解释状态延续的约束机制，HYP-T0-01作为条件性备择保留。这个判断不是把观点数量简单相加，也不是把A级来源数量当作方向投票。

## 9. 判别性监测和转换条件

正式G3形成3项判别性需求、4项指标和8条触发规则，观察窗口为2025年1月1日至2026年6月30日，来源计划限于公开可核验的A/B级材料。监测分别观察共用平台实际采用、项目由演示转入应用的路径、资源与治理瓶颈状态，以及瓶颈变化后的应用广度。跨军种或跨任务实际复用增强HYP-T0-01；任务专属采办或部署增强HYP-T0-02；明确约束使项目继续停留在演示增强HYP-T0-03。瓶颈缓解但分布式模式不变时，需削弱“约束是唯一主因”的解释；瓶颈缓解并伴随应用扩展时，三项假设共同重评。否定性判断不得由“未搜索到材料”单独推出，必须有完整检索和人工确认。

## 10. 证据局限与G4待签发事项

T0只覆盖公开、非密材料和2024年底以前的窗口，不能替代T1状态数据，也不能证明未公开项目状态。正式G3已经完成；当前未完成的是T1更新和对照实验。本报告不宣称已经预测未来，不宣称提高准确率，不把模型观点写成专家逐条评价，也不把局部试验写成全面部署。作者整体签核已完成；后续工作是T1更新和对照实验。
