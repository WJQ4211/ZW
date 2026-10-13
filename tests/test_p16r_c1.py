from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs" / "P16R-C1-G4QR-C1-PAPER-CASE-C1-20260827T100034Z-v1"
SNAPSHOT_ID = "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A"
EXCLUDED = {
    "CE-T1-6B7DB29A3B242353",
    "CE-T1-1797A97EF532F332",
    "CE-T1-1DB188A712B4C1C6",
    "CE-T1-9D61AF59EFDD3694",
    "CE-T1-4DB05E964D50AC61",
    "CE-T1-3DC907538A406628",
}
HISTORICAL = {"CE-T1-7DE738404CD33807", "CE-T1-C1E0527174B2F72F"}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_c1_manifest_semantic_correction_and_offline_boundary() -> None:
    manifest = json.loads((RUN / "p16r_c1_manifest.json").read_text(encoding="utf-8"))
    correction = json.loads((RUN / "manifest_semantic_correction.json").read_text(encoding="utf-8"))
    assert correction["original_field"] == "historical_artifacts_modified"
    assert correction["original_value"] is True
    assert correction["original_expression"] == "protected_before == protected_after"
    assert correction["corrected_historical_artifacts_modified"] is False
    assert correction["historical_files_modified_count"] == 0
    assert manifest["model_api_calls"] == 0
    assert manifest["database_writes"] == 0
    assert manifest["viewpoint_versions_appended"] == 0
    assert manifest["formal_g3_t1_executed"] is False
    assert manifest["formal_g4_t1_signed"] is False
    assert manifest["t1_snapshot_item_count"] == 125
    assert manifest["historical_artifacts_modified"] is False
    assert manifest["invalid_reference_count"] == 0
    assert manifest["semantic_mismatch_count"] == 0
    assert manifest["api_key_written"] is False


def test_c1_disagreements_and_monitoring_foreign_keys_are_valid() -> None:
    disagreements = _jsonl(RUN / "P16R_C1" / "candidate_disagreements_c1.jsonl")
    needs = _jsonl(RUN / "P16R_C1" / "discriminative_needs_c1.jsonl")
    indicators = _jsonl(RUN / "P16R_C1" / "monitor_indicators_c1.jsonl")
    triggers = _jsonl(RUN / "P16R_C1" / "trigger_rules_c1.jsonl")
    assert len(disagreements) == 3
    assert len(needs) == 3
    assert len(indicators) == 4
    assert len(triggers) == 8
    assert [row["resolution_action"] for row in disagreements] == ["conditional_retain", "monitor", "scope_split"]
    assert {row["type"] for row in disagreements} == {"mechanism", "risk_weight", "scope"}
    disagreement_ids = {row["disagreement_id"] for row in disagreements}
    need_ids = {row["need_id"] for row in needs}
    indicator_ids = {row["indicator_id"] for row in indicators}
    conn = sqlite3.connect(ROOT / "data" / "research.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        current_views = {
            f"{row['viewpoint_id']}:{row['version']}"
            for row in conn.execute("SELECT viewpoint_id, MAX(version) AS version FROM viewpoint GROUP BY viewpoint_id")
        }
        snapshot_evidence = {
            row[0]
            for row in conn.execute("SELECT evidence_id FROM snapshot_items WHERE snapshot_id=?", (SNAPSHOT_ID,))
        }
    finally:
        conn.close()
    for disagreement in disagreements:
        assert set(disagreement["viewpoint_refs"]).issubset(current_views)
        for branch in (disagreement["branch_a"], disagreement["branch_b"]):
            assert set(branch["viewpoint_refs"]).issubset(current_views)
            assert set(branch["evidence_ids"]).issubset(snapshot_evidence)
            assert not set(branch["evidence_ids"]) & EXCLUDED
            assert not set(branch["evidence_ids"]) & HISTORICAL
    for need in needs:
        assert need["disagreement_id"] in disagreement_ids
    for indicator in indicators:
        assert indicator["need_id"] in need_ids
        assert set(indicator["affected_viewpoint_ids"]).issubset(current_views)
    for trigger in triggers:
        assert trigger["indicator_id"] in indicator_ids
        assert trigger["human_approval_required"] is True
        assert set(trigger["affected_viewpoint_ids"]).issubset(current_views)


def test_c1_paper_and_figure_outputs_meet_quality_boundary() -> None:
    paper = (RUN / "PAPER_CASE_C1" / "paper_case_section_v4.md").read_text(encoding="utf-8")
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", paper))
    assert 2500 <= chinese_count <= 3200
    assert all(text in paper for text in ("卢克基地", "eJARVIS", "SABER", "三项分歧", "38条变化信号", "46条影响边"))
    figures = RUN / "PAPER_CASE_C1" / "figures"
    assert (figures / "figure_2_t0_t1_evidence_structure_v2.png").exists()
    assert (figures / "figure_2_t0_t1_evidence_structure_v2.svg").exists()
    assert (figures / "figure_5_disagreement_lifecycle_v2.png").exists()
    assert (figures / "figure_5_disagreement_lifecycle_v2.svg").exists()
    assert len(list(figures.glob("figure_*.png"))) == 6
    assert len(list(figures.glob("figure_*.svg"))) == 6
    assert (RUN / "PAPER_CASE_C1" / "contact_sheet_v2.png").exists()
    qc = (RUN / "PAPER_CASE_C1" / "visual_qc_report_v2.md").read_text(encoding="utf-8")
    assert "结论：PASS" in qc
    assert "300 DPI" in qc


def test_c1_output_hashes_and_database_hash_are_consistent() -> None:
    manifest = json.loads((RUN / "p16r_c1_manifest.json").read_text(encoding="utf-8"))
    mismatches = []
    for relative, expected in manifest["output_file_hashes"].items():
        path = RUN / relative
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatches.append(relative)
    assert mismatches == []
    assert manifest["database_hash_before"] == manifest["database_hash_after"]
    assert manifest["historical_artifacts_unchanged"] is True
