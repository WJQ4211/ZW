from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs" / "G4-P11-P12-20260827T051332Z-v1"


def _csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))


def test_g4_signed_before_blind_t1_collection():
    g4 = json.loads((RUN / "G4_FORMAL" / "g4_t0_signoff_manifest.json").read_text(encoding="utf-8"))
    p11 = json.loads((RUN / "P11_T1_COLLECTION" / "p11_t1_manifest.json").read_text(encoding="utf-8"))
    assert g4["g4_signed"] is True
    assert g4["t1_unsealed"] is True
    assert p11["blind_collection_verified"] is True
    assert p11["t0_hypotheses_provided_to_collection"] is False
    assert p11["t0_viewpoints_provided_to_collection"] is False
    assert p11["t0_disagreements_provided_to_collection"] is False
    assert p11["t0_monitoring_provided_to_collection"] is False
    assert p11["t0_main_judgment_provided_to_collection"] is False


def test_p11_sources_are_bound_and_inside_t1_window():
    rows = _csv(RUN / "P11_T1_COLLECTION" / "source_manifest_t1_candidate.csv")
    assert len(rows) == 12
    assert all(row["case_id"] == "us_military_ai_deployment" for row in rows)
    for row in rows:
        if row["full_text_status"] == "verified_full_text":
            assert row["raw_sha256"]
            assert row["text_sha256"]
            assert Path(ROOT / row["raw_path"]).exists()
            assert Path(ROOT / row["text_path"]).exists()
            assert date.fromisoformat(row["document_date"]) >= date(2025, 1, 1)
            assert date.fromisoformat(row["document_date"]) <= date(2026, 6, 30)
    assert any(row["category"] == "independent_review_research" for row in rows)


def test_search_summary_is_not_a_source_and_author_fields_are_empty():
    logs = _csv(RUN / "P11_T1_COLLECTION" / "search_log_t1.csv")
    assert len(logs) == 4
    assert all(row["search_result_is_source"].lower() == "false" for row in logs)
    rows = _csv(RUN / "G1_T1_PREP" / "g1_t1_author_decision_template.csv")
    assert rows
    for row in rows:
        assert row["ai_preannotation"].lower() == "true"
        assert row["human_reviewed"].lower() == "false"
        for field in ("author_decision", "final_nature", "final_dimensions", "human_corrected_excerpt", "final_excerpt_zh", "reviewer", "reviewed_at", "review_note"):
            assert row[field] == ""


def test_t1_candidate_quotes_are_locatable_and_database_unchanged():
    selected = {row["source_id"]: row for row in _csv(RUN / "P11_T1_COLLECTION" / "selected_source_inventory_t1.csv")}
    candidates = [json.loads(line) for line in (RUN / "P12_T1_EXTRACTION" / "t1_evidence_candidates.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert candidates
    assert all(candidate["snapshot_tag"] == "T1" for candidate in candidates)
    assert all(candidate["excerpt_original"] in (ROOT / selected[candidate["source_id"]]["text_path"]).read_text(encoding="utf-8") for candidate in candidates)
    assert all(candidate["excerpt_zh"] and candidate["normalized_claim"] for candidate in candidates)
    with sqlite3.connect(f"file:{ROOT / 'data' / 'research.sqlite3'}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", ("us_military_ai_deployment",)).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND phase='T1'", ("us_military_ai_deployment",)).fetchone()[0] == 1


def test_g1_t1_preparation_boundary_flags():
    root_manifest = json.loads((RUN / "p11_p12_manifest.json").read_text(encoding="utf-8"))
    assert root_manifest["evidence_inserted"] == 0
    assert root_manifest["formal_g1_t1_executed"] is False
    assert root_manifest["t1_snapshot_frozen"] is False
    assert root_manifest["impact_localization_executed"] is False
    assert root_manifest["viewpoint_update_executed"] is False
    assert root_manifest["api_key_written"] is False
    assert all("sk-sp-" not in path.read_text(encoding="utf-8", errors="ignore") for path in RUN.rglob("*") if path.is_file())
