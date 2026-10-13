import json
from pathlib import Path

from src.p3ar import SourceSegment
from src.p3q import _dimension_reviews, _dimension_support, deterministic_gate_reasons
from src.schemas import EvidenceCandidate


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs/P3Q-20260826T023500Z-offline-v2"


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _manifest():
    return json.loads((RUN_DIR / "p3q_manifest.json").read_text(encoding="utf-8"))


def test_p3q_is_offline_and_protects_real_api_pilot():
    manifest = _manifest()
    assert manifest["p3ar_status"] == "real_api_technical_pilot_passed"
    assert manifest["input_p3ar_status"] == "real_api_technical_pilot_passed"
    assert manifest["p3ar_not_evidence_quality_validated"] is True
    assert manifest["model_api_called"] is False
    assert manifest["file_bridge_called"] is False
    assert manifest["provider_call_count"] == 0
    assert manifest["requested_temperature"] == 0.0
    assert manifest["provider_echoed_temperature"] is None
    assert manifest["p3ar_v6_hashes_unchanged"] is True
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p3b_executed"] is False
    assert manifest["p4_executed"] is False
    assert manifest["g1_executed"] is False


def test_p3q_rejudges_all_historical_candidates_without_accuracy_claim():
    manifest = _manifest()
    rows = _jsonl(RUN_DIR / "candidate_rejudgment_preview.jsonl")
    assert len(rows) == 21
    assert manifest["raw_candidate_count"] == 21
    assert 0.0 <= manifest["final_candidate_acceptance_rate"] <= 1.0
    assert manifest["dimension_support_is_not_accuracy"] is True
    for key in (
        "schema_valid_count",
        "exact_quote_matched_count",
        "deterministic_gate_passed_count",
        "same_model_semantic_screen_pass_count",
        "final_candidate_count",
    ):
        assert 0 <= manifest[key] <= manifest["raw_candidate_count"]
    reasons = {reason for row in rows for reason in row["hard_rejection_reasons"]}
    assert not any(reason.startswith("capitalized_entity") for reason in reasons)


def test_pdf_status_is_page_level_and_preserves_machine_text():
    manifest = _manifest()
    status = manifest["pdf_status"]
    assert status["pdf_page_count"] == 47
    assert status["non_text_pages"] == [47]
    assert status["page_47_disposition"] == "decorative_back_cover_no_ocr_required"
    assert status["ocr_required_pages"] == []
    assert {25, 26, 27, 28, 29, 30, 37} <= set(status["layout_visual_review_required_pages"])
    assert all(item.get("human_corrected_excerpt") is None for item in _jsonl(RUN_DIR / "final_candidate_preview.jsonl"))


def test_dimension_regressions_are_conservative():
    assert _dimension_support("human_machine_authority", "The systems coordinated with a command center.")[0] is False
    assert _dimension_support("human_machine_authority", "The system provided information to watchstanders.")[0] is True
    assert _dimension_support("human_machine_authority", "Training and documentation were required.")[0] is False


def test_offline_dimension_screen_is_not_model_or_human_review():
    candidate = EvidenceCandidate(
        source_id="T0-TD-008",
        source_locator="article/paragraph-01",
        excerpt_original="The system provided information to watchstanders.",
        excerpt_zh="系统向值班人员提供信息。",
        normalized_claim="系统向值班人员提供信息。",
        coding_dimensions=["human_machine_authority"],
        evidence_nature="test_result",
        topics=["watchstanding"],
    )
    reviews, _ = _dimension_reviews(candidate)
    assert reviews[0]["review_origin"] == "offline_keyword_heuristic"
    assert reviews[0]["model_generated"] is False
    assert reviews[0]["human_reviewed"] is False


def test_gate_admission_does_not_change_only_with_claim_language():
    segment = SourceSegment(source_id="T0-TD-008", source_locator="article/paragraph-01", text="A test event occurred.")
    base = {
        "source_id": "T0-TD-008",
        "source_locator": "article/paragraph-01",
        "excerpt_original": "A test event occurred.",
        "excerpt_zh": "发生了一次测试事件。",
        "coding_dimensions": ["application_maturity"],
        "evidence_nature": "test_event",
        "topics": ["test"],
    }
    english = EvidenceCandidate(**base, normalized_claim="A test event occurred.")
    chinese = EvidenceCandidate(**base, normalized_claim="发生了一次测试事件。")
    assert deterministic_gate_reasons(english, segment) == deterministic_gate_reasons(chinese, segment)


def test_p3q_preflight_templates_are_versioned_and_boundary_safe():
    assert (RUN_DIR / "validator_change_log.md").exists()
    assert (RUN_DIR / "p3b_preflight_report.md").exists()
    assert (RUN_DIR / "p3b_preflight/evidence_extract_p3b_v2.yaml").exists()
    assert (RUN_DIR / "p3b_preflight/same_model_separate_call_semantic_screening_v1.yaml").exists()
    assert (RUN_DIR / "p3b_preflight/p3b_preflight.yaml").exists()
    combined = "\n".join(path.read_text(encoding="utf-8") for path in RUN_DIR.rglob("*") if path.is_file())
    assert "sk-sp-" not in combined
    assert "t1_sealed" not in combined
