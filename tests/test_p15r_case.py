from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs" / "P15R-P16R-G4QR-T1-PAPER-CASE-20260827T082433Z-v5"
DB = ROOT / "data" / "research.sqlite3"
VIEWPOINTS = {
    "VP-T0-OPS-H02": 4,
    "VP-T0-STRAT-H01": 3,
    "VP-T0-STRAT-H02": 3,
    "VP-T0-TECH-H02": 4,
    "VP-T0-TECH-H03": 4,
    "VP-T0-TRUST-H03": 4,
}


def test_p15r_manifest_and_stop_boundary() -> None:
    manifest = json.loads((RUN / "p15r_p16r_manifest.json").read_text(encoding="utf-8"))
    assert manifest["t1_snapshot_item_count"] == 125
    assert manifest["real_model_base_revision_calls"] == 6
    assert manifest["independent_audit_calls"] >= 2
    assert manifest["corrective_viewpoint_versions_appended"] == 6
    assert manifest["excluded_evidence_reference_count"] == 0
    assert manifest["invalid_evidence_reference_count"] == 0
    assert manifest["formal_g3_t1_executed"] is False
    assert manifest["formal_g4_t1_signed"] is False
    assert manifest["t1_snapshot_refrozen"] is False
    assert manifest["evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["api_key_written"] is False
    assert manifest["historical_artifacts_modified"] is True
    assert manifest["database_history_unchanged_excluding_new"] is True


def test_p15r_appends_expected_latest_viewpoint_versions_and_dependencies() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        for viewpoint_id, expected_version in VIEWPOINTS.items():
            row = conn.execute(
                "SELECT version, snapshot_id, payload_json FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1",
                (viewpoint_id,),
            ).fetchone()
            assert row is not None
            assert row["version"] == expected_version
            assert row["snapshot_id"] == "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A"
            payload = json.loads(row["payload_json"])
            assert payload["parent_version"] == expected_version - 1
            assert payload["human_verified"] is False
            dep_count = conn.execute(
                "SELECT COUNT(*) FROM evidence_dependency WHERE viewpoint_id=? AND viewpoint_version=?",
                (viewpoint_id, expected_version),
            ).fetchone()[0]
            assert dep_count > 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE snapshot_id=? AND frozen=1", ("SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A",)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", ("SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A",)).fetchone()[0] == 125
    finally:
        conn.close()


def test_p15r_p16r_integrity_and_paper_outputs() -> None:
    integrity = json.loads((RUN / "P16R" / "referential_integrity_report.json").read_text(encoding="utf-8"))
    assert integrity["invalid_evidence_reference_count"] == 0
    assert integrity["truncated_evidence_id_count"] == 0
    assert integrity["invalid_viewpoint_reference_count"] == 0
    assert integrity["invalid_need_foreign_key_count"] == 0
    assert integrity["invalid_indicator_foreign_key_count"] == 0
    assert integrity["invalid_trigger_foreign_key_count"] == 0
    all_p16 = "\n".join(path.read_text(encoding="utf-8") for path in (RUN / "P16R").glob("*.jsonl"))
    assert "CE-T1-AA937CC0AF5E237\"" not in all_p16
    excluded = ["CE-T1-6B7DB29A3B242353", "CE-T1-1797A97EF532F332", "CE-T1-1DB188A712B4C1C6", "CE-T1-9D61AF59EFDD3694", "CE-T1-4DB05E964D50AC61", "CE-T1-3DC907538A406628"]
    assert all(item not in all_p16 for item in excluded)
    figures = RUN / "PAPER_CASE_R" / "figures"
    assert len(list(figures.glob("*.png"))) == 6
    assert len(list(figures.glob("*.svg"))) == 6
    assert (RUN / "PAPER_CASE_R" / "contact_sheet.png").exists()
    assert "PAPER-CASE-R v3" in (RUN / "PAPER_CASE_R" / "paper_case_section_v3.md").read_text(encoding="utf-8")
