from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from src.formal_g3_g4_t1 import CASE_ID, DB_PATH, ROOT, T1_SNAPSHOT_ID


def _run_dir() -> Path:
    runs = sorted(ROOT.glob("outputs/FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-*/final_case_manifest.json"))
    assert runs, "formal G3/G4 run output is missing"
    return runs[-1].parent


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_final_manifest_and_formal_counts() -> None:
    manifest = json.loads((_run_dir() / "final_case_manifest.json").read_text(encoding="utf-8"))
    expected = {
        "formal_g3_t1_executed": True,
        "formal_g4_t1_signed": True,
        "formal_disagreement_inserted": 3,
        "formal_need_inserted": 3,
        "formal_indicator_inserted": 4,
        "formal_trigger_inserted": 8,
        "disagreement_lineage_inserted": 3,
        "g3_review_record_inserted": 1,
        "g4_review_record_inserted": 1,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "viewpoint_inserted": 0,
        "dependency_inserted": 0,
        "snapshot_refrozen": False,
        "t1_snapshot_item_count": 125,
        "invalid_reference_count": 0,
        "model_api_calls": 0,
        "api_key_written": False,
        "final_paper_case_generated": True,
    }
    for key, value in expected.items():
        assert manifest[key] == value
    assert manifest["rollback_test_passed"] is True
    assert manifest["idempotency_test_passed"] is True
    assert manifest["protected_history_hash_unchanged"] is True


def test_formal_objects_and_lineage_are_complete() -> None:
    run = _run_dir()
    disagreements = _jsonl(run / "FINAL_PAPER_CASE" / "formal_g3_t1_disagreements.jsonl")
    needs = _jsonl(run / "FINAL_PAPER_CASE" / "formal_g3_t1_needs.jsonl")
    indicators = _jsonl(run / "FINAL_PAPER_CASE" / "formal_g3_t1_indicators.jsonl")
    triggers = _jsonl(run / "FINAL_PAPER_CASE" / "formal_g3_t1_triggers.jsonl")
    lineage = _jsonl(run / "FINAL_PAPER_CASE" / "t0_t1_disagreement_lineage.jsonl")
    assert {row["disagreement_id"] for row in disagreements} == {"DISAG-T1R-C1-01", "DISAG-T1R-C1-02", "DISAG-T1R-C1-03"}
    assert {row["need_id"] for row in needs} == {"NEED-T1R-C1-01", "NEED-T1R-C1-02", "NEED-T1R-C1-03"}
    assert {row["indicator_id"] for row in indicators} == {"IND-T1R-C1-01", "IND-T1R-C1-02", "IND-T1R-C1-03", "IND-T1R-C1-04"}
    assert {row["trigger_id"] for row in triggers} == {f"TRG-T1R-C1-{i:02d}" for i in range(1, 9)}
    assert len(lineage) == 3
    assert {row["parent_disagreement_id"] for row in lineage} == {"DISAG-T0-01", "DISAG-T0-02", "DISAG-T0-03"}
    assert {row["child_disagreement_id"] for row in lineage} == {"DISAG-T1R-C1-01", "DISAG-T1R-C1-02", "DISAG-T1R-C1-03"}
    assert all(row["lineage_relation"] == "evolved_from" for row in lineage)


def test_database_boundaries_and_foreign_keys() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()[0] == 125
        assert conn.execute("SELECT COUNT(*) FROM disagreement WHERE disagreement_id LIKE 'DISAG-T1R-C1-%'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM discriminative_need WHERE need_id LIKE 'NEED-T1R-C1-%'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM monitor_indicator WHERE indicator_id LIKE 'IND-T1R-C1-%'").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM trigger_rule WHERE trigger_id LIKE 'TRG-T1R-C1-%'").fetchone()[0] == 8
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, column in (("evidence", "evidence_id"), ("hypothesis", "hypothesis_id"), ("viewpoint", "viewpoint_id"), ("evidence_dependency", "edge_id")):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    finally:
        conn.close()


def test_authorization_and_paper_boundary() -> None:
    run = _run_dir()
    auth = (run / "authorization.txt").read_bytes()
    manifest = json.loads((run / "final_case_manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(auth.rstrip(b"\n")).hexdigest() == manifest["authorization_sha256"]
    assert "当前状态：`signed`" in (run / "FINAL_PAPER_CASE" / "g4_t1_final_report.md").read_text(encoding="utf-8")
    paper = (run / "FINAL_PAPER_CASE" / "paper_case_section_v5_final.md").read_text(encoding="utf-8")
    assert "已完成作者签核" in paper
    assert "尚未执行" not in paper
    assert "尚未签发" not in paper
    assert all("sk-sp-" not in p.read_text(encoding="utf-8", errors="ignore") for p in run.rglob("*") if p.is_file())


def test_final_figures_have_svg_png_pairs() -> None:
    fig_dir = _run_dir() / "FINAL_PAPER_CASE" / "figures"
    pngs = {p.stem.removesuffix("_final") for p in fig_dir.glob("*.png")}
    svgs = {p.stem.removesuffix("_final") for p in fig_dir.glob("*.svg")}
    assert len(pngs) == 6
    assert pngs == svgs
