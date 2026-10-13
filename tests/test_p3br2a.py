import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = sorted((ROOT / "outputs").glob("P3BR2A-*-content-completeness-v1"))[-1]


def _csv(name: str) -> list[dict[str, str]]:
    with (RUN_DIR / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (RUN_DIR / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def _manifest() -> dict:
    return json.loads((RUN_DIR / "p3br2a_manifest.json").read_text(encoding="utf-8"))


def test_author_sheet_has_55_unique_rows_and_expected_risk_coverage():
    rows = _csv("g1_author_decision_sheet_p3br2a.csv")
    assert len(rows) == 55
    assert len({row["evidence_id"] for row in rows}) == 55
    assert sum(row["risk_level"] == "high" for row in rows) == 49
    assert sum(row["risk_level"] == "medium" for row in rows) == 4
    assert sum(row["risk_level"] == "low" for row in rows) == 2
    assert Counter(row["proposed_decision"] for row in rows) == {
        "accept": 15,
        "revise": 34,
        "split": 3,
        "exclude": 3,
    }


def test_all_five_required_author_fields_are_nonempty_and_dimensions_are_legal():
    allowed = {
        "application_maturity",
        "human_machine_authority",
        "integration_level",
        "engineering_resource_conditions",
        "trust_governance_constraints",
        "organizational_system_fit",
    }
    rows = _csv("g1_author_decision_sheet_p3br2a.csv")
    for row in rows:
        assert row["proposed_nature"]
        dimensions = json.loads(row["proposed_dimensions"])
        assert dimensions and set(dimensions) <= allowed
        assert row["proposed_normalized_claim"]
        assert row["proposed_excerpt_zh"]
        assert row["excerpt_original"]


def test_visual_items_use_queue_mapping_and_remain_nonhuman():
    visual = {row["evidence_id"]: row for row in _jsonl("ai_visual_pre_review_log_p3br2a.jsonl")}
    rows = {row["evidence_id"]: row for row in _csv("g1_author_decision_sheet_p3br2a.csv")}
    assert len(visual) == 7
    for evidence_id, source in visual.items():
        row = rows[evidence_id]
        assert row["source_locator"] == source["source_locator"]
        assert row["proposed_source_locator"] == source["proposed_source_locator"]
        assert row["proposed_nature"] == source["proposed_nature"]
        assert json.loads(row["proposed_dimensions"]) == source["proposed_dimensions"]
        assert row["proposed_normalized_claim"]
        assert row["proposed_excerpt_zh"]
        assert row["excerpt_original"]
        assert row["model_generated"].lower() == "false"
        assert row["ai_assisted"].lower() == "true"
        assert row["human_reviewed"].lower() == "false"


def test_three_split_parents_have_six_complete_unique_children_and_lineage():
    rows = {row["evidence_id"]: row for row in _csv("g1_author_decision_sheet_p3br2a.csv")}
    children = _jsonl("split_child_proposals_p3br2a.jsonl")
    parents = {"EV-T0-DB1424F52E33432F", "EV-T0-F25379FE058D2680", "EV-T0-794C24A620FE0968"}
    assert len(children) == 6
    assert len({row["proposal_child_id"] for row in children}) == 6
    assert {row["parent_evidence_id"] for row in children} == parents
    assert all(rows[parent]["proposed_decision"] == "split" for parent in parents)
    for parent in parents:
        subset = [row for row in children if row["parent_evidence_id"] == parent]
        assert {row["part_index"] for row in subset} == {1, 2}
        for row in subset:
            assert all(row[field] for field in ("proposal_child_id", "source_id", "source_locator", "excerpt_original", "proposed_excerpt_zh", "proposed_normalized_claim", "proposed_nature", "proposed_dimensions"))
            assert row["human_reviewed"] is False
            assert row["final_decision"] == ""
            assert row["reviewer"] == ""
            assert row["reviewed_at"] == ""
            assert row["review_note"] == ""


def test_author_fields_are_blank_and_execution_boundaries_hold():
    rows = _csv("g1_author_decision_sheet_p3br2a.csv")
    fields = ("final_decision", "final_nature", "final_dimensions", "final_normalized_claim", "human_corrected_excerpt", "final_excerpt_zh", "reviewer", "reviewed_at", "review_note")
    assert all(all(row[field] == "" for field in fields) and row["human_reviewed"].lower() == "false" for row in rows)
    manifest = _manifest()
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert manifest["g1_executed"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False


def test_p3br2_input_tree_is_unchanged_and_reproducibility_metadata_exists():
    manifest = _manifest()
    source = ROOT / "outputs/P3BR2-20260826T050236Z-gate-correction-v1"
    current = {
        path.resolve().relative_to(ROOT.resolve()).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }
    assert current == manifest["input_p3br2_hashes"]
    assert manifest["protected_prior_outputs_unchanged"] is True
    assert set(manifest["implementation_file_hashes"]) == {"src/p3br2a.py", "scripts/15_run_p3br2a.py", "tests/test_p3br2a.py"}
    assert manifest["python_version"]
    assert manifest["git_head"] == "unavailable_not_a_git_repository"
    assert manifest["model_client_called"] is False
    assert manifest["api_key_written"] is False
