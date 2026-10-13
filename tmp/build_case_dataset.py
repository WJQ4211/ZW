import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def compact_evidence(row, snapshot):
    return {
        "id": row.get("evidence_id"),
        "snapshot": snapshot,
        "title": row.get("title", ""),
        "claim": row.get("normalized_claim") or row.get("excerpt_zh") or row.get("excerpt_original", ""),
        "excerptZh": row.get("excerpt_zh", ""),
        "excerptOriginal": row.get("excerpt_original") or row.get("excerpt", ""),
        "nature": row.get("evidence_nature", ""),
        "grade": row.get("source_grade", ""),
        "sourceId": row.get("source_id", ""),
        "publisher": row.get("publisher", ""),
        "publishedAt": row.get("published_at", ""),
        "locator": row.get("source_locator") or row.get("page_or_section", ""),
        "sourceType": row.get("source_type", ""),
        "dimensions": row.get("coding_dimensions", []),
        "topics": row.get("topics", []),
        "urlOrPath": row.get("url_or_path", ""),
        "changeSignal": bool(row.get("t1_change_signal_eligible")) if snapshot == "T1" else False,
        "humanReviewed": bool(row.get("human_reviewed")) if snapshot == "T1" else bool(row.get("reviewed_by")),
        "reviewedBy": row.get("reviewer") or row.get("reviewed_by", ""),
    }


def latest_case_viewpoints(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """
        SELECT v.viewpoint_id, v.version, v.snapshot_id, v.status, v.payload_json
        FROM viewpoint v
        JOIN (
          SELECT viewpoint_id, MAX(version) AS max_version
          FROM viewpoint
          WHERE viewpoint_id LIKE 'VP-T0-%'
          GROUP BY viewpoint_id
        ) x ON x.viewpoint_id=v.viewpoint_id AND x.max_version=v.version
        ORDER BY v.viewpoint_id
        """
    ).fetchall()
    con.close()
    result = []
    for viewpoint_id, version, snapshot_id, status, payload_json in rows:
        payload = json.loads(payload_json)
        result.append({
            "id": viewpoint_id,
            "version": version,
            "ref": f"{viewpoint_id}:{version}",
            "snapshotId": snapshot_id,
            "status": status,
            "perspective": payload.get("perspective_id", ""),
            "hypothesisId": payload.get("hypothesis_id", ""),
            "judgment": payload.get("judgment", ""),
            "stance": payload.get("stance", ""),
            "confidence": payload.get("confidence", ""),
            "supportingEvidence": payload.get("supporting_evidence", []),
            "counterEvidence": payload.get("counter_evidence", []),
            "mechanismClaims": payload.get("mechanism_claims", []),
            "keyAssumptions": payload.get("key_assumptions", []),
            "boundaryConditions": payload.get("boundary_conditions", []),
            "uncertainties": payload.get("uncertainties", []),
            "informationGaps": payload.get("information_gaps", []),
            "falsifiers": payload.get("falsifiers", []),
            "parentVersion": payload.get("parent_version"),
            "humanVerified": bool(payload.get("human_verified")),
        })
    return result


def normalize_dependency(row, phase):
    return {
        "id": row.get("edge_id", ""),
        "phase": phase,
        "evidenceId": row.get("evidence_id", ""),
        "viewpointId": row.get("viewpoint_id", ""),
        "viewpointVersion": row.get("viewpoint_version"),
        "hypothesisId": row.get("hypothesis_id", ""),
        "targetField": row.get("target_field", ""),
        "relation": row.get("relation", ""),
        "importance": row.get("importance", ""),
        "confidence": row.get("confidence", ""),
        "rationale": row.get("rationale", ""),
        "humanVerified": bool(row.get("human_verified")),
    }


def build_viewpoint_histories(stage_specs):
    """Collect the auditable P7/P8/P15/P15R viewpoint lineage by real version."""
    histories = defaultdict(dict)
    for path, phase, stage, default_version in stage_specs:
        for row in load_jsonl(path):
            viewpoint_id = row.get("viewpoint_id")
            if not viewpoint_id:
                continue
            version = row.get("version") or default_version
            supporting = row.get("supporting_evidence", []) or []
            counter = row.get("counter_evidence", []) or []
            histories[viewpoint_id][int(version)] = {
                "version": int(version),
                "phase": phase,
                "stage": stage,
                "judgment": row.get("judgment", ""),
                "stance": row.get("stance", ""),
                "confidence": row.get("confidence", ""),
                "evidenceIds": [
                    item.get("evidence_id")
                    for item in supporting + counter
                    if item.get("evidence_id")
                ],
            }
    return {
        viewpoint_id: [versions[v] for v in sorted(versions)]
        for viewpoint_id, versions in histories.items()
    }


def main():
    t0_evidence_path = ROOT / "outputs/P4-T0-C6C812108AF95309-freeze-v1/p4_t0_frozen_evidence.jsonl"
    t1_evidence_path = ROOT / "outputs/G1-T1-P13-P16-99A723BFBC6D273A-formal-v1/G1_T1/g1_t1_final_evidence.jsonl"
    hypothesis_path = ROOT / "outputs/G2-T0-1B8D6A450720F041-formal-v1/g2_final_hypotheses.jsonl"
    t0_dependency_path = ROOT / "outputs/P7R-P9R-20260827T020335Z-case-strengthening-v1/P8R/evidence_dependencies.jsonl"
    t1_impact_path = ROOT / "outputs/G1-T1-P13-P16-99A723BFBC6D273A-formal-v1/P14_IMPACT_LOCALIZATION/p14_impact_edges.jsonl"
    update_decision_path = ROOT / "outputs/G1-T1-P13-P16-99A723BFBC6D273A-formal-v1/P14_IMPACT_LOCALIZATION/p14_update_decision_list.csv"
    update_diff_path = ROOT / "outputs/P15R-P16R-G4QR-T1-PAPER-CASE-20260827T082433Z-v5/P15R/viewpoint_update_diff.csv"
    before_after_path = ROOT / "outputs/P15R-P16R-G4QR-T1-PAPER-CASE-20260827T082433Z-v5/P15R/viewpoint_before_after_complete.jsonl"
    viewpoint_history_specs = [
        (ROOT / "outputs/P7R-P9R-20260827T020335Z-case-strengthening-v1/P7R/initial_viewpoints_15.jsonl", "T0", "P7 初始观点", 1),
        (ROOT / "outputs/P7R-P9R-20260827T020335Z-case-strengthening-v1/P8R/final_viewpoints_latest.jsonl", "T0", "P8 质疑响应后／T0冻结版", 1),
        (ROOT / "outputs/G1-T1-P13-P16-99A723BFBC6D273A-formal-v1/P15_VIEWPOINT_UPDATE/updated_viewpoints_latest.jsonl", "T1", "P15 T1证据定向追加", 2),
        (ROOT / "outputs/P15R-P16R-G4QR-T1-PAPER-CASE-20260827T082433Z-v5/P15R/corrected_viewpoints_latest.jsonl", "T1", "P15R 审计修复／最终版", 2),
    ]
    t0_disagreement_path = ROOT / "outputs/G3-T0-20260827T-formal-offline-v5/g3_formal_disagreements.jsonl"
    final_dir = ROOT / "outputs/FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-20260827T112444Z-v1/FINAL_PAPER_CASE"

    evidence = [compact_evidence(r, "T0") for r in load_jsonl(t0_evidence_path)]
    evidence += [compact_evidence(r, "T1") for r in load_jsonl(t1_evidence_path)]
    evidence_by_id = {row["id"]: row for row in evidence}

    hypotheses_raw = load_jsonl(hypothesis_path)
    hypotheses = []
    for row in hypotheses_raw:
        hypotheses.append({
            "id": row["hypothesis_id"],
            "name": {
                "HYP-T0-01": "体系整合牵引型",
                "HYP-T0-02": "任务分布牵引型",
                "HYP-T0-03": "约束锁定渐进型",
            }.get(row["hypothesis_id"], row["hypothesis_id"]),
            "statement": row.get("statement", ""),
            "scope": row.get("scope", ""),
            "assumptions": row.get("key_assumptions", []),
            "supportingEvidenceIds": row.get("supporting_evidence_ids", []),
            "counterEvidenceIds": row.get("counter_evidence_ids", []),
            "conversionCriteria": row.get("conversion_criteria", []),
            "unresolvedQuestions": row.get("unresolved_questions", []),
            "decision": row.get("decision", ""),
            "reviewedBy": row.get("reviewed_by", ""),
        })

    viewpoints = latest_case_viewpoints(ROOT / "data/research.sqlite3")
    update_decisions = {r["viewpoint_id"]: r for r in load_csv(update_decision_path)}
    update_diffs = {r["viewpoint_id"]: r for r in load_csv(update_diff_path)}
    before_after = {r["viewpoint_id"]: r for r in load_jsonl(before_after_path)}
    viewpoint_histories = build_viewpoint_histories(viewpoint_history_specs)
    for row in viewpoints:
        decision = update_decisions.get(row["id"], {})
        diff = update_diffs.get(row["id"], {})
        pair = before_after.get(row["id"], {})
        row["updateDecision"] = decision.get("decision", "retain")
        row["updateDecisionReason"] = decision.get("reason", "")
        row["newEvidenceCount"] = int(decision.get("new_evidence_count") or 0)
        row["updateRationale"] = diff.get("update_rationale", "")
        try:
            row["changedFields"] = json.loads(diff.get("changed_fields", "[]"))
        except json.JSONDecodeError:
            row["changedFields"] = []
        try:
            row["newEvidenceIds"] = json.loads(diff.get("new_evidence_ids", "[]"))
        except json.JSONDecodeError:
            row["newEvidenceIds"] = []
        row["beforeJudgment"] = pair.get("before", {}).get("judgment", "")
        row["beforeVersion"] = pair.get("before", {}).get("version")
        row["versionHistory"] = viewpoint_histories.get(row["id"], [])

    t0_dependencies = [normalize_dependency(r, "T0正式依赖") for r in load_jsonl(t0_dependency_path)]
    t1_impacts = [normalize_dependency(r, "T1字段影响") for r in load_jsonl(t1_impact_path)]
    dependencies = t0_dependencies + t1_impacts

    t1_disagreements = load_jsonl(final_dir / "formal_g3_t1_disagreements.jsonl")
    t0_disagreements = load_jsonl(t0_disagreement_path)
    needs = load_jsonl(final_dir / "formal_g3_t1_needs.jsonl")
    indicators = load_jsonl(final_dir / "formal_g3_t1_indicators.jsonl")
    triggers = load_jsonl(final_dir / "formal_g3_t1_triggers.jsonl")
    lineage = load_jsonl(final_dir / "t0_t1_disagreement_lineage.jsonl")

    hypothesis_links = defaultdict(list)
    for hyp in hypotheses:
        for ev_id in hyp["supportingEvidenceIds"]:
            hypothesis_links[ev_id].append({"id": hyp["id"], "relation": "support"})
        for ev_id in hyp["counterEvidenceIds"]:
            hypothesis_links[ev_id].append({"id": hyp["id"], "relation": "counter"})

    viewpoint_links = defaultdict(list)
    for dep in dependencies:
        if dep["evidenceId"] and dep["viewpointId"]:
            viewpoint_links[dep["evidenceId"]].append({
                "id": dep["viewpointId"],
                "field": dep["targetField"],
                "relation": dep["relation"],
                "phase": dep["phase"],
            })
    for vp in viewpoints:
        for item in vp["supportingEvidence"] + vp["counterEvidence"]:
            ev_id = item.get("evidence_id")
            if ev_id:
                key = (vp["id"], "current_viewpoint", item.get("relation", ""), "当前版本")
                existing = {(x["id"], x["field"], x["relation"], x["phase"]) for x in viewpoint_links[ev_id]}
                if key not in existing:
                    viewpoint_links[ev_id].append({"id": vp["id"], "field": "current_viewpoint", "relation": item.get("relation", ""), "phase": "当前版本"})

    disagreement_links = defaultdict(list)
    for dis in t1_disagreements:
        for branch_key in ("branch_a", "branch_b"):
            branch = dis.get(branch_key, {})
            for ev_id in branch.get("evidence_ids", []):
                disagreement_links[ev_id].append({"id": dis["disagreement_id"], "branch": branch_key})

    for ev in evidence:
        ev["hypothesisLinks"] = hypothesis_links.get(ev["id"], [])
        ev["viewpointLinks"] = viewpoint_links.get(ev["id"], [])
        ev["disagreementLinks"] = disagreement_links.get(ev["id"], [])

    process = [
        {"phase": "P0", "title": "协议与数据骨架", "what": "固定案例问题、T0/T1时间窗、证据字段、人工门和不可回流规则。", "gate": "无", "artifact": "军用人工智能案例研究协议_Luna可执行版.md", "status": "完成"},
        {"phase": "P1–P2", "title": "T0来源搜集与人工筛选", "what": "按战略治理、项目资源、试验结果、部署应用等类型搜集；人工确定纳入与排除。", "gate": "作者筛选", "artifact": "P1/P1B/P1C；P2-20260825T154432Z-author-pilot", "status": "完成"},
        {"phase": "P3 + 纠错链", "title": "T0证据抽取、语义筛查与恢复", "what": "经历P3Q、P3B、P3BR、P3BR2、P3BR2A、P3S、P3SH、P3SR等质量修复，形成可冻结Evidence。", "gate": "G1-T0", "artifact": "G1-T0 formal + C1 + supplement", "status": "完成"},
        {"phase": "P4", "title": "冻结T0证据快照", "what": "冻结85条逻辑Evidence，后续T1材料不得回流改写。", "gate": "G1-T0签核", "artifact": "SNAP-T0-US-MILITARY-AI-C6C812108AF95309", "status": "完成"},
        {"phase": "P5–G2", "title": "候选假设生成、去重与冻结", "what": "多路生成后经作者归并为3项同层级竞争性假设。", "gate": "G2", "artifact": "G2-T0-1B8D6A450720F041-formal-v1", "status": "完成"},
        {"phase": "P7–P8", "title": "5视角×3假设形成15条观点", "what": "战略、技术、工程、任务、可信治理视角独立评价并接受证据指向式质疑；形成94条字段级依赖。", "gate": "观点准入", "artifact": "P7R-P9R-20260827T020335Z-case-strengthening-v1", "status": "完成"},
        {"phase": "P9–G3", "title": "T0分歧诊断与监测转化", "what": "把高影响分歧转为判别性需求、指标与触发条件。", "gate": "G3-T0", "artifact": "G3-T0-20260827T-formal-offline-v5", "status": "完成"},
        {"phase": "G4-T0", "title": "T0条件化报告签发", "what": "签发主判断：任务分布更可观察，约束解释不均衡，体系整合保留为待验证方向。", "gate": "G4-T0", "artifact": "G4R_CASE_REVISION + AUTHORIZATION-G4-AUDIT-C1", "status": "完成"},
        {"phase": "P11–P12", "title": "解封T1并新增40条正式证据", "what": "只收集2025-01-01至2026-06-30材料，完成T1证据审核；累计快照125项。", "gate": "G1-T1", "artifact": "G1-T1-P13-P16-99A723BFBC6D273A-formal-v1", "status": "完成"},
        {"phase": "P13–P14", "title": "变化识别与字段级影响定位", "what": "40条新增证据中38条成为变化信号，生成46条Evidence→观点字段影响边。", "gate": "影响范围复核", "artifact": "P14_IMPACT_LOCALIZATION", "status": "完成"},
        {"phase": "P15R", "title": "六条观点定向追加版本", "what": "仅更新受影响的6条观点，9条保持原版本；保留before/after和新增证据依赖。", "gate": "模型复评+后续作者签核", "artifact": "P15R-P16R-G4QR-T1...v5", "status": "完成"},
        {"phase": "P16R-C1", "title": "分歧重映射与语义纠正", "what": "将T1证据重新绑定到最新观点，形成3项分歧、3项需求、4项指标和8条触发规则。", "gate": "关键分歧裁决", "artifact": "P16R-C1-G4QR-C1-PAPER-CASE-C1...", "status": "完成"},
        {"phase": "G3/G4-T1", "title": "最终作者签核与论文案例包", "what": "三项正式分歧获作者裁决，签发T1条件化判断并生成论文图表与追溯表。", "gate": "作者整体签核", "artifact": "FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-20260827T112444Z-v1", "status": "正式"},
    ]

    nature_counts = Counter(ev["nature"] or "unknown" for ev in evidence)
    grade_counts = Counter(ev["grade"] or "unknown" for ev in evidence)
    source_counts = Counter((ev["snapshot"], ev["sourceId"]) for ev in evidence)
    dependency_relation_counts = Counter(dep["relation"] or "unknown" for dep in dependencies)
    dependency_field_counts = Counter(dep["targetField"] or "unspecified" for dep in dependencies)

    data = {
        "meta": {
            "caseId": "us_military_ai_deployment",
            "title": "美国军用人工智能 T0—T1 动态预估案例全过程",
            "t0Window": "2022-01-01—2024-12-31",
            "t1Window": "2025-01-01—2026-06-30",
            "t0SnapshotId": "SNAP-T0-US-MILITARY-AI-C6C812108AF95309",
            "t1SnapshotId": "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A",
            "formalRunId": "FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-20260827T112444Z-v1",
            "signedAt": "2026-08-27T11:24:44.914099+00:00",
            "reviewer": "author_01",
            "reviewMode": "AI预审与复评 + 作者整体签核 + 重点分歧裁决",
            "auditCaveat": "最终包的作者签核不表示逐条重读全部原始资料，也不表示完成逐页原始来源视觉复核。",
        },
        "metrics": {
            "t0Evidence": 85,
            "t1NewEvidence": 40,
            "t1CumulativeEvidence": 125,
            "t1ChangeSignals": 38,
            "t0Dependencies": len(t0_dependencies),
            "t1ImpactEdges": len(t1_impacts),
            "viewpoints": len(viewpoints),
            "updatedViewpoints": sum(1 for v in viewpoints if v["updateDecision"] == "update"),
            "retainedViewpoints": sum(1 for v in viewpoints if v["updateDecision"] != "update"),
            "disagreements": len(t1_disagreements),
            "needs": len(needs),
            "indicators": len(indicators),
            "triggers": len(triggers),
            "grades": dict(sorted(grade_counts.items())),
            "natures": dict(sorted(nature_counts.items())),
            "sourcesT0": len({source for (snap, source) in source_counts if snap == "T0"}),
            "sourcesT1": len({source for (snap, source) in source_counts if snap == "T1"}),
            "dependencyRelations": dict(sorted(dependency_relation_counts.items())),
            "dependencyFields": dict(sorted(dependency_field_counts.items())),
        },
        "conclusions": {
            "t0": "任务需求牵引的分散试验、技术演示和局部部署，是T0更直接可观察的能力形成形态；资源与治理约束解释发展不均衡；体系整合作为待跨军种实际复用验证的条件性备择方向。",
            "t1": "截至2026年6月30日，美国军用人工智能呈现局部任务应用和组织化试验扩展、部门级体系整合意图增强并存的状态；公开证据仍不足以确认跨军种、跨任务的通用能力体系已经形成，资源、数据、作战化验证和可信治理条件继续造成不均衡。",
            "revision": "T1没有推翻T0，而是形成‘局部扩展信号增强、体系整合仍待实际复用验证、约束条件进一步显化’的条件化修订。",
            "strengthenIntegrationIf": "多个军种实际迁移或复用同一平台、共享数据流程，并由独立材料确认约束缓解伴随任务范围扩大。",
            "retainDistributedIf": "新增材料仍主要是单点演示、项目自报、竞赛结果或局部使用。",
        },
        "process": process,
        "evidence": evidence,
        "hypotheses": hypotheses,
        "viewpoints": viewpoints,
        "dependencies": dependencies,
        "disagreementsT0": t0_disagreements,
        "disagreementsT1": t1_disagreements,
        "disagreementLineage": lineage,
        "needs": needs,
        "indicators": indicators,
        "triggers": triggers,
        "audit": {
            "invalidEvidenceReferences": 0,
            "excludedEvidenceReferences": 0,
            "historicalChangeSignalReferences": 0,
            "semanticNatureMismatches": 0,
            "humanSourceVisualReviewCount": 0,
            "modelApiCallsAtFinalSigning": 0,
            "formalEvidenceInsertedAtFinalSigning": 0,
            "formalViewpointsInsertedAtFinalSigning": 0,
            "syntheticRunsExcluded": True,
            "note": "phase-a-demo与synthetic_military_ai仅用于流程测试，未混入正式案例证据与结论。",
        },
    }

    out = ROOT / "tmp/case_analysis_data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "size": out.stat().st_size,
        "evidence": len(evidence),
        "viewpoints": len(viewpoints),
        "dependencies": len(dependencies),
        "t0_disagreements": len(t0_disagreements),
        "t1_disagreements": len(t1_disagreements),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
