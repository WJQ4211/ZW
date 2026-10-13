from __future__ import annotations

import json
import csv
import sqlite3
from pathlib import Path

from src.p7r_p9r import HYPOTHESIS_IDS, OLD_RUN_DIR, PERSPECTIVE_ORDER, ROOT, SNAPSHOT_ID, _validate_monitoring_r, _validate_p9r_disagreements


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_prior_run_audit_facts_are_read_only() -> None:
    manifest = json.loads((OLD_RUN_DIR / "p7_p9_manifest.json").read_text(encoding="utf-8"))
    assert manifest["formal_admitted_viewpoint_count"] == 0
    assert manifest["dependency_edge_count"] == 0


def test_recovery_run_has_five_by_three_slots_when_available() -> None:
    runs = sorted((ROOT / "outputs").glob("P7R-P9R-*"))
    if not runs or not (runs[-1] / "P7R" / "initial_viewpoints_15.jsonl").exists():
        return
    rows = _jsonl(runs[-1] / "P7R" / "initial_viewpoints_15.jsonl")
    assert len(rows) == 15
    assert len({row["viewpoint_id"] for row in rows}) == 15
    assert {(row["perspective_id"], row["hypothesis_id"]) for row in rows} == {(p, h) for p in PERSPECTIVE_ORDER for h in HYPOTHESIS_IDS}


def test_target_database_history_has_old_suspended_versions() -> None:
    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        row = conn.execute("SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=? AND version=1 AND status='suspended'", (SNAPSHOT_ID,)).fetchone()
        assert row[0] >= 12
    finally:
        conn.close()


def test_p9r_monitoring_rejects_numeric_threshold_and_non_discriminative() -> None:
    views = {"VP": {"version": 1, "perspective_id": "TECH", "hypothesis_id": "HYP-T0-01"}}
    disagreements = [{"disagreement_id": "D", "decision_impact": "high", "resolvable_now": False, "resolution_action": "monitor"}]
    payload = {"discriminative_needs": [{"need_id": "N", "disagreement_id": "D", "question": "q", "what_to_observe": "o", "outcome_a": "a", "outcome_b": "b", "public_sources": ["A"], "observation_frequency": "event", "affected_viewpoint_ids": ["VP"], "discriminativeness": "non_discriminative"}], "monitor_indicators": [], "trigger_rules": []}
    needs, indicators, triggers, rejected = _validate_monitoring_r(payload, disagreements, views)
    assert needs == [] and indicators == [] and triggers == []
    assert any(row["kind"] == "non_discriminative" for row in rejected)


def _latest_recovery_dir() -> Path | None:
    runs = sorted(ROOT.joinpath("outputs").glob("P7R-P9R-*"))
    return runs[-1] if runs else None


def test_p9r_disagreements_use_latest_viewpoint_refs() -> None:
    run = _latest_recovery_dir()
    if run is None or not (run / "P9R" / "candidate_disagreements.jsonl").exists():
        return
    disagreements = _jsonl(run / "P9R" / "candidate_disagreements.jsonl")
    assert len(disagreements) == 3
    for disagreement in disagreements:
        assert len({ref["viewpoint_id"] for ref in disagreement["viewpoint_refs"]}) >= 2
        assert all(isinstance(ref.get("version"), int) and isinstance(ref.get("field"), str) for ref in disagreement["viewpoint_refs"])
        assert all(ref["viewpoint_id"].startswith("VP-T0-") for ref in disagreement["viewpoint_refs"])


def test_recovery_database_and_case_exhibit_are_append_only() -> None:
    run = _latest_recovery_dir()
    if run is None:
        return
    manifest = json.loads((run / "p7r_p9r_manifest.json").read_text(encoding="utf-8"))
    assert manifest["historical_artifacts_unchanged"] is True
    assert manifest["formal_evidence_inserted"] == 0
    assert manifest["author_decision_count"] == 0
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["t1_read"] is False
    assert manifest["dependency_edge_count"] > 0
    for name in (
        "figure_case_dependency_graph.graphml",
        "figure_case_dependency_graph.dot",
        "figure_case_dependency_nodes.csv",
        "figure_case_dependency_edges.csv",
    ):
        assert (run / "CASE_EXHIBIT" / name).exists()


def test_g3_author_fields_are_empty() -> None:
    run = _latest_recovery_dir()
    if run is None:
        return
    path = run / "G3_PREP_R" / "g3r_author_decision_template.csv"
    reader = csv.DictReader(path.read_text(encoding="utf-8").splitlines())
    rows = list(reader)
    required = {"author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"}
    assert required.issubset(set(reader.fieldnames or []))
    for row in rows:
        assert all(not row[field] for field in required)
