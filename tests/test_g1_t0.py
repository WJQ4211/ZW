import csv
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.g1_t0 import (
    AUTHORIZATION_TEXT,
    CASE_ID,
    DB_PATH,
    P3BR2A_DIR,
    run_g1_t0,
)


ROOT = Path(__file__).resolve().parents[1]


def _run_dir() -> Path:
    # The supplementary G1 run has its own artifact contract and must not be
    # selected by the original G1-T0 test fixture.
    run = ROOT / "outputs" / "G1-T0-1AE10600ABA2B674-formal-v1"
    assert run.exists()
    return run


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_formal_g1_counts_and_decisions():
    run = _run_dir()
    manifest = json.loads((run / "g1_t0_manifest.json").read_text(encoding="utf-8"))
    decisions = _csv(run / "g1_t0_author_decisions.csv")
    evidence = _jsonl(run / "g1_t0_final_evidence.jsonl")
    assert len(decisions) == 55
    assert len({row["evidence_id"] for row in decisions}) == 55
    assert {key: sum(row["final_decision"] == key for row in decisions) for key in ("accept", "revise", "split", "exclude")} == {
        "accept": 13,
        "revise": 37,
        "split": 3,
        "exclude": 2,
    }
    assert len(evidence) == 60
    assert len({row["evidence_id"] for row in evidence}) == 60
    assert all("+" not in row["evidence_nature"] for row in evidence)
    assert all(all(row.get(field) for field in ("case_id", "source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature", "coding_dimensions", "published_at", "content_hash")) for row in evidence)
    assert manifest["formal_evidence_db_rows_added"] == 60


def test_focus_adjudications_and_exclusions_are_applied():
    rows = {row["evidence_id"]: row for row in _csv(_run_dir() / "g1_t0_author_decisions.csv")}
    assert rows["EV-T0-CA999183D4D473C4"]["final_decision"] == "revise"
    assert rows["EV-T0-CA999183D4D473C4"]["final_nature"] == "resource_input"
    assert rows["EV-T0-794C24A620FE0968"]["final_decision"] == "split"
    assert rows["EV-T0-79B653E22A3D4EF4"]["final_decision"] == "revise"
    assert rows["EV-T0-F0773C8E2ECB5C8D"]["final_decision"] == "revise"
    assert rows["EV-T0-00FCD33BA877C8BA"]["final_dimensions"] == "[\"application_maturity\",\"organizational_system_fit\"]"
    excluded = {row["evidence_id"] for row in _csv(_run_dir() / "g1_t0_exclusions.csv")}
    evidence_ids = {row["evidence_id"] for row in _jsonl(_run_dir() / "g1_t0_final_evidence.jsonl")}
    assert excluded == {"EV-T0-246C331615E11FB3", "EV-T0-1C84CB9C7F271D13"}
    assert not excluded & evidence_ids


def test_low_risk_auto_accepts_are_not_human_reviewed():
    rows = _csv(_run_dir() / "g1_t0_low_risk_auto_accept.csv")
    assert len(rows) == 4
    assert all(row["human_reviewed"].lower() == "false" for row in rows)
    assert all(row["reviewer"] == "" for row in rows)
    assert all(row["review_mode"] == "protocol_low_risk_auto_accept_not_human_reviewed" for row in rows)


def test_split_lineage_has_three_parents_and_six_children():
    rows = _jsonl(_run_dir() / "g1_t0_split_lineage.jsonl")
    assert len(rows) == 6
    assert len({row["formal_child_evidence_id"] for row in rows}) == 6
    assert {row["parent_evidence_id"] for row in rows} == {
        "EV-T0-DB1424F52E33432F",
        "EV-T0-F25379FE058D2680",
        "EV-T0-794C24A620FE0968",
    }
    assert all(row["parent_evidence_id"] and row["proposal_child_id"] and row["split_reason"] for row in rows)


def test_author_review_semantics_and_boundary_flags():
    manifest = json.loads((_run_dir() / "g1_t0_manifest.json").read_text(encoding="utf-8"))
    rows = _csv(_run_dir() / "g1_t0_author_decisions.csv")
    assert all(row["human_reviewed"].lower() == "true" for row in rows)
    assert all(row["reviewer"] == "author_01" for row in rows)
    assert manifest["human_source_visual_review_count"] == 0
    assert manifest["ai_assisted_visual_pre_review_count"] == 7
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["model_api_call_count"] == 0
    assert manifest["api_key_written"] is False


def test_repeated_authorization_is_idempotent():
    # Replaying against the already committed database performs no second
    # Evidence insert and returns the existing authorization run.
    before = len(_jsonl(_run_dir() / "g1_t0_final_evidence.jsonl"))
    result = run_g1_t0(database_path=DB_PATH, output_dir=_run_dir())
    after_conn = sqlite3.connect(str(DB_PATH))
    try:
        ids = [row["evidence_id"] for row in _jsonl(_run_dir() / "g1_t0_final_evidence.jsonl")]
        count = after_conn.execute(
            "SELECT COUNT(DISTINCT evidence_id) FROM evidence WHERE case_id=? AND evidence_id IN (" + ",".join("?" for _ in ids) + ")",
            [CASE_ID, *ids],
        ).fetchone()[0]
    finally:
        after_conn.close()
    assert result.get("replayed") is True
    assert before == count == 60
    assert count == 60


def test_transaction_rolls_back_on_assertion_failure(tmp_path):
    copied_db = tmp_path / "rollback.sqlite3"
    shutil.copy2(DB_PATH, copied_db)
    # Remove only the explicit T0 target rows and this authorization audit in
    # the isolated copy; no T1 query is made.
    run = _run_dir()
    ids = [row["evidence_id"] for row in _jsonl(run / "g1_t0_final_evidence.jsonl")]
    conn = sqlite3.connect(str(copied_db))
    try:
        placeholders = ",".join("?" for _ in ids)
        conn.execute("DROP TRIGGER IF EXISTS prevent_evidence_delete")
        conn.execute(f"DELETE FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})", [CASE_ID, *ids])
        conn.execute("DELETE FROM g1_t0_audit WHERE authorization_sha256=?", (hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest(),))
        run_id = json.loads((run / "g1_t0_manifest.json").read_text(encoding="utf-8"))["run_id"]
        for table in ("pipeline_stage", "g1_t0_author_decision", "g1_t0_low_risk_auto_accept", "g1_t0_exclusion", "g1_t0_split_lineage"):
            conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM human_review WHERE review_id LIKE ?", ("G1-T0-EV-T0-%",))
        conn.execute("DELETE FROM pipeline_run WHERE run_id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match="injected transaction failure"):
        run_g1_t0(database_path=copied_db, output_dir=tmp_path / "failed-output", inject_failure_after=1)
    conn = sqlite3.connect(str(copied_db))
    try:
        assert conn.execute(f"SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})", [CASE_ID, *ids]).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM g1_t0_audit WHERE authorization_sha256=?", (hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest(),)).fetchone()[0] == 0
    finally:
        conn.close()


def test_input_p3br2a_and_prior_outputs_remain_hashed_in_manifest():
    manifest = json.loads((_run_dir() / "g1_t0_manifest.json").read_text(encoding="utf-8"))
    assert manifest["input_run_id"] == "P3BR2A-20260826T063553Z-content-completeness-v1"
    assert manifest["protected_prior_outputs_unchanged"] is True
    assert manifest["git_head"] == "unavailable_not_a_git_repository"
    assert len(manifest["implementation_file_hashes"]) == 3
    assert manifest["python_version"]
