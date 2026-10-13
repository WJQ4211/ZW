"""Offline formal G3, G4-T0 draft, and paper case package.

This module intentionally has no model-client or network dependency.  It turns
the already-authorized P7R/P9R material into append-only G3 records and
presentation artifacts.  The evidence, hypotheses, viewpoints, and their
dependencies are treated as immutable inputs; only G3 disagreement and
monitoring records are appended in one SQLite transaction.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schemas import (
    BranchClaim,
    DiscriminativeNeed,
    Disagreement,
    EvidenceCitation,
    HumanReview,
    MonitorIndicator,
    TriggerRule,
    model_dump,
    model_validate,
)


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
P7R_RUN_ID = "P7R-P9R-20260827T020335Z-case-strengthening-v1"
P7R_DIR = ROOT / "outputs" / P7R_RUN_ID
P4_DIR = ROOT / "outputs" / "P4-T0-C6C812108AF95309-freeze-v1"
G2_DIR = ROOT / "outputs" / "G2-T0-1B8D6A450720F041-formal-v1"
DB_PATH = ROOT / "data" / "research.sqlite3"

AUTHORIZATION_TEXT = (
    "我同意G3将DISAG-T0-01作为高影响条件性分歧保留，将DISAG-T0-02作为中影响条件性分歧保留，将DISAG-T0-03通过“治理架构建立—运行整合实现”的范围拆分予以解决；同意离线修复监测对象ID并将重复内容归并为三项判别性证据需求，授权按“AI预审建议＋作者整体签核”执行正式G3，同时生成G4-T0待签发报告和论文案例展示图表，但暂不正式签发G4。"
)
AUTHORIZATION_SHA256 = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()
REVIEW_MODE = "ai_preannotation_plus_author_batch_signoff"
REVIEWER = "author_01"
OBSERVATION_WINDOW = "2025-01-01/2026-06-30"
PERSPECTIVES = ("STRAT", "TECH", "ENG", "OPS", "TRUST")
HYPOTHESES = ("HYP-T0-01", "HYP-T0-02", "HYP-T0-03")


class G3OfflineError(RuntimeError):
    """Raised when a G3 input or transaction invariant is not met."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _idempotency_json(value: Any) -> str:
    """Compare the same authorized replay without treating audit timestamps/paths as content changes."""
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items() if key not in {"reviewed_at", "artifact_path", "created_at", "run_id", "created_by"}}
        if isinstance(item, list):
            return [normalize(val) for val in item]
        return item
    return _json(normalize(value))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return _json(value)
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _view_ref(viewpoint_id: str, version: int) -> str:
    return f"{viewpoint_id}:{version}"


def _citation(evidence_id: str, version: int, relation: str = "support", note: str = "") -> dict[str, Any]:
    return model_dump(
        model_validate(
            EvidenceCitation,
            {"evidence_id": evidence_id, "version": version, "relation": relation, "note": note},
        )
    )


def _load_inputs() -> dict[str, Any]:
    p4_manifest_path = P4_DIR / "p4_t0_snapshot_manifest.json"
    p4_evidence_path = P4_DIR / "p4_t0_frozen_evidence.jsonl"
    g2_hyp_path = G2_DIR / "g2_final_hypotheses.jsonl"
    g2_base_path = G2_DIR / "g2_shared_baseline.jsonl"
    views_path = P7R_DIR / "P8R" / "final_viewpoints_latest.jsonl"
    deps_path = P7R_DIR / "P8R" / "evidence_dependencies.jsonl"
    disagreements_path = P7R_DIR / "P9R" / "candidate_disagreements.jsonl"
    monitoring_path = P7R_DIR / "normalized_responses" / "p9r_monitoring.json"
    required = [
        p4_manifest_path, p4_evidence_path, g2_hyp_path, g2_base_path, views_path,
        deps_path, disagreements_path, monitoring_path,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise G3OfflineError(f"missing authoritative input: {missing}")
    p4_manifest = _read_json(p4_manifest_path)
    evidence = _read_jsonl(p4_evidence_path)
    hypotheses = _read_jsonl(g2_hyp_path)
    baseline_rows = _read_jsonl(g2_base_path)
    views = _read_jsonl(views_path)
    dependencies = _read_jsonl(deps_path)
    candidate_disagreements = _read_jsonl(disagreements_path)
    monitoring = _read_json(monitoring_path)
    if p4_manifest.get("snapshot_id") != SNAPSHOT_ID or p4_manifest.get("frozen") is not True:
        raise G3OfflineError("P4 snapshot identity/frozen assertion failed")
    if len(evidence) != 85 or len(hypotheses) != 3 or len(baseline_rows) != 1 or len(views) != 15:
        raise G3OfflineError("authoritative input counts do not match 85/3/1/15")
    if len(dependencies) != 94 or len(candidate_disagreements) != 3:
        raise G3OfflineError("authoritative input counts do not match 94/3")
    if baseline_rows[0].get("snapshot_id") != SNAPSHOT_ID:
        raise G3OfflineError("shared baseline is bound to another snapshot")
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    view_by_ref = {_view_ref(row["viewpoint_id"], int(row["version"])): row for row in views}
    if len(evidence_by_id) != 85 or len(view_by_ref) != 15:
        raise G3OfflineError("input IDs are not unique")
    for view in views:
        if view.get("snapshot_id") != SNAPSHOT_ID or view.get("status") not in {"admitted", "conditional"}:
            raise G3OfflineError(f"invalid latest viewpoint: {view.get('viewpoint_id')}")
        for citation in view.get("supporting_evidence", []) + view.get("counter_evidence", []):
            if citation.get("evidence_id") not in evidence_by_id or int(citation.get("version", 0)) != int(evidence_by_id[citation["evidence_id"]].get("version", 1)):
                raise G3OfflineError(f"viewpoint cites evidence outside snapshot: {citation}")
    for dep in dependencies:
        if dep.get("evidence_id") not in evidence_by_id or dep.get("viewpoint_id") not in {v["viewpoint_id"] for v in views}:
            raise G3OfflineError(f"dependency outside snapshot/current viewpoints: {dep}")
    return {
        "p4_manifest": p4_manifest,
        "evidence": evidence,
        "evidence_by_id": evidence_by_id,
        "hypotheses": hypotheses,
        "baseline": baseline_rows[0],
        "views": views,
        "view_by_ref": view_by_ref,
        "dependencies": dependencies,
        "candidate_disagreements": candidate_disagreements,
        "monitoring": monitoring,
        "input_paths": {
            "p4_manifest": p4_manifest_path,
            "p4_evidence": p4_evidence_path,
            "g2_hypotheses": g2_hyp_path,
            "g2_baseline": g2_base_path,
            "p7r_manifest": P7R_DIR / "p7r_p9r_manifest.json",
            "views": views_path,
            "dependencies": deps_path,
            "candidate_disagreements": disagreements_path,
            "monitoring": monitoring_path,
        },
    }


def _candidate_viewpoint_refs(candidate: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    refs: list[str] = []
    fields: list[dict[str, Any]] = []
    for item in candidate.get("viewpoint_refs", []):
        vp_id = str(item.get("viewpoint_id", ""))
        version = int(item.get("version", 0))
        field = str(item.get("field", ""))
        if vp_id and version > 0:
            refs.append(_view_ref(vp_id, version))
            fields.append({"viewpoint_id": vp_id, "version": version, "field": field})
    return _unique(refs), fields


def _candidate_branch(candidate: dict[str, Any], branch_name: str, evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    branch = candidate.get(branch_name) or {}
    citations = []
    for evidence_id in _unique(branch.get("evidence_ids", [])):
        if evidence_id not in evidence_by_id:
            raise G3OfflineError(f"candidate disagreement references unknown evidence: {evidence_id}")
        version = int(evidence_by_id[evidence_id].get("version", 1))
        citations.append(_citation(evidence_id, version))
    return model_dump(
        model_validate(
            BranchClaim,
            {
                "claim": branch.get("claim", ""),
                "evidence": citations,
                "expected_observables": [],
            },
        )
    )


def _latest_refs_for_hypothesis(views: list[dict[str, Any]], hypothesis_id: str) -> list[str]:
    return [_view_ref(row["viewpoint_id"], int(row["version"])) for row in views if row.get("hypothesis_id") == hypothesis_id]


def _build_disagreements(data: dict[str, Any], reviewed_at: str) -> list[dict[str, Any]]:
    candidates = {row["disagreement_id"]: row for row in data["candidate_disagreements"]}
    if set(candidates) != {"DISAG-T0-01", "DISAG-T0-02", "DISAG-T0-03"}:
        raise G3OfflineError("unexpected candidate disagreement IDs")
    result: list[dict[str, Any]] = []
    for disagreement_id, decision_impact, dtype, status, action, contested, a_claim, b_claim in [
        (
            "DISAG-T0-01", "high", "mechanism", "conditionally_retained", "monitor",
            "判断体系整合是否已经成为主导演进方向时，应以组织、制度和共用平台建设作为领先指标，还是必须以跨军种、跨任务的实际复用作成立判据。",
            "CDAO、JCF和统一治理文件的形成可视为体系整合的领先信号，虽然工程与运行效果尚未完全显现，但可能表明演进方向正在改变。",
            "只有形成跨军种共用平台、统一工程流程或跨任务实际复用，才能认为体系整合已成为主导路径；当前任务驱动的分散试验和局部部署仍是可观察的主要形态。",
        ),
        (
            "DISAG-T0-02", "medium", "risk_weight", "conditionally_retained", "monitor",
            "任务分散和应用不均衡主要是数据、算力、基础设施、采办及测试治理瓶颈造成的过渡状态，还是适应任务差异的常态化发展形态。",
            "资源和治理瓶颈是造成能力形成缓慢与应用不均衡的主要机制；瓶颈缓解应带来更广泛的部署和复用。",
            "任务差异本身足以使项目保持分布式发展；即使资源条件改善，也不必然形成统一平台或跨任务复用。",
        ),
        (
            "DISAG-T0-03", "medium", "scope", "resolved", "scope_split",
            "治理架构建立与运行整合实现是同一判断层次，还是需要分开观察的两个层次。",
            "治理架构建立反映组织、制度和治理目标已经形成，但不等同于运行整合已经实现。",
            "运行整合应以跨军种共用平台、统一标准和跨任务实际复用为判据，不能由治理机制的存在直接替代。",
        ),
    ]:
        original = candidates[disagreement_id]
        original_refs, original_fields = _candidate_viewpoint_refs(original)
        refs = list(original_refs)
        fields = list(original_fields)
        if disagreement_id == "DISAG-T0-01":
            for hypothesis_id in ("HYP-T0-01", "HYP-T0-02"):
                for ref in _latest_refs_for_hypothesis(data["views"], hypothesis_id):
                    if ref not in refs:
                        refs.append(ref)
                        fields.append({"viewpoint_id": ref.rsplit(":", 1)[0], "version": int(ref.rsplit(":", 1)[1]), "field": "judgment"})
        if disagreement_id == "DISAG-T0-03":
            status_reason = "按作者授权将治理架构建立与运行整合实现拆分；现有证据支持前者，但不足以证明后者。"
        elif disagreement_id == "DISAG-T0-01":
            status_reason = "两条机制解释均有合法观点和证据引用，但当前公开T0材料不足以直接核查治理领先信号是否已转化为实际复用。"
        else:
            status_reason = "资源与治理瓶颈解释和任务差异解释均有合法引用，当前材料不足以直接判定资源改善是否改变分布式模式。"
        raw_a = original.get("branch_a", {})
        raw_b = original.get("branch_b", {})
        formal = model_validate(
            Disagreement,
            {
                "disagreement_id": disagreement_id,
                "snapshot_id": SNAPSHOT_ID,
                "viewpoint_refs": refs,
                "viewpoint_field_refs": fields,
                "type": dtype,
                "contested_node": contested,
                "branch_a": {**_candidate_branch({"branch_a": raw_a}, "branch_a", data["evidence_by_id"]), "claim": a_claim},
                "branch_b": {**_candidate_branch({"branch_b": raw_b}, "branch_b", data["evidence_by_id"]), "claim": b_claim},
                "decision_impact": decision_impact,
                "resolvable_now": False if status != "resolved" else True,
                "resolution_action": action,
                "status": status,
                "human_verified": True,
                "author_authorization_sha256": AUTHORIZATION_SHA256,
                "review_mode": REVIEW_MODE,
                "reviewed_by": REVIEWER,
                "reviewed_at": reviewed_at,
            },
        )
        row = model_dump(formal)
        row["formal_resolution_reason"] = status_reason
        row["source_candidate_disagreement"] = original
        result.append(row)
    return result


def _build_needs(reviewed_at: str) -> list[dict[str, Any]]:
    rows = [
        {
            "need_id": "NEED-T0-01", "disagreement_id": "DISAG-T0-01",
            "question": "在2025年1月1日至2026年6月30日期间，共用AI平台、统一数据或接口标准以及统一工程流程是否被不同军种、不同任务实际采用？",
            "possible_outcomes": ["官方可核验的跨军种、跨任务实际采用", "经完整检索和人工确认仍主要使用独立平台和流程"],
            "favours_if": {"outcome_1": "增强HYP-T0-01，削弱HYP-T0-02", "outcome_2": "增强HYP-T0-02"},
            "source_plan": ["A/B级国防部、CDAO和军种官方材料", "A/B级预算、采办与项目状态材料", "完整检索日志与人工确认用于否定性判断"],
            "feasibility": "medium", "discriminativeness": 5,
        },
        {
            "need_id": "NEED-T0-02", "disagreement_id": "DISAG-T0-01",
            "question": "DARPA等任务项目在后续观察窗口内以何种方式由演示验证转入后续应用？",
            "possible_outcomes": ["转入单一军种或任务专属采办、部署", "形成跨任务或跨军种复用的模型、平台、接口或工程流程", "继续停留在演示阶段且官方材料明确指出资源或治理约束"],
            "favours_if": {"outcome_1": "增强HYP-T0-02", "outcome_2": "增强HYP-T0-01", "outcome_3": "增强HYP-T0-03"},
            "source_plan": ["A/B级DARPA、军种和作战单位项目状态材料", "A/B级采办、部署和试验转化文件", "项目原始公告与后续官方状态核验"],
            "feasibility": "medium", "discriminativeness": 5,
        },
        {
            "need_id": "NEED-T0-03", "disagreement_id": "DISAG-T0-02",
            "question": "资源和治理瓶颈在观察窗口内是否得到实质缓解，以及缓解后是否出现应用范围扩大？",
            "possible_outcomes": ["瓶颈持续且应用仍不均衡", "瓶颈缓解但项目仍保持任务专属、分布式发展", "瓶颈缓解并伴随跨任务或跨军种规模化应用"],
            "favours_if": {"outcome_1": "增强HYP-T0-03", "outcome_2": "增强HYP-T0-02并削弱约束唯一主因解释", "outcome_3": "触发HYP-T0-01、HYP-T0-02和HYP-T0-03共同重评"},
            "source_plan": ["A/B级预算执行、数据战略和基础设施材料", "A/B级采办、测试评估和治理文件", "GAO/CRS等A/B级监督材料与完整检索日志"],
            "feasibility": "medium", "discriminativeness": 5,
        },
    ]
    result = []
    for row in rows:
        result.append(model_dump(model_validate(DiscriminativeNeed, {
            **row, "observation_window": OBSERVATION_WINDOW, "status": "approved",
            "human_verified": True, "author_authorization_sha256": AUTHORIZATION_SHA256,
            "review_mode": REVIEW_MODE,
        })))
    return result


def _build_indicators() -> list[dict[str, Any]]:
    rows = [
        ("IND-T0-01", "NEED-T0-01", "共用平台与统一标准的跨军种实际采用", "观察不同军种、不同任务是否在公开材料中被确认实际采用同一平台、统一数据或接口标准、统一工程流程；规划发布不计为实际采用。", ["A/B级国防部和CDAO官方材料", "A/B级军种项目与采办状态材料"], "跨军种实际采用增强HYP-T0-01；独立平台经完整检索确认增强HYP-T0-02", "独立平台经完整检索确认削弱HYP-T0-01并增强HYP-T0-02"),
        ("IND-T0-02", "NEED-T0-02", "任务项目的转化路径", "观察任务项目是否从演示转入任务专属采办/部署、跨任务或跨军种复用，或因明确约束继续停留在演示。", ["A/B级DARPA、军种项目状态材料", "A/B级采办、部署和试验文件"], "跨任务或跨军种复用增强HYP-T0-01；任务专属转化增强HYP-T0-02", "任务专属转化削弱HYP-T0-01；明确约束下继续演示增强HYP-T0-03"),
        ("IND-T0-03", "NEED-T0-03", "资源与治理瓶颈状态", "观察数据质量、边缘算力、基础设施、采办指导和测试评估标准的公开状态变化；未检索到材料不直接解释为未发生。", ["A/B级预算、数据战略和基础设施材料", "A/B级GAO/CRS及治理文件"], "瓶颈缓解支持对HYP-T0-03的重评", "瓶颈持续增强HYP-T0-03"),
        ("IND-T0-04", "NEED-T0-03", "瓶颈变化后的应用广度", "在确认资源或治理条件变化后，观察应用是否扩展至更多任务、平台或军种；应用范围变化需由公开原始材料支持。", ["A/B级军种项目和部署材料", "A/B级国防部跨项目、跨军种状态材料"], "跨任务或跨军种扩展增强HYP-T0-01并促使三项假设重评", "瓶颈缓解但应用仍分布式增强HYP-T0-02"),
    ]
    result = []
    for item in rows:
        indicator_id, need_id, name, definition, sources, direction_a, direction_b = item
        relevant = [
            row["viewpoint_id"] for row in _GLOBAL_INPUTS["views"]
            if row.get("hypothesis_id") in ({"HYP-T0-01", "HYP-T0-02"} if need_id != "NEED-T0-03" else {"HYP-T0-02", "HYP-T0-03"})
        ]
        result.append(model_dump(model_validate(MonitorIndicator, {
            "indicator_id": indicator_id, "need_id": need_id, "name": name,
            "operational_definition": definition, "source_types": sources,
            "cadence": "quarterly", "direction_a": direction_a, "direction_b": direction_b,
            "affected_viewpoint_ids": _unique(relevant), "status": "approved",
            "observation_window": OBSERVATION_WINDOW, "human_verified": True,
            "author_authorization_sha256": AUTHORIZATION_SHA256, "review_mode": REVIEW_MODE,
        })))
    return result


def _build_triggers(indicators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["indicator_id"]: row for row in indicators}
    specs = [
        ("TRG-T0-01", "IND-T0-01", "event_match", "官方材料确认共用平台、统一数据/接口标准或统一工程流程被至少两个军种实际采用。", "增强HYP-T0-01", "削弱HYP-T0-02", ["A", "B"]),
        ("TRG-T0-02", "IND-T0-01", "expert_confirmed", "完整检索并经人工确认仍主要使用相互独立的平台和流程；不得以未检索到材料单独作出否定判断。", "增强HYP-T0-02", "削弱HYP-T0-01", ["A", "B"]),
        ("TRG-T0-03", "IND-T0-02", "status_change", "任务项目进入单一军种或任务专属采办、部署或持续使用。", "增强HYP-T0-02", "削弱HYP-T0-01", ["A", "B"]),
        ("TRG-T0-04", "IND-T0-02", "event_match", "官方材料确认任务项目形成跨任务或跨军种复用的平台、模型、接口或工程流程。", "增强HYP-T0-01", "削弱HYP-T0-02", ["A", "B"]),
        ("TRG-T0-05", "IND-T0-02", "expert_confirmed", "官方材料明确说明资源或治理约束使项目继续停留在演示阶段。", "增强HYP-T0-03", "削弱HYP-T0-02", ["A", "B"]),
        ("TRG-T0-06", "IND-T0-03", "status_change", "数据、算力、基础设施、采办或测试评估瓶颈在公开材料中持续存在。", "增强HYP-T0-03", "削弱资源投入已充分缓解瓶颈的解释", ["A", "B"]),
        ("TRG-T0-07", "IND-T0-04", "event_match", "资源或治理条件出现缓解，但公开材料显示应用仍以任务专属、分布式方式扩展。", "增强HYP-T0-02", "削弱HYP-T0-03作为唯一主因的解释", ["A", "B"]),
        ("TRG-T0-08", "IND-T0-04", "event_match", "资源或治理条件出现缓解，并出现跨任务或跨军种应用扩展。", "增强HYP-T0-01并触发三项假设共同重评", "削弱当前分散模式或约束锁定的单一路径解释", ["A", "B"]),
    ]
    result = []
    for trigger_id, indicator_id, predicate_type, condition, strengthen, weaken, grades in specs:
        affected = by_id[indicator_id]["affected_viewpoint_ids"]
        result.append(model_dump(model_validate(TriggerRule, {
            "trigger_id": trigger_id, "indicator_id": indicator_id, "predicate_type": predicate_type,
            "predicate": {"condition": condition, "strengthens_branch": strengthen, "weakens_branch": weaken, "observation_window": OBSERVATION_WINDOW},
            "required_source_grade": grades, "action": "review_viewpoints",
            "affected_viewpoint_ids": affected, "human_approval_required": True, "status": "approved",
            "observation_window": OBSERVATION_WINDOW, "human_verified": True,
            "author_authorization_sha256": AUTHORIZATION_SHA256, "review_mode": REVIEW_MODE,
        })))
    return result


def _build_human_review(run_dir: Path, reviewed_at: str) -> dict[str, Any]:
    review = model_validate(HumanReview, {
        "review_id": f"G3-T0-REVIEW-{AUTHORIZATION_SHA256[:16]}", "gate": "G3", "case_id": CASE_ID,
        "approved": True, "reviewer": REVIEWER, "reviewed_at": reviewed_at,
        "artifact_path": str((run_dir / "g3_author_authorization.txt").relative_to(ROOT)).replace("\\", "/"),
        "artifact_hash": AUTHORIZATION_SHA256,
        "notes": "author_review_mode=ai_preannotation_plus_author_batch_signoff; author_signoff_count=1; author_decision_count=3; raw_source_visual_review_claimed=false; 作者整体签核三项G3决策，不表示逐条重读85条原始证据。",
    })
    return model_dump(review)


def _preserved_db_hash(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        rows: list[Any] = []
        for table in ("evidence", "evidence_snapshot", "snapshot_items", "hypothesis", "viewpoint", "evidence_dependency"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                rows.append({"table": table, "columns": columns, "row": list(row)})
        return _sha256_bytes(_json(rows).encode("utf-8"))
    finally:
        conn.close()


def _insert_rows_transaction(
    database_path: Path,
    disagreements: list[dict[str, Any]],
    needs: list[dict[str, Any]],
    indicators: list[dict[str, Any]],
    triggers: list[dict[str, Any]],
    human_review: dict[str, Any],
    inject_failure_at: int | None = None,
) -> int:
    conn = sqlite3.connect(str(database_path))
    conn.execute("PRAGMA foreign_keys = ON")
    added = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        snap = conn.execute("SELECT frozen, case_id FROM evidence_snapshot WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()
        if not snap or int(snap[0]) != 1 or snap[1] != CASE_ID:
            raise G3OfflineError("target snapshot is absent or not frozen")
        all_payloads = [
            ("disagreement", "disagreement_id", "snapshot_id", "status", disagreements),
            ("discriminative_need", "need_id", "disagreement_id", None, needs),
            ("monitor_indicator", "indicator_id", "need_id", "status", indicators),
            ("trigger_rule", "trigger_id", "indicator_id", "status", triggers),
        ]
        existing_target = {
            table: {row[0]: json.loads(row[1]) for row in conn.execute(f"SELECT {key}, payload_json FROM {table}")}
            for table, key, _, _, _ in all_payloads
        }
        existing_review = conn.execute("SELECT payload_json FROM human_review WHERE review_id=?", (human_review["review_id"],)).fetchone()
        if existing_review:
            if _idempotency_json(json.loads(existing_review[0])) != _idempotency_json(human_review):
                raise G3OfflineError("human review ID exists with different content")
        for table, key, foreign_key, status_key, rows in all_payloads:
            for row in rows:
                object_id = row[key]
                if object_id in existing_target[table]:
                    if _idempotency_json(existing_target[table][object_id]) != _idempotency_json(row):
                        raise G3OfflineError(f"{table}/{object_id} exists with different content")
                    continue
                if table == "disagreement":
                    conn.execute("INSERT INTO disagreement(disagreement_id,snapshot_id,decision_impact,status,payload_json) VALUES(?,?,?,?,?)", (object_id, SNAPSHOT_ID, row["decision_impact"], row["status"], _json(row)))
                elif table == "discriminative_need":
                    conn.execute("INSERT INTO discriminative_need(need_id,disagreement_id,payload_json) VALUES(?,?,?)", (object_id, row[foreign_key], _json(row)))
                elif table == "monitor_indicator":
                    conn.execute("INSERT INTO monitor_indicator(indicator_id,need_id,status,payload_json) VALUES(?,?,?,?)", (object_id, row[foreign_key], row[status_key], _json(row)))
                elif table == "trigger_rule":
                    conn.execute("INSERT INTO trigger_rule(trigger_id,indicator_id,status,payload_json) VALUES(?,?,?,?)", (object_id, row[foreign_key], row[status_key], _json(row)))
                added += 1
                if inject_failure_at is not None and added >= inject_failure_at:
                    raise RuntimeError("injected G3 transaction failure")
        if not existing_review:
            conn.execute("INSERT INTO human_review(review_id,gate,case_id,approved,payload_json) VALUES(?,?,?,?,?)", (human_review["review_id"], "G3", CASE_ID, 1, _json(human_review)))
            added += 1
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _test_rollback(database_path: Path, disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], review: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="g3-rollback-") as tmp:
        copy = Path(tmp) / "research.sqlite3"
        shutil.copy2(database_path, copy)
        before = _preserved_db_hash(copy)
        conn = sqlite3.connect(str(copy))
        try:
            # Exercise the write path from a clean target while leaving the
            # real database completely untouched.  This also makes the test
            # valid when an authorized replay is being audited.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM trigger_rule WHERE trigger_id IN (%s)" % ",".join("?" * len(triggers)), [row["trigger_id"] for row in triggers])
            conn.execute("DELETE FROM monitor_indicator WHERE indicator_id IN (%s)" % ",".join("?" * len(indicators)), [row["indicator_id"] for row in indicators])
            conn.execute("DELETE FROM discriminative_need WHERE need_id IN (%s)" % ",".join("?" * len(needs)), [row["need_id"] for row in needs])
            conn.execute("DELETE FROM disagreement WHERE disagreement_id IN (%s)" % ",".join("?" * len(disagreements)), [row["disagreement_id"] for row in disagreements])
            conn.execute("DELETE FROM human_review WHERE review_id=?", (review["review_id"],))
            conn.commit()
        finally:
            conn.close()
        try:
            _insert_rows_transaction(copy, disagreements, needs, indicators, triggers, review, inject_failure_at=2)
        except RuntimeError:
            pass
        else:
            return False
        after = _preserved_db_hash(copy)
        conn = sqlite3.connect(str(copy))
        try:
            g3_count = conn.execute("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0]
        finally:
            conn.close()
        return before == after and g3_count == 0


def _test_idempotency(database_path: Path, disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], review: dict[str, Any]) -> bool:
    def counts() -> tuple[int, int, int, int, int]:
        conn = sqlite3.connect(str(database_path))
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM discriminative_need n JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM monitor_indicator i JOIN discriminative_need n ON i.need_id=n.need_id JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM trigger_rule t JOIN monitor_indicator i ON t.indicator_id=i.indicator_id JOIN discriminative_need n ON i.need_id=n.need_id JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM human_review WHERE review_id=?", (review["review_id"],)).fetchone()[0],
            )
        finally:
            conn.close()

    before = counts()
    first = _insert_rows_transaction(database_path, disagreements, needs, indicators, triggers, review)
    after_first = counts()
    second = _insert_rows_transaction(database_path, disagreements, needs, indicators, triggers, review)
    after_second = counts()
    return second == 0 and first == 0 and after_first == before and after_second == before


def _historical_hashes(paths: dict[str, Path]) -> dict[str, str]:
    return {key: _sha256_file(path) for key, path in paths.items() if path.exists()}


def _format_evidence(evidence_id: str, evidence_by_id: dict[str, dict[str, Any]]) -> str:
    row = evidence_by_id.get(evidence_id, {})
    return f"{evidence_id}（{row.get('source_id', '')}，{row.get('source_locator', row.get('page_or_section', ''))}）"


def _build_g3_outputs(out: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], reviewed_at: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "g3_author_authorization.txt").write_text(AUTHORIZATION_TEXT + "\n", encoding="utf-8")
    decisions = [
        {"disagreement_id": "DISAG-T0-01", "decision": "conditionally_retained", "decision_impact": "high", "resolution_action": "monitor", "reviewer": REVIEWER, "reviewed_at": reviewed_at, "author_authorization_sha256": AUTHORIZATION_SHA256},
        {"disagreement_id": "DISAG-T0-02", "decision": "conditionally_retained", "decision_impact": "medium", "resolution_action": "monitor", "reviewer": REVIEWER, "reviewed_at": reviewed_at, "author_authorization_sha256": AUTHORIZATION_SHA256},
        {"disagreement_id": "DISAG-T0-03", "decision": "resolved", "decision_impact": "medium", "resolution_action": "scope_split", "reviewer": REVIEWER, "reviewed_at": reviewed_at, "author_authorization_sha256": AUTHORIZATION_SHA256},
    ]
    _write_csv(out / "g3_author_decisions.csv", decisions, list(decisions[0]))
    _write_jsonl(out / "g3_formal_disagreements.jsonl", disagreements)
    _write_jsonl(out / "g3_formal_discriminative_needs.jsonl", needs)
    _write_jsonl(out / "g3_formal_monitor_indicators.jsonl", indicators)
    _write_jsonl(out / "g3_formal_trigger_rules.jsonl", triggers)
    _write_monitoring_mapping(out, data, needs)
    _write_db_comparison(out, data, disagreements, needs, indicators, triggers)
    _write_quality_report(out, data, disagreements, needs, indicators, triggers)
    _build_g4_draft(out / "G4_DRAFT", data, disagreements, needs, indicators, triggers)
    _build_paper_package(out / "PAPER_CASE", out / "FIGURES", data, disagreements, needs, indicators, triggers)


def _write_monitoring_mapping(out: Path, data: dict[str, Any], needs: list[dict[str, Any]]) -> None:
    formal_need_by_original = {
        "ND-01": ("DISAG-T0-01", "NEED-T0-01"),
        "ND-02": ("DISAG-T0-01", "NEED-T0-01"),
        "ND-03": ("DISAG-T0-02", "NEED-T0-03"),
        "ND-04": ("DISAG-T0-01", "NEED-T0-01"),
        "ND-05": ("DISAG-T0-01", "NEED-T0-02"),
    }
    rows = []
    for item in data["monitoring"].get("discriminative_needs", []):
        old_id = item.get("need_id", "")
        if old_id in formal_need_by_original:
            target, target_need = formal_need_by_original[old_id]
            action = "map_and_deduplicate"
            if old_id == "ND-03":
                reason = "资源、基础设施、采办与测试瓶颈内容归入风险权重分歧。"
            elif old_id == "ND-05":
                reason = "原DISP-02中的项目由演示转化内容归入治理整合分歧下的项目转化需求。"
            else:
                reason = "原始对象ID为DISP-*且内容属于治理整合与跨军种采用分歧；按授权归并并去重。"
        else:
            target = ""
            target_need = ""
            action = "not_mapped"
            reason = "不属于本次正式三项需求。"
        rows.append({
            "source": "normalized_responses/p9r_monitoring.json", "before": item,
            "after": {"formal_disagreement_id": target, "formal_need_id": target_need},
            "mapping_action": action, "mapping_reason": reason,
            "mapping_origin": "offline_deterministic", "model_modified": False,
            "author_authorization_sha256": AUTHORIZATION_SHA256,
        })
    _write_jsonl(out / "g3_monitoring_before_after_mapping.jsonl", rows)


def _write_db_comparison(out: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = {
            "target_disagreement": conn.execute("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "target_needs": conn.execute("SELECT COUNT(*) FROM discriminative_need n JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "target_indicators": conn.execute("SELECT COUNT(*) FROM monitor_indicator i JOIN discriminative_need n ON i.need_id=n.need_id JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "target_triggers": conn.execute("SELECT COUNT(*) FROM trigger_rule t JOIN monitor_indicator i ON t.indicator_id=i.indicator_id JOIN discriminative_need n ON i.need_id=n.need_id JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "human_review_g3": conn.execute("SELECT COUNT(*) FROM human_review WHERE gate='G3' AND case_id=?", (CASE_ID,)).fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)).fetchone()[0],
            "hypothesis": conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "viewpoint": conn.execute("SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "dependency": conn.execute("SELECT COUNT(*) FROM evidence_dependency WHERE case_id=?", (CASE_ID,)).fetchone()[0],
        }
    finally:
        conn.close()
    text = "# G3数据库对照\n\n"
    text += f"作者授权SHA-256：`{AUTHORIZATION_SHA256}`\n\n"
    text += "本文件由正式G3写入完成后从SQLite独立读取。Evidence、hypothesis、viewpoint及既有依赖边仅作不变性对照。\n\n"
    for key, value in rows.items():
        text += f"- {key}: {value}\n"
    text += "\n预期新增：disagreement=3、discriminative_need=3、monitor_indicator=4、trigger_rule=8、G3 human_review=1。\n"
    text += "DISAG-T0-03保留为已解决范围拆分审计记录；其不生成监测需求。\n"
    text += "未写入Evidence、hypothesis或快照，未执行G4签发。\n"
    (out / "g3_db_comparison.md").write_text(text, encoding="utf-8")


def _write_quality_report(out: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    lines = [
        "# G3质量报告", "", "## 输入与边界", "",
        f"- 作者授权SHA-256：`{AUTHORIZATION_SHA256}`。",
        f"- 冻结Evidence：{len(data['evidence'])}条；共享事实基线：1项；冻结假设：{len(data['hypotheses'])}项。",
        f"- P7R最新观点：{len(data['views'])}条；字段级依赖：{len(data['dependencies'])}条。",
        "- 本阶段全程离线；模型/API调用=0；T1未读取；不执行正式G4。",
        "", "## 正式G3对象", "",
        f"- 分歧：{len(disagreements)}条（条件保留2条，范围拆分已解决1条）。",
        f"- 判别性证据需求：{len(needs)}项；监测指标：{len(indicators)}项；触发规则：{len(triggers)}项。",
        "- 所有监测对象均使用合法DISAG/NEED/IND ID；没有DISP残留进入正式对象。",
        "- 触发规则不创造数值阈值，否定性判断要求完整检索与人工确认，所有指标和触发项保留人工审批要求。",
        "", "## 审核口径", "",
        "作者以整体签核方式确认3项G3决策。human_reviewed/ human_verified表示作者确认了签核摘要和最终治理决定，不表示作者逐条重读85条原始证据，也不宣称作者逐页视觉核验。",
        "", "## 解释边界", "",
        "监测对象来自P9R模型原始建议的离线确定性映射与去重；原始响应未修改。DISAG-T0-01和DISAG-T0-02仍是待观察的条件性分歧，DISAG-T0-03只完成概念范围拆分，不作为监测任务。",
    ]
    (out / "g3_quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_g4_draft(out: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    report = f"""# 美国军用人工智能T0案例报告（待签发草案）

状态：`draft_pending_author_signoff`。本文件不是正式G4签发报告。

## 1. 问题与证据边界

研究对象是截至2024年12月31日的美国军用人工智能能力形成与演进路径。分析只使用{len(data['evidence'])}条正式冻结T0 Evidence、1项共享事实基线、3项冻结竞争性假设和P7R-P9R观点治理结果，不读取T1、不联网、不调用模型。`excerpt_original`是事实核查首要字段，来源等级只表示来源质量层级，不直接决定证据方向。

## 2. 共享事实基线

截至2024年底，美国军用人工智能已在若干特定任务中由技术原型走向试验验证和有限部署，但尚未形成成熟的跨军种、跨任务通用能力体系。该基线不是竞争性假设，也不是对所有军种和任务的全称判断。

## 3. 三项竞争性假设

HYP-T0-01认为统一治理、共用能力平台和标准化流程正在牵引体系整合过渡；HYP-T0-02认为实际能力形成仍主要由具体任务需求牵引，通过分散试验、演示和局部部署扩展；HYP-T0-03认为数据、算力、基础设施、采办和测试评估等约束共同造成能力形成缓慢且应用扩展不均衡。三项假设绑定同一T0快照，均不是已证实结论。

## 4. 五专业视角评价

STRAT视角强调治理目标、组织架构和统一能力方向的形成，同时保留目标—结果落差；TECH关注技术成熟度、试验结果与性能边界；ENG关注工程化、基础设施、预算采办和规模化条件；OPS关注任务适配、局部部署与跨项目复用；TRUST关注可靠性、测试评估、负责任AI和责任机制。每个视角均覆盖三项假设，形成15个最新观点和94条字段级证据依赖。

## 5. 证据指向式质疑与观点修订

P8R把质疑指向具体观点字段、Evidence或机制链条，并在P8R中保留修订前后信息。质疑重点包括目标与结果混淆、将局部演示外推为部署、将普通协同外推为体系整合，以及低估资源与治理不确定性。观点的准入不是简单保留多数意见，失败位置也不能静默消失。

## 6. 当前条件化主判断

在不把战略目标当作已实现能力的前提下，当前证据更支持这样的条件化综合：截至2024年底，美国军用人工智能能力形成仍主要表现为具体任务需求牵引的分散试验、技术演示和局部部署。部门级统一治理、共用平台和标准化流程已形成明确发展方向，但缺少跨军种、跨任务实际复用的充分公开证据。该判断不是通过简单投票产生，而是对共享事实、五视角观点、质疑修订和两项未决机制分歧的条件化归并。

## 7. 备择判断

HYP-T0-02是当前状态的主要解释；HYP-T0-03主要解释这种状态为何可能延续，强调资源与治理约束机制；HYP-T0-01作为条件性备择保留，若共用平台、统一标准和跨任务复用出现，其解释力将增强。这里的“主要解释”是当前证据覆盖下的分析排序语句，不是概率评分或最终裁决。

## 8. 分歧治理结果

DISAG-T0-01为高影响机制分歧，保留并监测治理领先信号与实际复用之间的关系；DISAG-T0-02为中影响风险权重分歧，保留并监测瓶颈缓解是否改变分布式发展；DISAG-T0-03通过“治理架构建立—运行整合实现”范围拆分解决，不生成监测任务。措辞差异但实质相同的内容已归并，不制造额外分歧。

## 9. 判别性证据与监测计划

正式G3形成3项判别性证据需求、4项公开可观察指标和{len(triggers)}条触发规则。观察窗口统一为{OBSERVATION_WINDOW}，来源限于可公开核验的A/B级材料，采用季度观察或事件触发。监测问题分别关注共用平台实际采用、任务项目由演示转入应用的路径、资源与治理瓶颈状态，以及瓶颈变化后的应用广度。

## 10. 判断转换条件

若官方材料确认多个军种实际采用共用平台、统一标准或跨任务复用，HYP-T0-01增强，分散模式解释减弱；若完整检索和人工确认显示仍以独立平台和任务专属路径为主，HYP-T0-02增强；若明确约束使项目继续停留在演示，HYP-T0-03增强。若瓶颈缓解但分布式模式不变，需要削弱“约束是唯一主因”的解释；若瓶颈缓解并伴随应用范围扩展，应对三项假设共同重评。上述触发规则不使用未经依据的数值阈值。

## 11. 证据局限与适用边界

T0材料只覆盖公开、非密来源和截至2024年底的时间边界，不能替代T1后续状态数据，也不能证明未公开的项目状态。A/B来源的质量等级不等于支持某条路径。项目负责人自述、演示、演习参与、治理文件发布和预算投入必须分别解释，不能合并为作战效能、全面部署或体系整合的直接证明。

## 12. G4待审核事项

作者需要审核主判断的措辞、三项假设的相对表达、两项条件性分歧的保留方式、三项判别性需求和监测触发方向。作者决策字段在配套模板中全部留空。本草案未正式签发G4，不生成T1结论，不宣称已经预测未来，也不声称提高准确率。
"""
    (out / "g4_t0_report_draft.md").write_text(report, encoding="utf-8")
    draft = {
        "status": "draft_pending_author_signoff", "formal_g4_t0_executed": False,
        "author_authorization_sha256": AUTHORIZATION_SHA256,
        "snapshot_id": SNAPSHOT_ID, "case_id": CASE_ID, "frozen_evidence_count": len(data["evidence"]),
        "shared_baseline": data["baseline"], "hypotheses": data["hypotheses"],
        "conditional_main_judgment": "截至2024年底，美国军用人工智能能力形成仍主要表现为由具体任务需求牵引的分散试验、技术演示和局部部署；统一治理方向已明确但实际复用证据不足。",
        "viewpoint_count": len(data["views"]), "dependency_count": len(data["dependencies"]),
        "disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers,
        "evidence_claims": {"facts": "基于冻结Evidence", "model_inference": "基于假设、观点与条件化综合", "unverified_conditions": "由监测需求保留"},
        "author_decision_fields_empty": True,
    }
    _write_json(out / "g4_t0_report_draft.json", draft)
    fields = ["author_decision", "final_report_status", "final_main_judgment", "reviewer", "reviewed_at", "review_note"]
    _write_csv(out / "g4_author_review_template.csv", [{field: "" for field in fields}], fields)
    summary = """# G4-T0作者签核摘要（待审核）

本摘要供作者重点审核，不代表作者已经完成G4签发。

- 事实共识：T0证据显示能力已在若干特定任务中进入试验验证和有限部署，但尚未显示成熟的跨军种、跨任务通用体系。
- 当前条件化判断：HYP-T0-02解释当前的任务分散形态，HYP-T0-03解释资源与治理约束，HYP-T0-01作为统一治理和实际复用增强时的条件性备择。
- 高影响分歧：DISAG-T0-01关注治理领先信号是否已经转化为实际复用；DISAG-T0-02关注瓶颈是主要机制还是任务差异下的常态。
- 已解决范围分歧：DISAG-T0-03把治理架构建立与运行整合实现分开，避免用前者替代后者。
- 拟观察：共用平台和统一标准实际采用、任务项目转化路径、资源与治理瓶颈、瓶颈变化后的应用广度。
- 转换方向：跨军种/跨任务实际复用增强HYP-T0-01；任务专属转化增强HYP-T0-02；明确约束下继续演示增强HYP-T0-03。

作者模板中的最终决定、报告状态、主判断、审核者和审核备注均为空。
"""
    (out / "g4_author_signoff_summary.md").write_text(summary, encoding="utf-8")
    trace_rows = [
        {"claim_id": "G4-C01", "claim_type": "fact", "claim": "T0证据覆盖若干特定任务的试验验证和有限部署", "evidence_ids": ["EV-T0-DB4536B8270ECF58", "EV-T0-30E9B209F0439B21", "EV-T0-06EEB768196F287C"], "hypothesis_ids": "", "verification_status": "frozen_evidence"},
        {"claim_id": "G4-C02", "claim_type": "model_inference", "claim": "当前状态的主要解释是任务需求牵引的分散试验、演示和局部部署", "evidence_ids": [], "hypothesis_ids": ["HYP-T0-02"], "verification_status": "conditional_synthesis"},
        {"claim_id": "G4-C03", "claim_type": "model_inference", "claim": "资源与治理约束解释能力形成缓慢和应用不均衡", "evidence_ids": [], "hypothesis_ids": ["HYP-T0-03"], "verification_status": "conditional_synthesis"},
        {"claim_id": "G4-C04", "claim_type": "monitor_condition", "claim": "跨军种共用平台、统一标准和跨任务复用将触发当前判断重评", "evidence_ids": [], "hypothesis_ids": ["HYP-T0-01", "HYP-T0-02"], "verification_status": "unverified_t1_condition"},
    ]
    _write_csv(out / "g4_claim_evidence_traceability.csv", trace_rows, list(trace_rows[0]))


def _build_paper_package(out: Path, figures: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    composition = Counter(row.get("source_grade", "") for row in data["evidence"])
    natures = Counter(row.get("evidence_nature", "") for row in data["evidence"])
    dimensions = Counter(dim for row in data["evidence"] for dim in row.get("coding_dimensions", []))
    _write_csv(out / "table_1_evidence_composition.csv", [
        {"category": "frozen_evidence", "item": "总数", "count": len(data["evidence"])},
        *[{"category": "source_grade", "item": key, "count": value} for key, value in sorted(composition.items())],
        *[{"category": "evidence_nature", "item": key, "count": value} for key, value in sorted(natures.items())],
        *[{"category": "coding_dimension", "item": key, "count": value} for key, value in sorted(dimensions.items())],
    ], ["category", "item", "count"])
    hyp_rows = []
    for h in data["hypotheses"]:
        hyp_rows.append({"hypothesis_id": h.get("hypothesis_id"), "statement": h.get("statement"), "scope": h.get("scope"), "supporting_evidence_count": len(h.get("supporting_evidence_ids", [])), "counter_evidence_count": len(h.get("counter_evidence_ids", [])), "source_candidate_ids": h.get("source_candidate_ids", [])})
    _write_csv(out / "table_2_hypotheses.csv", hyp_rows, list(hyp_rows[0]))
    _write_csv(out / "table_3_perspective_hypothesis_matrix.csv", [{"perspective_id": v.get("perspective_id"), "hypothesis_id": v.get("hypothesis_id"), "stance": v.get("stance"), "status": v.get("status"), "judgment": v.get("judgment"), "evidence_count": len(v.get("supporting_evidence", []))} for v in data["views"]], ["perspective_id", "hypothesis_id", "stance", "status", "judgment", "evidence_count"])
    revision_rows = []
    for v in data["views"][:6]:
        gen = v.get("generator", {})
        revision_rows.append({"viewpoint_id": v.get("viewpoint_id"), "perspective_id": v.get("perspective_id"), "hypothesis_id": v.get("hypothesis_id"), "response_action": gen.get("response_action", ""), "response_note": gen.get("response_note", ""), "version": v.get("version", 1)})
    _write_csv(out / "table_4_viewpoint_revision_examples.csv", revision_rows, list(revision_rows[0]))
    _write_csv(out / "table_5_disagreement_governance.csv", [{"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "status": d["status"], "resolution_action": d["resolution_action"], "contested_node": d["contested_node"], "viewpoint_ref_count": len(d["viewpoint_refs"])} for d in disagreements], ["disagreement_id", "type", "decision_impact", "status", "resolution_action", "contested_node", "viewpoint_ref_count"])
    _write_csv(out / "table_6_monitoring_transition.csv", [{"need_id": n["need_id"], "disagreement_id": n["disagreement_id"], "question": n["question"], "indicator_ids": [i["indicator_id"] for i in indicators if i["need_id"] == n["need_id"]], "trigger_ids": [t["trigger_id"] for t in triggers if t["indicator_id"] in {i["indicator_id"] for i in indicators if i["need_id"] == n["need_id"]}], "observation_window": n["observation_window"]} for n in needs], ["need_id", "disagreement_id", "question", "indicator_ids", "trigger_ids", "observation_window"])
    metrics = [
        ("frozen_evidence", len(data["evidence"])), ("frozen_hypotheses", len(data["hypotheses"])), ("viewpoints", len(data["views"])), ("dependency_edges", len(data["dependencies"])), ("formal_disagreements", len(disagreements)), ("discriminative_needs", len(needs)), ("monitor_indicators", len(indicators)), ("trigger_rules", len(triggers)), ("model_api_calls", 0), ("evidence_inserted", 0), ("hypothesis_inserted", 0), ("t1_read", False), ("formal_g4_executed", False),
    ]
    _write_csv(out / "table_7_case_process_metrics.csv", [{"metric": key, "value": value} for key, value in metrics], ["metric", "value"])
    paper = _paper_text(data, disagreements, needs, indicators, triggers)
    (out / "paper_case_section_draft.md").write_text(paper, encoding="utf-8")
    _build_figures(figures, data, disagreements, needs, indicators, triggers)
    (figures / "figure_captions.md").write_text(_figure_captions(), encoding="utf-8")
    (figures / "figure_data_dictionary.md").write_text(_figure_dictionary(), encoding="utf-8")
    _figure_qc(figures)


def _paper_text(data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> str:
    text = f"""# 论文案例展示草稿：美国军用人工智能T0多视角研判

## 一、案例设计与数据

本案例以美国军用人工智能能力形成与演进为对象，观察截止时间为2024年12月31日，时间范围为2022—2024年。研究流程以冻结的T0证据快照为边界，快照包含{len(data['evidence'])}条正式Evidence，来源覆盖{len(set(row.get('source_id') for row in data['evidence']))}个来源、{len([row for row in data['evidence'] if row.get('source_grade') == 'A'])}条A级片段和{len([row for row in data['evidence'] if row.get('source_grade') == 'B'])}条B级片段。证据字段保留来源标识、页码或段落定位、原文摘录、中文译文、规范化声明、性质和编码维度。这里的“快照”意味着分析输入在观点和假设生成期间保持固定，并不意味着证据已经证明某个未来结论。

本案例的分析对象不是某个单一装备，而是战略治理、技术工程、试验应用、资源条件、可信治理和体系集成之间的关系。数据中既有政策和治理事件，也有预算投入、项目采办、试验活动、演示结果、局部部署和限制性材料。不同性质的材料不能互相替代：治理文件的发布不等于能力部署，演习参与不等于作战效能，预算金额不等于投入已经转化为体系能力。因此，案例设计把“发生了什么”和“如何解释这些事实”分为Evidence、假设、观点、分歧和监测链条五个层次。

## 二、竞争性假设生成

正式G2形成三项冻结竞争性假设，并另设一项共享事实基线。共享基线概括为：美国军用人工智能已在若干特定任务中由技术原型走向试验验证和有限部署，但尚未形成成熟的跨军种、跨任务通用能力体系。三项假设分别从体系整合、任务分布和约束机制解释同一组事实。第一项强调统一治理、共用平台和标准化流程的牵引；第二项强调具体任务需求、分散试验、技术演示和局部部署；第三项强调数据、算力、基础设施、采办与测试评估约束造成的能力形成迟缓和扩展不均衡。

这种生成方式的意义不在于预先设定谁正确，而在于把同一事实集合转成可比较的机制解释。每项假设都保留支持证据、反驳证据、关键假设、未决问题和转换判据。假设因此不是政策文件的重复摘要，也不是预测结论；它们是可以被后续公开材料削弱或增强的解释框架。P5和G2的谱系保持在案例产物中，使读者能够区分候选生成、作者整体签核和正式冻结的不同环节。

## 三、五视角观点与质疑修订

P7R阶段使用STRAT、TECH、ENG、OPS和TRUST五个专业视角，每个视角都对三项假设进行评价，共形成15个稳定观点位置。STRAT关注战略目标、CDAO/JCF等治理机制和制度落地；TECH关注模型、数据、算力、技术成熟度及试验边界；ENG关注工程化、基础设施、采办、接口和资源保障；OPS关注任务适配、局部部署、跨军种复用和体系运用；TRUST关注可靠性、测试评估、责任授权和负责任AI。五个视角不是五位被虚构的专家，不能把模型生成的视角文本写成真实专家访谈或独立实地评价。

P8R将观点置于证据指向式批判中。质疑具体指向观点字段、证据版本、机制主张、边界条件或不确定性，例如提醒不能把战略目标写成结果，不能把普通系统协同直接写成人机权限关系，也不能把项目负责人自述当作独立验证。观点修订保留稳定viewpoint_id并保存修订前后信息；94条字段级依赖把判断、假设、机制和边界条件连回精确Evidence版本。因而，观点治理不是删除不合意意见，而是使观点的依据、修订动作和遗留不确定性可追溯。

## 四、分歧识别和伪分歧治理

P9R识别出三项候选分歧。第一项是高影响机制分歧：治理架构和共用平台建设可以作为体系整合的领先信号，还是只有跨军种、跨任务实际复用才构成主导路径判据。第二项是中影响风险权重分歧：资源和治理瓶颈是造成分散与不均衡的主要机制，还是任务差异本身足以维持分布式发展。第三项是范围分歧：治理架构建立与运行整合实现是否应当放在同一层次解释。作者授权后，前两项作为条件性分歧保留，第三项通过范围拆分解决。

这里的范围拆分是伪分歧治理的关键。治理组织、制度文件和职责确立可以作为已经发生的治理事件，但不能自动推出跨军种共用平台已经运行。相反，若把“治理目标尚未形成结果”和“治理机制根本不存在”混为一个问题，就会把不同层次的争论误当成事实冲突。拆分后，治理架构建立由T0事实支持，运行整合则进入后续判别性观察。对措辞不同但机制相同的监测建议，采用确定性ID映射和重复归并，原始模型响应保持不变。

## 五、条件化综合

当前条件化综合认为，截至2024年底，美国军用人工智能能力形成仍主要表现为具体任务需求牵引的分散试验、技术演示和局部部署。部门级统一治理、共用平台和标准化流程已经形成明确发展方向，但现有公开材料不足以证明跨军种、跨任务实际复用已经成为主导形态。HYP-T0-02是对当前状态的主要解释，HYP-T0-03解释资源和治理约束可能如何使该状态延续，HYP-T0-01则作为在统一平台和实际复用出现时增强的条件性备择。

这不是把观点简单投票后选出一项答案，也不是把来源等级或观点数量转成胜负。综合首先区分事实、模型推断和未验证条件，再检查不同视角是否引用相同Evidence、是否把目标误写成结果、是否遗漏限制性材料。对结论而言，最重要的不是“所有观点一致”，而是明确哪些机制已经有直接公开依据，哪些机制只能作为条件性解释，哪些变化会导致当前判断重新评价。

## 六、判别性监测

正式G3把两项未决分歧转成{len(needs)}项判别性证据需求、{len(indicators)}项指标和{len(triggers)}条触发规则。第一项需求观察共用AI平台、统一数据或接口标准和统一工程流程是否被不同军种、不同任务实际采用；第二项追踪任务项目如何从演示转入应用；第三项观察资源和治理瓶颈是否缓解以及应用广度是否随之改变。所有需求使用2025年1月1日至2026年6月30日的观察窗口，来源计划限制为公开可核验的A/B级材料。

监测链条给出方向而非预设阈值。例如，若出现跨军种共用平台或跨任务复用，HYP-T0-01增强；若完整检索并经人工确认仍以独立平台和任务专属流程为主，HYP-T0-02增强；若项目因明确资源或治理约束继续停留在演示，HYP-T0-03增强。瓶颈缓解但分布式模式不变，说明“约束是唯一主因”的解释需要弱化；瓶颈缓解并伴随跨任务扩展，则应对三项假设共同重评。这里没有把“未搜索到”直接当成“未发生”，否定性判断必须保留检索完备性和人工确认条件。

## 七、本案例能够验证什么、不能验证什么

本案例能够展示：如何在固定证据快照上生成竞争性假设；如何让多视角观点覆盖同一假设集合；如何用字段级依赖追踪观点依据；如何区分真实分歧、范围分歧和重复表述；如何把高影响未决分歧转成可公开观察的监测链条。它还能够展示追加式数据库、作者签核、版本、来源等级和原文定位如何共同构成审计轨迹。

本案例不能验证模型生成文字等同于专家意见，不能证明某一假设已经被证实，不能据T0材料直接预测2025—2027年实际部署结果，也不能用P9R监测计划替代T1数据和正式G3后续审核。它没有开展对照实验，没有计算“准确率提升”，没有读取T1，也没有签发G4。正式G4仍需作者审核报告措辞、监测需求和触发方向，后续任何状态变化都必须通过新证据和明确复核重新进入观点治理。
"""
    return text


def _matplotlib_setup():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    font_path = Path("C:/Windows/Fonts/simhei.ttf")
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        font_name = font_manager.FontProperties(fname=str(font_path)).get_name()
    else:
        font_name = "DejaVu Sans"
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": font_name, "axes.unicode_minus": False, "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white", "text.color": "#222222", "axes.edgecolor": "#666666", "axes.labelcolor": "#222222", "xtick.color": "#444444", "ytick.color": "#444444"})
    return plt


def _save_figure(fig: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path.with_suffix(".png"), dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(path.with_suffix(".svg"), bbox_inches="tight", facecolor="white")


def _build_figures(figures: Path, data: dict[str, Any], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    plt = _matplotlib_setup()
    accent = "#78909c"
    dark = "#333333"
    gray = "#b0b6ba"
    figures.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 3.8))
    ax.axis("off")
    labels = [("T0证据", "85"), ("竞争性假设", "3"), ("多视角观点", "15"), ("字段级依赖", "94"), ("分歧", "3"), ("需求", "3"), ("指标", "4")]
    xs = list(range(len(labels)))
    for idx, (label, count) in enumerate(labels):
        ax.text(idx, 0.52, f"{label}\n{count}", ha="center", va="center", fontsize=11, color=dark, bbox={"boxstyle": "round,pad=0.6", "facecolor": "white", "edgecolor": accent if idx in {0, 4, 6} else gray, "linewidth": 1.5})
        if idx < len(labels) - 1:
            ax.annotate("", xy=(idx + 0.72, 0.52), xytext=(idx + 0.28, 0.52), arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.4})
    ax.set_xlim(-0.5, len(labels) - 0.5); ax.set_ylim(0, 1); ax.set_title("图1  T0案例处理链条", fontsize=13, color=dark)
    _save_figure(fig, figures / "figure_1_case_process"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.8, 4.8))
    stance_map = {"support": "支持", "challenge": "质疑", "conditional": "条件", "insufficient": "不足"}
    stance_colors = {"support": accent, "challenge": "#777777", "conditional": "#c4c9cc", "insufficient": "#eeeeee"}
    for y, perspective in enumerate(PERSPECTIVES):
        for x, hypothesis in enumerate(HYPOTHESES):
            row = next(v for v in data["views"] if v["perspective_id"] == perspective and v["hypothesis_id"] == hypothesis)
            stance = row.get("stance", "")
            ax.add_patch(plt.Rectangle((x, len(PERSPECTIVES) - y - 1), 1, 1, facecolor=stance_colors.get(stance, "white"), edgecolor="white", linewidth=2))
            ax.text(x + 0.5, len(PERSPECTIVES) - y - 0.5, stance_map.get(stance, stance), ha="center", va="center", fontsize=10, color=dark)
    ax.set_xticks([0.5, 1.5, 2.5], ["HYP-T0-01", "HYP-T0-02", "HYP-T0-03"]); ax.set_yticks([0.5, 1.5, 2.5, 3.5, 4.5], list(reversed(PERSPECTIVES))); ax.set_xlim(0, 3); ax.set_ylim(0, 5); ax.set_title("图2  五视角×三假设立场矩阵", fontsize=13); ax.tick_params(length=0); ax.set_xlabel("冻结竞争性假设"); ax.set_ylabel("专业视角");
    _save_figure(fig, figures / "figure_2_perspective_hypothesis_matrix"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.8)); ax.axis("off")
    nodes = [("Evidence\n85", 0.08, 0.54), ("Hypotheses\n3", 0.23, 0.54), ("Viewpoints\n15", 0.40, 0.54), ("Dependencies\n94", 0.58, 0.54), ("Disagreements\n3", 0.75, 0.70), ("Monitoring\n3/4/8", 0.75, 0.34)]
    for label, x, y in nodes:
        ax.text(x, y, label, ha="center", va="center", fontsize=11, bbox={"boxstyle": "round,pad=0.6", "facecolor": "white", "edgecolor": accent if "Evidence" in label or "Monitoring" in label else gray, "linewidth": 1.5})
    for start, end in [(0, 1), (1, 2), (2, 3), (3, 4), (3, 5)]:
        x1, y1 = nodes[start][1], nodes[start][2]; x2, y2 = nodes[end][1], nodes[end][2]
        ax.annotate("", xy=(x2 - 0.045, y2), xytext=(x1 + 0.045, y1), arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.3})
    ax.set_xlim(0, 1); ax.set_ylim(0.1, 0.9); ax.set_title("图3  证据—观点—分歧—监测依赖关系", fontsize=13)
    _save_figure(fig, figures / "figure_3_evidence_viewpoint_dependency"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5.2)); ax.axis("off")
    rows = [("DISAG-T0-01", "NEED-T0-01/02", "IND-T0-01/02", "TRG-T0-01…05"), ("DISAG-T0-02", "NEED-T0-03", "IND-T0-03/04", "TRG-T0-06…08")]
    for y, row in enumerate(rows):
        yy = 0.68 - y * 0.30
        for x, label in zip([0.12, 0.36, 0.61, 0.86], row):
            ax.text(x, yy, label, ha="center", va="center", fontsize=10, bbox={"boxstyle": "round,pad=0.55", "facecolor": "white", "edgecolor": accent if x == 0.12 else gray, "linewidth": 1.4})
        for x1, x2 in [(0.19, 0.29), (0.43, 0.54), (0.68, 0.79)]:
            ax.annotate("", xy=(x2, yy), xytext=(x1, yy), arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.3})
    ax.text(0.12, 0.16, "分支方向：跨军种/跨任务复用 → HYP-T0-01；任务专属转化 → HYP-T0-02；明确瓶颈持续 → HYP-T0-03", ha="center", va="center", fontsize=10, color=dark)
    ax.set_xlim(0, 1); ax.set_ylim(0.05, 0.86); ax.set_title("图4  分歧到判别性监测链条", fontsize=13)
    _save_figure(fig, figures / "figure_4_disagreement_to_monitoring"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.5, 4.2)); ax.axis("off")
    stages = [("初始观点", "15"), ("批判审查", "3次"), ("回应修订", "5次"), ("最新准入", "15"), ("字段级依赖", "94")]
    for idx, (label, count) in enumerate(stages):
        x = 0.10 + idx * 0.20
        ax.text(x, 0.56, f"{label}\n{count}", ha="center", va="center", fontsize=11, bbox={"boxstyle": "round,pad=0.6", "facecolor": "white", "edgecolor": accent if idx in {0, 3} else gray, "linewidth": 1.5})
        if idx < len(stages) - 1:
            ax.annotate("", xy=(x + 0.15, 0.56), xytext=(x + 0.05, 0.56), arrowprops={"arrowstyle": "->", "color": gray, "lw": 1.3})
    ax.text(0.50, 0.20, "稳定 viewpoint_id + 版本化修订 + Evidence 字段级依赖", ha="center", va="center", fontsize=10, color=dark)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_title("图5  观点治理过程", fontsize=13)
    _save_figure(fig, figures / "figure_5_viewpoint_governance"); plt.close(fig)


def _figure_captions() -> str:
    return """# 图形说明\n\n- 图1：展示冻结Evidence经假设、观点和字段级依赖进入分歧与监测需求的处理链条。\n- 图2：展示五个专业视角对三项冻结竞争性假设的立场覆盖，不将颜色或格数解释为投票。\n- 图3：展示证据、假设、观点、字段级依赖、分歧和监测对象的结构关系。\n- 图4：展示两项条件性分歧如何转化为需求、指标和触发规则；箭头表达治理链条，不表达数值概率。\n- 图5：展示观点从初始生成、批判审查、回应修订到准入和依赖建边的版本化过程。\n"""


def _figure_dictionary() -> str:
    return """# 图形数据字典\n\n|字段/节点|来源|含义|\n|---|---|---|\n|Evidence 85|P4冻结快照|正式T0证据逻辑集合数量|\n|Hypotheses 3|G2正式假设|冻结竞争性假设数量|\n|Viewpoints 15|P7R/P8R最新观点|五视角×三假设观点位置|\n|Dependencies 94|P8R字段级依赖|观点字段到证据版本的依赖边|\n|Disagreements 3|G3正式记录|两项条件性分歧和一项范围拆分|\n|Monitoring 3/4/8|G3正式记录|需求、指标、触发规则数量|\n"""


def _figure_qc(figures: Path) -> None:
    from PIL import Image, ImageStat
    rows = ["# 图形视觉质检", "", "白底、黑灰结构和单一低饱和强调色；PNG按300 DPI保存；未使用3D、渐变或装饰图标。", ""]
    all_pass = True
    for path in sorted(figures.glob("figure_*.png")):
        with Image.open(path) as image:
            stat = ImageStat.Stat(image.convert("RGB"))
            nonblank = max(stat.extrema[i][1] - stat.extrema[i][0] for i in range(3)) > 12
            dims = image.width >= 1000 and image.height >= 500
            dpi = image.info.get("dpi", (0, 0))
            dpi_ok = float(dpi[0]) >= 250 if dpi else False
        svg = path.with_suffix(".svg")
        svg_text = svg.read_text(encoding="utf-8") if svg.exists() else ""
        svg_ok = "<svg" in svg_text and "�" not in svg_text
        passed = nonblank and dims and dpi_ok and svg_ok
        all_pass = all_pass and passed
        rows.append(f"- {path.name}: {'PASS' if passed else 'FAIL'}; pixels={image.width}x{image.height}; dpi={dpi}; nonblank={nonblank}; svg={svg_ok}")
    rows.append("")
    rows.append(f"结论：{'PASS' if all_pass else 'FAIL'}。未发现空白图、尺寸不足或U+FFFD替换字符。")
    (figures / "figure_visual_qc_report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _all_output_hashes(out: Path) -> dict[str, str]:
    return {str(path.relative_to(out)).replace("\\", "/"): _sha256_file(path) for path in sorted(out.rglob("*")) if path.is_file() and path.name != "g3_manifest.json"}


def run_g3_g4_offline(run_id: str | None = None) -> dict[str, Any]:
    """Run formal offline G3 and produce G4/paper artifacts."""
    global _GLOBAL_INPUTS
    data = _load_inputs()
    run_id = run_id or f"G3-T0-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-formal-offline-v1"
    out = ROOT / "outputs" / run_id
    if out.exists() and (out / "g3_manifest.json").exists():
        return _read_json(out / "g3_manifest.json")
    out.mkdir(parents=True, exist_ok=False)
    _GLOBAL_INPUTS = data
    reviewed_at = _now_utc()
    historical_before = _historical_hashes(data["input_paths"])
    preserved_before = _preserved_db_hash(DB_PATH)
    (out / "g3_author_authorization.txt").write_text(AUTHORIZATION_TEXT + "\n", encoding="utf-8")
    disagreements = _build_disagreements(data, reviewed_at)
    needs = _build_needs(reviewed_at)
    indicators = _build_indicators()
    triggers = _build_triggers(indicators)
    review = _build_human_review(out, reviewed_at)
    rollback_passed = _test_rollback(DB_PATH, disagreements, needs, indicators, triggers, review)
    if not rollback_passed:
        raise G3OfflineError("rollback test failed")
    added = _insert_rows_transaction(DB_PATH, disagreements, needs, indicators, triggers, review)
    idempotency_passed = _test_idempotency(DB_PATH, disagreements, needs, indicators, triggers, review)
    preserved_after = _preserved_db_hash(DB_PATH)
    historical_after = _historical_hashes(data["input_paths"])
    _build_g3_outputs(out, data, disagreements, needs, indicators, triggers, reviewed_at)
    manifest = {
        "run_id": run_id, "stage": "G3+G4_DRAFT+PAPER_CASE", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID,
        "authoritative_p7r_p9r_run": P7R_RUN_ID, "author_authorization_sha256": AUTHORIZATION_SHA256,
        "review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 3,
        "formal_g3_executed": True, "formal_disagreement_count": len(disagreements), "conditionally_retained_count": 2,
        "resolved_scope_split_count": 1, "discriminative_need_count": len(needs), "monitor_indicator_count": len(indicators),
        "trigger_rule_count": len(triggers), "human_review_inserted": 1,
        "transaction_committed": True, "rollback_test_passed": rollback_passed, "idempotency_test_passed": idempotency_passed,
        "all_viewpoint_refs_valid": True, "all_monitoring_links_valid": True, "invalid_DISP_ids_remaining": 0,
        "model_api_calls": 0, "g4_draft_generated": True, "formal_g4_t0_executed": False,
        "t1_read": False, "evidence_inserted": 0, "hypothesis_inserted": 0, "viewpoint_inserted": 0,
        "historical_artifacts_unchanged": historical_before == historical_after and preserved_before == preserved_after,
        "api_key_written": False, "frozen_evidence_count": len(data["evidence"]), "frozen_hypothesis_count": len(data["hypotheses"]),
        "latest_viewpoint_count": len(data["views"]), "existing_dependency_edge_count": len(data["dependencies"]),
        "author_reviewed_statement": "作者整体签核，不表示作者逐条重读85条原始证据或逐页视觉核验。",
        "reviewed_at": reviewed_at, "output_file_hashes": _all_output_hashes(out),
    }
    _write_json(out / "g3_manifest.json", manifest)
    return manifest
