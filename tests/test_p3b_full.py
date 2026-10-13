import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs/P3B-20260826T040000Z-full-t0-v3"
P3Q_DIR = ROOT / "outputs/P3Q-20260826T023500Z-offline-v2"


def _manifest():
    return json.loads((RUN_DIR / "p3b_manifest.json").read_text(encoding="utf-8"))


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p3b_full_scope_and_two_real_calls_per_batch():
    manifest = _manifest()
    assert manifest["source_count"] == 9
    assert set(manifest["included_source_ids"]) == {
        "T0-SP-007", "T0-SP-008", "T0-PB-003", "T0-PB-007", "T0-TD-004",
        "T0-TD-006", "T0-TD-007", "T0-TD-008", "T0-IR-006",
    }
    assert manifest["extraction_batch_count"] == 15
    assert manifest["semantic_screen_batch_count"] == 15
    assert manifest["provider_call_count"] == 30
    assert manifest["actual_same_model_semantic_screen_pass_count"] == 43
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["p4_executed"] is False
    assert manifest["g1_executed"] is False
    assert manifest["snapshot_frozen"] is False


def test_p3b_has_complete_coverage_and_no_minimum_per_source_rule():
    manifest = _manifest()
    rows = list(csv.DictReader((RUN_DIR / "source_coverage_matrix.csv").open(encoding="utf-8", newline="")))
    segment_rows = [row for row in rows if row["row_type"] == "page_or_paragraph"]
    assert len(segment_rows) == 319
    assert manifest["coverage_segment_count"] == len(segment_rows)
    assert all(row["source_locator"] for row in segment_rows)
    assert all(row["text_present"] in {"True", "False"} for row in segment_rows)
    assert {row["source_id"] for row in segment_rows} == set(manifest["included_source_ids"])


def test_p3b_candidates_are_candidates_only_and_g1_fields_are_blank():
    manifest = _manifest()
    candidates = _jsonl(RUN_DIR / "evidence_candidates_p3b.jsonl")
    assert len(candidates) == manifest["final_candidate_count"]
    assert candidates
    assert all("正式" not in item.get("status", "") for item in candidates)
    assert all(item["status"] == "candidate" for item in candidates)
    assert all(item["reviewed_by"] == "" for item in candidates)
    assert all(any("\u3400" <= char <= "\u9fff" for char in item["normalized_claim"]) for item in candidates)
    assert all(item["excerpt_original"].strip() and item["excerpt_zh"].strip() for item in candidates)
    rows = list(csv.DictReader((RUN_DIR / "g1_t0_review_template.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == len(candidates)
    for row in rows:
        for field in ("final_decision", "final_nature", "final_dimensions", "human_corrected_excerpt", "reviewer", "reviewed_at", "review_note"):
            assert row[field] == ""


def test_p3b_semantic_screen_is_real_same_model_separate_call():
    results = _jsonl(RUN_DIR / "semantic_screen_results_p3b.jsonl")
    revisions = _jsonl(RUN_DIR / "dimension_revision_log.jsonl")
    assert results
    assert all(row["review"]["status"] in {"pass", "revise", "reject"} for row in results)
    assert all(row["review"]["dimension_reviews"] for row in results)
    assert all(row["review_origin"] == "same_model_separate_call_semantic_screening" for row in revisions)
    assert all(row["model_generated"] is True and row["human_reviewed"] is False for row in revisions)
    assert len(list((RUN_DIR / "raw_model_outputs").glob("*.json"))) == 15
    assert len(list((RUN_DIR / "raw_semantic_reviews").glob("*.json"))) == 15
    assert len(list((RUN_DIR / "api_call_metadata").glob("*.json"))) == 30


def test_p3q_audit_correction_preserves_immutable_input():
    manifest = _manifest()
    current = {}
    for path in sorted(P3Q_DIR.rglob("*")):
        if path.is_file():
            current[str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert current == {key: value for key, value in manifest["p3q_input_hashes"].items()}
    correction = json.loads((RUN_DIR / "audit_correction/p3q_audit_correction.json").read_text(encoding="utf-8"))
    assert correction["dimension_support_origin"] == "offline_keyword_dimension_heuristic"
    assert correction["dimension_support_is_model_generated"] is False
    assert correction["dimension_support_is_human_reviewed"] is False
    assert correction["model_api_called_by_correction"] is False
    assert correction["hybrid_preview_pass_count"] == 14


def test_p3b_output_contains_no_api_key_material():
    for path in RUN_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "sk-sp-" not in text
            assert "Authorization: Bearer" not in text
