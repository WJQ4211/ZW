import csv
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.model_client import RAW_CANDIDATE_FIELDS
from src.schemas import Evidence, EvidenceCandidate, SnapshotTag


ROOT = Path(__file__).resolve().parents[1]


def _latest_manifest() -> tuple[Path, dict]:
    manifests = sorted((ROOT / "outputs").glob("P3A-*/p3a_manifest.json"))
    assert manifests, "P3a manifest is required"
    path = manifests[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_evidence_candidate_forbids_deterministic_metadata():
    payload = {
        "source_id": "T0-SP-007",
        "source_locator": "pdf_page_4",
        "excerpt_original": "A verbatim excerpt.",
        "excerpt_zh": "逐字摘录。",
        "normalized_claim": "A bounded claim.",
        "coding_dimensions": ["trust_governance_constraints"],
        "evidence_nature": "goal",
        "topics": ["governance"],
    }
    candidate = EvidenceCandidate(**payload)
    assert set(payload) == RAW_CANDIDATE_FIELDS
    assert candidate.source_id == "T0-SP-007"
    with pytest.raises(ValidationError):
        EvidenceCandidate(**{**payload, "evidence_id": "EV-ILLEGAL"})


def test_evidence_supports_month_precision_and_program_excerpt_hashing():
    evidence = Evidence(
        case_id="p3a-test",
        evidence_id="EV-P3A-TEST",
        snapshot_tag=SnapshotTag.T0,
        title="Synthetic",
        source_type="synthetic",
        source_grade="A",
        publisher="fixture",
        published_at="2022-06-01",
        published_at_precision="month",
        url_or_path="synthetic://source",
        page_or_section="pdf_page_4",
        excerpt="A verbatim excerpt.",
        excerpt_original="A verbatim excerpt.",
        normalized_claim="A claim.",
        reviewed_by="fixture",
    )
    assert evidence.published_at.isoformat() == "2022-06-01"
    assert evidence.published_at_precision == "month"
    assert len(evidence.content_hash) == 0


def test_raw_bridge_outputs_contain_only_eight_model_fields():
    _, manifest = _latest_manifest()
    output_dir = ROOT / "outputs" / manifest["run_id"] / "raw_model_outputs"
    response_files = sorted(output_dir.glob("*.json"))
    response_files = [path for path in response_files if not path.name.endswith(".meta.json")]
    assert len(response_files) == 3
    for path in response_files:
        for item in json.loads(path.read_text(encoding="utf-8")):
            assert set(item) == RAW_CANDIDATE_FIELDS


def test_p3a_candidates_are_injected_and_not_formal_evidence():
    manifest_path, manifest = _latest_manifest()
    run_dir = manifest_path.parent
    candidates = _jsonl(run_dir / "evidence_candidates_pilot.jsonl")
    assert set(manifest["source_ids"]) == {"T0-SP-007", "T0-TD-008"}
    assert len(candidates) == 11
    assert all(item["source_id"] in manifest["source_ids"] for item in candidates)
    assert all(item["excerpt"] == item["excerpt_original"] for item in candidates)
    assert all(item["reviewed_by"] == "" for item in candidates)
    assert all(item["status"] == "candidate" for item in candidates)
    assert all(len(item["content_hash"]) == 64 for item in candidates)
    assert {item["published_at_precision"] for item in candidates if item["source_id"] == "T0-SP-007"} == {"month"}
    assert {item["published_at_precision"] for item in candidates if item["source_id"] == "T0-TD-008"} == {"day"}
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False


def test_p3a_quote_validation_is_100_percent_and_scope_is_exact():
    manifest_path, manifest = _latest_manifest()
    rows = list(csv.DictReader((manifest_path.parent / "quote_validation.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == 11
    assert all(row["accepted"] == "True" for row in rows)
    assert all(row["exact_match_after_normalization"] == "True" for row in rows)
    assert all(row["source_id"] in {"T0-SP-007", "T0-TD-008"} for row in rows)
    assert manifest["exact_locator_quote_match_rate"] == 1.0
    assert _jsonl(manifest_path.parent / "rejected_candidates.jsonl") == []


def test_p3a_protected_input_hashes_remain_unchanged():
    _, manifest = _latest_manifest()
    assert manifest["protected_p1_p1b_p1c_p2_unchanged"] is True
    for path_string, expected_hash in manifest["input_files"].items():
        if isinstance(expected_hash, dict) and "path" in expected_hash:
            path = ROOT / expected_hash["path"]
            assert path.exists()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash["sha256"]
