"""Offline regression tests for P3S-H hardening.

These tests use synthetic candidates and never construct or call a live API
client.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.model_client import ModelAPIError
from src.p3ar import SourceSegment
from src.p3s import (
    EXTRACTION_TEMPLATE,
    P3S_MODEL_FIELDS,
    P3SDocument,
    _build_extraction_prompt,
    _field_audit,
    _load_yaml,
    _protected_output_hashes,
    _write_failure_manifest,
    candidate_count_exceeds_limit,
    checkpoint_identity,
    checkpoint_reuse_allowed,
    semantic_skip_reason,
)


def _synthetic_document() -> P3SDocument:
    segment = SourceSegment(
        source_id="SYN-001",
        source_locator="web_paragraph_0001",
        text="[PLACEHOLDER_SOURCE_TEXT]",
    )
    mapping = {
        "source_id": "SYN-001",
        "source_locator": "web_paragraph_0001",
        "character_start": 0,
        "character_end": len(segment.text),
        "text": segment.text,
    }
    return P3SDocument(
        source={"source_id": "SYN-001"},
        segments=(segment,),
        paragraph_map=(mapping,),
        source_text_sha256="synthetic-source-sha",
    )


def test_extraction_prompt_has_only_eight_model_fields_and_placeholder_example():
    template = _load_yaml(EXTRACTION_TEMPLATE)
    assert template["version"] == "1.2"
    exact_fields = template["output_schema"]["exact_fields"]
    assert len(exact_fields) == 8
    assert set(exact_fields) == P3S_MODEL_FIELDS
    assert "source_id" not in exact_fields
    assert template["output_schema"]["example"][0]["excerpt_original"].startswith("[PLACEHOLDER_")
    prompt = _build_extraction_prompt(_synthetic_document(), template, "SYN-001-extract-001")
    assert "exactly eight fields" in prompt
    assert "ten fields" not in prompt.lower()
    assert "```" not in prompt
    assert "source_id" not in prompt.split("OUTPUT:", 1)[1].split("Do not produce", 1)[0]


def test_model_schema_actual_missing_and_extra_fields_are_recorded():
    candidate = {
        "source_locator": "web_paragraph_0001",
        "excerpt_original": "[PLACEHOLDER_SOURCE_TEXT]",
        "excerpt_zh": "[占位译文]",
        "normalized_claim": "[占位命题]",
        "evidence_nature": "goal",
        "coding_dimensions": ["application_maturity"],
        "topics": [],
        "possible_corroboration_group": "",
        "forbidden_extra": True,
    }
    actual, missing, extra = _field_audit(candidate)
    assert "forbidden_extra" in extra
    assert missing == []
    assert set(actual) == P3S_MODEL_FIELDS | {"forbidden_extra"}


def test_more_than_six_candidates_is_rejected_without_truncation():
    values = [{"candidate_index": index} for index in range(7)]
    assert len(values) == 7
    assert candidate_count_exceeds_limit(values)
    assert not candidate_count_exceeds_limit(values[:6])


def test_empty_valid_candidates_skip_semantic_call():
    assert semantic_skip_reason([]) == "semantic_skipped_no_valid_candidates"
    assert semantic_skip_reason([object()]) is None


def test_checkpoint_reuse_requires_same_hashes_and_successful_status():
    identity = checkpoint_identity("RUN-SYN", {"SYN-001": "source-sha"}, "extract-sha", "semantic-sha")
    checkpoint = {
        "status": "in_progress",
        "checkpoint_identity": identity,
        "prompt_sha256_by_batch": {"SYN-001-extract-001": "prompt-sha"},
        "completed_batches": ["SYN-001-extract-001"],
        "call_records": {"SYN-001-extract-001": {"phase": "extraction", "prompt_sha256": "prompt-sha"}},
    }
    assert checkpoint_reuse_allowed(checkpoint, identity, "SYN-001-extract-001", "prompt-sha")
    assert not checkpoint_reuse_allowed(checkpoint, identity, "SYN-001-extract-001", "changed-prompt-sha")
    changed_identity = checkpoint_identity("RUN-SYN", {"SYN-001": "changed-source-sha"}, "extract-sha", "semantic-sha")
    assert not checkpoint_reuse_allowed(checkpoint, changed_identity, "SYN-001-extract-001", "prompt-sha")
    checkpoint["status"] = "failed"
    assert checkpoint_reuse_allowed(checkpoint, identity, "SYN-001-extract-001", "prompt-sha")
    checkpoint["status"] = "completed"
    assert not checkpoint_reuse_allowed(checkpoint, identity, "SYN-001-extract-001", "prompt-sha")


def test_ssl_eof_writes_only_redacted_failure_manifest(tmp_path: Path):
    error = ModelAPIError(
        "redacted",
        exception_type="SSLEOFError",
        http_status_code=None,
        attempts=4,
        retry_count=3,
    )
    _write_failure_manifest(
        tmp_path,
        "RUN-SYN",
        stage="extraction",
        source_id="SYN-001",
        error=error,
        completed_logical_calls=1,
        saved_batches=["SYN-001-extract-001"],
    )
    payload = json.loads((tmp_path / "p3s_failure_manifest.json").read_text(encoding="utf-8"))
    assert set(payload) == {
        "run_id",
        "failure_stage",
        "source_id",
        "exception_type",
        "http_status_code",
        "completed_logical_calls",
        "saved_batches",
        "boundary_state",
    }
    assert payload["exception_type"] == "SSLEOFError"
    assert payload["http_status_code"] is None
    assert "authorization" not in json.dumps(payload).lower()
    assert "api_key" not in json.dumps(payload).lower()


def test_api_key_is_not_written_by_failure_manifest(tmp_path: Path):
    _write_failure_manifest(
        tmp_path,
        "RUN-SYN",
        stage="semantic",
        source_id="SYN-001",
        error=RuntimeError("provider error redacted"),
        completed_logical_calls=0,
        saved_batches=[],
    )
    assert all(b"sk-sp-" not in path.read_bytes() for path in tmp_path.rglob("*"))


def test_protected_history_hash_snapshot_is_stable():
    before = _protected_output_hashes(Path("tests"))
    after = _protected_output_hashes(Path("tests"))
    assert before == after
