"""Acceptance tests for the authorized formal G2 write."""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from src.g2_t0 import AUTHORIZATION_TEXT, CASE_ID, DB_PATH, FORMAL_IDS, ROOT, SNAPSHOT_ID, G2Error, run_g2_t0


RUN_GLOB = "G2-T0-*-formal-v1"


def _run_dir():
    runs = sorted((ROOT / "outputs").glob(RUN_GLOB))
    assert runs
    return runs[-1]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_g2_manifest_counts_and_boundaries():
    run = _run_dir()
    manifest = json.loads((run / "g2_manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == SNAPSHOT_ID
    assert manifest["author_authorization_sha256"]
    assert manifest["author_review_mode"] == "ai_preannotation_plus_author_batch_signoff"
    assert manifest["author_signoff_count"] == 1
    assert manifest["author_decision_count"] == 5
    assert manifest["shared_baseline_count"] == 1
    assert manifest["formal_hypothesis_count"] == 3
    assert manifest["hypothesis_inserted"] == 3
    assert manifest["frozen_hypothesis_count"] == 3
    assert manifest["all_evidence_ids_in_snapshot"] is True
    assert manifest["minimum_source_requirement_passed"] is True
    assert manifest["lineage_complete"] is True
    assert manifest["transaction_committed"] is True
    assert manifest["rollback_test_passed"] is True
    assert manifest["idempotency_test_passed"] is True
    assert manifest["frozen_immutability_test_passed"] is True
    assert manifest["model_api_calls"] == 0
    assert manifest["viewpoint_inserted"] == 0
    assert manifest["disagreement_inserted"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p7_executed"] is False
    assert manifest["api_key_written"] is False


def test_g2_baseline_lineage_and_formal_ids():
    run = _run_dir()
    baseline = _jsonl(run / "g2_shared_baseline.jsonl")
    hypotheses = _jsonl(run / "g2_final_hypotheses.jsonl")
    lineage = _jsonl(run / "g2_hypothesis_lineage.jsonl")
    assert len(baseline) == 1
    assert baseline[0]["baseline_id"] == "BASE-T0-01"
    assert baseline[0]["source_candidate_ids"] == ["G2-H02"]
    assert baseline[0]["formal_hypothesis"] is False
    assert [row["hypothesis_id"] for row in hypotheses] == list(FORMAL_IDS)
    assert all(row["snapshot_id"] == SNAPSHOT_ID and row["frozen"] is True and row["status"] == "active" for row in hypotheses)
    assert {row["hypothesis_id"] for row in lineage if "hypothesis_id" in row} == set(FORMAL_IDS)
    assert any(row.get("baseline_id") == "BASE-T0-01" for row in lineage)
    assert hypotheses[0]["statement"].startswith("截至2024年底，美国军用人工智能已进入")
    assert hypotheses[1]["statement"].startswith("截至2024年底，美国军用人工智能的实际能力形成")
    assert hypotheses[2]["source_candidate_ids"] == ["G2-H04", "G2-H05"]


def test_g2_database_matches_exports_and_is_immutable():
    run = _run_dir()
    manifest = json.loads((run / "g2_manifest.json").read_text(encoding="utf-8"))
    exported = {row["hypothesis_id"]: row for row in _jsonl(run / "g2_final_hypotheses.jsonl")}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute("SELECT hypothesis_id, snapshot_id, frozen, payload_json FROM hypothesis WHERE snapshot_id=? ORDER BY hypothesis_id", (SNAPSHOT_ID,)).fetchall()
        assert len(rows) == 3
        assert {row[0]: json.loads(row[3]) for row in rows} == exported
        try:
            conn.execute("UPDATE hypothesis SET frozen=0 WHERE hypothesis_id=?", (FORMAL_IDS[0],))
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("frozen hypothesis update was not blocked")
        conn.rollback()
        try:
            conn.execute("DELETE FROM hypothesis WHERE hypothesis_id=?", (FORMAL_IDS[0],))
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("frozen hypothesis delete was not blocked")
        conn.rollback()
    finally:
        conn.close()
    result = run_g2_t0(output_dir=run)
    assert result["replayed"] is True


def test_g2_transaction_rolls_back(tmp_path):
    copied = tmp_path / "rollback.sqlite3"
    shutil.copy2(DB_PATH, copied)
    conn = sqlite3.connect(str(copied))
    try:
        conn.execute("DROP TRIGGER IF EXISTS prevent_frozen_hypothesis_update")
        conn.execute("DROP TRIGGER IF EXISTS prevent_frozen_hypothesis_delete")
        conn.execute("DELETE FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,))
        conn.execute("DROP TABLE IF EXISTS g2_shared_baseline_audit")
        conn.execute("DROP TABLE IF EXISTS g2_author_decision")
        conn.execute("DROP TABLE IF EXISTS g2_authorization_audit")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(G2Error, match="injected G2 transaction failure"):
        run_g2_t0(database_path=copied, output_dir=tmp_path / "failed-output", inject_failure_after=1)
    conn = sqlite3.connect(str(copied))
    try:
        assert conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='g2_authorization_audit'").fetchone() is None
    finally:
        conn.close()
