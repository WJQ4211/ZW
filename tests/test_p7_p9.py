"""Offline acceptance checks for the P7-P9/G3 preparation run."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from src.p7_p9 import HYPOTHESIS_IDS, P4_DIR, PERSPECTIVES, ROOT, SNAPSHOT_ID, _parse_json_compat


RUN_GLOB = "P7P9-*-t0-viewpoint-disagreement-prep-v1"


def _run_dir() -> Path:
    runs = sorted((ROOT / "outputs").glob(RUN_GLOB))
    assert runs
    return runs[-1]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_json_compat_accepts_fence_and_single_wrapper() -> None:
    assert _parse_json_compat("```json\n[{\"x\": 1}]\n```") == [{"x": 1}]
    assert _parse_json_compat('{"viewpoints": [{"x": 1}]}') == {"viewpoints": [{"x": 1}]}


def test_p7_covers_all_perspective_hypothesis_slots() -> None:
    initial = _jsonl(_run_dir() / "P7" / "initial_viewpoints.jsonl")
    assert len(initial) == 12
    assert len({row["viewpoint_id"] for row in initial}) == 12
    assert {(row["perspective_id"], row["hypothesis_id"]) for row in initial} == {(p, h) for p in PERSPECTIVES for h in HYPOTHESIS_IDS}


def test_final_viewpoints_and_dependencies_use_frozen_evidence() -> None:
    run = _run_dir()
    manifest = json.loads((run / "p7_p9_manifest.json").read_text(encoding="utf-8"))
    evidence = {row["evidence_id"]: row for row in _jsonl(P4_DIR / "p4_t0_frozen_evidence.jsonl")}
    final = _jsonl(run / "P8" / "final_viewpoints.jsonl")
    assert len(final) == 12
    assert len({row["viewpoint_id"] for row in final}) == 12
    assert all(row["snapshot_id"] == SNAPSHOT_ID and row["version"] == 1 and row["human_verified"] is False for row in final)
    assert all(ref["evidence_id"] in evidence and ref["version"] == evidence[ref["evidence_id"]]["version"] for row in final for ref in row["supporting_evidence"] + row["counter_evidence"])
    deps = _jsonl(run / "P8" / "evidence_dependencies.jsonl")
    view_ids = {row["viewpoint_id"] for row in final}
    assert all(dep["viewpoint_id"] in view_ids and dep["viewpoint_version"] == 1 and dep["human_verified"] is False for dep in deps)
    assert all(dep["evidence_id"] in evidence and dep["evidence_version"] == evidence[dep["evidence_id"]]["version"] for dep in deps)
    assert manifest["all_evidence_ids_in_snapshot"] is True
    assert manifest["field_level_dependencies_complete"] is True


def test_goal_and_deployment_boundary_is_in_prompt() -> None:
    run = _run_dir()
    prompts = "\n".join(path.read_text(encoding="utf-8") for path in (run / "prompts").glob("*.txt"))
    assert "战略目标、计划和预期能力不得写成已实现能力" in prompts
    assert "不得投票、排名" in prompts
    assert "data/raw/t1_sealed" not in prompts


def test_challenges_point_to_valid_viewpoints_and_fields() -> None:
    run = _run_dir()
    valid = {row["viewpoint_id"] for row in _jsonl(run / "P8" / "final_viewpoints.jsonl")}
    for path in (run / "P8").glob("hypothesis_h*_challenges.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("challenges", []):
            assert row["target_viewpoint_id"] in valid
            assert row["target_field"]
            assert row["challenge_type"] in {"evidence_mismatch", "goal_result_confusion", "mechanism_gap", "assumption_risk", "scope_overreach", "counter_evidence_omitted", "uncertainty_understated"}


def test_revision_trace_exists_for_all_stable_viewpoint_slots() -> None:
    run = _run_dir()
    with (run / "P8" / "viewpoint_before_after_diff.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 12
    assert len({row["viewpoint_id"] for row in rows}) == 12
    assert all(row["version_before"] == "0" and row["version_after"] == "1" for row in rows)


def test_disagreements_have_two_distinct_viewpoints_and_monitor_links() -> None:
    run = _run_dir()
    disagreements = _jsonl(run / "P9" / "candidate_disagreements.jsonl")
    for row in disagreements:
        assert len({ref["viewpoint_id"] for ref in row["viewpoint_refs"]}) >= 2
    indicators = {row["indicator_id"]: row for row in _jsonl(run / "P9" / "monitor_indicators_candidates.jsonl")}
    triggers = _jsonl(run / "P9" / "trigger_rules_candidates.jsonl")
    for row in triggers:
        assert row["indicator_id"] in indicators
        assert row["affected_viewpoint_ids"]
        assert row["required_source_grade"] in {"A", "B"}
        assert row["human_approval_required"] is True


def test_p7_validation_and_boundary_audit_are_explicit() -> None:
    run = _run_dir()
    with (run / "P7" / "viewpoint_validation.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 12
    assert sum(row["initial_validation_passed"] == "True" for row in rows) == 12
    assert (run / "combined_quality_report.md").exists()
    assert "suspended" in (run / "combined_quality_report.md").read_text(encoding="utf-8")


def test_g3_author_fields_are_empty_and_no_formal_g3() -> None:
    run = _run_dir()
    manifest = json.loads((run / "p7_p9_manifest.json").read_text(encoding="utf-8"))
    assert manifest["author_decision_count"] == 0
    assert manifest["formal_g3_executed"] is False
    assert manifest["formal_g4_t0_executed"] is False
    assert manifest["t1_read"] is False
    with (run / "G3_PREP" / "g3_author_decision_template.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        assert all(row[field] == "" for field in ("author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"))


def test_database_transaction_and_historical_protection() -> None:
    run = _run_dir()
    manifest = json.loads((run / "p7_p9_manifest.json").read_text(encoding="utf-8"))
    assert manifest["viewpoint_transaction_committed"] is True
    assert manifest["rollback_test_passed"] is True
    assert manifest["viewpoint_idempotency_test_passed"] is True
    assert manifest["historical_artifacts_unchanged"] is True
    assert manifest["formal_evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["api_key_written"] is False
    conn = sqlite3.connect(str(ROOT / "data" / "research.sqlite3"))
    try:
        recovery_runs = sorted((ROOT / "outputs").glob("P7R-P9R-*/p7r_p9r_manifest.json"))
        expected_viewpoint_rows = 27 if recovery_runs else 12
        g3_runs = sorted((ROOT / "outputs").glob("G3-T0-*/g3_manifest.json"))
        expected_disagreement_rows = 3 if g3_runs else 0
        assert conn.execute("SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0] == expected_viewpoint_rows
        assert conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", ("us_military_ai_deployment",)).fetchone()[0] == 126
        assert conn.execute("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0] == expected_disagreement_rows
    finally:
        conn.close()
