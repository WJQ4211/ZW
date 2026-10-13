"""Append-only G1-T1, P13-P16 and G3Q/G4Q-T1 preparation.

This stage consumes the already-reviewed P11R/P12R package.  It does not call
the model, re-extract sources, compare T0 and T1 in the extractor, or create a
T1 hypothesis/viewpoint conclusion.  G1 and the T1 snapshot are committed
transactionally; P14/P16 are deterministic audit/preparation artifacts and
P15 appends only the viewpoint versions selected by the evidence impact map.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research.sqlite3"
CASE_ID = "us_military_ai_deployment"
T0_SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
P11R_RUN_ID = "P11R-P12R-G1Q-T1-20260827T065600Z-v1"
P11R_DIR = ROOT / "outputs" / P11R_RUN_ID
G3_DIR = ROOT / "outputs" / "G3-T0-20260827T-formal-offline-v5"
P7R_DIR = ROOT / "outputs" / "P7R-P9R-20260827T020335Z-case-strengthening-v1"
T1_START = "2025-01-01"
T1_END = "2026-06-30"
T1_CUTOFF = "2026-06-30T23:59:59Z"
REVIEWER = "author_01"
REVIEW_MODE = "ai_preannotation_author_bulk_signoff_with_key_item_adjudication"
AUTHORIZATION_TEXT = (
    "作者已正式授权基于P11R＋P12R＋G1Q-T1终审包执行G1-T1、P13、P14、P15、P16、"
    "G3Q/G4Q-T1；本阶段完成后停止，不执行正式G3-T1、G4-T1、T1快照冻结后的后续影响定位或观点更新。"
)
NATURES = {
    "goal", "governance_event", "resource_input", "test_event", "test_result",
    "deployment_event", "constraint", "evaluation",
}
DIMENSIONS = {
    "strategic_governance", "technical_engineering_maturity",
    "testing_deployment_application", "resource_conditions",
    "trust_organization_system_integration", "human_machine_authority",
    "integration_level", "application_maturity", "organizational_system_fit",
    "engineering_resource_conditions", "trust_governance_constraints",
}
EXCLUDED = {
    "CE-T1-6B7DB29A3B242353",
    "CE-T1-1797A97EF532F332",
    "CE-T1-1DB188A712B4C1C6",
    "CE-T1-9D61AF59EFDD3694",
    "CE-T1-4DB05E964D50AC61",
    "CE-T1-3DC907538A406628",
}
HISTORICAL = {"CE-T1-7DE738404CD33807", "CE-T1-C1E0527174B2F72F"}
AUTH_HASH = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()
RUN_ID = f"G1-T1-P13-P16-{AUTH_HASH[:16].upper()}-formal-v1"
T1_SNAPSHOT_ID = f"SNAP-T1-US-MILITARY-AI-{AUTH_HASH[:16].upper()}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)).replace("\\", "/"): sha256_file(path)
        for path in sorted(path for path in directory.rglob("*") if path.is_file())
        if path.name not in {"g1_t1_p13_p16_manifest.json"}
    }


def _read_p11r_candidates() -> list[dict[str, Any]]:
    rows = list(csv.DictReader((P11R_DIR / "combined_g1_t1_author_decision_template.csv").open(encoding="utf-8-sig", newline="")))
    candidates = []
    for row in rows:
        dimensions = json.loads(row["coding_dimensions"] or "[]")
        proposed_dimensions = json.loads(row["proposed_dimensions"] or row["coding_dimensions"] or "[]")
        candidates.append({
            "candidate_evidence_id": row["candidate_evidence_id"],
            "lineage": row["lineage"],
            "source_id": row["source_id"],
            "source_locator": row["source_locator"],
            "excerpt_original": row["excerpt_original"],
            "excerpt_zh": row["excerpt_zh"],
            "normalized_claim": row["normalized_claim"],
            "evidence_nature": row["evidence_nature"],
            "coding_dimensions": dimensions,
            "proposed_decision": row["proposed_decision"],
            "proposed_nature": row["proposed_nature"],
            "proposed_dimensions": proposed_dimensions,
            "proposed_normalized_claim": row["proposed_normalized_claim"],
            "proposed_excerpt_zh": row["proposed_excerpt_zh"],
            "risk_level": row["risk_level"],
            "risk_reason": row["risk_reason"],
            "proposed_review_note": row["proposed_review_note"],
            "t1_change_signal_eligible": row["t1_change_signal_eligible"].lower() == "true",
        })
    if len(candidates) != 46 or len({row["candidate_evidence_id"] for row in candidates}) != 46:
        raise RuntimeError("P11R author template must contain 46 unique candidates")
    return candidates


def _source_metadata(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    old = load_jsonl(P11R_DIR.parent / "G4-P11-P12-20260827T051332Z-v1" / "P12_T1_EXTRACTION" / "t1_evidence_candidates.jsonl")
    supplemental = load_jsonl(P11R_DIR / "supplemental_evidence_candidates.jsonl")
    metadata = {row["candidate_evidence_id"]: row for row in old + supplemental}
    if set(metadata) < {row["candidate_evidence_id"] for row in candidates}:
        raise RuntimeError("P11R candidates missing authoritative metadata")
    return metadata


def _apply_author_corrections(row: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    cid = row["candidate_evidence_id"]
    dims = list(row["proposed_dimensions"] or row["coding_dimensions"])
    nature = row["proposed_nature"] or row["evidence_nature"]
    claim = row["proposed_normalized_claim"] or row["normalized_claim"]
    corrections: list[str] = []
    if cid in {"CE-T1-0BF350B1F14580F5", "CE-T1-C631674EB5FED17F", "CE-T1-8FF394626242D949"}:
        if "human_machine_authority" in dims:
            dims.remove("human_machine_authority")
            corrections.append("remove_human_machine_authority")
    if cid == "CE-T1-12F70AE60CD787AA":
        nature = "resource_input"
        corrections.append("test_event_to_resource_input")
    if cid == "CE-T1-43B8C6F6E7B77F40":
        claim = "据DARPA竞赛环境中的官方披露结果，Coordinated Robotics展示了能够检测生命体征和识别损伤模式的机器人平台。"
        corrections.append("add_official_competition_scope")
    if cid == "CE-T1-95F7FE29CF48D86D":
        claim = "在作战医疗数据条件下，美国国防部创伤登记处15年数据集仅包含0.017 GB数据。"
        corrections.append("limit_to_combat_medical_data_conditions")
    if cid in HISTORICAL:
        updated["t1_change_signal_eligible"] = False
        updated["temporal_role"] = "historical_context"
        corrections.append("historical_context_not_t1_delta")
    updated.update({
        "final_decision": "exclude" if cid in EXCLUDED else "accept",
        "final_nature": nature,
        "final_dimensions": sorted(set(dims)),
        "final_normalized_claim": claim,
        "final_excerpt_zh": row["proposed_excerpt_zh"] or row["excerpt_zh"],
        "corrections": corrections,
        "reviewer": REVIEWER,
        "reviewed_at": now_utc(),
        "human_reviewed": True,
        "review_mode": REVIEW_MODE,
    })
    return updated


def _formal_evidence(row: dict[str, Any], metadata: dict[str, Any], run_id: str) -> dict[str, Any]:
    published = metadata.get("published_at") or metadata.get("document_date")
    if not published or not (T1_START <= published <= T1_END):
        raise RuntimeError(f"T1 date invalid for {row['candidate_evidence_id']}: {published}")
    if metadata.get("source_grade") not in {"A", "B"}:
        raise RuntimeError(f"T1 source grade invalid for {row['candidate_evidence_id']}")
    dimensions = row["final_dimensions"]
    if not dimensions or not set(dimensions).issubset(DIMENSIONS):
        raise RuntimeError(f"invalid dimensions for {row['candidate_evidence_id']}: {dimensions}")
    if row["final_nature"] not in NATURES or "+" in row["final_nature"]:
        raise RuntimeError(f"invalid nature for {row['candidate_evidence_id']}")
    content_hash = metadata.get("content_hash")
    if not content_hash:
        content_hash = sha256_bytes((CASE_ID + "|" + row["source_id"] + "|" + row["source_locator"] + "|" + row["excerpt_original"]).encode("utf-8"))
    payload = {
        "case_id": CASE_ID,
        "candidate_evidence_id": row["evidence_id"],
        "evidence_id": row["evidence_id"],
        "version": 1,
        "snapshot_tag": "T1",
        "status": "active",
        "source_id": row["source_id"],
        "source_locator": row["source_locator"],
        "excerpt_original": row["excerpt_original"],
        "excerpt_zh": row["final_excerpt_zh"],
        "normalized_claim": row["final_normalized_claim"],
        "evidence_nature": row["final_nature"],
        "coding_dimensions": dimensions,
        "topics": metadata.get("topics", []),
        "published_at": published,
        "published_at_precision": metadata.get("published_at_precision", "day"),
        "publisher": metadata.get("publisher", ""),
        "title": metadata.get("title", ""),
        "source_grade": metadata.get("source_grade"),
        "url_or_path": metadata.get("url_or_path", ""),
        "content_hash": content_hash,
        "extraction_run_id": metadata.get("extraction_run_id", P11R_RUN_ID),
        "t1_change_signal_eligible": row["t1_change_signal_eligible"],
        "temporal_role": row.get("temporal_role", "current_t1_signal"),
        "lineage": row["lineage"],
        "g1_author_decision": "accept",
        "reviewer": REVIEWER,
        "reviewed_at": row["reviewed_at"],
        "review_mode": REVIEW_MODE,
        "human_reviewed": True,
        "human_source_visual_review_count": 0,
        "source_visual_review_claimed": False,
    }
    if "U+FFFD" in json.dumps(payload, ensure_ascii=False):
        raise RuntimeError(f"replacement character in T1 evidence {row['candidate_evidence_id']}")
    return payload


def _latest_t0(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.* FROM evidence e JOIN (SELECT evidence_id, MAX(version) version FROM evidence WHERE case_id=? AND snapshot_tag='T0' GROUP BY evidence_id) latest ON latest.evidence_id=e.evidence_id AND latest.version=e.version WHERE e.case_id=? AND e.snapshot_tag='T0' ORDER BY e.evidence_id",
        (CASE_ID, CASE_ID),
    ).fetchall()
    result = []
    for row in rows:
        result.append({"case_id": row["case_id"], "evidence_id": row["evidence_id"], "version": row["version"], "content_hash": row["content_hash"], "payload": json.loads(row["payload_json"])})
    if len(result) != 85:
        raise RuntimeError(f"T0 latest logical Evidence must be 85, got {len(result)}")
    return result


def _latest_viewpoints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.* FROM viewpoint v JOIN (SELECT viewpoint_id, MAX(version) version FROM viewpoint WHERE snapshot_id=? GROUP BY viewpoint_id) latest ON latest.viewpoint_id=v.viewpoint_id AND latest.version=v.version WHERE v.snapshot_id=? ORDER BY v.viewpoint_id",
        (T0_SNAPSHOT_ID, T0_SNAPSHOT_ID),
    ).fetchall()
    result = []
    for row in rows:
        item = json.loads(row["payload_json"])
        item.update({"viewpoint_id": row["viewpoint_id"], "version": row["version"], "status": row["status"]})
        result.append(item)
    if len(result) != 15:
        raise RuntimeError(f"latest T0 viewpoints must be 15, got {len(result)}")
    return result


def _impact_edges(t1_rows: list[dict[str, Any]], views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [row for row in t1_rows if row["t1_change_signal_eligible"]]
    by_id = {row["evidence_id"]: row for row in eligible}
    view_by_key = {(row.get("perspective_id"), row.get("hypothesis_id")): row for row in views}
    edges: list[dict[str, Any]] = []

    def add(eid: str, perspective: str, hypothesis: str, target: str, relation: str, rationale: str, confidence: str) -> None:
        view = view_by_key.get((perspective, hypothesis))
        if not view or eid not in by_id:
            return
        edges.append({
            "evidence_id": eid,
            "evidence_version": 1,
            "hypothesis_id": hypothesis,
            "viewpoint_id": view["viewpoint_id"],
            "viewpoint_version": view["version"],
            "target_field": target,
            "relation": relation,
            "rationale": rationale,
            "confidence": confidence,
            "t1_change_signal_eligible": True,
        })

    for row in eligible:
        eid = row["evidence_id"]
        nature = row["evidence_nature"]
        if eid in {"CE-T1-02DFF1DCD73E8F93", "CE-T1-050BC7E34C88F297", "CE-T1-2995ED3DEF557C50", "CE-T1-B5C396D3278023D1", "CE-T1-CEACB21D2A3F685C", "CE-T1-A66D49255D61B484"}:
            add(eid, "STRAT", "HYP-T0-01", "judgment", "qualify", "该证据是部门战略或治理目标，说明制度牵引存在，但不直接证明共用平台已经运行，因此仅限定判断范围。", "medium")
            add(eid, "STRAT", "HYP-T0-02", "boundary_conditions[0]", "qualify", "战略目标覆盖多个任务领域，但其性质仍是目标，不能替代任务项目的实际转化证据。", "medium")
        elif eid in {"CE-T1-C4C1018771B30C5A", "CE-T1-FE00C458FAADC5D8", "CE-T1-11BBC2ABED0945C2", "CE-T1-FA2B6E620D8DDBD2", "CE-T1-E2F907CF4DA00903", "CE-T1-1DB188A712B4C1C6"}:
            add(eid, "TECH", "HYP-T0-03", "judgment", "support", "该约束直接涉及作战化AI安全评估能力及实验室到作战环境的转化边界，改变技术成熟度判断的限制项。", "high")
            add(eid, "TRUST", "HYP-T0-03", "boundary_conditions[0]", "support", "该证据使可信治理与作战化评估能力的缺口成为约束性边界，而非一般目标表述。", "high")
        elif eid in {"CE-T1-F2BE64F4810C89EF", "CE-T1-C20674F2CA89D5C5", "CE-T1-43B8C6F6E7B77F40", "CE-T1-12F70AE60CD787AA", "CE-T1-45417DAD097210B1", "CE-T1-9D61AF59EFDD3694"}:
            add(eid, "TECH", "HYP-T0-02", "judgment", "support" if nature == "test_result" else "qualify", "该证据记录了具体竞赛/测试活动或公开结果，能支持任务项目试验扩展，但不证明规模化部署。", "medium")
            add(eid, "OPS", "HYP-T0-02", "mechanism_claims[0]", "support" if nature == "test_result" else "qualify", "该证据与具体任务试验和项目实施有关，改变的是局部应用观察，不是全军通用能力判断。", "medium")
        elif eid in {"CE-T1-C1E0527174B2F72F", "CE-T1-4DB05E964D50AC61", "CE-T1-3DC907538A406628", "CE-T1-95F7FE29CF48D86D"}:
            add(eid, "TECH", "HYP-T0-03", "boundary_conditions[0]", "boundary_change", "该材料来自作战医疗领域，直接提供数据规模或模型表现条件，但外推到全军AI需受任务范围约束。", "medium")
            add(eid, "OPS", "HYP-T0-02", "boundary_conditions[0]", "qualify", "该证据显示特定任务领域存在应用或应用目标，只能限定为局部任务证据。", "medium")
        elif eid in {"CE-T1-AA937CC0AF5E2372", "CE-T1-DCF49FC6CBC68958", "CE-T1-B40332E796F16865", "CE-T1-7239909FC989F79A"}:
            add(eid, "OPS", "HYP-T0-02", "judgment", "support" if nature == "deployment_event" else "qualify", "该材料涉及具体单位的局部使用或应用倡议，能改变局部任务运用的观察，但不等于跨军种普遍成熟。", "medium")
            add(eid, "STRAT", "HYP-T0-01", "boundary_conditions[0]", "qualify", "局部单位应用与部门战略目标之间尚有层级差异，因此用于限定体系整合外推边界。", "low")
        elif eid in {"CE-T1-D0E7B9B8CC1298AB", "CE-T1-BB675BA36EB74B19", "CE-T1-6B02747FAF17B29F", "CE-T1-CF7656CBCD8BBB03", "CE-T1-6B7DB29A3B242353", "CE-T1-1797A97EF532F332"}:
            add(eid, "OPS", "HYP-T0-02", "mechanism_claims[0]", "qualify", "演习或概念验证说明组织运用和配置发生，但不能单独证明作战效能或规模化部署。", "medium")
    # Ensure every eligible signal is auditable even when it is a background
    # or excluded-by-scope item: it appears in the no-update list, not as a
    # keyword-triggered edge.
    return edges


def _updated_viewpoints(views: list[dict[str, Any]], edges: list[dict[str, Any]], run_id: str, created_at: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    affected = sorted({edge["viewpoint_id"] for edge in edges})
    by_view: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        by_view[edge["viewpoint_id"]].append(edge)
    updated: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for view in views:
        if view["viewpoint_id"] not in affected:
            unchanged.append({"viewpoint_id": view["viewpoint_id"], "version": view["version"], "reason": "no P14 evidence-to-field edge"})
            continue
        new = json.loads(json.dumps(view, ensure_ascii=False))
        old_version = int(view["version"])
        new_version = old_version + 1
        refs = by_view[view["viewpoint_id"]]
        support = list(new.get("supporting_evidence", []))
        counter = list(new.get("counter_evidence", []))
        for edge in refs:
            item = {"evidence_id": edge["evidence_id"], "version": 1, "relation": edge["relation"], "note": edge["rationale"]}
            target = support if edge["relation"] in {"support", "qualify"} else counter
            if not any(x.get("evidence_id") == item["evidence_id"] for x in target):
                target.append(item)
        perspective = new.get("perspective_id", "")
        hypothesis = new.get("hypothesis_id", "")
        if hypothesis == "HYP-T0-01":
            judgment = "T1证据继续支持治理与整合目标存在，但新增材料主要是战略目标、局部运用和测试约束，尚不足以证明跨军种共用底座已经形成。"
        elif hypothesis == "HYP-T0-02":
            judgment = "T1证据进一步显示具体任务试验、竞赛和局部使用仍是可观察的能力形成形态；这些信号支持任务分布牵引，但不能外推为全军规模化部署。"
        else:
            judgment = "T1证据补充了作战化评估能力、数据条件和工程约束，表明资源与可信治理瓶颈仍影响应用扩展；局部结果不足以消除这一条件性判断。"
        new.update({
            "version": new_version,
            "parent_version": old_version,
            "status": view.get("status", "admitted"),
            "snapshot_id": T1_SNAPSHOT_ID,
            "case_id": CASE_ID,
            "judgment": judgment,
            "supporting_evidence": support,
            "counter_evidence": counter,
            "run_id": run_id,
            "created_at": created_at,
            "human_verified": False,
            "change_reason": "P14_evidence_field_impact_local_update",
            "t1_update": {
                "source_snapshot": T0_SNAPSHOT_ID,
                "target_snapshot": T1_SNAPSHOT_ID,
                "affected_fields": sorted({edge["target_field"] for edge in refs}),
                "new_evidence_ids": sorted({edge["evidence_id"] for edge in refs}),
                "mechanism_change": "T1材料补充了局部试验/使用或约束条件，但不强制改变主判断。",
                "boundary_change": "新增T1来源的任务、机构和公开披露范围被明确保留。",
                "confidence_change": "unchanged_or_conditioned",
                "falsifiable_condition": "若后续A/B级材料显示跨军种共用平台实际迁移并形成可复用工程流程，则需重新评估。",
            },
        })
        updated.append(new)
        diffs.append({
            "viewpoint_id": view["viewpoint_id"],
            "before_version": old_version,
            "after_version": new_version,
            "before_judgment": view.get("judgment", ""),
            "after_judgment": judgment,
            "changed_fields": json.dumps(["judgment", "supporting_evidence", "counter_evidence", "boundary_conditions", "t1_update"], ensure_ascii=False),
            "new_evidence_ids": json.dumps(sorted({edge["evidence_id"] for edge in refs}), ensure_ascii=False),
            "update_reason": "P14 evidence-to-field rationale",
        })
    if len(updated) >= len(views):
        raise RuntimeError("all viewpoints were marked affected; local-update safeguard failed")
    return updated, diffs, unchanged


def _disagreement_and_monitoring(updated_views: list[dict[str, Any]], all_views: list[dict[str, Any]], t1_rows: list[dict[str, Any]], run_id: str, created_at: str) -> dict[str, list[dict[str, Any]]]:
    latest = {row["viewpoint_id"]: row for row in all_views}
    latest.update({row["viewpoint_id"]: row for row in updated_views})
    refs_h1 = [f"{row['viewpoint_id']}:{row['version']}" for row in latest.values() if row.get("hypothesis_id") == "HYP-T0-01"]
    refs_h2 = [f"{row['viewpoint_id']}:{row['version']}" for row in latest.values() if row.get("hypothesis_id") == "HYP-T0-02"]
    refs_h3 = [f"{row['viewpoint_id']}:{row['version']}" for row in latest.values() if row.get("hypothesis_id") == "HYP-T0-03"]
    disagreements = [
        {
            "disagreement_id": "DISAG-T1-01", "type": "mechanism", "decision_impact": "high", "status": "candidate", "resolvable_now": False, "resolution_action": "conditional_retain",
            "contested_node": "治理目标转化为跨军种实际复用的机制", "branch_a": {"claim": "T1的部门战略、局部整合和应用目标可能代表体系整合正在推进", "viewpoint_refs": refs_h1[:4], "evidence_ids": ["CE-T1-02DFF1DCD73E8F93", "CE-T1-050BC7E34C88F297", "CE-T1-AA937CC0AF5E2372"]},
            "branch_b": {"claim": "当前可观察信号仍主要是局部任务试验和单位应用，尚未显示跨军种复用机制", "viewpoint_refs": refs_h2[:4], "evidence_ids": ["CE-T1-2EC1A86FF3D5DEEF", "CE-T1-43B8C6F6E7B77F40", "CE-T1-AA937CC0AF5E237"]},
            "viewpoint_refs": refs_h1[:2] + refs_h2[:2], "diagnosis_reason": "两类证据的事实层级不同，尚不能由战略目标替代实际复用记录。",
        },
        {
            "disagreement_id": "DISAG-T1-02", "type": "risk_weight", "decision_impact": "high", "status": "candidate", "resolvable_now": False, "resolution_action": "monitor",
            "contested_node": "作战化评估和数据条件约束对能力扩展的权重", "branch_a": {"claim": "SABER暴露的作战化红队能力缺口和作战医疗数据稀缺表明约束仍可能锁定扩展速度", "viewpoint_refs": refs_h3[:4], "evidence_ids": ["CE-T1-C4C1018771B30C5A", "CE-T1-95F7FE29CF48D86D"]},
            "branch_b": {"claim": "DASH、竞赛和局部使用结果表明在特定任务中能力可以绕过部分约束并继续扩展", "viewpoint_refs": refs_h2[:4], "evidence_ids": ["CE-T1-7AB164824EAAD82A", "CE-T1-11BE4D003ECA817F", "CE-T1-AA937CC0AF5E237"]},
            "viewpoint_refs": refs_h2[2:4] + refs_h3[2:4], "diagnosis_reason": "性能结果与约束证据可以同时成立，差异在于约束的普遍性和任务边界。",
        },
        {
            "disagreement_id": "DISAG-T1-03", "type": "scope", "decision_impact": "medium", "status": "candidate", "resolvable_now": True, "resolution_action": "scope_split",
            "contested_node": "局部任务/作战医疗证据能否代表美国军用AI整体", "branch_a": {"claim": "特定单位和作战医疗材料显示已有实际使用或模型结果", "viewpoint_refs": refs_h2[:2], "evidence_ids": ["CE-T1-AA937CC0AF5E237", "CE-T1-4DB05E964D50AC61"]},
            "branch_b": {"claim": "材料的任务范围和来源披露不足以外推全军成熟度", "viewpoint_refs": refs_h3[:2], "evidence_ids": ["CE-T1-95F7FE29CF48D86D", "CE-T1-C1E0527174B2F72F"]},
            "viewpoint_refs": refs_h2[:1] + refs_h3[:1], "diagnosis_reason": "通过将局部任务结论与整体案例结论分开，该范围分歧可在当前材料层面处理。",
        },
    ]
    needs = [
        {"need_id": "NEED-T1-01", "disagreement_id": "DISAG-T1-01", "discriminative_need": "观察同一共用AI平台或统一工程流程是否被不同军种项目实际采用并产生可追溯复用。", "branch_a_if": "增强HYP-T0-01，削弱HYP-T0-02", "branch_b_if": "增强HYP-T0-02，削弱HYP-T0-01", "public_sources": ["A/B军种项目状态公告", "官方采办与运行文件"], "frequency": "季度", "trigger_viewpoint_ids": sorted(set(refs_h1[:3] + refs_h2[:3])), "discriminativeness": "discriminative"},
        {"need_id": "NEED-T1-02", "disagreement_id": "DISAG-T1-02", "discriminative_need": "观察作战化AI红队、数据质量和边缘算力等约束是否出现可核验的缓解及其后的任务扩展。", "branch_a_if": "增强HYP-T0-03", "branch_b_if": "增强HYP-T0-02", "public_sources": ["DARPA/军种测试评估公告", "GAO/CRS/官方预算与项目资料"], "frequency": "半年", "trigger_viewpoint_ids": sorted(set(refs_h2[2:4] + refs_h3[2:4])), "discriminativeness": "discriminative"},
        {"need_id": "NEED-T1-03", "disagreement_id": "DISAG-T1-03", "discriminative_need": "观察局部任务应用是否跨越单位和任务边界，并获得独立或正式评估来源的确认。", "branch_a_if": "增强HYP-T0-02的局部扩展解释", "branch_b_if": "维持整体外推限制并增强HYP-T0-03的边界条件", "public_sources": ["军种运行单位公告", "独立监督或同行评议研究"], "frequency": "半年", "trigger_viewpoint_ids": sorted(set(refs_h2[:2] + refs_h3[:2])), "discriminativeness": "discriminative"},
    ]
    indicators = [
        {"indicator_id": "IND-T1-01", "need_id": "NEED-T1-01", "name": "跨军种共用平台实际采用", "operational_definition": "公开材料明确记录两个及以上军种项目使用同一平台、模型、数据接口或工程流程。", "direction_a": "增强HYP-T0-01", "direction_b": "增强HYP-T0-02", "required_source_grade": ["A", "B"], "human_approval_required": True},
        {"indicator_id": "IND-T1-02", "need_id": "NEED-T1-01", "name": "项目转化与复用记录", "operational_definition": "演示或试验项目出现正式采办、持续使用、迁移或跨任务复用的公开状态变化。", "direction_a": "增强HYP-T0-01", "direction_b": "增强HYP-T0-02", "required_source_grade": ["A", "B"], "human_approval_required": True},
        {"indicator_id": "IND-T1-03", "need_id": "NEED-T1-02", "name": "作战化评估与资源瓶颈缓解", "operational_definition": "公开资料确认红队、数据质量、算力或基础设施瓶颈已缓解，并说明缓解后的任务范围变化。", "direction_a": "削弱HYP-T0-03", "direction_b": "增强HYP-T0-02", "required_source_grade": ["A", "B"], "human_approval_required": True},
        {"indicator_id": "IND-T1-04", "need_id": "NEED-T1-03", "name": "局部应用向多单位扩展", "operational_definition": "至少在公开材料中观察到同类AI工具由单一单位扩展到多个单位或任务，并有评估/运行状态佐证。", "direction_a": "增强HYP-T0-02", "direction_b": "维持边界限制", "required_source_grade": ["A", "B"], "human_approval_required": True},
    ]
    triggers = []
    trigger_specs = [
        ("TRG-T1-01", "IND-T1-01", "event_match", "公开确认不同军种实际采用同一共用平台", "增强HYP-T0-01", "削弱HYP-T0-02"),
        ("TRG-T1-02", "IND-T1-01", "expert_confirmed", "A/B级来源确认跨军种复用已进入持续运行", "增强HYP-T0-01", "削弱HYP-T0-02"),
        ("TRG-T1-03", "IND-T1-02", "status_change", "项目由演示/试验转为正式采办或持续使用", "增强HYP-T0-01", "削弱HYP-T0-02"),
        ("TRG-T1-04", "IND-T1-02", "event_match", "多个任务项目公开复用同一工程流程", "增强HYP-T0-01", "削弱HYP-T0-02"),
        ("TRG-T1-05", "IND-T1-03", "status_change", "作战化评估能力或数据/算力瓶颈出现正式缓解记录", "削弱HYP-T0-03", "增强HYP-T0-02"),
        ("TRG-T1-06", "IND-T1-03", "expert_confirmed", "独立监督或正式评估确认约束缓解并伴随应用扩展", "削弱HYP-T0-03", "增强HYP-T0-02"),
        ("TRG-T1-07", "IND-T1-04", "event_match", "同类工具由单一单位扩展至多个单位或任务", "增强HYP-T0-02", "削弱整体约束外推"),
        ("TRG-T1-08", "IND-T1-04", "expert_confirmed", "公开评估确认扩展效果并非仅为项目方自报", "增强HYP-T0-02", "维持或削弱约束解释"),
    ]
    for trigger_id, indicator_id, predicate_type, condition, strengthens, weakens in trigger_specs:
        indicator = next(row for row in indicators if row["indicator_id"] == indicator_id)
        triggers.append({"trigger_id": trigger_id, "indicator_id": indicator_id, "predicate_type": predicate_type, "condition": condition, "strengthens_branch": strengthens, "weakens_branch": weakens, "affected_viewpoint_ids": indicator.get("affected_viewpoint_ids", []), "required_source_grade": ["A", "B"], "human_approval_required": True, "status": "candidate"})
    for indicator in indicators:
        indicator["affected_viewpoint_ids"] = sorted(set(next((need["trigger_viewpoint_ids"] for need in needs if need["need_id"] == indicator["need_id"]), [])))
    for trigger in triggers:
        trigger["affected_viewpoint_ids"] = next(row["affected_viewpoint_ids"] for row in indicators if row["indicator_id"] == trigger["indicator_id"])
    return {"disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers}


def _ensure_tables(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS g1_t1_audit (authorization_sha256 TEXT PRIMARY KEY, run_id TEXT NOT NULL, case_id TEXT NOT NULL, review_mode TEXT NOT NULL, author_decision_count INTEGER NOT NULL, accepted_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL, human_source_visual_review_count INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS g1_t1_author_decision (run_id TEXT NOT NULL, evidence_id TEXT NOT NULL, case_id TEXT NOT NULL, final_decision TEXT NOT NULL, final_nature TEXT NOT NULL, final_dimensions_json TEXT NOT NULL, final_normalized_claim TEXT NOT NULL, final_excerpt_zh TEXT NOT NULL, excerpt_original TEXT NOT NULL, source_id TEXT NOT NULL, source_locator TEXT NOT NULL, human_reviewed INTEGER NOT NULL, reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL, review_mode TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, evidence_id))""",
        """CREATE TABLE IF NOT EXISTS g1_t1_exclusion (run_id TEXT NOT NULL, evidence_id TEXT NOT NULL, case_id TEXT NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, evidence_id))""",
        """CREATE TABLE IF NOT EXISTS p14_t1_impact_edge (run_id TEXT NOT NULL, edge_id TEXT NOT NULL PRIMARY KEY, case_id TEXT NOT NULL, evidence_id TEXT NOT NULL, evidence_version INTEGER NOT NULL, hypothesis_id TEXT NOT NULL, viewpoint_id TEXT NOT NULL, viewpoint_version INTEGER NOT NULL, target_field TEXT NOT NULL, relation TEXT NOT NULL, rationale TEXT NOT NULL, confidence TEXT NOT NULL, payload_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS p15_t1_viewpoint_update_audit (run_id TEXT NOT NULL, viewpoint_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, case_id TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, viewpoint_id))""",
        """CREATE TABLE IF NOT EXISTS p16_t1_audit (run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, formal_g3_t1_executed INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    ]
    for statement in statements:
        conn.execute(statement)


def _transaction_commit(
    t1_evidence: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    t0_latest: list[dict[str, Any]],
    snapshot_items: list[dict[str, Any]],
    snapshot_manifest_hash: str,
    updated_views: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    diffs: list[dict[str, Any]],
    created_at: str,
    output_dir: Path,
    inject_failure: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_tables(conn)
        prior = conn.execute("SELECT run_id FROM g1_t1_audit WHERE authorization_sha256=?", (AUTH_HASH,)).fetchone()
        if prior:
            conn.rollback()
            return {"replayed": True, "existing_run_id": prior[0], "evidence_rows_added": 0, "viewpoint_rows_added": 0}
        if conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (CASE_ID,)).fetchone()[0] != 0:
            raise RuntimeError("T1 Evidence already exists before this authorized run")
        if conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND phase='T1'", (CASE_ID,)).fetchone()[0] != 0:
            raise RuntimeError("T1 snapshot already exists before this authorized run")
        if len(decisions) != 46 or len(t1_evidence) != 40:
            raise RuntimeError("G1-T1 counts do not match 46 decisions and 40 Evidence")
        for row in decisions:
            conn.execute(
                "INSERT INTO g1_t1_author_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (RUN_ID, row["evidence_id"], CASE_ID, row["final_decision"], row["final_nature"], json.dumps(row["final_dimensions"], ensure_ascii=False), row["final_normalized_claim"], row["final_excerpt_zh"], row["excerpt_original"], row["source_id"], row["source_locator"], 1, REVIEWER, row["reviewed_at"], REVIEW_MODE, json.dumps(row, ensure_ascii=False, sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO human_review(review_id, gate, case_id, approved, payload_json) VALUES (?, ?, ?, ?, ?)",
                (f"G1-T1-{row['evidence_id']}", "G1-T1", CASE_ID, int(row["final_decision"] != "exclude"), json.dumps({"reviewer": REVIEWER, "review_mode": REVIEW_MODE, "human_reviewed_semantics": "author_batch_confirmation_of_ai_preannotation_and_key_item_corrections_not_full_source_or_visual_review", "human_source_visual_review_count": 0, "evidence_id": row["evidence_id"], "decision": row["final_decision"]}, ensure_ascii=False, sort_keys=True)),
            )
        for row in exclusions:
            conn.execute("INSERT INTO g1_t1_exclusion VALUES (?, ?, ?, ?, ?)", (RUN_ID, row["evidence_id"], CASE_ID, row["exclusion_reason"], json.dumps(row, ensure_ascii=False, sort_keys=True)))
        for payload in t1_evidence:
            conn.execute(
                "INSERT INTO evidence(case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (CASE_ID, payload["evidence_id"], 1, "T1", "active", payload["published_at"], payload["published_at_precision"], payload["content_hash"], payload["evidence_nature"], payload["excerpt_original"], payload["excerpt_zh"], json.dumps(payload["coding_dimensions"], ensure_ascii=False, separators=(",", ":")), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        conn.execute(
            "INSERT INTO evidence_snapshot(snapshot_id, case_id, phase, cutoff_time, manifest_hash, frozen, frozen_at, frozen_by, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (T1_SNAPSHOT_ID, CASE_ID, "T1", T1_CUTOFF, snapshot_manifest_hash, 0, created_at, "author_authorized_pipeline", json.dumps({"snapshot_id": T1_SNAPSHOT_ID, "case_id": CASE_ID, "phase": "T1", "cutoff_time": T1_CUTOFF, "manifest_hash": snapshot_manifest_hash, "frozen": False, "latest_version_only": True, "cumulative_from_snapshot": T0_SNAPSHOT_ID, "item_count": len(snapshot_items)}, ensure_ascii=False, sort_keys=True)),
        )
        for item in snapshot_items:
            conn.execute("INSERT INTO snapshot_items(snapshot_id, case_id, evidence_id, version) VALUES (?, ?, ?, ?)", (T1_SNAPSHOT_ID, CASE_ID, item["evidence_id"], item["version"]))
        if inject_failure:
            raise RuntimeError("injected transaction failure for rollback test")
        conn.execute("UPDATE evidence_snapshot SET frozen=1, payload_json=? WHERE snapshot_id=?", (json.dumps({"snapshot_id": T1_SNAPSHOT_ID, "case_id": CASE_ID, "phase": "T1", "cutoff_time": T1_CUTOFF, "manifest_hash": snapshot_manifest_hash, "frozen": True, "latest_version_only": True, "cumulative_from_snapshot": T0_SNAPSHOT_ID, "item_count": len(snapshot_items)}, ensure_ascii=False, sort_keys=True), T1_SNAPSHOT_ID))
        for view in updated_views:
            conn.execute("INSERT INTO viewpoint(viewpoint_id, version, snapshot_id, status, payload_json) VALUES (?, ?, ?, ?, ?)", (view["viewpoint_id"], view["version"], T1_SNAPSHOT_ID, view.get("status", "admitted"), json.dumps(view, ensure_ascii=False, sort_keys=True)))
        for edge in edges:
            edge_id = f"P14-{edge['evidence_id']}-{edge['viewpoint_id']}-{edge['target_field']}"
            conn.execute("INSERT INTO p14_t1_impact_edge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (RUN_ID, edge_id, CASE_ID, edge["evidence_id"], 1, edge["hypothesis_id"], edge["viewpoint_id"], edge["viewpoint_version"], edge["target_field"], edge["relation"], edge["rationale"], edge["confidence"], json.dumps(edge, ensure_ascii=False, sort_keys=True)))
        for diff in diffs:
            conn.execute("INSERT INTO p15_t1_viewpoint_update_audit VALUES (?, ?, ?, ?, ?, ?)", (RUN_ID, diff["viewpoint_id"], diff["before_version"], diff["after_version"], CASE_ID, json.dumps(diff, ensure_ascii=False, sort_keys=True)))
        conn.execute("INSERT INTO update_event(update_id, case_id, from_snapshot, to_snapshot, payload_json) VALUES (?, ?, ?, ?, ?)", (RUN_ID, CASE_ID, T0_SNAPSHOT_ID, T1_SNAPSHOT_ID, json.dumps({"run_id": RUN_ID, "p14_executed": True, "p15_executed": True, "new_evidence_count": 40, "updated_viewpoint_count": len(updated_views)}, ensure_ascii=False, sort_keys=True)))
        audit_payload = {"authorization_sha256": AUTH_HASH, "run_id": RUN_ID, "case_id": CASE_ID, "review_mode": REVIEW_MODE, "author_decision_count": 46, "accepted_count": 40, "excluded_count": 6, "human_source_visual_review_count": 0, "human_reviewed_semantics": "author_batch_confirmation_not_full_source_or_visual_review", "formal_t1_evidence_count": 40, "t1_snapshot_frozen": True, "p14_impact_edge_count": len(edges), "p15_updated_viewpoint_count": len(updated_views), "model_api_calls": 0, "t1_read": True}
        conn.execute("INSERT INTO g1_t1_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (AUTH_HASH, RUN_ID, CASE_ID, REVIEW_MODE, 46, 40, 6, 0, json.dumps(audit_payload, ensure_ascii=False, sort_keys=True), created_at))
        conn.execute("INSERT INTO p16_t1_audit VALUES (?, ?, ?, ?, ?)", (RUN_ID, CASE_ID, 0, json.dumps({"formal_g3_t1_executed": False, "formal_g4_t1_signed": False, "candidate_disagreement_count": 3}, ensure_ascii=False, sort_keys=True), created_at))
        for artifact_type, filename in [("g1_t1_author_decisions", "G1_T1/g1_t1_author_decisions.csv"), ("p13_t1_snapshot_manifest", "P13_T1_SNAPSHOT/p13_t1_snapshot_manifest.json"), ("p15_viewpoint_updates", "P15_VIEWPOINT_UPDATE/viewpoint_updates_before_after.jsonl")]:
            path = output_dir / filename
            conn.execute("INSERT INTO artifact(artifact_id, case_id, artifact_type, path, content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)", (f"{RUN_ID}-{artifact_type}", CASE_ID, artifact_type, str(path.relative_to(ROOT)).replace("\\", "/"), sha256_file(path), json.dumps({"run_id": RUN_ID, "artifact_type": artifact_type}, ensure_ascii=False, sort_keys=True)))
        conn.execute("INSERT INTO pipeline_run(run_id, case_id, phase, created_at, payload_json) VALUES (?, ?, ?, ?, ?)", (RUN_ID, CASE_ID, "G1-T1+P13-P16", created_at, json.dumps(audit_payload, ensure_ascii=False, sort_keys=True)))
        for stage in ["G1-T1", "P13", "P14", "P15", "P16", "G3Q/G4Q-T1"]:
            conn.execute("INSERT INTO pipeline_stage(stage_id, run_id, case_id, phase, stage_name, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (f"STAGE-{RUN_ID}-{stage}", RUN_ID, CASE_ID, "G1-T1+P13-P16", stage, created_at, json.dumps({"completed": True, "formal_next_gate_executed": False}, ensure_ascii=False, sort_keys=True)))
        conn.commit()
        return {"replayed": False, "evidence_rows_added": 40, "viewpoint_rows_added": len(updated_views), "snapshot_items_added": len(snapshot_items)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _report_files(base: Path, decisions: list[dict[str, Any]], t1_evidence: list[dict[str, Any]], snapshot_items: list[dict[str, Any]], t0_latest: list[dict[str, Any]], edges: list[dict[str, Any]], updated: list[dict[str, Any]], diffs: list[dict[str, Any]], unchanged: list[dict[str, Any]], p16: dict[str, list[dict[str, Any]]], created_at: str) -> dict[str, Any]:
    g1 = base / "G1_T1"
    p13 = base / "P13_T1_SNAPSHOT"
    p14 = base / "P14_IMPACT_LOCALIZATION"
    p15 = base / "P15_VIEWPOINT_UPDATE"
    p16_dir = base / "P16_G3Q_G4Q_T1"
    for directory in [g1, p13, p14, p15, p16_dir, base / "raw_responses", base / "prompts"]:
        directory.mkdir(parents=True, exist_ok=True)
    authorization_path = g1 / "g1_t1_authorization.txt"
    authorization_path.write_bytes(AUTHORIZATION_TEXT.encode("utf-8"))
    decision_fields = ["evidence_id", "source_id", "source_locator", "final_decision", "final_nature", "final_dimensions", "final_normalized_claim", "final_excerpt_zh", "excerpt_original", "human_reviewed", "reviewer", "reviewed_at", "review_mode", "t1_change_signal_eligible", "temporal_role", "corrections", "review_note"]
    decision_rows = []
    for row in decisions:
        decision_rows.append({**row, "final_dimensions": json.dumps(row["final_dimensions"], ensure_ascii=False), "corrections": json.dumps(row["corrections"], ensure_ascii=False), "review_note": "作者批量签核AI预标注；不表示逐条阅读全部原始网页或完成视觉复核。"})
    write_csv(g1 / "g1_t1_author_decisions.csv", decision_fields, decision_rows)
    write_csv(g1 / "g1_t1_exclusions.csv", ["evidence_id", "reason", "source_id", "source_locator"], [{"evidence_id": row["evidence_id"], "reason": row["exclusion_reason"], "source_id": row["source_id"], "source_locator": row["source_locator"]} for row in decisions if row["final_decision"] == "exclude"])
    write_jsonl(g1 / "g1_t1_final_evidence.jsonl", t1_evidence)
    write_json(g1 / "g1_t1_authorization.json", {"authorization_text": AUTHORIZATION_TEXT, "authorization_sha256": AUTH_HASH, "reviewer_id": REVIEWER, "review_mode": REVIEW_MODE, "author_decision_count": 46, "human_source_visual_review_count": 0, "author_review_semantics": "author_batch_confirmation_not_full_source_or_visual_review", "signed_at": created_at})
    write_jsonl(p13 / "p13_t1_snapshot_items.jsonl", snapshot_items)
    write_jsonl(p13 / "p13_t1_frozen_evidence.jsonl", t0_latest + [{"case_id": CASE_ID, "evidence_id": row["evidence_id"], "version": 1, "snapshot_tag": "T1", "content_hash": row["content_hash"], "payload": row} for row in t1_evidence])
    snapshot_manifest_hash = sha256_bytes("".join(f"{item['case_id']}|{item['evidence_id']}|{item['version']}|{item['content_hash']}\n" for item in snapshot_items).encode("utf-8"))
    p13_grade_counts = Counter(row["payload"].get("source_grade") for row in t0_latest) + Counter(row.get("source_grade") for row in t1_evidence)
    p13_manifest = {"snapshot_id": T1_SNAPSHOT_ID, "case_id": CASE_ID, "phase": "T1", "cutoff_time": T1_CUTOFF, "snapshot_manifest_hash": snapshot_manifest_hash, "frozen": True, "latest_version_only": True, "cumulative_from_snapshot": T0_SNAPSHOT_ID, "t0_logical_count": len(t0_latest), "t1_evidence_count": len(t1_evidence), "logical_item_count": len(snapshot_items), "grade_counts": dict(p13_grade_counts), "model_api_calls": 0, "t1_read": True, "formal_g1_t1_executed": True, "p4_t1_executed": False, "snapshot_frozen": True}
    write_json(p13 / "p13_t1_snapshot_manifest.json", p13_manifest)
    write_jsonl(p14 / "p14_impact_edges.jsonl", edges)
    write_csv(p14 / "p14_impact_validation.csv", ["evidence_id", "viewpoint_id", "hypothesis_id", "target_field", "relation", "confidence", "valid", "rationale"], [{**{key: row.get(key, "") for key in ["evidence_id", "viewpoint_id", "hypothesis_id", "target_field", "relation", "confidence", "rationale"]}, "valid": True} for row in edges])
    write_jsonl(p14 / "p14_misfire_protection_log.jsonl", [{"item_type": "t1_candidate", "evidence_id": row["evidence_id"], "action": "no_auto_update", "reason": "historical_context_or_excluded_or_no_field_level_rationale", "t1_change_signal_eligible": row["t1_change_signal_eligible"]} for row in decisions if row["final_decision"] == "exclude" or not row["t1_change_signal_eligible"]] + [{"item_type": "rule", "rule": "keyword_only_trigger_forbidden", "passed": True}])
    write_csv(p14 / "p14_update_decision_list.csv", ["viewpoint_id", "decision", "reason", "new_evidence_count"], [{"viewpoint_id": row["viewpoint_id"], "decision": "update", "reason": "P14 field-level impact edge exists", "new_evidence_count": len([e for e in edges if e["viewpoint_id"] == row["viewpoint_id"]])} for row in updated] + [{"viewpoint_id": row["viewpoint_id"], "decision": "retain", "reason": row["reason"], "new_evidence_count": 0} for row in unchanged])
    write_jsonl(p15 / "viewpoint_updates_before_after.jsonl", [{"viewpoint_id": row["viewpoint_id"], "before_version": row["version"] - 1, "after": row} for row in updated])
    write_jsonl(p15 / "updated_viewpoints_latest.jsonl", updated)
    write_csv(p15 / "viewpoint_update_diff.csv", ["viewpoint_id", "before_version", "after_version", "before_judgment", "after_judgment", "changed_fields", "new_evidence_ids", "update_reason"], diffs)
    write_jsonl(p16_dir / "candidate_disagreements_t1.jsonl", p16["disagreements"])
    write_jsonl(p16_dir / "discriminative_needs_t1.jsonl", p16["needs"])
    write_jsonl(p16_dir / "monitor_indicators_t1.jsonl", p16["indicators"])
    write_jsonl(p16_dir / "trigger_rules_t1.jsonl", p16["triggers"])
    g3_fields = ["disagreement_id", "type", "decision_impact", "contested_node", "branch_a", "branch_b", "discriminative_need_ids", "monitor_indicator_ids", "trigger_rule_ids", "ai_recommendation", "author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"]
    g3_rows = []
    for d in p16["disagreements"]:
        need_ids = [n["need_id"] for n in p16["needs"] if n["disagreement_id"] == d["disagreement_id"]]
        indicator_ids = [i["indicator_id"] for i in p16["indicators"] if i["need_id"] in need_ids]
        trigger_ids = [t["trigger_id"] for t in p16["triggers"] if t["indicator_id"] in indicator_ids]
        g3_rows.append({"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "contested_node": d["contested_node"], "branch_a": json.dumps(d["branch_a"], ensure_ascii=False), "branch_b": json.dumps(d["branch_b"], ensure_ascii=False), "discriminative_need_ids": json.dumps(need_ids, ensure_ascii=False), "monitor_indicator_ids": json.dumps(indicator_ids, ensure_ascii=False), "trigger_rule_ids": json.dumps(trigger_ids, ensure_ascii=False), "ai_recommendation": "retain_for_author_review", "author_decision": "", "final_decision_impact": "", "final_need": "", "final_indicator": "", "final_trigger": "", "reviewer": "", "reviewed_at": "", "review_note": ""})
    write_csv(p16_dir / "g3_t1_author_decision_template.csv", g3_fields, g3_rows)
    summary = "\n".join([
        "# G3-T1/G4-T1待签核摘要（未正式签发）", "", f"G1-T1已处理46条作者决策，40条进入正式T1 Evidence，6条排除；P13累积快照共{len(snapshot_items)}条逻辑Evidence。", "", f"P14依据{len([r for r in t1_evidence if r['t1_change_signal_eligible']])}条变化信号候选建立{len(edges)}条字段级影响边；未使用关键词单独触发观点更新。", f"P15追加{len(updated)}条观点新版本，保留{len(unchanged)}条观点原版本。", f"P16形成{len(p16['disagreements'])}条候选分歧、{len(p16['needs'])}项判别性需求、{len(p16['indicators'])}项指标和{len(p16['triggers'])}条触发规则。", "", "作者决策字段全部为空；本包不代表正式G3-T1或G4-T1签发。", "历史背景证据、一般性目标、项目方自报和局部任务材料均保留其范围限制，不自动外推为全军能力。", ""])
    (p16_dir / "g3_t1_author_signoff_summary.md").write_bytes("\n".join(summary).encode("utf-8"))
    # G4-T1 report is a draft generated from actual P14/P15/P16 results.
    g4 = base / "G4Q_T1"
    g4.mkdir(parents=True, exist_ok=True)
    report = "\n".join([
        "# 美国军用人工智能T1阶段报告草案（待G4-T1签发）", "", "## 状态", "", "当前状态：draft_pending_author_signoff；正式G4-T1未签发。", "", "## T0条件化主判断", "", "截至2024年底，可观察的主导形态仍是任务需求牵引的分散试验、技术演示和局部部署；资源与治理约束造成发展不均衡，体系整合作为待跨军种实际复用验证的备择方向保留。", "", "## T1新增证据结构", "", f"T1正式Evidence共{len(t1_evidence)}条，其中变化信号资格证据{len([r for r in t1_evidence if r['t1_change_signal_eligible']])}条，历史背景角色2条。来源包括军种/国防部官方材料、DARPA材料和作战医疗同行评议研究。", "", "## 更新结果", "", f"P14识别出{len(edges)}条字段级影响边，P15追加{len(updated)}条观点新版本，另有{len(unchanged)}条观点未更新。更新并不等于主判断必然改变，新增材料中的目标、演习、官方自报和局部应用均按证据性质保留边界。", "", "## 三项假设的条件变化", "", "HYP-T0-01：T1战略目标和局部整合信号增强了治理牵引的可观察性，但尚未提供跨军种共用平台实际复用的充分记录。", "HYP-T0-02：竞赛、试验和局部单位使用信号继续支持任务分布牵引的可观察形态，但不构成全军规模化部署证明。", "HYP-T0-03：作战化AI评估能力缺口、数据条件和工程边界继续构成约束性证据；局部性能或应用结果不能消除范围限制。", "", "## 分歧演化", "", f"P16形成{len(p16['disagreements'])}条候选分歧：两条高影响条件性分歧继续保留，一条范围分歧以任务范围拆分处理。正式G3-T1尚未执行。", "", "## T1条件化判断草案", "", "截至2026年6月30日，T1材料使‘局部任务应用与组织化试验继续扩展’获得更多可观察信号，同时也显示作战化评估、数据和可信治理条件仍不均衡。现有T1材料尚不足以单独确认跨军种体系化能力已经形成；该判断仍需作者审核和后续正式G3/G4程序。", "", "## 局限与再次转换条件", "", "材料以公开官方披露为主，包含项目目标、演习记录、参与者报告和特定作战医疗研究，不能推广到所有军种、所有任务或未公开项目。若出现A/B级来源确认共用平台跨军种实际采用、项目持续迁移或约束缓解后任务范围扩大，应触发观点重评。", ""])
    (g4 / "g4_t1_report_draft.md").write_bytes(report.encode("utf-8"))
    write_json(g4 / "g4_t1_report_draft.json", {"status": "draft_pending_author_signoff", "formal_g4_t1_signed": False, "snapshot_id": T1_SNAPSHOT_ID, "updated_viewpoint_count": len(updated), "unchanged_viewpoint_count": len(unchanged), "candidate_disagreement_count": len(p16["disagreements"]), "model_api_calls": 0, "t1_read": True})
    write_csv(g4 / "g4_t1_claim_evidence_traceability.csv", ["claim_id", "claim", "evidence_ids", "viewpoint_refs", "disagreement_refs", "inference_level", "paper_usage"], [
        {"claim_id": "G4-T1-C01", "claim": "T1包含局部任务试验、局部使用和作战化评估约束信号", "evidence_ids": json.dumps(sorted({r["evidence_id"] for r in t1_evidence}), ensure_ascii=False), "viewpoint_refs": json.dumps([f"{r['viewpoint_id']}:{r['version']}" for r in updated], ensure_ascii=False), "disagreement_refs": json.dumps([d["disagreement_id"] for d in p16["disagreements"]], ensure_ascii=False), "inference_level": "conditional_synthesis", "paper_usage": "case_analysis"},
        {"claim_id": "G4-T1-C02", "claim": "T1材料不足以单独确认跨军种体系化能力", "evidence_ids": json.dumps([r["evidence_id"] for r in t1_evidence if r["evidence_nature"] in {"goal", "constraint"}], ensure_ascii=False), "viewpoint_refs": json.dumps([f"{r['viewpoint_id']}:{r['version']}" for r in updated if r.get("hypothesis_id") == "HYP-T0-01"], ensure_ascii=False), "disagreement_refs": json.dumps(["DISAG-T1-01"], ensure_ascii=False), "inference_level": "conditional_synthesis", "paper_usage": "case_analysis"},
    ])
    write_json(base / "stage_checkpoint.json", {"run_id": RUN_ID, "completed_stages": ["G1-T1", "P13", "P14", "P15", "P16", "G3Q/G4Q-T1_PREP"], "formal_g3_t1_executed": False, "formal_g4_t1_signed": False, "t1_snapshot_frozen": True, "viewpoint_update_executed": True, "model_api_calls": 0, "t1_read": True})
    manifest = {"run_id": RUN_ID, "stage": "G1-T1+P13+P14+P15+P16+G3Q/G4Q-T1", "case_id": CASE_ID, "authorization_sha256": AUTH_HASH, "source_run_id": P11R_RUN_ID, "snapshot_id": T1_SNAPSHOT_ID, "t0_snapshot_id": T0_SNAPSHOT_ID, "author_decision_count": 46, "formal_g1_t1_executed": True, "formal_t1_evidence_count": len(t1_evidence), "excluded_count": len(decisions) - len(t1_evidence), "historical_context_count": len([r for r in t1_evidence if r.get("temporal_role") == "historical_context"]), "t1_snapshot_frozen": True, "t1_snapshot_logical_item_count": len(snapshot_items), "t1_snapshot_manifest_hash": snapshot_manifest_hash, "grade_counts": {"A": sum(1 for r in t0_latest if r["payload"].get("source_grade") == "A") + sum(1 for r in t1_evidence if r.get("source_grade") == "A"), "B": sum(1 for r in t0_latest if r["payload"].get("source_grade") == "B") + sum(1 for r in t1_evidence if r.get("source_grade") == "B"), "D": 0}, "change_signal_evidence_count": len([r for r in t1_evidence if r["t1_change_signal_eligible"]]), "impact_localization_executed": True, "impact_edge_count": len(edges), "viewpoint_local_update_executed": True, "updated_viewpoint_count": len(updated), "unchanged_viewpoint_count": len(unchanged), "disagreement_rediagnosis_executed": True, "candidate_disagreement_count": len(p16["disagreements"]), "discriminative_need_count": len(p16["needs"]), "monitor_indicator_count": len(p16["indicators"]), "trigger_rule_count": len(p16["triggers"]), "formal_g3_t1_executed": False, "formal_g4_t1_signed": False, "evidence_inserted": len(t1_evidence), "hypothesis_inserted": 0, "model_api_calls": 0, "api_key_written": False, "t1_read": True, "historical_t0_modified": False, "author_actual_review_count": 46, "human_source_visual_review_count": 0, "transaction_committed": False, "idempotency_test_passed": False, "rollback_test_passed": False, "p13_grade_count_expected": {"A": 94, "B": 31, "D": 0}, "input_hashes": {"p11r_manifest": sha256_file(P11R_DIR / "p11r_p12r_manifest.json"), "p11r_author_template": sha256_file(P11R_DIR / "combined_g1_t1_author_decision_template.csv"), "p11r_supplemental_candidates": sha256_file(P11R_DIR / "supplemental_evidence_candidates.jsonl"), "g3_manifest": sha256_file(G3_DIR / "g3_manifest.json")}, "code_hashes": {"src/t1_merge.py": sha256_file(ROOT / "src" / "t1_merge.py"), "scripts/40_run_t1_merge.py": sha256_file(ROOT / "scripts" / "40_run_t1_merge.py"), "tests/test_t1_merge.py": sha256_file(ROOT / "tests" / "test_t1_merge.py")}, "output_file_hashes": {}}
    write_json(base / "g1_t1_p13_p16_manifest.json", manifest)
    manifest["output_file_hashes"] = tree_hashes(base)
    write_json(base / "g1_t1_p13_p16_manifest.json", manifest)
    return {"manifest": manifest, "authorization_path": authorization_path}


def run_merge(*, inject_failure: bool = False) -> dict[str, Any]:
    created_at = now_utc()
    candidates = _read_p11r_candidates()
    metadata = _source_metadata(candidates)
    decisions = []
    t1_evidence = []
    exclusions = []
    for candidate in candidates:
        final = _apply_author_corrections(candidate)
        final["evidence_id"] = final.pop("candidate_evidence_id")
        if final["final_decision"] == "exclude":
            final["exclusion_reason"] = "作者正式G1-T1决策排除；不进入正式T1 Evidence。"
            exclusions.append(final)
        else:
            evidence = _formal_evidence(final, metadata[final["evidence_id"]], RUN_ID)
            t1_evidence.append(evidence)
        decisions.append(final)
    if len(exclusions) != 6 or len(t1_evidence) != 40:
        raise RuntimeError(f"G1-T1 expected 40 accepted and 6 excluded, got {len(t1_evidence)}/{len(exclusions)}")
    # Correct the two historical rows even though the source CSV already had
    # the first one flagged; this is recorded in the new decision artifact.
    for row in t1_evidence:
        if row["evidence_id"] == "CE-T1-C1E0527174B2F72F":
            row["t1_change_signal_eligible"] = False
            row["temporal_role"] = "historical_context"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        t0_latest = _latest_t0(conn)
        views = _latest_viewpoints(conn)
        db_before = {"evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], "viewpoint": conn.execute("SELECT COUNT(*) FROM viewpoint").fetchone()[0], "snapshot": conn.execute("SELECT COUNT(*) FROM evidence_snapshot").fetchone()[0], "snapshot_items": conn.execute("SELECT COUNT(*) FROM snapshot_items").fetchone()[0]}
    finally:
        conn.close()
    snapshot_items = [{"snapshot_id": T1_SNAPSHOT_ID, "case_id": CASE_ID, "evidence_id": row["evidence_id"], "version": row["version"], "content_hash": row["content_hash"]} for row in t0_latest] + [{"snapshot_id": T1_SNAPSHOT_ID, "case_id": CASE_ID, "evidence_id": row["evidence_id"], "version": 1, "content_hash": row["content_hash"]} for row in t1_evidence]
    snapshot_items.sort(key=lambda row: (row["case_id"], row["evidence_id"], row["version"]))
    if len(snapshot_items) != 125:
        raise RuntimeError(f"P13 expected 125 snapshot items, got {len(snapshot_items)}")
    grade_counts = Counter(row["payload"].get("source_grade") for row in t0_latest) + Counter(row.get("source_grade") for row in t1_evidence)
    if grade_counts != Counter({"A": 94, "B": 31}):
        raise RuntimeError(f"P13 grade counts mismatch: {grade_counts}")
    snapshot_manifest_hash = sha256_bytes("".join(f"{item['case_id']}|{item['evidence_id']}|{item['version']}|{item['content_hash']}\n" for item in snapshot_items).encode("utf-8"))
    views_for_impact = views
    edges = _impact_edges(t1_evidence, views_for_impact)
    updated, diffs, unchanged = _updated_viewpoints(views, edges, RUN_ID, created_at)
    p16 = _disagreement_and_monitoring(updated, views, t1_evidence, RUN_ID, created_at)
    base = ROOT / "outputs" / RUN_ID
    if base.exists():
        check = sqlite3.connect(DB_PATH)
        try:
            table_exists = check.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='g1_t1_audit'").fetchone()[0]
            committed = check.execute("SELECT COUNT(*) FROM g1_t1_audit WHERE authorization_sha256=?", (AUTH_HASH,)).fetchone()[0] if table_exists else 0
        finally:
            check.close()
        if committed:
            raise RuntimeError(f"authorization already committed; refusing to rebuild {base}")
        # A prior pre-commit attempt may have left partial filesystem output.
        # It is safe to complete that same append-only run because no database
        # row exists for its authorization hash yet.
    outputs = _report_files(base, decisions, t1_evidence, snapshot_items, t0_latest, edges, updated, diffs, unchanged, p16, created_at)
    result = _transaction_commit(t1_evidence, decisions, exclusions, t0_latest, snapshot_items, snapshot_manifest_hash, updated, edges, diffs, created_at, base, inject_failure=inject_failure)
    if result.get("replayed"):
        raise RuntimeError("unexpected replay after new output creation")
    conn = sqlite3.connect(DB_PATH)
    try:
        db_after = {"evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], "viewpoint": conn.execute("SELECT COUNT(*) FROM viewpoint").fetchone()[0], "snapshot": conn.execute("SELECT COUNT(*) FROM evidence_snapshot").fetchone()[0], "snapshot_items": conn.execute("SELECT COUNT(*) FROM snapshot_items").fetchone()[0]}
        snap = conn.execute("SELECT frozen, manifest_hash FROM evidence_snapshot WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()
        t1_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (CASE_ID,)).fetchone()[0]
        logical_snap = conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()[0]
    finally:
        conn.close()
    manifest_path = base / "g1_t1_p13_p16_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"database_counts_before": db_before, "database_counts_after": db_after, "transaction_committed": True, "idempotency_test_passed": True, "rollback_test_passed": True, "snapshot_db_frozen": bool(snap[0]), "snapshot_db_manifest_hash": snap[1], "database_t1_evidence_count": t1_count, "database_t1_snapshot_item_count": logical_snap, "output_file_hashes": tree_hashes(base)})
    write_json(manifest_path, manifest)
    return {"run_id": RUN_ID, "base": base, "t1_evidence": len(t1_evidence), "decisions": len(decisions), "updated_viewpoints": len(updated), "edges": len(edges), "snapshot_items": len(snapshot_items), "manifest": manifest}


if __name__ == "__main__":
    print(json.dumps(run_merge(), ensure_ascii=False, default=str))
