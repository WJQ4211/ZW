import csv
import hashlib
import json
from pathlib import Path

from src.p3br import _repairable_schema_candidate
from src.schemas import EvidenceCandidate


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "outputs/P3BR-20260826T064000Z-recovery-v3"
P3B_DIR = ROOT / "outputs/P3B-20260826T040000Z-full-t0-v3"


def _manifest():
    return json.loads((RUN_DIR / "p3br_manifest.json").read_text(encoding="utf-8"))


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_governance_event_is_a_supported_candidate_nature():
    candidate = EvidenceCandidate(
        source_id="synthetic-source",
        source_locator="paragraph-01",
        excerpt_original="A governance mechanism was established.",
        excerpt_zh="一项治理机制已经建立。",
        normalized_claim="一项治理机制已经建立。",
        coding_dimensions=["trust_governance_constraints"],
        evidence_nature="governance_event",
        topics=["synthetic"],
    )
    assert candidate.evidence_nature == "governance_event"


def test_schema_repair_removes_only_invalid_nature_labels_and_moves_note():
    raw = {
        "source_id": "synthetic-source",
        "source_locator": "paragraph-01",
        "excerpt_original": "A test occurred.",
        "excerpt_zh": "发生了一次测试。",
        "normalized_claim": "发生了一次测试。",
        "coding_dimensions": ["goal", "integration_level", "test_result"],
        "evidence_nature": "test_event",
        "topics": ["synthetic"],
        "excerpt_zh_note": "pending",
    }
    cleaned, actions, notes, model_fields = _repairable_schema_candidate(raw)
    assert cleaned["coding_dimensions"] == ["integration_level"]
    assert "excerpt_zh_note" not in cleaned
    assert "remove_invalid_coding_dimensions" in actions
    assert "move_excerpt_zh_note_to_repair_note" in actions
    assert "pending" in notes
    assert model_fields == []


def test_p3br_is_candidate_only_and_author_fields_are_blank():
    manifest = _manifest()
    assert manifest["p3b_original_candidate_count"] == 43
    assert manifest["schema_invalid_candidate_count"] == 13
    assert manifest["claim_repair_model_call_count"] == 4
    assert manifest["semantic_screen_model_call_count"] == 1
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["g1_executed"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["t1_read"] is False
    assert manifest["protected_prior_outputs_unchanged"] is True
    assert manifest["api_key_written"] is False

    combined = _jsonl(RUN_DIR / "evidence_candidates_p3br_combined.jsonl")
    assert len(combined) == manifest["combined_candidate_count"]
    assert all(item.get("status") == "candidate" for item in combined)
    rows = list(csv.DictReader((RUN_DIR / "g1_author_decision_sheet.csv").open(encoding="utf-8", newline="")))
    assert rows
    for row in rows:
        for field in (
            "final_decision",
            "final_nature",
            "final_dimensions",
            "final_normalized_claim",
            "human_corrected_excerpt",
            "final_excerpt_zh",
            "reviewer",
            "reviewed_at",
            "review_note",
        ):
            assert row[field] == ""
        assert row["human_reviewed"].lower() == "false"


def test_visual_rescue_queue_keeps_all_manual_decision_fields_empty():
    rows = list(csv.DictReader((RUN_DIR / "g1_visual_rescue_queue.csv").open(encoding="utf-8", newline="")))
    assert len(rows) == 7
    assert sum(row["recommended_action"] == "visual_review" for row in rows) == 5
    assert sum(row["recommended_action"] == "exclude" for row in rows) == 2
    assert all(row["final_decision"] == "" and row["human_corrected_excerpt"] == "" for row in rows)


def test_p3br_does_not_change_authoritative_p3b_tree():
    before = json.loads((RUN_DIR / "p3br_manifest.json").read_text(encoding="utf-8"))["p3b_input_hashes"]
    current = {
        str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(P3B_DIR.rglob("*"))
        if path.is_file()
    }
    assert current == before


def test_no_api_key_material_in_p3br_outputs():
    for path in RUN_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "sk-sp-" not in text
            assert "Authorization: Bearer" not in text
