from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from src.g4r_case_revision import ROOT


RUN = ROOT / "outputs" / "G4R_CASE_REVISION-20260827T040000Z-v3"
SOURCE = ROOT / "outputs" / "G3-T0-20260827T-formal-offline-v5"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_revision_manifest_is_offline_and_read_only() -> None:
    manifest = json.loads((RUN / "g4r_case_revision_manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
    assert manifest["source_g3_run"] == "G3-T0-20260827T-formal-offline-v5"
    assert manifest["frozen_evidence_count"] == 85
    assert manifest["source_grade_counts"] == {"A": 56, "B": 29}
    assert manifest["viewpoint_count"] == 15
    assert manifest["dependency_count"] == 94
    assert manifest["conditional_disagreement_count"] == 2
    assert manifest["resolved_scope_disagreement_count"] == 1
    assert manifest["discriminative_need_count"] == 3
    assert manifest["monitor_indicator_count"] == 4
    assert manifest["trigger_rule_count"] == 8
    assert manifest["model_api_calls"] == 0
    assert manifest["database_writes"] == 0
    assert manifest["formal_g3_modified"] is False
    assert manifest["formal_g4_t0_executed"] is False
    assert manifest["t1_read"] is False
    assert manifest["api_key_written"] is False
    assert manifest["historical_artifacts_unchanged"] is True
    assert manifest["g4_status"] == "draft_pending_author_signoff"


def test_report_contains_representative_facts_and_current_boundaries() -> None:
    report = (RUN / "G4_DRAFT" / "g4_t0_report_draft_v2.md").read_text(encoding="utf-8")
    for evidence_id in [
        "EV-T0-0A73030BBF2649F9", "EV-T0-7E37FBD511FC654E", "EV-T0-51D65DEC82584BCD", "EV-T0-A515E3B9391C51B4",
        "EV-T0-DB4536B8270ECF58", "EV-T0-DDA30BF3C0C38B0C", "EV-T0-9C79EF2837BC80A2",
        "EV-T0-A8459E6E8776A15D", "EV-T0-F0773C8E2ECB5C8D", "EV-T0-AD4CCAC18E1BB26C", "EV-T0-62B1ECDA7DF87F1A", "EV-T0-8E35B508DF5C2E65", "EV-T0-0405886E04C205A6", "EV-T0-FB39056D1BA1F579",
    ]:
        assert evidence_id in report
    assert "2025年1月1日至2026年6月30日" in report
    assert "两项条件性分歧" in report
    assert "正式G3已经完成" in report
    assert "等待正式G3后续审核" not in report
    assert "计划目标" in report and "演习事件" in report


def test_traceability_has_evidence_and_viewpoints_for_syntheses() -> None:
    rows = list(csv.DictReader((RUN / "G4_DRAFT" / "g4_claim_evidence_traceability_v2.csv").read_text(encoding="utf-8-sig").splitlines()))
    by_id = {row["claim_id"]: row for row in rows}
    for claim_id in ["G4-C02", "G4-C03"]:
        row = by_id[claim_id]
        assert row["evidence_ids"] not in {"", "[]"}
        assert row["viewpoint_refs"] not in {"", "[]"}
        assert row["inference_level"] == "conditional_synthesis"
    assert "viewpoint_refs" in rows[0]
    assert "disagreement_refs" in rows[0]
    assert "paper_usage" in rows[0]


def test_paper_metrics_and_revision_examples() -> None:
    paper = (RUN / "PAPER_CASE" / "paper_case_section_draft_v2.md").read_text(encoding="utf-8")
    manifest = json.loads((RUN / "g4r_case_revision_manifest.json").read_text(encoding="utf-8"))
    assert 3500 <= len(paper) <= 4500
    assert len(paper) == manifest["paper_character_count"]
    for phrase in ["HYP-T0-01", "HYP-T0-02", "HYP-T0-03", "五个专业视角", "94条字段级依赖", "3项判别性需求", "4项指标", "8条触发规则", "不是投票结果"]:
        assert phrase in paper
    table = (RUN / "PAPER_CASE" / "table_4_viewpoint_revision_examples_v2.csv").read_text(encoding="utf-8-sig")
    assert "VP-T0-STRAT-H01" in table and "VP-T0-STRAT-H02" in table and "VP-T0-TECH-H02" in table and "VP-T0-TECH-H01" in table


def test_revised_figures_and_visual_qc() -> None:
    figure_dir = RUN / "FIGURES"
    assert len(list(figure_dir.glob("figure_*.png"))) == 5
    assert len(list(figure_dir.glob("figure_*.svg"))) == 5
    assert (figure_dir / "contact_sheet.png").exists()
    qc = (figure_dir / "visual_qc_report_v2.md").read_text(encoding="utf-8")
    assert "总体验收：PASS" in qc
    svg3 = (figure_dir / "figure_3_evidence_viewpoint_dependency.svg").read_text(encoding="utf-8")
    assert "94条字段级依赖" in svg3
    assert "Dependencies" not in svg3
    svg4 = (figure_dir / "figure_4_disagreement_to_monitoring.svg").read_text(encoding="utf-8")
    assert "范围拆分解决" in svg4
    svg5 = (figure_dir / "figure_5_viewpoint_governance.svg").read_text(encoding="utf-8")
    assert "现有公开证据未显示复用" in svg5
    for path in figure_dir.glob("figure_*.svg"):
        assert "�" not in path.read_text(encoding="utf-8")


def test_g4_signoff_is_empty_and_source_g3_unchanged() -> None:
    row = next(csv.DictReader((RUN / "G4_DRAFT" / "g4_author_review_template_v2.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert all(value == "" for value in row.values())
    manifest = json.loads((RUN / "g4r_case_revision_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_draft_sha256"] == _sha256(SOURCE / "G4_DRAFT" / "g4_t0_report_draft.md")
    assert _sha256(RUN / "_input_original_g4_draft.md") == manifest["source_draft_sha256"]
    assert all("sk-sp-" not in path.read_text(encoding="utf-8", errors="ignore") for path in RUN.rglob("*") if path.is_file())
