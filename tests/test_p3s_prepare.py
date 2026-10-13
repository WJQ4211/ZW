"""Prepare-stage tests for P3S when the real API is not configured."""

from __future__ import annotations

import json
from pathlib import Path

from src.p3s import P3S_MODEL_FIELDS, _parse_p3s_candidate


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs" / "P3S-20260826T084335Z-supplement-v1"
P2S_DIR = ROOT / "outputs" / "P4Q-P2S-20260826T082341Z-v1"


def test_p3s_prepare_validates_inputs_and_stops_before_api():
    manifest = json.loads((RUN_DIR / "p3s_manifest.json").read_text(encoding="utf-8"))
    assert manifest["prepare_completed"] is True
    assert manifest["p3s_executed"] is False
    assert manifest["stop_reason"] == "model_api_unavailable_at_prepare_stage"
    assert manifest["source_count"] == 5
    assert manifest["logical_extraction_batch_count"] == 5
    assert manifest["logical_semantic_screen_batch_count"] == 5
    assert manifest["canonical_sha256"] == "ac307f553614bd4b6114ecb117d7e2516b91a5e28ae2884c44a14043b4a0900c"
    assert set(manifest["missing_model_environment_variables"]) == {"MODEL_API_BASE_URL", "MODEL_API_KEY", "MODEL_ID"}
    assert manifest["model_api_calls"] == 0
    assert manifest["evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["formal_g1_supplement_executed"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["p5_executed"] is False
    assert manifest["t1_read"] is False
    assert manifest["author_actual_review_count"] == 0
    assert manifest["api_key_written"] is False
    assert not (RUN_DIR / "raw_model_outputs").exists()
    assert not (RUN_DIR / "evidence_candidates_p3s_final.jsonl").exists()


def test_p3s_paragraph_anchors_are_sequential_and_exact():
    rows = [json.loads(line) for line in (RUN_DIR / "paragraph_anchor_map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {row["source_id"] for row in rows} == {"T0-SP-001", "T0-TD-001", "T0-TD-002", "T0-TD-003", "T0-TD-005"}
    for source_id in {row["source_id"] for row in rows}:
        source_rows = [row for row in rows if row["source_id"] == source_id]
        assert [row["paragraph_id"] for row in source_rows] == [f"web_paragraph_{i:04d}" for i in range(1, len(source_rows) + 1)]
        source_text = (ROOT / source_rows[0]["source_text_path"]).read_text(encoding="utf-8")
        for row in source_rows:
            assert source_text[row["character_start"] : row["character_end"]] == row["text"]
            assert row["preprocessing"] == "retained_exact_text"
            assert row["text"].strip()
            assert row["text"].strip() not in {"# # #", "Tweet @darpa", "&nbsp;"}


def test_p3s_prompt_packets_are_source_isolated_and_have_no_quota_instruction():
    prompt_files = sorted((RUN_DIR / "prompt_packets").glob("*-extract-001.txt"))
    assert len(prompt_files) == 5
    for prompt_path in prompt_files:
        text = prompt_path.read_text(encoding="utf-8")
        source_id = prompt_path.name.split("-extract-001")[0]
        assert f"SOURCE_ID: {source_id}" in text
        assert "至少13" not in text
        assert "60%" not in text
        assert "A级" not in text
        assert "no minimum quantity" in text
        assert text.count("SOURCE_ID:") == 1


def test_p3s_p2s_manifest_and_canonical_inputs_remain_unchanged():
    p2s_manifest = json.loads((P2S_DIR / "p2s_manifest.json").read_text(encoding="utf-8"))
    assert p2s_manifest["input_canonical_sha256"] == "ac307f553614bd4b6114ecb117d7e2516b91a5e28ae2884c44a14043b4a0900c"
    assert p2s_manifest["verified_original_page_count"] == 5
    assert p2s_manifest["evidence_inserted"] == 0


def test_p3s_model_schema_excludes_source_id_and_program_injects_it():
    value = {
        "source_locator": "web_paragraph_0001",
        "excerpt_original": "The first test event occurred.",
        "excerpt_zh": "首次测试事件已经发生。",
        "normalized_claim": "首次测试事件已经发生。",
        "coding_dimensions": ["application_maturity"],
        "evidence_nature": "test_event",
        "topics": ["test"],
        "possible_corroboration_group": "",
    }
    candidate, _ = _parse_p3s_candidate(value, "T0-TD-001")
    assert "source_id" not in P3S_MODEL_FIELDS
    assert candidate.source_id == "T0-TD-001"
