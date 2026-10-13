"""Acceptance tests for the append-only G1-T0-C1 correction."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.g1_t0_c1 import (
    CASE_ID,
    G1_DIR,
    ROOT,
    TARGET_EVIDENCE_ID,
    G1T0C1Error,
    correction_sha256,
    run_g1_t0_c1,
)


RUN_GLOB = "G1-T0-C1-*-correction-v1"


def _run_dir() -> Path:
    candidates = sorted(ROOT.joinpath("outputs").glob(RUN_GLOB))
    assert candidates, "C1 run directory is missing"
    return candidates[-1]


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_c1_manifest_and_target_v2_are_authoritative():
    run_dir = _run_dir()
    manifest = json.loads((run_dir / "g1_t0_c1_manifest.json").read_text(encoding="utf-8"))
    target = json.loads((run_dir / "g1_t0_c1_corrected_evidence_v2.json").read_text(encoding="utf-8"))
    canonical = _load_jsonl(run_dir / "g1_t0_c1_latest_evidence_canonical.jsonl")

    assert manifest["physical_t0_evidence_rows_before"] == 60
    assert manifest["physical_t0_evidence_rows_after"] == 61
    assert manifest["logical_evidence_id_count"] == 60
    assert manifest["target_latest_version"] == "2"
    assert manifest["latest_version_counts"] == {"1": 59, "2": 1}
    assert manifest["canonical_field_diff_count"] == 0
    assert manifest["canonical_count"] == 60
    assert manifest["canonical_ids_unique"] is True
    assert manifest["canonical_no_composite_nature"] is True
    assert manifest["canonical_no_replacement_character"] is True
    assert manifest["canonical_created_at_from_db"] is True
    assert manifest["formal_evidence_db_rows_added"] == 1
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["model_api_call_count"] == 0
    assert manifest["api_key_written"] is False

    assert target["evidence_id"] == TARGET_EVIDENCE_ID
    assert target["version"] == 2
    assert target["case_id"] == CASE_ID
    assert target["evidence_nature"] == "resource_input"
    assert target["coding_dimensions"] == ["engineering_resource_conditions", "application_maturity"]
    assert target["normalized_claim"] == "REMA项目首个开发周期于2023年12月启动。"
    assert target["correction_of_version"] == 1
    assert target["correction_reason"] == "落实作者已授权的G1重点裁决：开发周期启动不是测试或演习事件"
    assert target["reviewed_by"] == "author_01"
    assert target["review_mode"] == "authorized_g1_adjudication_correction"
    assert len(canonical) == 60
    assert [item["evidence_id"] for item in canonical] == sorted(item["evidence_id"] for item in canonical)
    assert len({item["evidence_id"] for item in canonical}) == 60
    assert all("+" not in item["evidence_nature"] for item in canonical)
    assert all("\ufffd" not in json.dumps(item, ensure_ascii=False) for item in canonical)
    assert all(item.get("created_at") for item in canonical)

    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        db_rows = conn.execute(
            "SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag=? ORDER BY evidence_id, version",
            (CASE_ID, "T0"),
        ).fetchall()
    finally:
        conn.close()
    db_latest = {}
    for evidence_id, version, payload_json in db_rows:
        if evidence_id not in db_latest or version > db_latest[evidence_id][0]:
            db_latest[evidence_id] = (version, json.loads(payload_json))
    assert {evidence_id: payload for evidence_id, (_, payload) in db_latest.items() if evidence_id in {item["evidence_id"] for item in canonical}} == {
        item["evidence_id"]: item for item in canonical
    }


def test_c1_preserves_v1_and_other_latest_evidence():
    run_dir = _run_dir()
    canonical = {item["evidence_id"]: item for item in _load_jsonl(run_dir / "g1_t0_c1_latest_evidence_canonical.jsonl")}
    original = {item["evidence_id"]: item for item in _load_jsonl(G1_DIR / "g1_t0_final_evidence.jsonl")}
    assert TARGET_EVIDENCE_ID in original
    assert TARGET_EVIDENCE_ID in canonical

    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        versions = conn.execute(
            "SELECT version, payload_json, content_hash FROM evidence WHERE case_id=? AND evidence_id=? ORDER BY version",
            (CASE_ID, TARGET_EVIDENCE_ID),
        ).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in versions] == [1, 2]
    v1_payload = json.loads(versions[0][1])
    v2_payload = json.loads(versions[1][1])
    v1_without_runtime_timestamp = {k: v for k, v in v1_payload.items() if k != "created_at"}
    original_without_runtime_timestamp = {k: v for k, v in original[TARGET_EVIDENCE_ID].items() if k != "created_at"}
    assert v1_without_runtime_timestamp == original_without_runtime_timestamp
    assert v1_payload["evidence_nature"] == "test_event"
    assert v2_payload == canonical[TARGET_EVIDENCE_ID]
    assert versions[0][2] == original[TARGET_EVIDENCE_ID]["content_hash"]
    assert v1_payload["created_at"] != ""
    assert all(
        {k: v for k, v in canonical[evidence_id].items() if k != "created_at"}
        == {k: v for k, v in original[evidence_id].items() if k != "created_at"}
        for evidence_id in original
        if evidence_id != TARGET_EVIDENCE_ID
    )

    with (G1_DIR / "g1_t0_exclusions.csv").open(encoding="utf-8", newline="") as handle:
        exclusions = {row["evidence_id"] for row in csv.DictReader(handle)}
    assert "EV-T0-246C331615E11FB3" not in canonical
    assert "EV-T0-1C84CB9C7F271D13" not in canonical
    assert all(parent_id not in canonical for parent_id in {
        "EV-T0-DB1424F52E33432F",
        "EV-T0-F25379FE058D2680",
        "EV-T0-794C24A620FE0968",
    })
    assert exclusions == {"EV-T0-246C331615E11FB3", "EV-T0-1C84CB9C7F271D13"}


def test_c1_replay_is_idempotent_without_new_rows():
    run_dir = _run_dir()
    result = run_g1_t0_c1(output_dir=run_dir)
    assert result["replayed"] is True
    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id=?",
            (CASE_ID, TARGET_EVIDENCE_ID),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM g1_t0_c1_correction_audit WHERE correction_sha256=?",
            (correction_sha256(),),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_c1_transaction_rolls_back_on_failure():
    source_db = ROOT / "data" / "research.sqlite3"
    temporary_root = Path(tempfile.mkdtemp(prefix=".c1-rollback-", dir=str(ROOT)))
    copied_db = temporary_root / "rollback.sqlite3"
    try:
        shutil.copy2(source_db, copied_db)
        conn = sqlite3.connect(str(copied_db))
        try:
            conn.execute("DROP TRIGGER IF EXISTS prevent_evidence_delete")
            # The formal P4 run now exists in the production database.  This
            # legacy C1 rollback fixture must represent the pre-P4 state, so
            # remove only that snapshot from the temporary copy.
            conn.execute("DROP TRIGGER IF EXISTS prevent_frozen_snapshot_item_delete")
            conn.execute("DROP TRIGGER IF EXISTS prevent_frozen_snapshot_delete")
            conn.execute("DROP TRIGGER IF EXISTS prevent_viewpoint_delete")
            conn.execute("PRAGMA foreign_keys=OFF")
            # The current working database also contains the later T1 merge.
            # Remove only those rows from this disposable legacy-C1 fixture so
            # the test represents C1's original pre-T1 precondition.
            conn.execute("DELETE FROM evidence_dependency WHERE case_id=?", (CASE_ID,))
            conn.execute("DELETE FROM snapshot_items WHERE snapshot_id IN (SELECT snapshot_id FROM evidence_snapshot WHERE case_id=? AND phase='T1')", (CASE_ID,))
            conn.execute("DELETE FROM viewpoint WHERE snapshot_id IN (SELECT snapshot_id FROM evidence_snapshot WHERE case_id=? AND phase='T1')", (CASE_ID,))
            conn.execute("DELETE FROM evidence_snapshot WHERE case_id=? AND phase='T1'", (CASE_ID,))
            conn.execute("DELETE FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (CASE_ID,))
            conn.execute("DELETE FROM human_review WHERE gate='G1-T1' AND case_id=?", (CASE_ID,))
            conn.execute("DELETE FROM snapshot_items WHERE snapshot_id IN (SELECT snapshot_id FROM evidence_snapshot WHERE case_id=?)", (CASE_ID,))
            conn.execute("DELETE FROM evidence_snapshot WHERE case_id=?", (CASE_ID,))
            conn.execute("DELETE FROM evidence WHERE case_id=? AND evidence_id=? AND version=2", (CASE_ID, TARGET_EVIDENCE_ID))
            conn.execute("DELETE FROM g1_t0_c1_correction_audit WHERE correction_sha256=?", (correction_sha256(),))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(G1T0C1Error, match="injected C1 transaction failure"):
            run_g1_t0_c1(database_path=copied_db, output_dir=temporary_root / "failed-output", inject_failure=True)

        conn = sqlite3.connect(str(copied_db))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id=? AND version=2",
                (CASE_ID, TARGET_EVIDENCE_ID),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM g1_t0_c1_correction_audit WHERE correction_sha256=?",
                (correction_sha256(),),
            ).fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
