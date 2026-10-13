"""Acceptance tests for the formal T0 snapshot freeze."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from src.p4_t0 import CASE_ID, DB_PATH, ROOT, TARGET_EVIDENCE_ID, run_p4_t0


RUN_GLOB = "P4-T0-*-freeze-v1"


def _run_dir():
    runs = sorted((ROOT / "outputs").glob(RUN_GLOB))
    assert runs, "P4 T0 run directory is missing"
    return runs[-1]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p4_manifest_and_snapshot_counts():
    run = _run_dir()
    manifest = json.loads((run / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8"))
    items = _jsonl(run / "p4_t0_snapshot_items.jsonl")
    frozen = _jsonl(run / "p4_t0_frozen_evidence.jsonl")
    assert manifest["physical_evidence_count"] == 86
    assert manifest["logical_evidence_count"] == 85
    assert manifest["snapshot_item_count"] == 85
    assert manifest["grade_a_count"] == 56
    assert manifest["grade_b_count"] == 29
    assert manifest["grade_d_count"] == 0
    assert manifest["grade_a_ratio"] == 0.6588235294
    assert manifest["source_count"] == 14
    assert manifest["latest_version_only"] is True
    assert manifest["target_v2_included"] is True
    assert manifest["excluded_items_absent"] is True
    assert manifest["frozen"] is True
    assert manifest["p4_gate_open"] is True
    assert manifest["p4_executed"] is True
    assert manifest["transaction_committed"] is True
    assert manifest["idempotency_test_passed"] is True
    assert manifest["rollback_test_passed"] is True
    assert manifest["historical_artifacts_unchanged"] is True
    assert manifest["model_api_calls"] == 0
    assert manifest["t1_read"] is False
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["p5_executed"] is False
    assert manifest["evidence_inserted"] == 0
    assert len(items) == len({(row["case_id"], row["evidence_id"], row["version"]) for row in items}) == 85
    assert len(frozen) == len({row["evidence_id"] for row in frozen}) == 85


def test_snapshot_matches_latest_database_payloads():
    run = _run_dir()
    manifest = json.loads((run / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8"))
    snapshot_items = _jsonl(run / "p4_t0_snapshot_items.jsonl")
    frozen = {row["evidence_id"]: row for row in _jsonl(run / "p4_t0_frozen_evidence.jsonl")}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        physical = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T0'", (CASE_ID,)).fetchone()[0]
        db_rows = conn.execute("SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag='T0'", (CASE_ID,)).fetchall()
        db_items = conn.execute("SELECT snapshot_id, case_id, evidence_id, version FROM snapshot_items WHERE snapshot_id=? ORDER BY case_id, evidence_id, version", (manifest["snapshot_id"],)).fetchall()
        snapshot_row = conn.execute("SELECT case_id, phase, cutoff_time, manifest_hash, frozen, frozen_by FROM evidence_snapshot WHERE snapshot_id=?", (manifest["snapshot_id"],)).fetchone()
    finally:
        conn.close()
    latest = {}
    for evidence_id, version, payload_json in db_rows:
        if evidence_id not in latest or version > latest[evidence_id][0]:
            latest[evidence_id] = (version, json.loads(payload_json))
    assert physical == 86
    assert {key: value for key, (_, value) in latest.items()} == frozen
    assert db_items == [(row["snapshot_id"], row["case_id"], row["evidence_id"], int(row["version"])) for row in snapshot_items]
    assert snapshot_row == (CASE_ID, "T0", "2024-12-31T23:59:59Z", manifest["manifest_hash"], 1, "author_authorized_pipeline")
    assert frozen[TARGET_EVIDENCE_ID]["version"] == 2
    assert all(row["evidence_id"] != TARGET_EVIDENCE_ID or row["version"] == 2 for row in snapshot_items)


def test_frozen_snapshot_is_immutable_and_replay_is_idempotent():
    run = _run_dir()
    manifest = json.loads((run / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(str(DB_PATH))
    try:
        try:
            conn.execute("UPDATE evidence_snapshot SET frozen_by='intruder' WHERE snapshot_id=?", (manifest["snapshot_id"],))
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("frozen snapshot update was not blocked")
        try:
            conn.execute("INSERT INTO snapshot_items(snapshot_id,case_id,evidence_id,version) VALUES (?,?,?,?)", (manifest["snapshot_id"], CASE_ID, "EV-NOT-ALLOWED", 1))
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("frozen snapshot item insert was not blocked")
        conn.rollback()
        before = conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (manifest["snapshot_id"],)).fetchone()[0]
    finally:
        conn.close()
    result = run_p4_t0(output_dir=run)
    assert result["replayed"] is True
    conn = sqlite3.connect(str(DB_PATH))
    try:
        after = conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (manifest["snapshot_id"],)).fetchone()[0]
    finally:
        conn.close()
    assert before == after == 85


def test_authoritative_input_hashes_and_no_api_key_in_outputs():
    run = _run_dir()
    manifest = json.loads((run / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["authoritative_input_hashes"].items():
        path = ROOT / "outputs" / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    assert all(b"sk-sp-" not in path.read_bytes() for path in run.rglob("*") if path.is_file())
