from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from src.pipeline import T1SealedAccessError, ingest, init_case, read_records, read_t1_sealed_records
from src.schemas import Evidence, HumanReview, SnapshotTag
from src.storage import Store


def test_p0_case_config_templates_and_evidence_schema_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    config = (root / "config" / "case_us_military_ai.yaml").read_text(encoding="utf-8")
    assert "case_id: us_military_ai_deployment" in config
    assert "2024-12-31T23:59:59+00:00" in config
    assert "2026-06-30T23:59:59+00:00" in config
    for name in ("source_manifest_t0.csv", "source_manifest_t1.csv", "search_log_t0.csv", "search_log_t1.csv"):
        with (root / "data" / "curated" / name).open(newline="", encoding="utf-8") as handle:
            assert next(csv.reader(handle))
    assert (root / "prompts" / "luna_source_collection.txt").exists()
    assert (root / "prompts" / "luna_evidence_extraction.txt").exists()
    evidence = Evidence(
        case_id="p0",
        evidence_id="EV-P0",
        snapshot_tag=SnapshotTag.T0,
        title="Synthetic",
        source_type="synthetic",
        source_grade="A",
        publisher="fixture",
        published_at="2024-01-01",
        url_or_path="synthetic://p0",
        excerpt="legacy excerpt",
        normalized_claim="legacy claim",
        reviewed_by="test",
    )
    assert evidence.evidence_nature == "legacy_unclassified"
    assert evidence.excerpt_original == ""
    assert evidence.excerpt_zh == ""
    assert evidence.coding_dimensions == []


def test_p0_t1_sealed_directory_is_locked_until_g4_t0(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    shutil.copy(project_root / "config" / "case_synthetic.yaml", tmp_path / "case.yaml")
    init_case(tmp_path, tmp_path / "case.yaml", run_id="RUN-P0-INIT")
    sealed = tmp_path / "data" / "raw" / "t1_sealed" / "synthetic_t1.jsonl"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(project_root / "tests" / "fixtures" / "p0" / "t1_sealed" / "synthetic_t1.jsonl", sealed)

    with pytest.raises(T1SealedAccessError, match="G4_T0"):
        ingest(tmp_path, case_id="synthetic_military_ai", phase=SnapshotTag.T1, input_path=sealed, run_id="RUN-P0-LOCKED")
    with pytest.raises(T1SealedAccessError, match="G4_T0"):
        read_t1_sealed_records(tmp_path, case_id="synthetic_military_ai", input_path=sealed)
    with pytest.raises(T1SealedAccessError, match="Direct reads"):
        read_records(sealed)

    with Store(tmp_path / "data" / "research.sqlite3") as store:
        store.record_human_review(
            HumanReview(
                review_id="REV-P0-G4-T0",
                gate="G4_T0",
                case_id="synthetic_military_ai",
                approved=True,
                reviewer="synthetic-p0-reviewer",
                artifact_path="outputs/p0/report_t0.md",
                artifact_hash="synthetic-hash",
                run_id="RUN-P0-G4",
            )
        )
    assert read_t1_sealed_records(tmp_path, case_id="synthetic_military_ai", input_path=sealed)
