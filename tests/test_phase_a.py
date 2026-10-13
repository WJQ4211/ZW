from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.pipeline import run_synthetic
from src.schemas import (
    DependencyRelation,
    Evidence,
    EvidenceCitation,
    EvidenceSnapshot,
    EvidenceStatus,
    Hypothesis,
    SnapshotTag,
    SourceGrade,
    Stance,
    Viewpoint,
    ViewpointStatus,
)
from src.storage import StorageError, Store


def make_evidence(
    evidence_id: str,
    tag: SnapshotTag = SnapshotTag.T0,
    case_id: str = "synthetic_test",
) -> Evidence:
    return Evidence(
        case_id=case_id,
        evidence_id=evidence_id,
        version=1,
        snapshot_tag=tag,
        title=f"Synthetic {evidence_id}",
        source_type="synthetic_test",
        source_grade=SourceGrade.B,
        publisher="synthetic-test",
        published_at=date(2025, 1, 1) if tag == SnapshotTag.T0 else date(2026, 1, 1),
        url_or_path=f"synthetic://{evidence_id}",
        excerpt=f"Excerpt for {evidence_id}",
        normalized_claim=f"Claim for {evidence_id}",
        topics=[evidence_id.lower()],
        status=EvidenceStatus.ACTIVE,
        reviewed_by="test",
        synthetic=True,
    )


def make_snapshot(store: Store, tag: SnapshotTag = SnapshotTag.T0) -> EvidenceSnapshot:
    evidence = make_evidence(f"EV-{tag.value}", tag, case_id="synthetic_test")
    store.insert_evidence(evidence)
    return store.create_snapshot(
        snapshot_id=f"SNAP-{tag.value}",
        case_id="synthetic_test",
        phase=tag,
        cutoff_time=datetime(2025 if tag == SnapshotTag.T0 else 2026, 12, 31, tzinfo=timezone.utc),
        evidence_versions=[f"EV-{tag.value}:1"],
        frozen_by="test-reviewer",
        run_id="RUN-test",
    )


def make_viewpoint(snapshot_id: str, viewpoint_id: str = "VP-1", version: int = 1, **updates: object) -> Viewpoint:
    data = dict(
        viewpoint_id=viewpoint_id,
        version=version,
        snapshot_id=snapshot_id,
        perspective_id="technical",
        hypothesis_id="HYP-1",
        stance=Stance.SUPPORT,
        judgment="Synthetic judgment",
        supporting_evidence=[EvidenceCitation(evidence_id="EV-T0", version=1, relation=DependencyRelation.SUPPORT)],
        counter_evidence=[],
        key_assumptions=["synthetic assumption"],
        mechanism_claims=["synthetic mechanism"],
        boundary_conditions=["synthetic boundary"],
        falsifiers=["synthetic falsifier"],
        uncertainties=["synthetic uncertainty"],
        status=ViewpointStatus.ADMITTED,
        generator={"kind": "test", "llm_called": False},
    )
    data.update(updates)
    return Viewpoint(**data)


def test_frozen_snapshot_is_immutable(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshot = make_snapshot(store)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.conn.execute(
                "UPDATE evidence_snapshot SET frozen_by='intruder' WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.conn.execute(
                "DELETE FROM snapshot_items WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )


def test_t1_evidence_cannot_leak_into_t0_snapshot(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        evidence = make_evidence("EV-T1", SnapshotTag.T1)
        store.insert_evidence(evidence)
        with pytest.raises(StorageError, match="T0 snapshot"):
            store.create_snapshot(
                snapshot_id="SNAP-INVALID",
                case_id="synthetic_test",
                phase=SnapshotTag.T0,
                cutoff_time=datetime(2026, 12, 31, tzinfo=timezone.utc),
                evidence_versions=["EV-T1:1"],
                frozen_by="test",
                run_id="RUN-test",
            )


def test_viewpoint_citation_must_exist_in_snapshot(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshot = make_snapshot(store)
        invalid = make_viewpoint(
            snapshot.snapshot_id,
            supporting_evidence=[EvidenceCitation(evidence_id="EV-MISSING", version=1)],
        )
        with pytest.raises(StorageError, match="outside snapshot"):
            store.insert_viewpoint(invalid)


def test_viewpoint_versions_append_and_keep_parent(tmp_path: Path) -> None:
    with Store(tmp_path / "research.sqlite3") as store:
        snapshot = make_snapshot(store)
        first = make_viewpoint(snapshot.snapshot_id)
        store.insert_viewpoint(first)
        second = make_viewpoint(
            snapshot.snapshot_id,
            version=2,
            parent_version=1,
            stance=Stance.CONDITIONAL,
            status=ViewpointStatus.CONDITIONAL,
            change_reason="Synthetic T1 update",
        )
        store.insert_viewpoint(second)
        assert store.get_viewpoint("VP-1", 1).judgment == "Synthetic judgment"
        assert store.get_viewpoint("VP-1", 2).parent_version == 1
        assert store.get_viewpoint("VP-1", 2).stance == Stance.CONDITIONAL
        invalid = make_viewpoint(snapshot.snapshot_id, version=3, parent_version=None)
        with pytest.raises(StorageError, match="previous version"):
            store.insert_viewpoint(invalid)


def test_synthetic_acceptance_runs_local_update_only(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "config", tmp_path / "config")
    shutil.copytree(project_root / "fixtures", tmp_path / "fixtures")
    shutil.copytree(project_root / "data" / "raw", tmp_path / "data" / "raw")

    result = run_synthetic(tmp_path)
    assert len(result["updated"]) == 2
    assert set(result["unchanged"]) == {"VP-OPS-A", "VP-TECH-B"}

    with Store(tmp_path / "data" / "research.sqlite3") as store:
        t0 = store.get_snapshot(result["t0_snapshot"])
        t1 = store.get_snapshot(result["t1_snapshot"])
        assert t0.phase == SnapshotTag.T0
        assert t1.phase == SnapshotTag.T1
        assert len(store.list_hypotheses(t0.snapshot_id)) == 2
        assert len(store.list_viewpoints(t0.snapshot_id)) == 4
        assert store.get_viewpoint("VP-TECH-A", 1).version == 1
        assert store.get_viewpoint("VP-TECH-A", 2).parent_version == 1
        assert store.get_viewpoint("VP-OPS-A", 1) is not None
        assert store.get_viewpoint("VP-OPS-A", 2) is None
        assert len(store.list_disagreements(t0.snapshot_id)) == 2

    report = (tmp_path / "outputs" / result["run_id"] / "report_t1.md").read_text(encoding="utf-8")
    assert "EV-T1-001:1" in report
    assert "VP-OPS-A" in report
