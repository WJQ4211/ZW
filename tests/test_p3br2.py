import csv
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = sorted((ROOT / "outputs").glob("P3BR2-*-gate-correction-v1"))[-1]


def _csv(name: str) -> list[dict[str, str]]:
    with (RUN_DIR / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (RUN_DIR / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def _manifest() -> dict:
    return json.loads((RUN_DIR / "p3br2_manifest.json").read_text(encoding="utf-8"))


def test_author_decision_sheet_includes_every_medium_record():
    pre = _csv("g1_ai_preannotation_p3br2.csv")
    decision = {row["evidence_id"] for row in _csv("g1_author_decision_sheet_p3br2.csv")}
    medium = {row["evidence_id"] for row in pre if row["risk_level"] == "medium"}
    assert medium
    assert medium <= decision
    assert _manifest()["medium_missing_from_author_decision_sheet"] == []


def test_author_sampling_is_high_medium_and_ceil_twenty_percent_low():
    rows = _csv("g1_author_decision_sheet_p3br2.csv")
    assert len(rows) == 55
    assert sum(row["risk_level"] == "high" for row in rows) == 49
    assert sum(row["risk_level"] == "medium" for row in rows) == 4
    assert sum(row["risk_level"] == "low" for row in rows) == 2
    low_total = sum(row["risk_level"] == "low" for row in _csv("g1_ai_preannotation_p3br2.csv"))
    assert len(_csv("g1_author_low_risk_sample_p3br2.csv")) == math.ceil(low_total * 0.20)


def test_visual_pre_review_is_not_human_review():
    rows = {row["evidence_id"]: row for row in _jsonl("ai_visual_pre_review_log.jsonl")}
    assert rows["EV-T0-63E815AC3300FE8A"]["proposed_source_locator"] == "pdf_pages_52_53"
    assert rows["EV-T0-51D65DEC82584BCD"]["proposed_nature"] == "constraint"
    assert rows["EV-T0-246C331615E11FB3"]["proposed_decision"] == "exclude"
    assert rows["EV-T0-6FE734DE05953A3C"]["proposed_dimensions"] == [
        "trust_governance_constraints",
        "engineering_resource_conditions",
    ]
    assert rows["EV-T0-04572D780936A8D6"]["proposed_nature"] == "goal"
    assert rows["EV-T0-CA999183D4D473C4"]["proposed_decision"] == "exclude"
    assert rows["EV-T0-1C84CB9C7F271D13"]["proposed_decision"] == "exclude"
    assert all(row["human_reviewed"] is False for row in rows.values())


def test_recovery_corrections_and_split_dimensions():
    rows = {row["evidence_id"]: row for row in _csv("g1_ai_preannotation_p3br2.csv")}
    assert rows["EV-T0-2D9AD11D628EFF58"]["proposed_nature"] == "constraint"
    assert rows["EV-T0-A515E3B9391C51B4"]["proposed_nature"] == "constraint"
    assert "路线图" in rows["EV-T0-A515E3B9391C51B4"]["proposed_review_note"]
    assert rows["EV-T0-794C24A620FE0968"]["proposed_decision"] == "split"
    split = _jsonl("recovery_split_proposals.jsonl")
    assert len(split) == 2
    assert {row["proposed_nature"] for row in split} == {"test_event", "goal"}
    assert all(row["proposed_dimensions"] for row in split)
    assert rows["EV-T0-E4EADC8D35D78E4A"]["proposed_nature"] == "evaluation"
    assert "自我评价" in rows["EV-T0-E4EADC8D35D78E4A"]["proposed_review_note"]


def test_all_author_final_fields_blank_and_no_execution_side_effects():
    manifest = _manifest()
    assert manifest["protected_prior_outputs_unchanged"] is True
    assert manifest["model_client_called"] is False
    assert manifest["provider_call_count"] == 0
    assert manifest["formal_evidence_db_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["g1_executed"] is False
    assert manifest["p4_executed"] is False
    assert manifest["snapshot_frozen"] is False
    assert manifest["t1_read"] is False
    final_fields = (
        "final_decision",
        "final_nature",
        "final_dimensions",
        "final_normalized_claim",
        "human_corrected_excerpt",
        "final_excerpt_zh",
        "reviewer",
        "reviewed_at",
        "review_note",
    )
    for row in _csv("g1_author_decision_sheet_p3br2.csv"):
        assert all(row[field] == "" for field in final_fields)
        assert row["human_reviewed"].lower() == "false"


def test_p3br_v3_hashes_are_still_the_input_snapshot():
    manifest = _manifest()
    source = ROOT / "outputs/P3BR-20260826T064000Z-recovery-v3"
    current = {
        path.resolve().relative_to(ROOT.resolve()).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }
    expected = {key: value for key, value in manifest["input_v3_hashes"].items() if key.startswith("outputs/P3BR-20260826T064000Z-recovery-v3/")}
    assert current == expected
