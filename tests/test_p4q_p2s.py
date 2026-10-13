"""Acceptance tests for the P4Q gate audit and P2S source preparation."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs" / "P4Q-P2S-20260826T082341Z-v1"
CASE_ID = "us_military_ai_deployment"
TARGET_ID = "EV-T0-3D9421B5DCC381FC"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_p4q_gate_counts_and_block_reason():
    manifest = read_json(RUN_DIR / "p4q_preflight_manifest.json")
    p4q = manifest["p4q"]
    assert p4q["canonical_logical_evidence_count"] == 60
    assert p4q["sqlite_physical_t0_evidence_rows"] == 61
    assert p4q["sqlite_latest_version_logical_evidence_count"] == 60
    assert p4q["canonical_latest_field_difference_count"] == 0
    assert p4q["target_versions_in_sqlite"] == [1, 2]
    assert p4q["target_entries_in_canonical"] == [{"version": 2, "evidence_nature": "resource_input"}]
    assert p4q["formal_evidence_source_count"] == 9
    assert p4q["a_grade_fragment_count"] == 31
    assert p4q["b_grade_fragment_count"] == 29
    assert p4q["d_grade_fragment_count"] == 0
    assert p4q["a_grade_fragment_ratio"] == 0.516667
    assert p4q["published_after_2024_12_31_count"] == 0
    assert p4q["snapshot_count"] == 0
    assert p4q["snapshot_items_count"] == 0
    assert manifest["p4_gate_open"] is False
    assert manifest["p4_executed"] is False
    assert manifest["p3s_executed"] is False
    assert manifest["g1_supplement_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["model_api_calls"] == 0
    assert manifest["t1_read"] is False
    assert p4q["p4_block_reason"] == "A级正式证据片段占比51.67%，低于60%"


def test_p4q_canonical_is_directly_reconciled_to_sqlite():
    canonical = [
        json.loads(line)
        for line in (RUN_DIR / ".." / "G1-T0-C1-31F142A5227EA580-correction-v1" / "g1_t0_c1_latest_evidence_canonical.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag=? ORDER BY evidence_id, version",
            (CASE_ID, "T0"),
        ).fetchall()
    finally:
        conn.close()
    latest: dict[str, tuple[int, dict]] = {}
    for evidence_id, version, payload_json in rows:
        if evidence_id not in latest or version > latest[evidence_id][0]:
            latest[evidence_id] = (version, json.loads(payload_json))
    canonical_ids = {item["evidence_id"] for item in canonical}
    assert {item["evidence_id"]: item for item in canonical} == {key: value for key, (_, value) in latest.items() if key in canonical_ids}
    assert [item["evidence_id"] for item in canonical] == sorted(item["evidence_id"] for item in canonical)
    assert canonical[[item["evidence_id"] for item in canonical].index(TARGET_ID)]["version"] == 2


def test_p2s_has_five_verified_original_sources_and_no_evidence_side_effects():
    manifest = read_json(RUN_DIR / "p2s_manifest.json")
    rows = read_csv(RUN_DIR / "p2s_source_manifest.csv")
    captures = read_csv(RUN_DIR / "p2s_source_capture_log.csv")
    assert manifest["source_count"] == 5
    assert manifest["source_ids"] == ["T0-SP-001", "T0-TD-001", "T0-TD-002", "T0-TD-003", "T0-TD-005"]
    assert manifest["verified_original_page_count"] == 5
    assert manifest["blocked_original_page_count"] == 0
    assert manifest["p3s_eligible_count"] == 5
    assert manifest["p3s_executed"] is False
    assert manifest["formal_evidence_created"] == 0
    assert manifest["evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["model_api_calls"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert len(rows) == len(captures) == 5
    assert all(row["source_grade"] == "A" for row in rows)
    assert all(row["verification_status"] == "verified_original_page_html" for row in rows)
    assert all(row["p2s_review_decision"] == "pending_author_review" for row in rows)
    assert all(row["formal_evidence_created"] == "0" for row in rows)
    assert all(row["http_status"] == "200" for row in captures)
    assert all(row["published_at_verified"] == "true" for row in captures)
    assert all(row["blocked_original_page"] == "false" for row in captures)

    for row in rows:
        raw = ROOT / row["raw_path"]
        text = ROOT / row["text_path"]
        assert raw.exists() and text.exists()
        assert sha256(raw) == row["raw_sha256"]
        assert sha256(text) == row["text_sha256"]
        assert text.read_text(encoding="utf-8").strip()


def test_p2s_coverage_and_protected_outputs():
    manifest = read_json(RUN_DIR / "p2s_manifest.json")
    coverage = read_csv(RUN_DIR / "p2s_coverage_gap_matrix.csv")
    assert len(coverage) >= 10
    assert {row["source_id"] for row in coverage} == set(manifest["source_ids"])
    assert all(row["evidence_created"] == "0" for row in coverage)
    assert all(row["p3s_eligibility"] == "eligible_for_p3s_review" for row in coverage)
    assert manifest["selection_reason"].startswith("均为A级官方一手来源")
    assert "最小样本控制" in manifest["selection_reason"]
    assert manifest["output_file_hashes"]
    assert manifest["manifest_self_hash_excluded_from_output_file_hashes"] is True

    p4q_report = (RUN_DIR / "p4q_quality_gate_report.md").read_text(encoding="utf-8")
    assert "p4_gate_open=false" in p4q_report
    assert "A级正式证据片段占比51.67%，低于60%" in p4q_report
    assert "未抽取Evidence" in p4q_report
