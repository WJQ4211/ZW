from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs" / "P11R-P12R-G1Q-T1-20260827T065600Z-v1"
OLD = ROOT / "outputs" / "G4-P11-P12-20260827T051332Z-v1"


def _csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_recovery_has_three_verified_sources_and_source_hashes():
    rows = _csv(RUN / "supplemental_source_inventory.csv")
    assert {row["source_id"] for row in rows} == {"T1-DARPA-001", "T1-DARPA-003", "T1-IR-001"}
    assert all(row["case_id"] == "us_military_ai_deployment" for row in rows)
    assert all(row["full_text_status"] == "verified_full_text" for row in rows)
    assert all(row["raw_sha256"] and row["text_sha256"] for row in rows)
    for row in rows:
        assert (ROOT / row["raw_path"]).exists()
        text_path = ROOT / row["text_path"]
        assert text_path.exists()
        assert row["text_sha256"] == __import__("hashlib").sha256(text_path.read_bytes()).hexdigest()
    assert any(row["pmcid"] == "PMC12108871" and row["doi"] == "10.3390/bioengineering12050519" for row in rows)


def test_only_incremental_sources_were_sent_to_model_and_candidates_are_locatable():
    candidates = _jsonl(RUN / "supplemental_evidence_candidates.jsonl")
    old = _jsonl(OLD / "P12_T1_EXTRACTION" / "t1_evidence_candidates.jsonl")
    old_ids = {row["candidate_evidence_id"] for row in old}
    assert len(candidates) == 16
    assert len({row["candidate_evidence_id"] for row in candidates}) == 16
    assert not old_ids.intersection(row["candidate_evidence_id"] for row in candidates)
    assert {row["source_id"] for row in candidates} == {"T1-DARPA-001", "T1-DARPA-003", "T1-IR-001"}
    anchor_text = (RUN / "P12R_T1_EXTRACTION" / "paragraph_anchor_map.jsonl").read_text(encoding="utf-8")
    for row in candidates:
        assert row["excerpt_original"]
        assert row["excerpt_zh"]
        assert row["normalized_claim"]
        assert row["evidence_nature"] in {"goal", "governance_event", "resource_input", "test_event", "test_result", "deployment_event", "constraint", "evaluation"}
        assert row["source_locator"] in anchor_text
        assert row["extraction_run_id"] == RUN.name
    assert all("T0" not in path.read_text(encoding="utf-8", errors="ignore") for path in (RUN / "prompts" / "P12R").glob("*.txt"))


def test_offline_correction_log_contains_requested_scope_corrections():
    rows = {row["candidate_evidence_id"]: row for row in _csv(RUN / "before_after_correction_log.csv")}
    assert len(rows) == 30
    assert rows["CE-T1-6B7DB29A3B242353"]["combined_status"] == "proposed_exclude"
    assert rows["CE-T1-1797A97EF532F332"]["combined_status"] == "proposed_exclude"
    assert rows["CE-T1-7DE738404CD33807"]["combined_status"] == "historical_context_not_t1_delta"
    assert rows["CE-T1-7DE738404CD33807"]["t1_change_signal_eligible"].lower() == "false"
    dash_after = json.loads(rows["CE-T1-7AB164824EAAD82A"]["after"])
    mash_after = json.loads(rows["CE-T1-6B0D6E320A21509E"]["after"])
    assert dash_after["normalized_claim"].startswith("据官方披露的初步结果")
    assert mash_after["normalized_claim"].startswith("据参与者报告")
    aa_after = json.loads(rows["CE-T1-AA937CC0AF5E2372"]["after"])
    assert "卢克空军基地部分人员" in aa_after["normalized_claim"]
    d0_after = json.loads(rows["CE-T1-D0E7B9B8CC1298AB"]["after"])
    assert d0_after["coding_dimensions"] == ["organizational_system_fit"]


def test_author_template_is_unreviewed_and_all_final_fields_are_empty():
    rows = _csv(RUN / "combined_g1_t1_author_decision_template.csv")
    assert len(rows) == 46
    assert sum(row["lineage"] == "prior_p12_v1" for row in rows) == 30
    assert sum(row["lineage"] == "p12r_supplemental_real_api" for row in rows) == 16
    for row in rows:
        assert row["ai_preannotation"].lower() == "true"
        assert row["human_reviewed"].lower() == "false"
        for field in ("author_decision", "final_decision", "final_nature", "final_dimensions", "final_normalized_claim", "human_corrected_excerpt", "final_excerpt_zh", "reviewer", "reviewed_at", "review_note"):
            assert row[field] == ""


def test_recovery_boundary_and_database_unchanged():
    manifest = json.loads((RUN / "p11r_p12r_manifest.json").read_text(encoding="utf-8"))
    assert manifest["p12r_model_call_count"] == 6
    assert manifest["p12r_extraction_logical_call_count"] == 3
    assert manifest["p12r_semantic_logical_call_count"] == 3
    assert manifest["formal_g1_t1_executed"] is False
    assert manifest["evidence_inserted"] == 0
    assert manifest["t1_snapshot_frozen"] is False
    assert manifest["impact_localization_executed"] is False
    assert manifest["viewpoint_update_executed"] is False
    assert manifest["api_key_written"] is False
    assert manifest["historical_input_unchanged"] is True
    assert manifest["database_counts_before"] == manifest["database_counts_after"]
    assert all("sk-sp-" not in path.read_text(encoding="utf-8", errors="ignore") for path in RUN.rglob("*") if path.is_file())
    with sqlite3.connect(f"file:{ROOT / 'data' / 'research.sqlite3'}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", ("us_military_ai_deployment",)).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND phase='T1'", ("us_military_ai_deployment",)).fetchone()[0] == 1
