import csv
import json
from pathlib import Path

from src.schemas import EvidenceCandidate


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs" / "P3AR-20260826T010914-real-api-v6"


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_historical_p3a_is_explicitly_fixture_only():
    note = json.loads((ROOT / "outputs/P3A-20260825T161738Z-pilot/p3a_r_audit_note.json").read_text(encoding="utf-8"))
    assert note["generation_mode"] == "deterministic_hardcoded_fixture"
    assert note["real_model_extraction_executed"] is False
    assert note["validation_scope"] == "structural_and_quote_presence_only"
    assert note["result_not_eligible_for_g1"] is True


def test_production_runner_contains_no_hardcoded_candidate_payloads():
    script = (ROOT / "scripts/07_run_p3a.py").read_text(encoding="utf-8")
    assert "candidate_payloads" not in script
    assert "The old P3a pilot used deterministic hardcoded candidate payloads" in script
    for path in (ROOT / "scripts").glob("*.py"):
        if path.name == "07_run_p3a.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "candidate_payloads" not in text


def test_prepare_execute_validate_are_separate_and_model_run_is_real():
    prepare = json.loads((RUN_DIR / "prepare_manifest.json").read_text(encoding="utf-8"))
    execute = json.loads((RUN_DIR / "execute_manifest.json").read_text(encoding="utf-8"))
    manifest = json.loads((RUN_DIR / "p3ar_manifest.json").read_text(encoding="utf-8"))
    assert prepare["raw_model_outputs_created"] is False
    assert execute["raw_model_outputs_created"] is True
    assert execute["transport"] == "openai_compatible_api"
    assert execute["provider_call_count"] == 6
    assert manifest["real_model_extraction_executed"] is True
    assert manifest["requested_model_id"] == "deepseek-v4-flash-0731"
    assert manifest["provider_model_ids_observed"] == ["deepseek-v4-flash-0731"]
    assert manifest["file_bridge_calls_mixed"] is False
    assert manifest["api_key_written"] is False


def test_model_outputs_use_strict_candidate_fields_and_candidates_are_injected():
    raw_paths = sorted((RUN_DIR / "raw_model_outputs").glob("*.json"))
    assert len(raw_paths) == 3
    allowed = {
        "source_id", "source_locator", "excerpt_original", "excerpt_zh",
        "normalized_claim", "coding_dimensions", "evidence_nature", "topics",
    }
    for path in raw_paths:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        content = envelope["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        for item in json.loads(content):
            assert set(item) == allowed
            EvidenceCandidate(**item)
    candidates = _jsonl(RUN_DIR / "evidence_candidates_p3ar.jsonl")
    assert candidates
    assert {item["source_id"] for item in candidates} <= {"T0-SP-007", "T0-TD-008"}
    assert all(item["excerpt_zh"].strip() for item in candidates)
    assert all(item["translation_status"] == "pending_g1_translation_review" for item in candidates)
    assert all(item["reviewed_by"] == "" for item in candidates)
    assert all(len(item["content_hash"]) == 64 for item in candidates)


def test_p3ar_quality_rates_are_separate_and_formal_writes_are_zero():
    manifest = json.loads((RUN_DIR / "p3ar_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["source_ids"]) == {"T0-SP-007", "T0-TD-008"}
    assert manifest["quote_presence_match_rate"] == 18 / 21
    assert 0.0 <= manifest["claim_quote_alignment_pass_rate"] <= 1.0
    assert 0.0 <= manifest["nature_supported_rate"] <= 1.0
    assert 0.0 <= manifest["dimension_supported_rate"] <= 1.0
    assert 0.0 <= manifest["translation_nonempty_rate"] <= 1.0
    rows = list(csv.DictReader((RUN_DIR / "quote_validation.csv").open(encoding="utf-8", newline="")))
    assert rows
    assert all(row["quote_presence_match"] == "True" for row in rows if row["accepted"] == "True")
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["g1_executed"] is False


def test_test_event_is_an_allowed_nature_and_no_t1_or_old_candidates_are_inputs():
    candidate = EvidenceCandidate(
        source_id="T0-TD-008",
        source_locator="article/paragraph-01",
        excerpt_original="A test event occurred.",
        excerpt_zh="发生了一次测试事件。",
        normalized_claim="A test event occurred.",
        coding_dimensions=["application_maturity"],
        evidence_nature="test_event",
        topics=["test"],
    )
    assert candidate.evidence_nature == "test_event"
    for name in ("07_prepare_p3ar.py", "08_execute_model_p3ar.py", "09_validate_p3ar.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "t1_sealed" not in text
        assert "P3A-20260825T161738Z-pilot" not in text
