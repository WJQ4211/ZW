from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import src.pipeline as pipeline
from src.pipeline import GatePending, apply_viewpoint_patch, approval, read_json, run_synthetic, save_manifest
from src.schemas import (
    DecisionImpact,
    DependencyRelation,
    Disagreement,
    DisagreementStatus,
    DisagreementType,
    DiscriminativeNeed,
    Evidence,
    EvidenceCitation,
    EvidenceStatus,
    Feasibility,
    Hypothesis,
    Importance,
    IndicatorCadence,
    IndicatorStatus,
    MonitorIndicator,
    PredicateType,
    SnapshotTag,
    SourceGrade,
    Stance,
    TriggerRule,
    Viewpoint,
    ViewpointStatus,
)
from src.storage import StorageError, Store


def make_evidence(case_id: str, evidence_id: str, tag: SnapshotTag = SnapshotTag.T0) -> Evidence:
    return Evidence(
        case_id=case_id,
        evidence_id=evidence_id,
        version=1,
        snapshot_tag=tag,
        title=f"{case_id} {evidence_id}",
        source_type="synthetic_test",
        source_grade=SourceGrade.A,
        publisher="test",
        published_at=date(2025, 1, 1) if tag == SnapshotTag.T0 else date(2026, 1, 1),
        url_or_path=f"synthetic://{case_id}/{evidence_id}",
        excerpt=f"Excerpt for {case_id}/{evidence_id}",
        normalized_claim=f"Claim for {case_id}/{evidence_id}",
        topics=[evidence_id.lower()],
        status=EvidenceStatus.ACTIVE,
        reviewed_by="test",
        synthetic=True,
    )


def make_snapshot(store: Store, case_id: str, snapshot_id: str, evidence_id: str = "EV-SHARED"):
    store.insert_evidence(make_evidence(case_id, evidence_id))
    return store.create_snapshot(
        snapshot_id=snapshot_id,
        case_id=case_id,
        phase=SnapshotTag.T0,
        cutoff_time=datetime(2025, 12, 31, tzinfo=timezone.utc),
        evidence_versions=[f"{evidence_id}:1"],
        frozen_by="test-reviewer",
        run_id=f"RUN-{case_id}",
    )


def make_viewpoint(snapshot_id: str, evidence_id: str, viewpoint_id: str) -> Viewpoint:
    return Viewpoint(
        viewpoint_id=viewpoint_id,
        version=1,
        snapshot_id=snapshot_id,
        perspective_id="technical",
        hypothesis_id="HYP-1",
        stance=Stance.SUPPORT,
        judgment=f"Judgment {viewpoint_id}",
        supporting_evidence=[EvidenceCitation(evidence_id=evidence_id, version=1, relation=DependencyRelation.SUPPORT)],
        key_assumptions=["assumption"],
        mechanism_claims=["mechanism"],
        boundary_conditions=["boundary"],
        falsifiers=["falsifier"],
        uncertainties=["uncertainty"],
        status=ViewpointStatus.ADMITTED,
        generator={"kind": "test", "llm_called": False},
    )


def copy_synthetic_workspace(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "config", tmp_path / "config")
    shutil.copytree(project_root / "fixtures", tmp_path / "fixtures")
    shutil.copytree(project_root / "data" / "raw", tmp_path / "data" / "raw")


def test_approval_binding_cannot_cross_gate_case_or_changed_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"version": 1}\n', encoding="utf-8")
    with Store(tmp_path / "research.sqlite3") as store:
        with pytest.raises(GatePending) as pending_error:
            approval(store, tmp_path, case_id="case-a", gate="G1_T0", artifact_path=artifact, approval_file=None, run_id="RUN-A")
        pending_path = pending_error.value.path
        pending = read_json(pending_path)
        assert {"case_id", "gate", "run_id", "artifact_path", "artifact_hash", "created_at"} <= set(pending)
        pending.update({"approved": True, "reviewer": "reviewer-a", "notes": "ok"})
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        approval(store, tmp_path, case_id="case-a", gate="G1_T0", artifact_path=artifact, approval_file=pending_path, run_id="RUN-A")

        with pytest.raises(GatePending):
            approval(store, tmp_path, case_id="case-a", gate="G1_T1", artifact_path=artifact, approval_file=None, run_id="RUN-A")
        with pytest.raises(StorageError, match="gate"):
            approval(store, tmp_path, case_id="case-a", gate="G1_T1", artifact_path=artifact, approval_file=pending_path, run_id="RUN-A")

        artifact.write_text('{"version": 2}\n', encoding="utf-8")
        with pytest.raises(StorageError, match="artifact_hash"):
            approval(store, tmp_path, case_id="case-a", gate="G1_T0", artifact_path=artifact, approval_file=pending_path, run_id="RUN-A")


def test_two_cases_keep_evidence_viewpoints_disagreements_and_monitoring_separate(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshots = {}
        for case_id in ("case-a", "case-b"):
            snapshot = make_snapshot(store, case_id, f"SNAP-{case_id}")
            snapshots[case_id] = snapshot
            store.insert_viewpoint(make_viewpoint(snapshot.snapshot_id, "EV-SHARED", f"VP-{case_id}-A"))
            store.insert_viewpoint(make_viewpoint(snapshot.snapshot_id, "EV-SHARED", f"VP-{case_id}-B"))
            disagreement = Disagreement(
                disagreement_id=f"DG-{case_id}",
                snapshot_id=snapshot.snapshot_id,
                viewpoint_refs=[f"VP-{case_id}-A:1", f"VP-{case_id}-B:1"],
                type=DisagreementType.MECHANISM,
                contested_node="mechanism",
                branch_a={"claim": "A"},
                branch_b={"claim": "B"},
                decision_impact=DecisionImpact.HIGH,
                resolvable_now=False,
                resolution_action="collect more evidence",
                status=DisagreementStatus.OPEN,
            )
            store.insert_disagreement(disagreement)
            need = DiscriminativeNeed(
                need_id=f"NEED-{case_id}",
                disagreement_id=disagreement.disagreement_id,
                question="Which branch is better supported?",
                possible_outcomes=["A", "B"],
                favours_if={"A": "A", "B": "B"},
                source_plan=["official record"],
                observation_window="next quarter",
                feasibility=Feasibility.MEDIUM,
            )
            store.insert_need(need)
            indicator = MonitorIndicator(
                indicator_id=f"IND-{case_id}",
                need_id=need.need_id,
                name=f"Indicator {case_id}",
                operational_definition="count of relevant official events",
                source_types=["official"],
                cadence=IndicatorCadence.MONTHLY,
                direction_a="up",
                direction_b="down",
                status=IndicatorStatus.APPROVED,
            )
            store.insert_indicator(indicator)
            store.insert_trigger(
                TriggerRule(
                    trigger_id=f"TRG-{case_id}",
                    indicator_id=indicator.indicator_id,
                    predicate_type=PredicateType.EVENT_MATCH,
                    predicate={"event_terms": [case_id]},
                    required_source_grade=[SourceGrade.A],
                    action="review",
                    status=IndicatorStatus.APPROVED,
                )
            )

        assert {e.case_id for e in store.list_evidence(case_id="case-a")} == {"case-a"}
        assert store.get_evidence("EV-SHARED", 1, "case-b").case_id == "case-b"
        assert store.get_snapshot("SNAP-case-b", "case-a") is None
        assert {v.viewpoint_id for v in store.list_viewpoints(case_id="case-a")} == {"VP-case-a-A", "VP-case-a-B"}
        assert store.list_disagreements(snapshots["case-b"].snapshot_id, "case-a") == []
        assert [n.need_id for n in store.list_needs(case_id="case-a")] == ["NEED-case-a"]
        assert [i.indicator_id for i in store.list_indicators(case_id="case-b")] == ["IND-case-b"]
        assert [t.trigger_id for t in store.list_triggers(case_id="case-a")] == ["TRG-case-a"]


def test_frozen_snapshot_and_history_are_append_only_at_storage_and_db_levels(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshot = make_snapshot(store, "case-a", "SNAP-A", "EV-A")
        store.insert_evidence(make_evidence("case-a", "EV-NEW"))
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            store.conn.execute(
                "INSERT INTO snapshot_items(snapshot_id, case_id, evidence_id, version) VALUES (?, ?, ?, ?)",
                (snapshot.snapshot_id, "case-a", "EV-NEW", 1),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.conn.execute("UPDATE evidence SET content_hash='mutated' WHERE case_id='case-a'")
        viewpoint = make_viewpoint(snapshot.snapshot_id, "EV-A", "VP-A")
        store.insert_viewpoint(viewpoint)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.conn.execute("UPDATE viewpoint SET status='retired' WHERE viewpoint_id='VP-A'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.conn.execute("DELETE FROM viewpoint WHERE viewpoint_id='VP-A'")


def test_t1_trigger_evidence_is_explicit_dependency_with_changed_fields(tmp_path: Path) -> None:
    copy_synthetic_workspace(tmp_path)
    result = run_synthetic(tmp_path)
    with Store(tmp_path / "data" / "research.sqlite3") as store:
        dependencies = store.list_dependencies(viewpoint_id="VP-TECH-A", viewpoint_version=2, case_id="synthetic_military_ai")
        trigger_dependencies = [d for d in dependencies if d.evidence_id == "EV-T1-001"]
        assert trigger_dependencies
        assert all(d.viewpoint_version == 2 for d in trigger_dependencies)
        assert all({"stance", "judgment", "key_assumptions[0]"} <= set(d.changed_fields) for d in trigger_dependencies)
    report = (tmp_path / "outputs" / result["run_id"] / "report_t1.md").read_text(encoding="utf-8")
    assert "T1 trigger evidence: `EV-T1-001:1` support" in report


def test_human_can_add_a_missed_viewpoint_without_overwriting_system_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy_synthetic_workspace(tmp_path)
    affected_path = tmp_path / "fixtures" / "approvals" / "affected_t1.json"
    affected = json.loads(affected_path.read_text(encoding="utf-8"))
    affected["override_reasons"] = {"VP-OPS-B": "专家判断操作授权变化也影响该观点，系统触发器未命中。"}
    affected_path.write_text(json.dumps(affected, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        pipeline,
        "locate_affected",
        lambda *args, **kwargs: ({"VP-TECH-A": ["manual-test-candidate"]}, {"VP-TECH-A": ["EV-T1-001:1"]}),
    )
    result = run_synthetic(tmp_path)
    assert result["system_candidates"] == ["VP-TECH-A"]
    assert result["missed"] == ["VP-OPS-B"]
    affected_output = json.loads((tmp_path / "outputs" / result["run_id"] / "affected_viewpoints.json").read_text(encoding="utf-8"))
    assert affected_output["system_candidates"] == ["VP-TECH-A"]
    assert affected_output["missed"] == ["VP-OPS-B"]
    change_log = json.loads((tmp_path / "outputs" / result["run_id"] / "change_log.json").read_text(encoding="utf-8"))
    assert next(row for row in change_log if row["viewpoint_id"] == "VP-OPS-B")["impact_classification"] == "human_override/system_false_negative"


def test_before_after_strictly_validates_list_paths(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshot = make_snapshot(store, "case-a", "SNAP-A", "EV-A")
        old = make_viewpoint(snapshot.snapshot_id, "EV-A", "VP-A")
        store.insert_viewpoint(old)
        base = {
            "changed_fields": ["key_assumptions[0]"],
            "changes": {"key_assumptions[0]": "changed"},
            "before": {"key_assumptions[0]": "assumption"},
            "after": {"key_assumptions[0]": "wrong"},
            "change_reason": "test list patch",
            "trigger_relations": [],
        }
        with pytest.raises(StorageError, match="after-value"):
            apply_viewpoint_patch(store, old=old, patch=base, to_snapshot_id=snapshot.snapshot_id, run_id="RUN-T1")
        missing_path = dict(base)
        missing_path["changed_fields"] = ["key_assumptions[1]"]
        missing_path["changes"] = {"key_assumptions[1]": "missing"}
        missing_path["before"] = {"key_assumptions[1]": "missing"}
        missing_path["after"] = {"key_assumptions[1]": "missing"}
        with pytest.raises(StorageError, match="list item"):
            apply_viewpoint_patch(store, old=old, patch=missing_path, to_snapshot_id=snapshot.snapshot_id, run_id="RUN-T1")


def test_no_viewpoint_update_is_a_valid_completed_path(tmp_path: Path) -> None:
    copy_synthetic_workspace(tmp_path)
    result = run_synthetic(tmp_path)
    run_id = "phase-a-no-change-test"
    from src.pipeline import update_t1
    no_change_input = tmp_path / "fixtures" / "no_change.json"
    no_change_input.write_text('{"patches": []}\n', encoding="utf-8")

    affected_file = None
    report_file = None
    for _ in range(3):
        try:
            output = update_t1(
                tmp_path,
                case_id="synthetic_military_ai",
                from_snapshot_id=result["t0_snapshot"],
                to_snapshot_id=result["t1_snapshot"],
                input_path=no_change_input,
                affected_approval_file=affected_file,
                report_approval_file=report_file,
                run_id=run_id,
            )
            break
        except GatePending as pending_error:
            data = read_json(pending_error.path)
            data.update({"approved": True, "reviewer": "no-change-reviewer", "approved_affected": [], "notes": "no active viewpoint requires update"})
            pending_error.path.write_text(json.dumps(data), encoding="utf-8")
            if pending_error.gate == "G4_T1_AFFECTED":
                affected_file = pending_error.path
            elif pending_error.gate == "G4_T1_REPORT":
                report_file = pending_error.path
    else:  # pragma: no cover
        raise AssertionError("no-change path did not complete")
    assert output["updated"] == []
    assert len(output["unchanged"]) == 4
    assert "没有观点需要更新" in (tmp_path / "outputs" / run_id / "report_t1.md").read_text(encoding="utf-8")


def test_pipeline_stage_records_are_append_only_and_model_calls_are_separate(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        store.record_model_run("RUN-1", "case-a", "A", "ingest", {"step": 1})
        store.record_model_run("RUN-1", "case-a", "T1", "update", {"step": 2})
        store.record_model_call(run_id="RUN-1", case_id="case-a", phase="T1", payload={"provider": "none"})
        assert store.conn.execute("SELECT COUNT(*) FROM pipeline_run WHERE run_id='RUN-1'").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM pipeline_stage WHERE run_id='RUN-1'").fetchone()[0] == 2
        assert store.conn.execute("SELECT COUNT(*) FROM model_call WHERE run_id='RUN-1'").fetchone()[0] == 1
        assert {row["stage_name"] for row in store.conn.execute("SELECT stage_name FROM pipeline_stage WHERE run_id='RUN-1'")} == {"ingest", "update"}


def test_manifest_input_hash_changes_with_input_content(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text('{"value": 1}\n', encoding="utf-8")
    first = save_manifest(tmp_path, "RUN-M", "case-a", stage="A+", artifacts={"input": "input.json"}, input_paths=[source], config={"mode": "synthetic"})
    first_data = json.loads(first.read_text(encoding="utf-8"))
    source.write_text('{"value": 2}\n', encoding="utf-8")
    second = save_manifest(tmp_path, "RUN-M", "case-a", stage="A+", artifacts={"input": "input.json"}, input_paths=[source], config={"mode": "synthetic"})
    second_data = json.loads(second.read_text(encoding="utf-8"))
    assert first_data["input_files"]["input.json"] != second_data["input_files"]["input.json"]
    assert first_data["reproducibility"]["input_hash"] != second_data["reproducibility"]["input_hash"]
    assert second_data["config_hash"]
    assert second_data["code_version"]
