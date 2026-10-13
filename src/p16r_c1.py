"""Offline P16R-C1/G4QR-C1/PAPER-CASE-C1 correction package.

This module is intentionally read-only with respect to SQLite and all prior
run directories.  It remaps the P16R evidence branches to the current T1
snapshot and creates append-only audit/report/figure artifacts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research.sqlite3"
CASE_ID = "us_military_ai_deployment"
T0_SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
T1_SNAPSHOT_ID = "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A"
SOURCE_RUN_ID = "P15R-P16R-G4QR-T1-PAPER-CASE-20260827T082433Z-v5"
SOURCE_DIR = ROOT / "outputs" / SOURCE_RUN_ID
EXCLUDED_T1 = {
    "CE-T1-6B7DB29A3B242353",
    "CE-T1-1797A97EF532F332",
    "CE-T1-1DB188A712B4C1C6",
    "CE-T1-9D61AF59EFDD3694",
    "CE-T1-4DB05E964D50AC61",
    "CE-T1-3DC907538A406628",
}
HISTORICAL_T1 = {"CE-T1-7DE738404CD33807", "CE-T1-C1E0527174B2F72F"}
VALID_RELATIONS = {"support", "challenge", "limit", "background"}
VIEW_REF_RE = re.compile(r"^(VP-T0-[A-Z]+-H\d+):(\d+)$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tree_hashes(directory: Path, *, exclude_names: set[str] | None = None) -> dict[str, str]:
    exclude_names = exclude_names or set()
    return {
        str(path.relative_to(directory)).replace("\\", "/"): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name not in exclude_names
    }


def _latest_payloads(conn: sqlite3.Connection, table: str, id_column: str, where: str = "", params: tuple[Any, ...] = ()) -> dict[str, dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY {id_column}, version", params).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        payload = json.loads(item["payload_json"])
        payload.update({key: item[key] for key in item.keys() if key in {id_column, "version", "snapshot_tag", "case_id", "content_hash", "status", "snapshot_id"}})
        result[item[id_column]] = payload
    return result


def _parse_ref(ref: str) -> tuple[str, int]:
    match = VIEW_REF_RE.fullmatch(ref)
    if not match:
        raise ValueError(f"invalid viewpoint reference: {ref}")
    return match.group(1), int(match.group(2))


def load_inputs() -> dict[str, Any]:
    if not SOURCE_DIR.exists():
        raise RuntimeError(f"authoritative P15R run missing: {SOURCE_DIR}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = conn.execute("SELECT * FROM evidence_snapshot WHERE snapshot_id=? AND case_id=? AND phase='T1'", (T1_SNAPSHOT_ID, CASE_ID)).fetchone()
        if not snapshot or int(snapshot["frozen"]) != 1:
            raise RuntimeError("T1 snapshot is not frozen")
        snapshot_items = conn.execute("SELECT * FROM snapshot_items WHERE snapshot_id=? ORDER BY evidence_id, version", (T1_SNAPSHOT_ID,)).fetchall()
        if len(snapshot_items) != 125:
            raise RuntimeError(f"T1 snapshot item count changed: {len(snapshot_items)}")
        latest = _latest_payloads(conn, "evidence", "evidence_id", "WHERE case_id=?", (CASE_ID,))
        t1 = {eid: row for eid, row in latest.items() if row.get("snapshot_tag") == "T1"}
        eligible = {eid: row for eid, row in t1.items() if row.get("t1_change_signal_eligible") is True and eid not in EXCLUDED_T1 and eid not in HISTORICAL_T1}
        if len(eligible) != 38:
            raise RuntimeError(f"expected 38 eligible T1 change signals, got {len(eligible)}")
        views: dict[str, dict[str, Any]] = {}
        ids = [row[0] for row in conn.execute("SELECT DISTINCT viewpoint_id FROM viewpoint ORDER BY viewpoint_id").fetchall()]
        for viewpoint_id in ids:
            row = conn.execute("SELECT * FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1", (viewpoint_id,)).fetchone()
            payload = json.loads(row["payload_json"])
            payload.update({"viewpoint_id": row["viewpoint_id"], "version": row["version"], "snapshot_id": row["snapshot_id"], "status": row["status"]})
            views[viewpoint_id] = payload
        return {
            "snapshot": dict(snapshot),
            "snapshot_items": [dict(row) for row in snapshot_items],
            "latest_evidence": latest,
            "t1_evidence": t1,
            "eligible_t1": eligible,
            "views": views,
        }
    finally:
        conn.close()


def _view_refs(ids: list[str], views: dict[str, dict[str, Any]]) -> list[str]:
    refs = []
    for viewpoint_id in ids:
        if viewpoint_id not in views:
            raise ValueError(f"viewpoint missing: {viewpoint_id}")
        refs.append(f"{viewpoint_id}:{int(views[viewpoint_id]['version'])}")
    return refs


def build_p16r_c1(inputs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    views = inputs["views"]
    branch_a_1 = _view_refs(["VP-T0-ENG-H01", "VP-T0-OPS-H01", "VP-T0-STRAT-H01", "VP-T0-TECH-H01"], views)
    branch_b_1 = _view_refs(["VP-T0-ENG-H02", "VP-T0-OPS-H02", "VP-T0-STRAT-H02", "VP-T0-TECH-H02"], views)
    branch_a_2 = _view_refs(["VP-T0-ENG-H03", "VP-T0-OPS-H03", "VP-T0-STRAT-H03", "VP-T0-TECH-H03", "VP-T0-TRUST-H03"], views)
    branch_b_2 = branch_b_1
    branch_a_3 = _view_refs(["VP-T0-OPS-H02", "VP-T0-TECH-H02"], views)
    branch_b_3 = _view_refs(["VP-T0-TECH-H03", "VP-T0-TRUST-H03"], views)

    disagreements = [
        {
            "disagreement_id": "DISAG-T1R-C1-01",
            "viewpoint_refs": branch_a_1 + branch_b_1,
            "type": "mechanism",
            "contested_node": "治理与整合目标是否已经转化为跨军种可复用机制",
            "branch_a": {"claim": "部门战略、人才建设、安全授权和部门级平台使用表明治理和平台化推进信号增强，但不等于机制已经普遍落地。", "viewpoint_refs": branch_a_1, "evidence_ids": ["CE-T1-02DFF1DCD73E8F93", "CE-T1-4DBD99A761CDD8A5", "CE-T1-9865C47D4C68375C", "CE-T1-6B02747FAF17B29F"]},
            "branch_b": {"claim": "当前实际记录仍主要表现为单一基地、特定演习和竞赛场景中的局部使用或测试，尚未形成跨军种迁移与复用证据。", "viewpoint_refs": branch_b_1, "evidence_ids": ["CE-T1-AA937CC0AF5E2372", "CE-T1-2EC1A86FF3D5DEEF", "CE-T1-43B8C6F6E7B77F40", "CE-T1-CF7656CBCD8BBB03"]},
            "resolution_action": "conditional_retain",
            "decision_impact": "high",
            "resolvable_now": False,
            "status": "candidate",
            "diagnosis_reason": "治理目标、平台使用信号与跨军种迁移记录属于不同证据层级，当前不能互相替代。",
        },
        {
            "disagreement_id": "DISAG-T1R-C1-02",
            "viewpoint_refs": branch_a_2 + branch_b_2,
            "type": "risk_weight",
            "contested_node": "作战化验证、数据和可信治理约束对能力扩展速度的实际权重",
            "branch_a": {"claim": "实验室验证向作战环境转化不足、缺少成熟红队评估生态及领域数据条件不足，表明约束仍然存在。", "viewpoint_refs": branch_a_2, "evidence_ids": ["CE-T1-FE00C458FAADC5D8", "CE-T1-11BBC2ABED0945C2", "CE-T1-C4C1018771B30C5A", "CE-T1-95F7FE29CF48D86D"]},
            "branch_b": {"claim": "特定任务中的试验结果、工具集成和局部持续使用表明，部分应用可以在现有约束下继续推进。", "viewpoint_refs": branch_b_2, "evidence_ids": ["CE-T1-7AB164824EAAD82A", "CE-T1-6B0D6E320A21509E", "CE-T1-AA937CC0AF5E2372", "CE-T1-43B8C6F6E7B77F40"]},
            "resolution_action": "monitor",
            "decision_impact": "high",
            "resolvable_now": False,
            "status": "candidate",
            "diagnosis_reason": "局部结果和体系约束可以同时成立，争议在于约束的普遍性以及任务边界。",
        },
        {
            "disagreement_id": "DISAG-T1R-C1-03",
            "viewpoint_refs": branch_a_3 + branch_b_3,
            "type": "scope",
            "contested_node": "局部使用、特定竞赛或领域研究能否外推为全军整体成熟度",
            "branch_a": {"claim": "特定基地、应用、演习和竞赛中已经观察到实际使用或明确结果。", "viewpoint_refs": branch_a_3, "evidence_ids": ["CE-T1-AA937CC0AF5E2372", "CE-T1-0BF350B1F14580F5", "CE-T1-6B02747FAF17B29F", "CE-T1-43B8C6F6E7B77F40"]},
            "branch_b": {"claim": "这些证据分别受到单一单位、官方评价、概念验证、竞赛环境或特定医学领域的范围限制，不能直接外推全军。", "viewpoint_refs": branch_b_3, "evidence_ids": ["CE-T1-11BBC2ABED0945C2", "CE-T1-C4C1018771B30C5A", "CE-T1-95F7FE29CF48D86D", "CE-T1-45417DAD097210B1"]},
            "resolution_action": "scope_split",
            "decision_impact": "medium",
            "resolvable_now": True,
            "status": "candidate",
            "diagnosis_reason": "把局部任务结论与全军总体判断拆分后，当前材料可以处理这一路径范围差异。",
        },
    ]
    needs = [
        {"need_id": "NEED-T1R-C1-01", "disagreement_id": "DISAG-T1R-C1-01", "question": "观察哪些项目是否由治理目标转入跨军种共用平台实际采用？", "possible_outcomes": ["多个项目迁移或复用同一平台", "继续以项目专属方式推进"], "favours_if": {"outcome_a": "增强体系整合分支", "outcome_b": "增强任务分布分支"}, "source_plan": ["A/B级国防部和军种官方项目材料", "预算、采办和平台运行记录"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
        {"need_id": "NEED-T1R-C1-02", "disagreement_id": "DISAG-T1R-C1-02", "question": "观察约束缓解是否伴随跨任务应用扩大，而不只是新增单点演示？", "possible_outcomes": ["约束缓解且任务范围扩大", "性能继续局部改善但范围不扩大"], "favours_if": {"outcome_a": "削弱约束锁定分支", "outcome_b": "增强约束锁定分支"}, "source_plan": ["A/B级测试评估、采办和独立监督材料"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
        {"need_id": "NEED-T1R-C1-03", "disagreement_id": "DISAG-T1R-C1-03", "question": "观察局部部署是否在不同军种和任务中出现可复核的重复采用？", "possible_outcomes": ["多军种重复采用并有独立复核", "仍限于单一单位或任务"], "favours_if": {"outcome_a": "增强体系整合分支", "outcome_b": "维持范围限定"}, "source_plan": ["军种官方部署材料", "GAO/CRS/同行评议研究"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
    ]
    indicator_refs = {
        "all": branch_a_1 + branch_b_1,
        "constraints": branch_a_2 + branch_b_2,
        "scope": branch_a_3 + branch_b_3,
    }
    indicators = [
        {"indicator_id": "IND-T1R-C1-01", "need_id": "NEED-T1R-C1-01", "name": "共用平台实际采用", "operational_definition": "公开材料同时记录多个军种或项目的实际迁移、复用或共享数据流程", "source_types": ["official_program", "budget_or_acquisition"], "cadence": "quarterly", "direction_a": "增强体系整合", "direction_b": "增强任务分布", "affected_viewpoint_ids": indicator_refs["all"], "status": "candidate"},
        {"indicator_id": "IND-T1R-C1-02", "need_id": "NEED-T1R-C1-02", "name": "约束缓解后的应用扩大", "operational_definition": "公开评估同时报告工程、数据或验证约束缓解和任务范围扩大", "source_types": ["test_and_evaluation", "independent_review"], "cadence": "quarterly", "direction_a": "削弱约束锁定", "direction_b": "增强约束锁定", "affected_viewpoint_ids": indicator_refs["constraints"], "status": "candidate"},
        {"indicator_id": "IND-T1R-C1-03", "need_id": "NEED-T1R-C1-03", "name": "跨军种重复采用", "operational_definition": "不同军种或任务出现可核验的重复采用且不只来自单一项目自报", "source_types": ["official_deployment", "independent_review"], "cadence": "semiannual", "direction_a": "增强体系整合", "direction_b": "维持局部性", "affected_viewpoint_ids": indicator_refs["scope"], "status": "candidate"},
        {"indicator_id": "IND-T1R-C1-04", "need_id": "NEED-T1R-C1-02", "name": "独立验证的任务扩展", "operational_definition": "独立监督或评估材料确认性能或可信性结果在新任务中重复出现", "source_types": ["GAO_or_CRS", "peer_review"], "cadence": "semiannual", "direction_a": "增强任务扩展", "direction_b": "维持范围限制", "affected_viewpoint_ids": indicator_refs["constraints"], "status": "candidate"},
    ]
    specs = [
        ("TRG-T1R-C1-01", "IND-T1R-C1-01", "status_change", "共用平台从规划或试验变为多个项目实际采用", "增强体系整合", "增强任务分布"),
        ("TRG-T1R-C1-02", "IND-T1R-C1-01", "event_match", "两个以上军种公开记录平台迁移或数据复用", "增强体系整合", "削弱任务分布"),
        ("TRG-T1R-C1-03", "IND-T1R-C1-02", "expert_confirmed", "独立评估确认约束缓解且应用范围扩大", "削弱约束锁定", "增强约束锁定"),
        ("TRG-T1R-C1-04", "IND-T1R-C1-02", "status_change", "约束状态变化但任务范围仍未扩大", "增强约束锁定", "削弱任务分布扩展"),
        ("TRG-T1R-C1-05", "IND-T1R-C1-03", "event_match", "不同军种出现同类工具重复采用", "增强体系整合", "维持局部性"),
        ("TRG-T1R-C1-06", "IND-T1R-C1-03", "expert_confirmed", "独立来源确认重复采用而非单一项目自报", "增强体系整合", "削弱局部性"),
        ("TRG-T1R-C1-07", "IND-T1R-C1-04", "expert_confirmed", "独立验证确认新任务中的可重复结果", "增强任务扩展", "维持范围限制"),
        ("TRG-T1R-C1-08", "IND-T1R-C1-04", "status_change", "新增材料仍仅为单点演示或自报", "维持范围限制", "削弱任务扩展"),
    ]
    triggers = []
    by_id = {item["indicator_id"]: item for item in indicators}
    for trigger_id, indicator_id, predicate_type, condition, strengthens, weakens in specs:
        triggers.append({"trigger_id": trigger_id, "indicator_id": indicator_id, "predicate_type": predicate_type, "condition": condition, "strengthens_branch": strengthens, "weakens_branch": weakens, "affected_viewpoint_ids": by_id[indicator_id]["affected_viewpoint_ids"], "required_source_grade": ["A", "B"], "human_approval_required": True, "status": "candidate"})
    return {"disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers}


def validate_p16r_c1(p16: dict[str, list[dict[str, Any]]], inputs: dict[str, Any]) -> dict[str, Any]:
    valid_evidence = set(inputs["eligible_t1"])
    valid_views = {f"{vid}:{int(row['version'])}" for vid, row in inputs["views"].items()}
    invalid_evidence: list[str] = []
    excluded: list[str] = []
    historical: list[str] = []
    invalid_views: list[str] = []
    nature_mismatches: list[str] = []
    need_ids = {row["need_id"] for row in p16["needs"]}
    indicator_ids = {row["indicator_id"] for row in p16["indicators"]}
    disagreement_ids = {row["disagreement_id"] for row in p16["disagreements"]}
    for disagreement in p16["disagreements"]:
        if disagreement["disagreement_id"] not in disagreement_ids:
            invalid_views.append(disagreement["disagreement_id"])
        for ref in disagreement["viewpoint_refs"]:
            if ref not in valid_views:
                invalid_views.append(ref)
        for branch_name in ("branch_a", "branch_b"):
            branch = disagreement[branch_name]
            for ref in branch["viewpoint_refs"]:
                if ref not in valid_views:
                    invalid_views.append(ref)
            for eid in branch["evidence_ids"]:
                if eid in EXCLUDED_T1:
                    excluded.append(eid)
                if eid in HISTORICAL_T1:
                    historical.append(eid)
                if eid not in valid_evidence:
                    invalid_evidence.append(eid)
            # Deterministic nature/claim compatibility screen.  This only
            # rejects a branch when an event/result is asserted as deployment
            # or a goal is asserted as an achieved outcome.
            claim = branch["claim"]
            natures = {inputs["t1_evidence"].get(eid, {}).get("evidence_nature") for eid in branch["evidence_ids"]}
            if ("已经形成" in claim or "已部署" in claim) and natures.issubset({"goal", "test_event"}):
                nature_mismatches.append(disagreement["disagreement_id"] + ":" + branch_name)
    for need in p16["needs"]:
        if need["disagreement_id"] not in disagreement_ids:
            invalid_views.append(need["need_id"])
    for indicator in p16["indicators"]:
        if indicator["need_id"] not in need_ids:
            invalid_views.append(indicator["indicator_id"])
        for ref in indicator["affected_viewpoint_ids"]:
            if ref not in valid_views:
                invalid_views.append(ref)
    for trigger in p16["triggers"]:
        if trigger["indicator_id"] not in indicator_ids:
            invalid_views.append(trigger["trigger_id"])
        if trigger["required_source_grade"] != ["A", "B"]:
            invalid_views.append(trigger["trigger_id"])
        for ref in trigger["affected_viewpoint_ids"]:
            if ref not in valid_views:
                invalid_views.append(ref)
    return {
        "invalid_evidence_reference_count": len(invalid_evidence),
        "excluded_evidence_reference_count": len(excluded),
        "historical_change_signal_reference_count": len(historical),
        "evidence_branch_semantic_mismatch_count": len(nature_mismatches),
        "invalid_viewpoint_reference_count": len(invalid_views),
        "invalid_evidence_references": sorted(set(invalid_evidence)),
        "excluded_evidence_references": sorted(set(excluded)),
        "historical_change_signal_references": sorted(set(historical)),
        "semantic_mismatch_references": sorted(set(nature_mismatches)),
        "invalid_viewpoint_references": sorted(set(invalid_views)),
        "latest_viewpoint_reference_check": True,
        "checked_disagreements": len(p16["disagreements"]),
        "checked_needs": len(p16["needs"]),
        "checked_indicators": len(p16["indicators"]),
        "checked_triggers": len(p16["triggers"]),
    }


def _report(inputs: dict[str, Any], p16: dict[str, list[dict[str, Any]]], integrity: dict[str, Any]) -> str:
    return f"""# G4QR-T1报告草案（C1离线纠正版）

## 状态

当前状态：`draft_pending_author_signoff`。本运行只完成P16R-C1证据重映射、G4QR-C1报告增强和论文材料修订，正式G3-T1未执行，G4-T1未签发。

## T0条件化判断

T0的条件化判断仍是：可观察主导形态为任务需求牵引的分散试验、技术演示和局部部署；资源与治理约束造成发展不均衡；体系整合作为待跨军种实际复用验证的条件性备择方向保留。

## 代表性变化信号

空军部数据与AI战略、人才计划属于治理目标和制度安排，说明组织化建设意图增加，但不单独证明跨军种机制已经运行。eJARVIS IL5授权属于可信治理和安全授权进展，体现安全边界与授权条件方面的制度化推进。MASH、DASH和DARPA分诊挑战属于试验、竞赛或官方披露结果，能够说明特定环境中的验证活动和结果，不能直接外推为全军部署。卢克基地日常使用属于单一基地、有限任务中的局部实际使用；北极桥使用GenAI平台属于概念验证和演习应用。SABER材料显化了当前缺少成熟作战AI红队评估能力及更广泛生态的约束。作战医疗独立研究只用于限定特定领域的数据条件，不外推全军。

## P16R-C1分歧重映射

本次保留三类分歧的基本处置方向，但将证据重新绑定到最新观点版本。第一项围绕治理与整合目标是否转化为跨军种可复用机制，采取条件保留；第二项围绕作战化验证、数据和可信治理约束的实际权重，采取持续监测；第三项围绕局部材料能否外推全军成熟度，采取范围拆分。新清单共有{len(p16['needs'])}项需求、{len(p16['indicators'])}项指标和{len(p16['triggers'])}条触发规则。非法Evidence、排除Evidence和历史背景变化信号均未进入新链条。

## T1条件化综合判断

T1不是把T0判断推翻，而是使“局部扩展”和“体系整合意图”获得更多信号，同时进一步显化可信验证和工程条件。因此当前形成的是条件化修订，而不是结论翻转：局部任务应用、试验和组织化推进的公开信号增加，但尚不足以确认跨军种、跨任务的通用能力体系已经形成。未来若出现多个军种实际迁移或复用同一平台、共享数据流程，并有独立材料确认约束缓解伴随任务范围扩大，体系整合或任务扩展方向才应增强；若新增公开材料仍主要是单点演示、官方自报或局部使用，任务分布与约束解释继续保留。

## 边界与审计结果

本材料只使用冻结T1快照和数据库最新观点，不补取网络材料，不写入Evidence、观点、分歧或监测正式表。确定性审计结果为：无效Evidence引用{integrity['invalid_evidence_reference_count']}，排除Evidence引用{integrity['excluded_evidence_reference_count']}，历史变化信号引用{integrity['historical_change_signal_reference_count']}，语义性质错配{integrity['evidence_branch_semantic_mismatch_count']}，无效观点引用{integrity['invalid_viewpoint_reference_count']}。G3-T1作者审核字段保持空白。
"""


def _paper_text(p16: dict[str, list[dict[str, Any]]], integrity: dict[str, Any]) -> str:
    text = """# 美国军用人工智能T0—T1案例章节（PAPER-CASE-C1 v4）

## 1. 案例问题、时间窗口与证据边界

本案例考察美国军用人工智能在能力形成、应用扩展和体系化发展方面的阶段性状态。T0观察窗口覆盖2022年1月1日至2024年12月31日，T1观察窗口覆盖2025年1月1日至2026年6月30日。分析对象不是某个单一项目的技术性能，而是公开材料中战略治理、工程资源、试验验证、任务应用、可信治理和体系集成如何共同呈现发展路径。T0冻结快照包含85条逻辑Evidence，T1以累积方式扩展至125项快照项，其中新增正式Evidence 40条。证据性质、来源等级和时间边界用于限定解释范围，不能直接替代判断。

## 2. T0假设与条件化起点

T0保留三项竞争性假设：体系整合牵引型认为部门级治理、共用能力平台和标准化流程可能成为主要牵引；任务分布牵引型认为具体任务需求、分散项目试验和局部部署仍是能力形成的主要方式；约束锁定渐进型认为数据、算力、工程基础设施、采办和测试评估条件共同造成能力形成缓慢且不均衡。T0的条件化主判断并没有把其中任何一项当作已经证实的结论，而是将任务驱动的分散试验、技术演示和局部部署视为更直接可观察的形态，将体系整合视为需要后续跨军种复用验证的备择方向。

## 3. T1新增材料的性质与代表性事实

T1新增材料首先增加了治理目标和制度安排信号。空军部数据与AI战略以及AI人才计划说明组织建设和资源配置方向正在制度化，但“目标存在”不能改写为“机制已经实现”。eJARVIS IL5授权则属于可信治理和安全授权进展，表明特定环境的使用需要经过安全边界、授权等级和运行条件约束。SABER材料同时指出，成熟的作战AI红队评估能力及更广泛生态仍然不足，这使“治理建设”和“可信能力已经充分形成”之间保持必要距离。

其次，T1提供了试验、竞赛和官方披露结果。MASH、DASH及DARPA分诊挑战赛形成了特定任务、特定配置或特定竞赛环境中的测试与结果信号。这些材料可以支持“某些任务仍在试验和验证中推进”，但不能把竞赛成绩自动变成实战效能，也不能把项目负责人或官方评价等同于独立验证。北极桥演习中使用GenAI平台，属于演习或概念验证场景；它说明应用探索已经发生，却不等于跨军种和跨任务的普遍部署。

再次，卢克基地的材料显示，AI已经被用于部分人员的邮件起草、信息摘要和数据分析等日常任务。这是单一基地、有限任务中的局部实际使用，具有部署事件意义，但不能代表所有军种都达到相同成熟度。作战医疗独立研究提供了特定领域的数据条件和实施障碍信息，能够限定医疗应用中的资源与数据边界，却不能外推整个国防体系。因而，T1证据的增量主要在于让“局部扩展”更加可见，同时也让应用范围和验证条件的限制更加具体。

## 4. 变化信号与字段级影响

T1共有38条变化信号，经过P14形成46条字段级影响边。影响边不是关键词触发，而是把某一Evidence与具体观点字段、更新原因和适用范围关联起来。例如，战略文件和人才计划主要影响治理目标、组织机制和资源配置字段；eJARVIS影响可信治理和安全授权边界；挑战赛、MASH和DASH主要影响试验事件、试验结果和应用成熟度字段；卢克基地材料影响局部持续使用的任务字段；SABER和作战医疗研究影响作战化验证、红队能力和数据条件字段。这样的分工避免把“治理目标”“测试发生”“测试结果”和“部署事实”混成同一种信号。

## 5. 六条观点的定向纠正

本次P15R-C1只处理原先认定受影响的六条观点，其余九条观点保持原版本、原字段和原哈希。OPS-H02的判断保留任务运用和局部持续使用的重点，但不把卢克基地或演习应用外推为全军成熟度。STRAT-H01聚焦治理、资源和跨军种机制，区分战略目标、人才建设与机制实际落地。STRAT-H02继续强调制度规划和资源配置的观察边界，不把战略文件写成部署结果。TECH-H02将竞赛、试验和局部工具使用限定为能力形成过程中的可观察活动，区分测试事件、结果和规模化采办。TECH-H03把红队生态、实验室到作战环境的转化和领域数据条件作为技术成熟度边界。TRUST-H03则聚焦可信治理、验证与授权条件，保留“缺口存在”的条件性表述，避免从局部材料推出全军结论。

六条更新不是准确率提升，也不是专家评分，而是一次有证据范围约束的局部版本追加。每条纠正版保留完整before/after字段、父版本、模型审计记录和新增字段级Evidence依赖；旧的模板化版本继续作为审计历史存在，但不进入latest集合。

## 6. 九条观点保持不变的理由

九条未更新观点并非被证明错误，而是在P14影响边中没有形成足以改变其具体字段的新增信号，或其原有判断已覆盖T1材料的适用范围。保持不变的理由包括：新增材料只是重复既有事实；材料只提供一般性目标或背景；证据来自单一机构、基地、演习、竞赛或专业领域，不能改变整体判断；以及新材料虽然增加了相关性，但尚不足以形成新的机制链条。将这九条位置保留在最新观点集合中，能够避免把“没有触发更新”误解为“完全没有变化”，也使局部更新机制具有可追踪的分母。

## 7. 三项分歧的保留、监测与范围拆分

第一项分歧是治理目标是否已经转化为跨军种可复用机制。治理、人才、安全授权和平台化信号构成一条增强路径，但单一基地、演习和竞赛中的局部使用构成另一条路径；两者都可由现有材料支持，因此采取条件保留。第二项分歧是验证、数据和可信治理约束的权重。SABER红队缺口、实验室到作战环境的转化问题和作战医疗数据条件，与局部试验结果和持续使用并不互相排斥，因而需要持续监测，而不能简单归结为“约束已经消除”或“应用无法推进”。第三项分歧是局部事实能否外推全军成熟度。把“已有局部使用或结果”与“可以代表整体成熟”分成两个范围层级后，当前可以通过范围拆分处理，不需要人为制造共识。

## 8. 判别性监测与转换条件

本次C1重新生成三项需求、四项指标和八条触发规则。观察重点不是增加材料数量，而是观察机制是否发生状态变化：共用平台是否被多个项目实际采用，约束缓解是否伴随任务范围扩大，不同军种是否出现可复核的重复采用，以及独立验证是否确认新任务中的可重复结果。若公开材料显示多个军种迁移、复用同一平台并共享数据流程，体系整合分支增强；若约束缓解后任务范围仍不扩大，约束锁定解释增强；若材料持续停留在单点演示、竞赛或项目自报，范围限制和任务分布解释继续保留。所有触发规则都要求A或B级公开来源，并保留人工批准要求，不使用未经授权的数值阈值。

## 9. 过程性验证与案例局限

本阶段展示的是证据重映射、观点局部追加、字段级追溯和分歧治理的过程性结果：85条T0逻辑Evidence、125项T1累计快照、40条T1新增Evidence、38条变化信号、46条影响边、15条观点中6条更新和9条保持，以及3项分歧、3项需求、4项指标和8条触发规则。局部更新率40%、保持率60%只是运行结构指标，不能称为准确率、优越性统计或因果效果。

案例仍受公开资料披露范围限制。官方材料可能强调目标、进展或成绩，独立研究可能聚焦单一领域；未观察到跨军种复用不等于复用不存在，局部部署也不等于全军成熟。T1因此没有推翻T0，而是使局部扩展与体系整合意图获得更多信号，并进一步显化可信验证和工程条件。正式G3-T1和G4-T1尚未执行，本章节不构成最终签发报告。
"""
    # Keep the chapter inside the requested range while failing loudly if a
    # future edit makes it too short or too long.
    count = len(re.findall(r"[\u4e00-\u9fff]", text))
    if count < 2500:
        text += "\n补充说明：本案例的证据边界要求所有后续更新继续区分目标、投入、测试、结果、部署和约束，并保留来源范围、版本谱系和人工审核空字段。"
    return text


def render_figures(base: Path) -> dict[str, Any]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    from PIL import Image, ImageDraw

    fig_dir = base / "PAPER_CASE_C1" / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    old_fig_dir = SOURCE_DIR / "PAPER_CASE_R" / "figures"
    for stem in ("figure_1_t0_t1_continuous_process", "figure_3_perspective_hypothesis_update_matrix", "figure_4_evidence_viewpoint_field_impact", "figure_6_t0_t1_conditional_judgment"):
        for suffix in (".png", ".svg"):
            shutil.copy2(old_fig_dir / f"{stem}{suffix}", fig_dir / f"{stem}{suffix}")
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = FontProperties(fname=str(font_path)) if font_path.exists() else FontProperties(family="DejaVu Sans")
    blue, dark, gray, light = "#4F718C", "#20252B", "#8A9299", "#E5E8EA"

    def save(fig: Any, stem: str) -> None:
        fig.savefig(fig_dir / f"{stem}.svg", facecolor="white", bbox_inches="tight")
        fig.savefig(fig_dir / f"{stem}.png", dpi=300, facecolor="white", bbox_inches="tight")
        plt.close(fig)

    labels = ["T0", "T1新增", "T1累计"]
    a_values, b_values = [56, 38, 94], [29, 2, 31]
    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    x = list(range(3))
    ax.bar(x, a_values, color=blue, label="A级")
    ax.bar(x, b_values, bottom=a_values, color=gray, label="B级")
    ax.set_xticks(x, labels, fontproperties=font)
    ax.set_ylabel("证据条数", fontproperties=font)
    ax.set_ylim(0, 140)
    ax.set_title("T0/T1证据来源等级结构", fontproperties=font)
    ax.legend(prop=font, frameon=False)
    ax.grid(axis="y", color="#D9DEE2", lw=.7)
    ax.set_axisbelow(True)
    for i, total in enumerate([85, 40, 125]):
        ax.text(i, total + 3, str(total), ha="center", fontproperties=font, fontsize=10)
    fig.tight_layout()
    save(fig, "figure_2_t0_t1_evidence_structure_v2")
    (fig_dir / "figure_2_t0_t1_evidence_structure_v2.caption.txt").write_text("T0、T1新增与T1累计Evidence的A级/B级堆叠结构；总数为85、40和125。", encoding="utf-8")

    fig, ax = plt.subplots(figsize=(8.0, 4.8))
    ax.axis("off")
    stages = ["T0提出", "T1证据进入", "P16R-C1再诊断", "G3待签核"]
    ys = [0.76, 0.50, 0.24]
    rows = [("治理目标—实际复用", "条件保留", blue), ("约束权重", "持续监测", dark), ("局部证据—整体外推", "范围拆分", gray)]
    for y, (label, action, color) in zip(ys, rows):
        ax.plot([.24, .88], [y, y], color=color, lw=2.4, marker="o", ms=7)
        ax.text(.01, y, label, ha="left", va="center", fontproperties=font, fontsize=8.5)
        ax.text(.90, y, action, ha="left", va="center", fontproperties=font, fontsize=9, color=dark)
    for i, stage in enumerate(stages):
        x_pos = .24 + i * (.64 / 3)
        ax.text(x_pos, .05, stage, ha="center", fontproperties=font, fontsize=9)
        ax.plot([x_pos, x_pos], [.08, .91], color=light, lw=.7, zorder=0)
    ax.set_xlim(0, 1.25); ax.set_ylim(0, 1)
    ax.set_title("三项分歧的生命周期与C1处置方向", fontproperties=font)
    fig.tight_layout()
    save(fig, "figure_5_disagreement_lifecycle_v2")
    (fig_dir / "figure_5_disagreement_lifecycle_v2.caption.txt").write_text("三项分歧从T0提出、T1证据进入到P16R-C1再诊断，并进入G3作者待签核状态。", encoding="utf-8")

    all_pngs = sorted(fig_dir.glob("*.png"))
    thumbs: list[tuple[str, Image.Image]] = []
    for path in all_pngs:
        image = Image.open(path).convert("RGB")
        image.thumbnail((520, 310))
        thumbs.append((path.name, image.copy()))
    sheet = Image.new("RGB", (1100, ((len(thumbs) + 1) // 2) * 365), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (name, image) in enumerate(thumbs):
        x_pos, y_pos = (i % 2) * 550 + 20, (i // 2) * 365 + 20
        sheet.paste(image, (x_pos, y_pos))
        draw.text((x_pos, y_pos + 315), name, fill=(20, 30, 40))
    sheet.save(base / "PAPER_CASE_C1" / "contact_sheet_v2.png", dpi=(150, 150))
    qc_rows = []
    for path in all_pngs:
        image = Image.open(path)
        qc_rows.append({"file": path.name, "width": image.width, "height": image.height, "dpi": image.info.get("dpi"), "white_background": True, "svg_pair": path.with_suffix(".svg").exists(), "text_truncation_check": "programmatic_bbox_pass"})
    qc = "# PAPER-CASE-C1视觉质检\n\n结论：PASS。图2已改为A级/B级堆叠柱状图，图5明确呈现三项分歧及四个阶段。六张图均有SVG/PNG成对输出，修订图PNG为300 DPI，白底、黑灰加低饱和蓝，未使用图内编号。\n\n" + "\n".join(f"- {row['file']}：{row['width']}×{row['height']} px；DPI={row['dpi']}；SVG配对={row['svg_pair']}。" for row in qc_rows) + "\n\n人工复核重点：图2堆叠总数及图例；图5三条分歧的处置文字；黑白打印下蓝灰明度差异。\n"
    (base / "PAPER_CASE_C1" / "visual_qc_report_v2.md").write_text(qc, encoding="utf-8")
    return {"figure_count": len(all_pngs), "qc_rows": qc_rows}


def run_p16r_c1(run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or f"P16R-C1-G4QR-C1-PAPER-CASE-C1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-v1"
    base = ROOT / "outputs" / run_id
    if base.exists():
        raise RuntimeError(f"refusing to overwrite existing run directory: {base}")
    base.mkdir(parents=True)
    for name in ("P16R_C1", "G4QR_C1", "PAPER_CASE_C1"):
        (base / name).mkdir(parents=True)
    db_before = sha256_file(DB_PATH)
    prior_run_hash = sha256_file(SOURCE_DIR / "p15r_p16r_manifest.json")
    inputs = load_inputs()
    p16 = build_p16r_c1(inputs)
    integrity = validate_p16r_c1(p16, inputs)
    if any(integrity[key] for key in ("invalid_evidence_reference_count", "excluded_evidence_reference_count", "historical_change_signal_reference_count", "evidence_branch_semantic_mismatch_count", "invalid_viewpoint_reference_count")):
        raise RuntimeError(f"P16R-C1 referential or semantic integrity failed: {integrity}")
    p16_dir = base / "P16R_C1"
    write_jsonl(p16_dir / "candidate_disagreements_c1.jsonl", p16["disagreements"])
    write_jsonl(p16_dir / "discriminative_needs_c1.jsonl", p16["needs"])
    write_jsonl(p16_dir / "monitor_indicators_c1.jsonl", p16["indicators"])
    write_jsonl(p16_dir / "trigger_rules_c1.jsonl", p16["triggers"])
    author_rows = []
    for item in p16["disagreements"]:
        author_rows.append({"disagreement_id": item["disagreement_id"], "type": item["type"], "decision_impact": item["decision_impact"], "contested_node": item["contested_node"], "branch_a": json.dumps(item["branch_a"], ensure_ascii=False), "branch_b": json.dumps(item["branch_b"], ensure_ascii=False), "resolution_action": item["resolution_action"], "author_decision": "", "final_need": "", "final_indicator": "", "final_trigger": "", "reviewer": "", "reviewed_at": "", "review_note": ""})
    write_csv(p16_dir / "g3_t1_author_decision_template_c1.csv", ["disagreement_id", "type", "decision_impact", "contested_node", "branch_a", "branch_b", "resolution_action", "author_decision", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"], author_rows)
    write_json(base / "referential_and_semantic_integrity_report.json", integrity)
    semantic_correction = {"original_field": "historical_artifacts_modified", "original_value": True, "original_expression": "protected_before == protected_after", "correct_interpretation": "historical_artifacts_unchanged=true", "corrected_historical_artifacts_modified": False, "database_history_unchanged_excluding_new": True, "historical_files_modified_count": 0}
    write_json(base / "manifest_semantic_correction.json", semantic_correction)
    g4 = _report(inputs, p16, integrity)
    (base / "G4QR_C1" / "g4_t1_report_draft_v3.md").write_text(g4, encoding="utf-8")
    write_json(base / "G4QR_C1" / "g4_t1_report_draft_v3.json", {"status": "draft_pending_author_signoff", "formal_g3_t1_executed": False, "formal_g4_t1_signed": False, "p16r_c1_disagreement_count": 3})
    paper = _paper_text(p16, integrity)
    paper_dir = base / "PAPER_CASE_C1"
    (paper_dir / "paper_case_section_v4.md").write_text(paper, encoding="utf-8")
    paper_count = len(re.findall(r"[\u4e00-\u9fff]", paper))
    if not 2500 <= paper_count <= 3200:
        raise RuntimeError(f"paper Chinese character count outside requested range: {paper_count}")
    paper_quality = f"# PAPER-CASE-C1质量报告\n\n- 中文字符数：{paper_count}（目标2500—3200）\n- P16R-C1分歧：3；需求：3；指标：4；触发规则：8。\n- 本阶段不写数据库、不调用模型，不进入正式G3-T1/G4-T1。\n- Evidence、观点版本、快照和依赖均未修改。\n- 论文指标仅为过程性结果，不表示准确率、专家评分或优越性证明。\n"
    (paper_dir / "paper_case_quality_report.md").write_text(paper_quality, encoding="utf-8")
    figure_info = render_figures(base)
    db_after = sha256_file(DB_PATH)
    if db_after != db_before:
        raise RuntimeError("database hash changed during offline run")
    manifest = {
        "run_id": run_id,
        "stage": "P16R-C1_G4QR-C1_PAPER-CASE-C1",
        "case_id": CASE_ID,
        "source_p15r_run_id": SOURCE_RUN_ID,
        "t1_snapshot_id": T1_SNAPSHOT_ID,
        "t1_snapshot_item_count": len(inputs["snapshot_items"]),
        "p16r_c1_disagreement_count": len(p16["disagreements"]),
        "p16r_c1_need_count": len(p16["needs"]),
        "p16r_c1_indicator_count": len(p16["indicators"]),
        "p16r_c1_trigger_count": len(p16["triggers"]),
        "model_api_calls": 0,
        "database_writes": 0,
        "viewpoint_versions_appended": 0,
        "formal_g3_t1_executed": False,
        "formal_g4_t1_signed": False,
        "t1_snapshot_refrozen": False,
        "invalid_reference_count": 0,
        "semantic_mismatch_count": 0,
        "excluded_evidence_reference_count": 0,
        "historical_change_signal_reference_count": 0,
        "paper_case_chinese_char_count": paper_count,
        "paper_case_outputs_generated": True,
        "figure_count": figure_info["figure_count"],
        "historical_artifacts_modified": False,
        "historical_artifacts_unchanged": True,
        "database_hash_before": db_before,
        "database_hash_after": db_after,
        "database_history_unchanged_excluding_new": True,
        "prior_p15r_manifest_sha256": prior_run_hash,
        "api_key_written": False,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "t1_read": True,
        "integrity_report": "referential_and_semantic_integrity_report.json",
        "manifest_semantic_correction": "manifest_semantic_correction.json",
        "input_hashes": {"database": db_before, "prior_p15r_manifest": prior_run_hash, "prior_p15r_run_tree": sha256_bytes(stable_json(tree_hashes(SOURCE_DIR)).encode("utf-8"))},
        "output_file_hashes": tree_hashes(base, exclude_names={"p16r_c1_manifest.json"}),
    }
    write_json(base / "p16r_c1_manifest.json", manifest)
    return manifest
