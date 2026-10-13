"""Tests for the authorized P3S-v5-R supplementary G1-T0 write."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3

import pytest

from src.g1_t0_supplement import (
    CASE_ID,
    DB_PATH,
    DIRECT_ACCEPT_PREFIXES,
    EXCLUDE_PREFIXES,
    REVISE_RULES,
    ROOT,
    G1T0SupplementError,
    _apply_author_decisions,
    run_g1_t0_supplement,
)


RUN_GLOB = "G1-T0-SUPP-*-formal-v1"


def _run_dir():
    runs = sorted((ROOT / "outputs").glob(RUN_GLOB))
    assert runs, "supplement G1 run directory is missing"
    return runs[-1]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_author_partition_and_requested_revisions():
    rows, exclusions, candidates = _apply_author_decisions()
    assert len(candidates) == len(rows) == 27
    assert len(exclusions) == 2
    assert len(DIRECT_ACCEPT_PREFIXES) == 19
    assert len(REVISE_RULES) == 6
    assert len(EXCLUDE_PREFIXES) == 2
    assert {row["final_decision"] for row in rows} == {"accept", "revise", "exclude"}
    assert {key: sum(row["final_decision"] == key for row in rows) for key in ("accept", "revise", "exclude")} == {
        "accept": 19,
        "revise": 6,
        "exclude": 2,
    }
    by_prefix = {row["evidence_id"].removeprefix("EV-T0-")[:8]: row for row in rows}
    assert by_prefix["28FDEEB8"]["excerpt_original"] == REVISE_RULES["28FDEEB8"]["excerpt_original"]
    assert by_prefix["2CF44972"]["final_nature"] == "goal"
    assert by_prefix["3FA6806C"]["final_dimensions"] == ["application_maturity"]
    assert by_prefix["4240F040"]["final_nature"] == "goal"
    assert by_prefix["8414D2FA"]["excerpt_original"] == REVISE_RULES["8414D2FA"]["excerpt_original"]
    assert by_prefix["8BF4D983"]["final_nature"] == "goal"


def test_committed_supplement_artifacts_and_boundaries():
    run = _run_dir()
    manifest = json.loads((run / "g1_t0_supplement_manifest.json").read_text(encoding="utf-8"))
    decisions = _csv(run / "g1_t0_supplement_author_decisions.csv")
    evidence = _jsonl(run / "g1_t0_supplement_final_evidence.jsonl")
    exclusions = _csv(run / "g1_t0_supplement_exclusions.csv")
    assert len(decisions) == 27
    assert len({row["evidence_id"] for row in decisions}) == 27
    assert {key: sum(row["final_decision"] == key for row in decisions) for key in ("accept", "revise", "exclude")} == {
        "accept": 19,
        "revise": 6,
        "exclude": 2,
    }
    assert len(evidence) == 25
    assert len({row["evidence_id"] for row in evidence}) == 25
    assert len(exclusions) == 2
    assert all(row["evidence_nature"] in {"goal", "resource_input", "test_event", "test_result", "deployment_event", "governance_event", "constraint", "evaluation"} for row in evidence)
    assert all("+" not in row["evidence_nature"] for row in evidence)
    assert all(all(row.get(field) for field in ("case_id", "source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature", "coding_dimensions", "published_at", "content_hash")) for row in evidence)
    assert manifest["formal_evidence_db_rows_added"] == 25
    assert manifest["decision_counts"] == {"accept": 19, "revise": 6, "exclude": 2}
    assert manifest["author_actual_review_count"] == 27
    assert manifest["new_model_api_calls"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["api_key_written"] is False


def test_supplement_transaction_rolls_back(tmp_path):
    copied_db = tmp_path / "rollback.sqlite3"
    shutil.copy2(DB_PATH, copied_db)
    run = _run_dir()
    manifest = json.loads((run / "g1_t0_supplement_manifest.json").read_text(encoding="utf-8"))
    evidence_ids = [row["evidence_id"] for row in _jsonl(run / "g1_t0_supplement_final_evidence.jsonl")]
    conn = sqlite3.connect(str(copied_db))
    try:
        # Isolate the rollback fixture from the already committed production
        # run.  This touches only the temporary database copy.
        conn.execute("DROP TRIGGER IF EXISTS prevent_evidence_delete")
        placeholders = ",".join("?" for _ in evidence_ids)
        conn.execute(f"DELETE FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})", [CASE_ID, *evidence_ids])
        conn.execute("DELETE FROM g1_t0_supplement_audit WHERE authorization_sha256=?", (manifest["authorization_sha256"],))
        conn.execute("DELETE FROM g1_t0_supplement_author_decision WHERE run_id=?", (manifest["run_id"],))
        conn.execute("DELETE FROM g1_t0_supplement_exclusion WHERE run_id=?", (manifest["run_id"],))
        conn.execute("DELETE FROM human_review WHERE gate=?", ("G1-T0-SUPPLEMENT",))
        conn.execute("DELETE FROM pipeline_stage WHERE run_id=?", (manifest["run_id"],))
        conn.execute("DELETE FROM pipeline_run WHERE run_id=?", (manifest["run_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(G1T0SupplementError, match="injected supplement transaction failure"):
        run_g1_t0_supplement(database_path=copied_db, output_dir=tmp_path / "failed-output", inject_failure_after=1)
    conn = sqlite3.connect(str(copied_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)).fetchone()[0] == 101
        audit_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='g1_t0_supplement_audit'").fetchone()
        assert audit_table is None or conn.execute("SELECT COUNT(*) FROM g1_t0_supplement_audit").fetchone()[0] == 0
    finally:
        conn.close()


def test_supplement_output_does_not_contain_api_key_prefix():
    run = _run_dir()
    for path in run.rglob("*"):
        if path.is_file():
            assert b"sk-sp-" not in path.read_bytes()


def test_replaying_same_authorization_adds_no_rows():
    run = _run_dir()
    before = sqlite3.connect(str(DB_PATH)).execute(
        "SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)
    ).fetchone()[0]
    result = run_g1_t0_supplement(output_dir=run)
    after = sqlite3.connect(str(DB_PATH)).execute(
        "SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)
    ).fetchone()[0]
    assert result["replayed"] is True
    assert before == after == 126


def test_supplement_prior_p3sr_manifest_hash_unchanged():
    run = _run_dir()
    manifest = json.loads((run / "g1_t0_supplement_manifest.json").read_text(encoding="utf-8"))
    source_manifest = ROOT / "outputs" / "P3SR-20260826T133937Z-v5-offline-recovery-v1" / "p3sr_manifest.json"
    assert manifest["input_source_run_hashes"]["p3sr_manifest.json"] == hashlib.sha256(source_manifest.read_bytes()).hexdigest()
    assert manifest["protected_prior_outputs_unchanged"] is True
