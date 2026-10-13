"""Acceptance tests for G1-T1/P13-P16 and G3Q/G4Q-T1 preparation."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs" / "G1-T1-P13-P16-8D0D8A0B0E6D5D42-formal-v1"


def _manifest():
    paths = sorted((ROOT / "outputs").glob("G1-T1-P13-P16-*-formal-v1/g1_t1_p13_p16_manifest.json"))
    assert paths, "merge run is missing"
    return paths[-1].parent, json.loads(paths[-1].read_text(encoding="utf-8"))


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_g1_and_p13_counts_and_grade_structure():
    base, m = _manifest()
    assert m["author_decision_count"] == 46
    assert m["formal_t1_evidence_count"] == 40
    assert m["t1_snapshot_frozen"] is True
    assert m["t1_snapshot_logical_item_count"] == 125
    assert m["grade_counts"] == {"A": 94, "B": 31, "D": 0}
    assert len(_jsonl(base / "P13_T1_SNAPSHOT" / "p13_t1_snapshot_items.jsonl")) == 125
    assert len(_jsonl(base / "G1_T1" / "g1_t1_final_evidence.jsonl")) == 40


def test_author_decisions_and_historical_roles():
    base, _ = _manifest()
    rows = list(csv.DictReader((base / "G1_T1" / "g1_t1_author_decisions.csv").open(encoding="utf-8-sig", newline="")))
    assert len(rows) == 46
    assert sum(row["final_decision"] == "exclude" for row in rows) == 6
    by_id = {row["evidence_id"]: row for row in rows}
    assert by_id["CE-T1-7DE738404CD33807"]["t1_change_signal_eligible"].lower() == "false"
    assert by_id["CE-T1-C1E0527174B2F72F"]["temporal_role"] == "historical_context"
    for row in rows:
        assert row["human_reviewed"].lower() == "true"
        assert row["reviewer"] == "author_01"


def test_p14_p15_p16_outputs_and_blank_author_fields():
    base, m = _manifest()
    edges = _jsonl(base / "P14_IMPACT_LOCALIZATION" / "p14_impact_edges.jsonl")
    updates = _jsonl(base / "P15_VIEWPOINT_UPDATE" / "updated_viewpoints_latest.jsonl")
    assert m["change_signal_evidence_count"] == 38
    assert edges
    assert 0 < len(updates) < 15
    rows = list(csv.DictReader((base / "P16_G3Q_G4Q_T1" / "g3_t1_author_decision_template.csv").open(encoding="utf-8-sig", newline="")))
    assert rows
    assert all(row["author_decision"] == "" and row["reviewer"] == "" for row in rows)
    assert m["formal_g3_t1_executed"] is False
    assert m["formal_g4_t1_signed"] is False
    conn = sqlite3.connect(ROOT / "data" / "research.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence_dependency WHERE edge_id LIKE ?", (f"DEP-T1-{m['run_id']}-%",)).fetchone()[0] == m["dependency_edge_count"]
        assert conn.execute("SELECT COUNT(*) FROM evidence_dependency WHERE edge_id LIKE ? AND viewpoint_version > 1", (f"DEP-T1-{m['run_id']}-%",)).fetchone()[0] == m["dependency_edge_count"]
    finally:
        conn.close()


def test_database_append_only_boundary_and_snapshot():
    base, m = _manifest()
    conn = sqlite3.connect(ROOT / "data" / "research.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", ("us_military_ai_deployment",)).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE snapshot_id=? AND frozen=1", (m["snapshot_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (m["snapshot_id"],)).fetchone()[0] == 125
        assert conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (m["snapshot_id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1' AND evidence_nature LIKE '%+%'", ("us_military_ai_deployment",)).fetchone()[0] == 0
    finally:
        conn.close()


def test_api_and_formal_next_gate_boundaries():
    _, m = _manifest()
    assert m["model_api_calls"] == 0
    assert m["evidence_inserted"] == 40
    assert m["hypothesis_inserted"] == 0
    assert m["formal_g3_t1_executed"] is False
    assert m["formal_g4_t1_signed"] is False
    assert m["api_key_written"] is False
    assert m["historical_t0_modified"] is False


def test_transaction_rollback_and_idempotent_replay_on_isolated_database(tmp_path, monkeypatch):
    """Exercise the real merge transaction without touching the working DB."""
    import src.t1_merge as tm

    base, manifest = _manifest()
    isolated = tmp_path / "research.sqlite3"
    shutil.copy2(ROOT / "data" / "research.sqlite3", isolated)
    run_id = manifest["run_id"]
    snapshot_id = manifest["snapshot_id"]

    conn = sqlite3.connect(isolated)
    conn.execute("PRAGMA foreign_keys=ON")
    # The working database deliberately prevents history deletion.  This is
    # a disposable copy, so remove only its triggers to reconstruct the
    # pre-run fixture without weakening the production database.
    for trigger_name, in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f'DROP TRIGGER "{trigger_name.replace(chr(34), chr(34) * 2)}"')
    # Remove only this run from the isolated copy, child rows first.
    for sql, args in [
        # The final G3-T1/G4-T1 sign-off adds monitoring rows that depend
        # on the T1 snapshot.  Remove those rows first in this disposable
        # copy so the pre-run merge fixture can be reconstructed.
        ("DELETE FROM trigger_rule WHERE trigger_id LIKE ?", ("TRG-T1R-C1-%",)),
        ("DELETE FROM monitor_indicator WHERE indicator_id LIKE ?", ("IND-T1R-C1-%",)),
        ("DELETE FROM discriminative_need WHERE need_id LIKE ?", ("NEED-T1R-C1-%",)),
        ("DELETE FROM disagreement WHERE disagreement_id LIKE ?", ("DISAG-T1R-C1-%",)),
        ("DELETE FROM human_review WHERE review_id LIKE ?", ("G3-T1-REVIEW-%",)),
        ("DELETE FROM human_review WHERE review_id LIKE ?", ("G4-T1-REVIEW-%",)),
        ("DELETE FROM evidence_dependency WHERE payload_json LIKE ?", ("%P15R-P16R-G4QR-T1-PAPER-CASE-%",)),
        ("DELETE FROM p15r_viewpoint_audit WHERE run_id LIKE ?", ("P15R-P16R-G4QR-T1-PAPER-CASE-%",)),
        ("DELETE FROM evidence_dependency WHERE edge_id LIKE ?", (f"DEP-T1-{run_id}-%",)),
        ("DELETE FROM p14_t1_impact_edge WHERE run_id=?", (run_id,)),
        ("DELETE FROM p15_t1_viewpoint_update_audit WHERE run_id=?", (run_id,)),
        ("DELETE FROM p16_t1_audit WHERE run_id=?", (run_id,)),
        ("DELETE FROM g1_t1_author_decision WHERE run_id=?", (run_id,)),
        ("DELETE FROM g1_t1_exclusion WHERE run_id=?", (run_id,)),
        ("DELETE FROM human_review WHERE review_id LIKE ?", ("G1-T1-%",)),
        ("DELETE FROM artifact WHERE artifact_id LIKE ?", (f"{run_id}-%",)),
        ("DELETE FROM pipeline_stage WHERE run_id=?", (run_id,)),
        ("DELETE FROM update_event WHERE update_id=?", (run_id,)),
        ("DELETE FROM viewpoint WHERE snapshot_id=?", (snapshot_id,)),
        ("DELETE FROM snapshot_items WHERE snapshot_id=?", (snapshot_id,)),
        ("DELETE FROM evidence_snapshot WHERE snapshot_id=?", (snapshot_id,)),
        ("DELETE FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (tm.CASE_ID,)),
        ("DELETE FROM pipeline_run WHERE run_id=?", (run_id,)),
        ("DELETE FROM g1_t1_audit WHERE authorization_sha256=?", (tm.AUTH_HASH,)),
    ]:
        conn.execute(sql, args)
    conn.commit()
    conn.close()

    candidates = tm._read_p11r_candidates()
    metadata = tm._source_metadata(candidates)
    decisions, evidence, exclusions = [], [], []
    for candidate in candidates:
        row = tm._apply_author_corrections(candidate)
        row["evidence_id"] = row.pop("candidate_evidence_id")
        if row["final_decision"] == "exclude":
            row["exclusion_reason"] = "isolated rollback/idempotency test"
            exclusions.append(row)
        else:
            evidence.append(tm._formal_evidence(row, metadata[row["evidence_id"]], run_id))
        decisions.append(row)

    conn = sqlite3.connect(isolated)
    conn.row_factory = sqlite3.Row
    t0_latest = tm._latest_t0(conn)
    views = tm._latest_viewpoints(conn)
    conn.close()
    items = ([{"snapshot_id": tm.T1_SNAPSHOT_ID, "case_id": tm.CASE_ID, "evidence_id": r["evidence_id"], "version": r["version"], "content_hash": r["content_hash"]} for r in t0_latest]
              + [{"snapshot_id": tm.T1_SNAPSHOT_ID, "case_id": tm.CASE_ID, "evidence_id": r["evidence_id"], "version": 1, "content_hash": r["content_hash"]} for r in evidence])
    items.sort(key=lambda r: (r["case_id"], r["evidence_id"], r["version"]))
    snapshot_hash = tm.sha256_bytes("".join(f"{r['case_id']}|{r['evidence_id']}|{r['version']}|{r['content_hash']}\n" for r in items).encode("utf-8"))
    edges = tm._impact_edges(evidence, views)
    updated, diffs, _ = tm._updated_viewpoints(views, edges, run_id, "2026-08-27T00:00:00Z")

    monkeypatch.setattr(tm, "DB_PATH", isolated)
    try:
        try:
            tm._transaction_commit(evidence, decisions, exclusions, t0_latest, items, snapshot_hash, updated, edges, diffs, "2026-08-27T00:00:00Z", base, inject_failure=True)
        except RuntimeError as exc:
            assert "injected transaction failure" in str(exc)
        conn = sqlite3.connect(isolated)
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (tm.CASE_ID,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE snapshot_id=?", (tm.T1_SNAPSHOT_ID,)).fetchone()[0] == 0
        conn.close()

        first = tm._transaction_commit(evidence, decisions, exclusions, t0_latest, items, snapshot_hash, updated, edges, diffs, "2026-08-27T00:00:00Z", base)
        assert first["replayed"] is False
        conn = sqlite3.connect(isolated)
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (tm.CASE_ID,)).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (tm.T1_SNAPSHOT_ID,)).fetchone()[0] == 125
        conn.close()

        replay = tm._transaction_commit(evidence, decisions, exclusions, t0_latest, items, snapshot_hash, updated, edges, diffs, "2026-08-27T00:00:00Z", base)
        assert replay["replayed"] is True
        conn = sqlite3.connect(isolated)
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'", (tm.CASE_ID,)).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (tm.T1_SNAPSHOT_ID,)).fetchone()[0] == 125
        conn.close()
    finally:
        monkeypatch.setattr(tm, "DB_PATH", tm.ROOT / "data" / "research.sqlite3")
