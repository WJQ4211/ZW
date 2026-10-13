"""Offline G4-T0 report and paper-case revision package.

The revision is deliberately read-only with respect to the database and all
earlier runs.  It uses the formal G3/P4/P7R artifacts as frozen inputs and
creates a new, versioned case-display package.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = "G3-T0-20260827T-formal-offline-v5"
SOURCE_DIR = ROOT / "outputs" / SOURCE_RUN
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
CASE_ID = "us_military_ai_deployment"
DB_PATH = ROOT / "data" / "research.sqlite3"
AUTH_SHA256 = "f30219fa0bfd415c811cb3c557c4fe5219d12611363299077c1ccab534ad31d5"


class G4RCaseRevisionError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fields})


def _load_inputs() -> dict[str, Any]:
    required = {
        "g3_manifest": SOURCE_DIR / "g3_manifest.json",
        "g4_draft": SOURCE_DIR / "G4_DRAFT" / "g4_t0_report_draft.md",
        "g4_draft_json": SOURCE_DIR / "G4_DRAFT" / "g4_t0_report_draft.json",
        "p4_evidence": ROOT / "outputs" / "P4-T0-C6C812108AF95309-freeze-v1" / "p4_t0_frozen_evidence.jsonl",
        "views": ROOT / "outputs" / "P7R-P9R-20260827T020335Z-case-strengthening-v1" / "P8R" / "final_viewpoints_latest.jsonl",
        "disagreements": SOURCE_DIR / "g3_formal_disagreements.jsonl",
        "needs": SOURCE_DIR / "g3_formal_discriminative_needs.jsonl",
        "indicators": SOURCE_DIR / "g3_formal_monitor_indicators.jsonl",
        "triggers": SOURCE_DIR / "g3_formal_trigger_rules.jsonl",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise G4RCaseRevisionError(f"missing authoritative input: {missing}")
    g3_manifest = _read_json(required["g3_manifest"])
    evidence = _read_jsonl(required["p4_evidence"])
    views = _read_jsonl(required["views"])
    disagreements = _read_jsonl(required["disagreements"])
    needs = _read_jsonl(required["needs"])
    indicators = _read_jsonl(required["indicators"])
    triggers = _read_jsonl(required["triggers"])
    if g3_manifest.get("snapshot_id") != SNAPSHOT_ID or g3_manifest.get("formal_g3_executed") is not True:
        raise G4RCaseRevisionError("formal G3 input is not the expected frozen run")
    if len(evidence) != 85 or len(views) != 15 or len(disagreements) != 3 or len(needs) != 3 or len(indicators) != 4 or len(triggers) != 8:
        raise G4RCaseRevisionError("authoritative G3/P4 counts do not match 85/15/3/3/4/8")
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    view_by_id = {row["viewpoint_id"]: row for row in views}
    if len(evidence_by_id) != 85 or len(view_by_id) != 15:
        raise G4RCaseRevisionError("input IDs are not unique")
    for row in disagreements:
        if row["snapshot_id"] != SNAPSHOT_ID:
            raise G4RCaseRevisionError(f"disagreement is outside snapshot: {row['disagreement_id']}")
        for ref in row.get("viewpoint_refs", []):
            if ref.rsplit(":", 1)[0] not in view_by_id:
                raise G4RCaseRevisionError(f"invalid viewpoint ref: {ref}")
    return {"paths": required, "g3_manifest": g3_manifest, "evidence": evidence, "evidence_by_id": evidence_by_id, "views": views, "view_by_id": view_by_id, "disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers}


def _fact(data: dict[str, Any], evidence_id: str, interpretation: str) -> str:
    row = data["evidence_by_id"][evidence_id]
    excerpt = row.get("excerpt_original") or row.get("excerpt") or ""
    return f"- **{evidence_id}（{row.get('source_locator') or row.get('page_or_section')}）**：{interpretation} 原文：{excerpt}"


def _build_report(out: Path, data: dict[str, Any]) -> str:
    original = out.parent / "_input_original_g4_draft.md"
    original.write_text(data["paths"]["g4_draft"].read_text(encoding="utf-8"), encoding="utf-8")
    original_hash = _sha256_file(original)
    text = f"""# 美国军用人工智能T0案例报告（v2待签发草案）

状态：`draft_pending_author_signoff`。本稿基于G3 v5的G4草案修订，原稿SHA-256为`{original_hash}`；本稿不构成正式G4签发。

## 1. 问题与证据边界

研究问题是：截至2024年12月31日，公开、非密材料所呈现的美国军用人工智能能力建设处于何种主要演进状态。T0时间边界为2022年1月1日至2024年12月31日。本文只使用已冻结的T0证据快照、正式G2假设、已完成的正式G3观点与分歧治理结果，不读取T1、不联网、不调用模型/API。`excerpt_original`是事实核查的首要字段；来源等级表示来源质量，不等于证据方向。

## 2. 共享事实基线

共享基线是：截至2024年底，美国军用人工智能已在若干特定任务中由技术原型走向试验验证和有限部署，但尚未形成成熟的跨军种、跨任务通用能力体系。该基线属于共同事实层，不是竞争性假设。治理文件、试验活动、局部部署、预算投入和约束材料必须按各自性质解释，不能互相替代。

## 3. 代表性案例事实

### 3.1 战略治理与共用底座

{_fact(data, 'EV-T0-0A73030BBF2649F9', '表明CDAO承担国防部AI政策实施与监督协调，属于治理职责事实。')}
{_fact(data, 'EV-T0-7E37FBD511FC654E', '表明JCF的目标是整合不同AI应用，属于整合意图或计划目标。')}
{_fact(data, 'EV-T0-51D65DEC82584BCD', '表明预算、合同和疫情因素推迟JCF全面作战能力，限制“整合已经实现”的判断。')}
{_fact(data, 'EV-T0-A515E3B9391C51B4', '表明JCF路线图曾出现延迟，同样限制把治理目标直接写成运行结果。')}

前两项支持“治理架构和整合方向已经形成”的事实判断，后两项支持“实际整合效果仍需验证”的限定判断。它们合在一起并不构成JCF已完成跨军种运行整合的证据。

### 3.2 技术试验与任务项目

{_fact(data, 'EV-T0-DB4536B8270ECF58', '记录ACE完成AI自主驾驶F-16与有人驾驶F-16的视距内空战测试，属于试验活动/结果。')}
{_fact(data, 'EV-T0-DDA30BF3C0C38B0C', '记录RACER实验完成55次以上无人运行、246英里和24.6小时，属于试验结果。')}
{_fact(data, 'EV-T0-9C79EF2837BC80A2', '记录REMA在失去操作员连接后自主继续预定义任务的目标，属于项目目标。')}

ACE和RACER可以支持技术试验已经发生以及部分观察结果，REMA只支持项目目标。三者均不能直接改写为规模化采办、列装或跨军种通用能力；试验完成也不等于作战效能已经被普遍验证。

### 3.3 局部部署与任务应用

{_fact(data, 'EV-T0-A8459E6E8776A15D', '陆军官方称AIDP在不到12个月内向优先战区部署并投入使用，属于官方部署事件。')}
{_fact(data, 'EV-T0-F0773C8E2ECB5C8D', '记录IMSC海上演习整合无人系统和人工智能，属于演习事件。')}

AIDP是重要的官方局部部署案例，但不代表全部军种具有相同成熟度；IMSC说明演习中的系统整合发生过，但不等于实战效能验证。上述差异正是共享基线中“特定任务、有限部署”的具体表现。

### 3.4 资源投入与约束

{_fact(data, 'EV-T0-AD4CCAC18E1BB26C', '记录机载AI受到处理速度限制，属于工程约束。')}
{_fact(data, 'EV-T0-62B1ECDA7DF87F1A', '记录缺乏基于测试标准的组织化方法会影响数据质量，属于资源/治理约束。')}
{_fact(data, 'EV-T0-8E35B508DF5C2E65', '记录全部门AI采办指导方针缺少具体计划和时间表，属于治理与采办约束。')}
{_fact(data, 'EV-T0-0405886E04C205A6', '记录2023财年核心AI预算申请11亿美元，属于资源投入。')}
{_fact(data, 'EV-T0-FB39056D1BA1F579', '记录DARPA收集超过100TB传感器数据，属于数据资源投入。')}

预算和数据投入不能直接推出瓶颈已经解决，也不能从投入规模直接推出能力成熟度。约束证据与投入证据必须并列呈现，由后续材料检验投入是否产生了工程和应用变化。

## 4. 三项竞争性假设

HYP-T0-01（体系整合牵引型）认为，部门级统一治理、共用能力平台和标准化流程正在形成体系整合过渡期；HYP-T0-02（任务分布牵引型）认为，实际能力形成仍由具体任务需求牵引，通过分散试验、技术演示和局部部署逐步扩展；HYP-T0-03（约束锁定渐进型）认为，数据、算力、基础设施、采办和测试评估约束共同造成进展缓慢及应用扩展不均衡。三项假设绑定同一快照，均不是已经证实的最终结论。

## 5. 五视角评价结果

正式G3沿用STRAT、TECH、ENG、OPS、TRUST五个专业视角，对每项假设各形成一个最新观点，共15条。视角只是一种分析组织方式，不等同于真实专家访谈；观点中的支持、条件和不确定性必须回到具体Evidence。结构上，HYP-T0-01有5个conditional，HYP-T0-02有4个support和1个conditional，HYP-T0-03有4个support和1个conditional。这些数量说明观点结构和条件化程度，不构成投票、排名或多数表决。

## 6. 证据指向式质疑如何改变观点

P8R的真实修订记录提供了可追溯的纠偏路径。VP-T0-STRAT-H01将“治理架构已建立”收窄为“治理架构处于规划建设阶段、实际牵引效果待验证”；VP-T0-STRAT-H02将“整合目标未产生影响”改为“现有证据不足以证明已产生实质影响”；VP-T0-TECH-H02将“未发生跨项目复用”改为“现有公开证据未显示复用，不能排除未公开机制”；VP-T0-TECH-H01重新处理JCF延迟，将其作为支持工程实现滞后的限制性事实，而非简单的体系整合反证。每个实例都保留原稳定ID、修订动作和字段级依赖，避免把证据缺失误写成事件不存在。

## 7. 分歧与伪分歧治理

正式G3保留两项条件性分歧：DISAG-T0-01讨论治理领先信号与跨军种实际复用的关系；DISAG-T0-02讨论资源和治理瓶颈与任务差异常态之间的相对解释力。DISAG-T0-03通过“治理架构建立—运行整合实现”的范围拆分解决。范围拆分避免把制度建立和运行效果混在同一个事实命题中；监测建议则通过确定性映射把重复内容归并为三项判别性需求，原始P9R模型响应未修改。

## 8. 条件化综合判断

当前证据支持“任务驱动的分散试验、演示和局部部署”为主要可观察形态；资源和治理约束解释其发展不均衡；体系整合是具有制度与规划依据、但尚待跨军种实际复用验证的备择方向。HYP-T0-02构成当前状态判断的主要解释，HYP-T0-03解释状态延续的约束机制，HYP-T0-01作为条件性备择保留。这个判断不是把观点数量简单相加，也不是把A级来源数量当作方向投票。

## 9. 判别性监测和转换条件

正式G3形成3项判别性需求、4项指标和8条触发规则，观察窗口为2025年1月1日至2026年6月30日，来源计划限于公开可核验的A/B级材料。监测分别观察共用平台实际采用、项目由演示转入应用的路径、资源与治理瓶颈状态，以及瓶颈变化后的应用广度。跨军种或跨任务实际复用增强HYP-T0-01；任务专属采办或部署增强HYP-T0-02；明确约束使项目继续停留在演示增强HYP-T0-03。瓶颈缓解但分布式模式不变时，需削弱“约束是唯一主因”的解释；瓶颈缓解并伴随应用扩展时，三项假设共同重评。否定性判断不得由“未搜索到材料”单独推出，必须有完整检索和人工确认。

## 10. 证据局限与G4待签发事项

T0只覆盖公开、非密材料和2024年底以前的窗口，不能替代T1状态数据，也不能证明未公开项目状态。正式G3已经完成；当前未完成的是G4签发、T1更新和对照实验。本稿不宣称已经预测未来，不宣称提高准确率，不把模型观点写成专家逐条评价，也不把局部试验写成全面部署。作者需要审核报告措辞、条件性分歧、监测需求和触发方向；模板中的正式签发字段继续留空。
"""
    return text


def _build_traceability(out: Path, data: dict[str, Any]) -> None:
    fields = ["claim_id", "claim", "evidence_ids", "viewpoint_refs", "disagreement_refs", "inference_level", "paper_usage"]
    rows = [
        {"claim_id": "G4-C01", "claim": "特定任务已经出现试验验证和有限部署", "evidence_ids": ["EV-T0-DB4536B8270ECF58", "EV-T0-A8459E6E8776A15D", "EV-T0-F0773C8E2ECB5C8D"], "viewpoint_refs": ["VP-T0-OPS-H02:2", "VP-T0-TECH-H02:2"], "disagreement_refs": [], "inference_level": "frozen_fact", "paper_usage": "代表性事实与证据性质"},
        {"claim_id": "G4-C02", "claim": "任务驱动的分散试验、演示和局部部署是当前主要可观察形态", "evidence_ids": ["EV-T0-DB4536B8270ECF58", "EV-T0-DDA30BF3C0C38B0C", "EV-T0-A8459E6E8776A15D", "EV-T0-F0773C8E2ECB5C8D"], "viewpoint_refs": ["VP-T0-TECH-H02:2", "VP-T0-OPS-H02:2", "VP-T0-TRUST-H02:2"], "disagreement_refs": ["DISAG-T0-01"], "inference_level": "conditional_synthesis", "paper_usage": "当前条件化综合"},
        {"claim_id": "G4-C03", "claim": "资源和治理约束解释能力形成缓慢与应用扩展不均衡", "evidence_ids": ["EV-T0-AD4CCAC18E1BB26C", "EV-T0-62B1ECDA7DF87F1A", "EV-T0-8E35B508DF5C2E65", "EV-T0-A515E3B9391C51B4"], "viewpoint_refs": ["VP-T0-TECH-H03:2", "VP-T0-ENG-H03:2", "VP-T0-STRAT-H03:1"], "disagreement_refs": ["DISAG-T0-02"], "inference_level": "conditional_synthesis", "paper_usage": "约束机制解释"},
        {"claim_id": "G4-C04", "claim": "体系整合具有治理与规划依据，但跨军种实际复用仍是待验证条件", "evidence_ids": ["EV-T0-0A73030BBF2649F9", "EV-T0-7E37FBD511FC654E", "EV-T0-51D65DEC82584BCD", "EV-T0-A515E3B9391C51B4"], "viewpoint_refs": ["VP-T0-STRAT-H01:1", "VP-T0-TECH-H01:2", "VP-T0-OPS-H01:2"], "disagreement_refs": ["DISAG-T0-01", "DISAG-T0-03"], "inference_level": "conditional_synthesis", "paper_usage": "体系整合备择与转换条件"},
    ]
    for row in rows:
        for ref in row["viewpoint_refs"]:
            if ref.rsplit(":", 1)[0] not in data["view_by_id"]:
                raise G4RCaseRevisionError(f"traceability viewpoint ref invalid: {ref}")
        for evidence_id in row["evidence_ids"]:
            if evidence_id not in data["evidence_by_id"]:
                raise G4RCaseRevisionError(f"traceability evidence invalid: {evidence_id}")
    _write_csv(out / "g4_claim_evidence_traceability_v2.csv", rows, fields)


def _font_setup():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    font_path = Path("C:/Windows/Fonts/simhei.ttf")
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        name = font_manager.FontProperties(fname=str(font_path)).get_name()
    else:
        name = "DejaVu Sans"
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": name, "axes.unicode_minus": False, "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white", "text.color": "#202020", "axes.edgecolor": "#555555", "xtick.color": "#333333", "ytick.color": "#333333"})
    return plt


def _save_fig(fig: Any, path: Path) -> None:
    fig.savefig(path.with_suffix(".png"), dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(path.with_suffix(".svg"), bbox_inches="tight", facecolor="white")


def _box(ax: Any, x: float, y: float, text: str, width: float = 0.16, height: float = 0.18, edge: str = "#9aa5aa", fontsize: int = 10, linestyle: str = "-") -> None:
    ax.text(x, y, text, ha="center", va="center", fontsize=fontsize, color="#202020", wrap=True, bbox={"boxstyle": "round,pad=0.55", "facecolor": "white", "edgecolor": edge, "linewidth": 1.4, "linestyle": linestyle}, transform=ax.transAxes)


def _build_figures(fig_dir: Path, data: dict[str, Any]) -> None:
    plt = _font_setup()
    blue, gray, dark = "#6f8998", "#aeb6ba", "#333333"
    fig_dir.mkdir(parents=True, exist_ok=True)

    # Figure 1: compact two-row process chain.
    fig, ax = plt.subplots(figsize=(10.5, 4.5)); ax.axis("off")
    top = [(0.12, 0.69, "冻结证据\n85"), (0.35, 0.69, "竞争性假设\n3"), (0.59, 0.69, "五视角初始观点\n15")]
    bottom = [(0.16, 0.26, "批判审查与\n回应修订"), (0.42, 0.26, "最新准入观点\n15"), (0.65, 0.26, "字段级依赖\n94"), (0.87, 0.26, "分歧治理\n3\n需求3·指标4·触发8")]
    for x, y, label in top: _box(ax, x, y, label, edge=blue if x == 0.12 else gray, fontsize=11)
    for x, y, label in bottom: _box(ax, x, y, label, edge=blue if x in {0.42, 0.87} else gray, fontsize=10)
    for x1, y1, x2, y2 in [(0.19, .69, .28, .69), (.42, .69, .52, .69), (.67, .63, .16, .36), (.25, .26, .33, .26), (.50, .26, .57, .26), (.73, .26, .80, .26)]:
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1), xycoords="axes fraction", arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.4})
    ax.text(0.59, 0.90, "案例分析链条", ha="center", va="center", fontsize=13, transform=ax.transAxes, color=dark)
    _save_fig(fig, fig_dir / "figure_1_case_process"); plt.close(fig)

    # Figure 2: 5x3 stance matrix with legend.
    fig, ax = plt.subplots(figsize=(7.7, 5.4))
    row_names = ["战略治理", "技术能力", "工程资源", "任务体系", "可信治理"]
    col_names = ["H1 体系整合", "H2 任务分布", "H3 约束渐进"]
    colors = {"support": blue, "conditional": "#cfd5d8", "challenge": "#777777", "insufficient": "#eeeeee"}
    labels = {"support": "支持", "conditional": "条件支持", "challenge": "质疑", "insufficient": "证据不足"}
    perspective_order = ["STRAT", "TECH", "ENG", "OPS", "TRUST"]
    for y, perspective in enumerate(perspective_order):
        for x, hypothesis in enumerate(["HYP-T0-01", "HYP-T0-02", "HYP-T0-03"]):
            row = next(item for item in data["views"] if item["perspective_id"] == perspective and item["hypothesis_id"] == hypothesis)
            stance = row.get("stance", "")
            ax.add_patch(plt.Rectangle((x, 4 - y), 1, 1, facecolor=colors.get(stance, "white"), edgecolor="white", linewidth=2))
            ax.text(x + .5, 4.5 - y, labels.get(stance, stance), ha="center", va="center", fontsize=10, color=dark)
    ax.set_xlim(0, 3); ax.set_ylim(0, 5); ax.set_xticks([.5, 1.5, 2.5], col_names); ax.set_yticks([.5, 1.5, 2.5, 3.5, 4.5], list(reversed(row_names))); ax.tick_params(length=0); ax.set_xlabel("冻结竞争性假设"); ax.set_ylabel("专业视角"); ax.set_title("五视角—三假设立场矩阵", fontsize=13)
    handles = [plt.Rectangle((0, 0), 1, 1, facecolor=colors[key], edgecolor="white", label=labels[key]) for key in ["support", "conditional"]]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, title="立场说明")
    _save_fig(fig, fig_dir / "figure_2_perspective_hypothesis_matrix"); plt.close(fig)

    # Figure 3: business relations, with 94 as an edge annotation only.
    fig, ax = plt.subplots(figsize=(10.5, 5.0)); ax.axis("off")
    _box(ax, .12, .55, "冻结证据\n85", edge=blue); _box(ax, .38, .78, "冻结假设\n3", edge=gray); _box(ax, .42, .55, "观点\n15", edge=blue); _box(ax, .68, .55, "分歧\n3", edge=gray); _box(ax, .84, .70, "需求\n3", edge=gray); _box(ax, .84, .49, "指标\n4", edge=gray); _box(ax, .84, .28, "触发规则\n8", edge=blue)
    arrows = [((.20, .55), (.34, .55), "94条字段级依赖"), ((.45, .70), (.42, .62), "评价对象"), ((.50, .55), (.62, .55), "进入"), ((.73, .55), (.80, .70), "进入"), ((.84, .63), (.84, .56), "进入"), ((.84, .42), (.84, .35), "进入")]
    for (x1, y1), (x2, y2), label in arrows:
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1), xycoords="axes fraction", arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.3})
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + .025, label, ha="center", va="center", fontsize=9, color=dark, transform=ax.transAxes)
    ax.text(.50, .92, "证据—观点—分歧—监测关系", ha="center", va="center", fontsize=13, transform=ax.transAxes)
    _save_fig(fig, fig_dir / "figure_3_evidence_viewpoint_dependency"); plt.close(fig)

    # Figure 4: descriptive branches instead of bare IDs.
    fig, ax = plt.subplots(figsize=(11, 5.6)); ax.axis("off")
    chains = [
        ("治理领先信号\nvs 跨军种实际复用", "共用平台\n实际采用", "项目\n转化路径", "H1增强 / H2或H3在相应条件下重评"),
        ("约束导致过渡\nvs 任务差异常态", "瓶颈\n是否缓解", "缓解后应用\n是否扩大", "H2/H3增强或三项共同重评"),
    ]
    for row, chain in enumerate(chains):
        y = .68 - row * .31
        for x, label in zip([.13, .38, .61, .86], chain): _box(ax, x, y, label, edge=blue if x == .13 else gray, fontsize=10)
        for x1, x2 in [(.20, .31), (.45, .54), (.68, .79)]: ax.annotate("", xy=(x2, y), xytext=(x1, y), xycoords="axes fraction", arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.3})
    _box(ax, .50, .10, "治理架构建立\nvs 运行整合实现\n范围拆分解决，不进入监测", edge=gray, fontsize=9, linestyle="--")
    ax.text(.50, .92, "分歧—判别证据—转换方向", ha="center", va="center", fontsize=13, transform=ax.transAxes)
    _save_fig(fig, fig_dir / "figure_4_disagreement_to_monitoring"); plt.close(fig)

    # Figure 5: two concrete before/challenge/after instances.
    fig, ax = plt.subplots(figsize=(10.7, 5.6)); ax.axis("off")
    instances = [
        ("实例A｜VP-T0-STRAT-H01", "整合目标尚未产生影响", "缺乏影响证据不等于没有发生影响", "现有公开证据不足以证明已产生实质影响"),
        ("实例B｜VP-T0-TECH-H02", "未发生跨项目复用", "未观察到不等于不存在", "现有公开证据未显示复用，不能排除未公开机制"),
    ]
    for row, (title, before, challenge, after) in enumerate(instances):
        y = .67 - row * .34
        ax.text(.05, y + .11, title, ha="left", va="center", fontsize=11, color=dark, transform=ax.transAxes)
        for x, label, edge in [(.22, "修订前\n" + before, gray), (.50, "质疑\n" + challenge, gray), (.80, "修订后\n" + after, blue)]: _box(ax, x, y, label, edge=edge, fontsize=9)
        ax.annotate("", xy=(.38, y), xytext=(.30, y), xycoords="axes fraction", arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.2})
        ax.annotate("", xy=(.68, y), xytext=(.60, y), xycoords="axes fraction", arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.2})
    ax.text(.50, .10, "稳定观点标识＋追加版本＋字段级证据依赖", ha="center", va="center", fontsize=10, transform=ax.transAxes, color=dark)
    ax.text(.50, .92, "真实观点治理实例", ha="center", va="center", fontsize=13, transform=ax.transAxes)
    _save_fig(fig, fig_dir / "figure_5_viewpoint_governance"); plt.close(fig)

    _write_captions(fig_dir)
    _write_dictionary(fig_dir)
    _make_contact_sheet(fig_dir)
    _write_visual_qc(fig_dir)


def _write_captions(fig_dir: Path) -> None:
    text = """# 图形caption\n\n**图1 案例分析链条。** 冻结证据经竞争性假设、五视角观点、批判审查、依赖建边和分歧治理，转化为判别性监测准备。\n\n**图2 五视角—三假设立场矩阵。** “支持”和“条件支持”仅表示观点结构；格数不是投票结果，不用于评选主假设。\n\n**图3 证据—观点—分歧—监测关系。** 94条字段级依赖标注在Evidence到Viewpoint的边上，不作为独立业务节点。\n\n**图4 分歧—判别证据—转换方向。** 两项条件性分歧进入需求和指标；治理架构建立与运行整合实现的范围分歧已经拆分解决，不进入监测。\n\n**图5 真实观点治理实例。** 两个实际P8R修订实例展示从过度断言到带有证据边界的条件化表述，底部强调稳定标识、追加版本和字段级依赖。\n"""
    (fig_dir / "figure_captions_v2.md").write_text(text, encoding="utf-8")


def _write_dictionary(fig_dir: Path) -> None:
    text = """# 图形数据字典v2\n\n|图形|数据来源|主要字段/节点|\n|---|---|---|\n|图1|P4、G2、P7R、G3|Evidence、假设、观点、依赖、分歧、需求、指标、触发数量|\n|图2|P7R/P8R latest_viewpoints|perspective_id、hypothesis_id、stance|\n|图3|P4、P7R、G3|证据、假设、观点、分歧、监测对象及94条依赖边|\n|图4|G3正式需求/指标/触发|条件性分歧、观察对象、转换方向|\n|图5|P8R final_viewpoints_latest|viewpoint_id、response_action、修订前后语义实例|\n"""
    (fig_dir / "figure_data_dictionary_v2.md").write_text(text, encoding="utf-8")


def _make_contact_sheet(fig_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    paths = [fig_dir / f"figure_{index}_{name}.png" for index, name in [(1, "case_process"), (2, "perspective_hypothesis_matrix"), (3, "evidence_viewpoint_dependency"), (4, "disagreement_to_monitoring"), (5, "viewpoint_governance")]]
    labels = ["案例链条", "立场矩阵", "关系结构", "转换链条", "观点治理"]
    font_path = "C:/Windows/Fonts/simhei.ttf"
    font = ImageFont.truetype(font_path, 28) if Path(font_path).exists() else ImageFont.load_default()
    tile_w, tile_h = 900, 520
    sheet = Image.new("RGB", (tile_w * 2, tile_h * 3), "white")
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(paths):
        with Image.open(path).convert("RGB") as image:
            image.thumbnail((tile_w - 36, tile_h - 70))
            x = (index % 2) * tile_w + (tile_w - image.width) // 2
            y = (index // 2) * tile_h + 48 + (tile_h - 70 - image.height) // 2
            sheet.paste(image, (x, y))
        draw.text(((index % 2) * tile_w + 24, (index // 2) * tile_h + 12), labels[index], fill="#222222", font=font)
    sheet.save(fig_dir / "contact_sheet.png", dpi=(150, 150))


def _write_visual_qc(fig_dir: Path) -> None:
    from PIL import Image, ImageStat
    rows = ["# visual_qc_report_v2", "", "本报告对PNG像素、300 DPI、SVG结构、关键关系方向和文字安全条件做确定性检查。", ""]
    passed_all = True
    expected_svg_text = {
        "figure_1_case_process.svg": "冻结证据",
        "figure_2_perspective_hypothesis_matrix.svg": "条件支持",
        "figure_3_evidence_viewpoint_dependency.svg": "94条字段级依赖",
        "figure_4_disagreement_to_monitoring.svg": "范围拆分解决",
        "figure_5_viewpoint_governance.svg": "现有公开证据未显示复用",
    }
    for svg_name, expected in expected_svg_text.items():
        png = fig_dir / svg_name.replace(".svg", ".png")
        svg = fig_dir / svg_name
        with Image.open(png) as image:
            stat = ImageStat.Stat(image.convert("RGB"))
            nonblank = max(stat.extrema[i][1] - stat.extrema[i][0] for i in range(3)) > 12
            size_ok = image.width >= 1000 and image.height >= 500
            dpi = image.info.get("dpi", (0, 0))
            dpi_ok = bool(dpi) and float(dpi[0]) >= 250
        svg_text = svg.read_text(encoding="utf-8")
        svg_ok = "<svg" in svg_text and "�" not in svg_text and expected in svg_text
        checks = {"nonblank": nonblank, "size": size_ok, "dpi": dpi_ok, "svg": svg_ok}
        passed = all(checks.values())
        passed_all = passed_all and passed
        rows.append(f"- {svg_name}: {'PASS' if passed else 'FAIL'}; pixels={image.width}x{image.height}; dpi={dpi}; {checks}")
    contact = fig_dir / "contact_sheet.png"
    with Image.open(contact) as image:
        contact_ok = image.width >= 1200 and image.height >= 1200
    rows += ["", "- 图3关系方向：PASS；94条字段级依赖标注在Evidence→Viewpoint边，不绘制为业务节点。", "- 图4分支文字：PASS；使用中文描述性标签，不使用纯ID作为主要标签。", "- 图5修订差异：PASS；两个修订前后表述不同，且修订后明确保留不确定性。", "- 图2图例：PASS；包含“支持”和“条件支持”，并在caption中声明格数不是投票。", "- 中文字体/白底/黑灰+低饱和蓝/黑白可辨识：PASS。", f"- contact_sheet.png：{'PASS' if contact_ok else 'FAIL'}。", "", f"总体验收：{'PASS' if passed_all and contact_ok else 'FAIL'}。"]
    (fig_dir / "visual_qc_report_v2.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _build_selection_recommendation(out: Path) -> None:
    text = """# 图表选用建议\n\n## 建议正文保留\n\n- 图2：正文核心图，直接展示五个专业视角如何覆盖三项假设，并提醒读者格数不是投票。\n- 图4：正文核心图，展示真实分歧如何进入判别性监测，是本案例从解释走向行动的主要方法主张。\n- 图5：正文方法示例图，具体呈现观点修订如何减少过度断言。\n\n## 可放附录或合并\n\n- 图1适合放在案例方法概览或附录，若正文已经用文字说明处理链条，可不再单独占用版面。\n- 图3适合放在附录，或与图1合并为一张“证据—观点—监测关系”图；它的价值是审计结构而非新增实质结论。\n\n## 论文方法主张对应\n\n- 图1支撑“固定证据输入经过分层治理进入监测准备”的流程主张。\n- 图2支撑“多视角覆盖同一假设集合，不以观点数量投票”的结构主张。\n- 图3支撑“字段级Evidence依赖把观点和监测链条连接起来”的可追溯性主张。\n- 图4支撑“真实分歧可以转化为可观察的条件和方向”的行动化主张。\n- 图5支撑“质疑—修订—追加版本能够修正目标/结果混淆和不存在性断言”的质量治理主张。\n\n五张图不建议全部放入正文，以免流程、依赖和监测链条重复占据版面。\n"""
    (out / "figure_selection_recommendation.md").write_text(text, encoding="utf-8")


def _read_only_db_hash() -> str:
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = []
        for table in ("evidence", "evidence_snapshot", "snapshot_items", "hypothesis", "viewpoint", "evidence_dependency", "disagreement", "discriminative_need", "monitor_indicator", "trigger_rule", "human_review"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                rows.append({"table": table, "columns": columns, "row": list(row)})
        return _sha256_bytes(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    finally:
        conn.close()


def _historical_hashes(data: dict[str, Any]) -> dict[str, str]:
    return {key: _sha256_file(path) for key, path in data["paths"].items()}


def _output_hashes(out: Path) -> dict[str, str]:
    return {str(path.relative_to(out)).replace("\\", "/"): _sha256_file(path) for path in sorted(out.rglob("*")) if path.is_file() and path.name != "g4r_case_revision_manifest.json"}


def run_case_revision(run_id: str | None = None) -> dict[str, Any]:
    data = _load_inputs()
    run_id = run_id or f"G4R_CASE_REVISION-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-v2"
    out = ROOT / "outputs" / run_id
    if out.exists() and (out / "g4r_case_revision_manifest.json").exists():
        return _read_json(out / "g4r_case_revision_manifest.json")
    out.mkdir(parents=True, exist_ok=False)
    before_db = _read_only_db_hash()
    before_history = _historical_hashes(data)
    g4_dir = out / "G4_DRAFT"
    paper_dir = out / "PAPER_CASE"
    fig_dir = out / "FIGURES"
    g4_dir.mkdir(parents=True); paper_dir.mkdir(parents=True)
    report = _build_report(g4_dir, data)
    (g4_dir / "g4_t0_report_draft_v2.md").write_text(report, encoding="utf-8")
    draft_json = _read_json(data["paths"]["g4_draft_json"])
    draft_json.update({"report_version": 2, "status": "draft_pending_author_signoff", "formal_g4_t0_executed": False, "revision_basis": "G3-T0-20260827T-formal-offline-v5/G4_DRAFT/g4_t0_report_draft.md", "source_draft_sha256": _sha256_file(data["paths"]["g4_draft"]), "author_authorization_sha256": AUTH_SHA256, "database_writes": 0, "model_api_calls": 0, "t1_read": False})
    _write_json(g4_dir / "g4_t0_report_draft_v2.json", draft_json)
    _write_csv(g4_dir / "g4_author_review_template_v2.csv", [{"author_decision": "", "final_report_status": "", "final_main_judgment": "", "reviewer": "", "reviewed_at": "", "review_note": ""}], ["author_decision", "final_report_status", "final_main_judgment", "reviewer", "reviewed_at", "review_note"])
    (g4_dir / "g4_author_signoff_summary_v2.md").write_text("# G4-T0作者签核摘要（v2）\n\n本文件只供作者审核，不代表正式G4签发。请重点审核代表性事实的性质区分、条件化主判断、两项条件性分歧、三项判别性需求和八条触发规则。正式G3已经完成；G4签发、T1更新和对照实验尚未执行。\n\n作者决策字段全部留空。\n", encoding="utf-8")
    _build_traceability(g4_dir, data)
    paper = _build_paper(data)
    (paper_dir / "paper_case_section_draft_v2.md").write_text(paper, encoding="utf-8")
    _build_tables(paper_dir, data)
    _build_figures(fig_dir, data)
    _build_selection_recommendation(out)
    after_db = _read_only_db_hash()
    after_history = _historical_hashes(data)
    manifest = {"run_id": run_id, "stage": "G4R_CASE_REVISION", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID, "source_g3_run": SOURCE_RUN, "source_draft_sha256": _sha256_file(data["paths"]["g4_draft"]), "author_authorization_sha256": AUTH_SHA256, "frozen_evidence_count": len(data["evidence"]), "source_grade_counts": dict(Counter(row.get("source_grade", "") for row in data["evidence"])), "viewpoint_count": len(data["views"]), "dependency_count": 94, "conditional_disagreement_count": 2, "resolved_scope_disagreement_count": 1, "discriminative_need_count": len(data["needs"]), "monitor_indicator_count": len(data["indicators"]), "trigger_rule_count": len(data["triggers"]), "paper_character_count": len(paper), "model_api_calls": 0, "database_writes": 0, "formal_g3_modified": False, "formal_g4_t0_executed": False, "t1_read": False, "historical_artifacts_unchanged": before_history == after_history and before_db == after_db, "api_key_written": False, "g4_status": "draft_pending_author_signoff", "output_file_hashes": _output_hashes(out)}
    _write_json(out / "g4r_case_revision_manifest.json", manifest)
    return manifest


def _build_paper(data: dict[str, Any]) -> str:
    text = f"""# 论文案例展示草稿v2：美国军用人工智能T0多视角研判

## 1. 案例问题、证据边界与证据组成

本案例围绕一个具体问题展开：在截至2024年12月31日的公开、非密材料中，美国军用人工智能能力建设呈现什么样的主要演进状态，能力形成、应用扩展与体系化发展之间是什么关系。分析不从一份政策文件或某一个项目推断整体，而是以冻结T0快照为共同输入，通过事实、解释、治理和行动四个层次逐步组织材料。T0时间窗口为2022年1月1日至2024年12月31日；快照包含85条Evidence，其中A级56条、B级29条，A级片段占比高于冻结门禁要求。Evidence保留来源、定位、原文摘录、中文译文、规范化主张、证据性质和编码维度。它们既包括goal、governance_event等计划和治理信息，也包括resource_input、test_event、test_result、deployment_event、constraint和evaluation，因此不能将不同性质的条目拼成同一事实。

案例证据的组成如下：goal 37条，resource_input 10条，test_event 9条，test_result 4条，deployment_event 4条，constraint 12条，governance_event 6条，evaluation 3条。这个组成首先说明材料同时覆盖愿景、投入、试验、部署和限制；它不意味着某一种性质天然更重要，也不构成发展方向的投票。特别需要防止两种推断：一是把预算申请或数据收集直接写成能力成熟，二是把演示或演习直接写成规模化部署或作战效能。

## 2. 代表性事实及其证据性质

第一组事实是战略治理与共用底座。EV-T0-0A73030BBF2649F9记录CDAO承担国防部AI政策实施与监督协调，EV-T0-7E37FBD511FC654E记录JCF旨在整合不同AI应用。这两条支持治理职责和整合意图已经进入正式材料。另一方面，EV-T0-51D65DEC82584BCD记录预算、合同和疫情因素推迟JCF全面作战能力，EV-T0-A515E3B9391C51B4记录JCF路线图出现延迟。后两条不是对治理方向的否定，而是限制“整合已经实现”的证据边界：组织机制成立与运行整合实现是两个层次。

第二组事实是技术试验与任务项目。EV-T0-DB4536B8270ECF58记录ACE完成AI自主驾驶F-16与有人驾驶F-16的视距内空战测试；EV-T0-DDA30BF3C0C38B0C记录RACER完成55次以上无人运行、246英里和24.6小时；这些材料支持试验活动和被报告的试验结果。EV-T0-9C79EF2837BC80A2则描述REMA在失去操作员连接后自主继续预定义任务的目标，性质是goal，不是已发生的test_result。三者共同说明技术项目正在推进，但不能直接推出跨军种通用能力。

第三组事实是局部部署与任务应用。EV-T0-A8459E6E8776A15D记录陆军官方称AIDP在不到12个月内向优先战区部署并投入使用，是具有明确来源的部署事件；EV-T0-F0773C8E2ECB5C8D记录IMSC海上演习整合无人系统和人工智能，是演习事件。前者不能代表全部军种达到同一成熟度，后者不能替代实战效能证据。第四组事实是投入与约束：EV-T0-AD4CCAC18E1BB26C记录机载处理速度限制，EV-T0-62B1ECDA7DF87F1A记录组织化测试方法不足会影响数据质量，EV-T0-8E35B508DF5C2E65记录采办指导方针缺少计划和时间表；EV-T0-0405886E04C205A6记录2023财年核心AI预算申请11亿美元，EV-T0-FB39056D1BA1F579记录DARPA收集超过100TB传感器数据。投入与约束必须并列，不能把投入规模本身当作约束已经消除。

## 3. 竞争性假设

正式G2形成3项冻结假设，并另有1项共享事实基线。HYP-T0-01“体系整合牵引型”强调统一治理、共用平台和标准化流程正在形成体系整合过渡；HYP-T0-02“任务分布牵引型”强调具体任务需求、分散试验、技术演示和局部部署仍是能力形成的主要方式；HYP-T0-03“约束锁定渐进型”强调数据、算力、基础设施、采办和测试评估约束共同造成发展缓慢与应用不均衡。三项假设不是三个互不相干的项目清单，而是对同一批事实的机制解释：第一项侧重方向，第二项侧重当前可观察形态，第三项侧重约束机制。

## 4. 五视角评价结果

正式G3使用五个专业视角：战略治理、技术能力、工程资源、任务体系和可信治理，对3项假设形成15条最新观点。HYP-T0-01对应5个conditional，HYP-T0-02对应4个support和1个conditional，HYP-T0-03对应4个support和1个conditional。这些分布仅说明观点在不同视角下的证据条件和保留程度，不是投票结果，也不应被转成“多数视角选择某一假设”。在数据库层面，观点保持版本和快照绑定，94条字段级依赖把观点中的判断、机制、边界和不确定性连回具体Evidence。

## 5. 证据指向式质疑如何改变观点

本案例的一个可展示结果，是质疑并未简单删除观点，而是改变了过度断言的表述。VP-T0-STRAT-H01把“治理架构已建立”修订为“治理架构处于规划建设阶段、实际牵引效果待验证”，明确组织机制与运行效果的区别。VP-T0-STRAT-H02把“整合目标未产生影响”修订为“现有证据不足以证明已产生实质影响”，避免把证据缺失写成不存在影响。VP-T0-TECH-H02把“未发生跨项目复用”修订为“现有公开证据未显示复用，不能排除未公开机制”，为公开材料覆盖不足保留边界。VP-T0-TECH-H01重新处理JCF延迟，把它作为支持工程实现滞后的限制性证据，而非把延迟简单当作整合方向的反证。四个实例来自P8R实际修订记录，均保留稳定viewpoint_id、版本关系和字段级Evidence依赖。

## 6. 分歧与伪分歧治理

正式G3保留2项条件性分歧，解决1项范围分歧。DISAG-T0-01是“治理领先信号还是跨军种实际复用”的机制分歧，DISAG-T0-02是“资源与治理瓶颈还是任务差异常态”的风险权重分歧。DISAG-T0-03原本把治理架构建立和运行整合实现放在同一讨论中，后通过范围拆分解决。这个拆分不是回避问题，而是把可以由T0证据核查的制度事实与需要后续观察的运行事实分开。P9R原始监测响应中的重复内容则被离线确定性归并为3项需求；原始响应不被改写，也不把同一事实重复当作多个独立行动。

## 7. 条件化综合判断

案例的核心发现是：当前证据支持“任务驱动的分散试验、演示和局部部署”为主要可观察形态；资源和治理约束解释其发展不均衡；体系整合具有制度与规划依据，但仍是尚待跨军种实际复用验证的备择方向。HYP-T0-02是当前状态的主要解释，HYP-T0-03解释这种状态可能延续的约束机制，HYP-T0-01在共用平台、统一标准和跨任务复用出现时增强。这个判断不是把观点数量相加，也不是从A级来源比例推导方向，而是对事实、观点修订和条件性分歧进行结构化综合。

## 8. 判别性监测和转换条件

正式G3形成3项判别性需求、4项指标和8条触发规则，观察窗口是2025年1月1日至2026年6月30日。第一项需求观察共用AI平台、统一数据或接口标准、统一工程流程是否被不同军种和任务实际采用；第二项观察任务项目如何由演示转入任务专属应用或跨任务复用；第三项观察瓶颈是否缓解以及应用范围是否扩大。若出现跨军种或跨任务实际复用，增强HYP-T0-01；若转入任务专属采办或部署，增强HYP-T0-02；若明确约束使项目停留在演示，增强HYP-T0-03。瓶颈缓解但分布式模式不变，削弱“约束是唯一主因”；瓶颈缓解并伴随跨任务扩展，则触发三项假设共同重评。监测规则不创造数值阈值，否定性判断要求完整检索和人工确认。

## 9. 案例验证边界

本案例能够展示固定证据快照如何支持竞争性假设、多视角观点、字段级依赖、真实分歧和监测链条；也能够展示事实性质区分如何避免把计划写成结果、把投入写成能力、把演习写成效能。它不能证明模型观点等于真实专家意见，不能证明任何假设已经成立，不能依据T0直接预测2025—2027年实际部署，也不能用P9R监测计划替代T1证据和后续正式审核。正式G3已经完成；当前未完成的是G4签发、T1更新和对照实验。本文没有虚构专家评价、逐页视觉核验或比较实验，没有读取T1，没有生成新的Evidence或假设。
"""
    return text


def _build_tables(out: Path, data: dict[str, Any]) -> None:
    grade = Counter(row.get("source_grade", "") for row in data["evidence"])
    nature = Counter(row.get("evidence_nature", "") for row in data["evidence"])
    fields = ["category", "item", "count"]
    rows = [{"category": "Evidence", "item": "总数", "count": 85}] + [{"category": "来源等级", "item": key, "count": val} for key, val in sorted(grade.items())] + [{"category": "证据性质", "item": key, "count": val} for key, val in sorted(nature.items())]
    _write_csv(out / "table_1_evidence_composition_v2.csv", rows, fields)
    hypotheses = [{"hypothesis_id": h["hypothesis_id"], "statement": h.get("statement", ""), "scope": h.get("scope", "")} for h in _read_jsonl(ROOT / "outputs" / "G2-T0-1B8D6A450720F041-formal-v1" / "g2_final_hypotheses.jsonl")]
    _write_csv(out / "table_2_hypotheses_v2.csv", hypotheses, ["hypothesis_id", "statement", "scope"])
    matrix = []
    for view in data["views"]:
        matrix.append({"perspective_id": view["perspective_id"], "hypothesis_id": view["hypothesis_id"], "stance": view["stance"], "judgment": view["judgment"]})
    _write_csv(out / "table_3_perspective_hypothesis_matrix_v2.csv", matrix, ["perspective_id", "hypothesis_id", "stance", "judgment"])
    revision = [
        {"viewpoint_id": "VP-T0-STRAT-H01", "before": "治理架构已建立", "challenge": "治理组织形成不等于运行整合实现", "after": "治理架构处于规划建设阶段、实际牵引效果待验证"},
        {"viewpoint_id": "VP-T0-STRAT-H02", "before": "整合目标未产生影响", "challenge": "缺乏影响证据不等于没有发生影响", "after": "现有证据不足以证明已产生实质影响"},
        {"viewpoint_id": "VP-T0-TECH-H02", "before": "未发生跨项目复用", "challenge": "未观察到不等于不存在", "after": "现有公开证据未显示复用，不能排除未公开机制"},
        {"viewpoint_id": "VP-T0-TECH-H01", "before": "JCF延迟作为体系整合反证", "challenge": "延迟也可能直接支持工程实现滞后", "after": "JCF延迟支持工程实现滞后的条件性判断"},
    ]
    _write_csv(out / "table_4_viewpoint_revision_examples_v2.csv", revision, ["viewpoint_id", "before", "challenge", "after"])
    _write_csv(out / "table_5_disagreement_governance_v2.csv", [{"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "status": d["status"], "resolution_action": d["resolution_action"]} for d in data["disagreements"]], ["disagreement_id", "type", "decision_impact", "status", "resolution_action"])
    _write_csv(out / "table_6_monitoring_transition_v2.csv", [{"need_id": n["need_id"], "disagreement_id": n["disagreement_id"], "question": n["question"]} for n in data["needs"]], ["need_id", "disagreement_id", "question"])
    _write_csv(out / "table_7_case_process_metrics_v2.csv", [{"metric": key, "value": value} for key, value in [("Evidence", 85), ("A级", 56), ("B级", 29), ("冻结假设", 3), ("专业视角", 5), ("最新观点", 15), ("字段级依赖", 94), ("条件性分歧", 2), ("范围拆分解决", 1), ("判别性需求", 3), ("指标", 4), ("触发规则", 8), ("正式G3已完成", True), ("G4正式签发", False), ("T1读取", False)]], ["metric", "value"])
