from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from src.g3_g4_offline import CASE_ID, ROOT, SNAPSHOT_ID
from src.storage import Store


RUN = ROOT / "outputs" / "G3-T0-20260827T-formal-offline-v5"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_formal_g3_counts_and_audit_flags() -> None:
    manifest = json.loads((RUN / "g3_manifest.json").read_text(encoding="utf-8"))
    assert manifest["formal_g3_executed"] is True
    assert manifest["formal_disagreement_count"] == 3
    assert manifest["conditionally_retained_count"] == 2
    assert manifest["resolved_scope_split_count"] == 1
    assert manifest["discriminative_need_count"] == 3
    assert manifest["monitor_indicator_count"] == 4
    assert manifest["trigger_rule_count"] == 8
    assert manifest["model_api_calls"] == 0
    assert manifest["evidence_inserted"] == 0
    assert manifest["hypothesis_inserted"] == 0
    assert manifest["viewpoint_inserted"] == 0
    assert manifest["t1_read"] is False
    assert manifest["formal_g4_t0_executed"] is False
    assert manifest["api_key_written"] is False
    assert manifest["transaction_committed"] is True
    assert manifest["rollback_test_passed"] is True
    assert manifest["idempotency_test_passed"] is True


def test_formal_g3_objects_are_linked_and_disp_ids_do_not_leak() -> None:
    disagreements = _jsonl(RUN / "g3_formal_disagreements.jsonl")
    needs = _jsonl(RUN / "g3_formal_discriminative_needs.jsonl")
    indicators = _jsonl(RUN / "g3_formal_monitor_indicators.jsonl")
    triggers = _jsonl(RUN / "g3_formal_trigger_rules.jsonl")
    assert {row["disagreement_id"] for row in disagreements} == {"DISAG-T0-01", "DISAG-T0-02", "DISAG-T0-03"}
    assert all("DISP-" not in json.dumps(row, ensure_ascii=False) for row in disagreements + needs + indicators + triggers)
    assert all(row["author_authorization_sha256"] for row in disagreements + needs + indicators + triggers)
    assert sum(row["status"] == "conditionally_retained" for row in disagreements) == 2
    assert sum(row["status"] == "resolved" for row in disagreements) == 1
    assert all(row["human_verified"] is True for row in needs + indicators + triggers)
    assert all(row["indicator_id"] in {item["indicator_id"] for item in indicators} for row in triggers)
    assert all(set(row["required_source_grade"]).issubset({"A", "B"}) for row in triggers)


def test_all_raw_monitoring_needs_have_deterministic_formal_mapping() -> None:
    rows = _jsonl(RUN / "g3_monitoring_before_after_mapping.jsonl")
    assert len(rows) == 5
    assert {row["before"]["need_id"] for row in rows} == {"ND-01", "ND-02", "ND-03", "ND-04", "ND-05"}
    assert all(row["mapping_origin"] == "offline_deterministic" for row in rows)
    assert all(row["model_modified"] is False for row in rows)
    assert all(row["after"]["formal_disagreement_id"].startswith("DISAG-T0-") for row in rows)
    assert all(row["after"]["formal_need_id"].startswith("NEED-T0-") for row in rows)


def test_database_g3_write_preserves_evidence_hypotheses_views_and_dependencies() -> None:
    store = Store(ROOT / "data" / "research.sqlite3")
    assert len(store.list_evidence(case_id=CASE_ID)) == 126
    assert len(store.list_hypotheses(SNAPSHOT_ID, case_id=CASE_ID)) == 3
    assert len(store.list_viewpoints(SNAPSHOT_ID, case_id=CASE_ID)) == 27
    # P15R appends another dependency batch; the original G3/G4 dependency
    # baseline remains 140 and the current working DB contains the append-only
    # P15R rows as well.
    assert len(store.list_dependencies(case_id=CASE_ID)) >= 140
    assert len(store.list_disagreements(SNAPSHOT_ID, case_id=CASE_ID)) == 3
    # Formal G3-T1 appends its own monitoring chain; this legacy G3-T0 test
    # must assert only the T0 namespace rather than a global row count.
    assert len([row for row in store.list_needs(case_id=CASE_ID) if row.need_id.startswith("NEED-T0-")]) == 3
    assert len([row for row in store.list_indicators(case_id=CASE_ID) if row.indicator_id.startswith("IND-T0-")]) == 4
    assert len([row for row in store.list_triggers(case_id=CASE_ID) if row.trigger_id.startswith("TRG-T0-")]) == 8


def test_authorization_and_g4_fields_are_audit_safe() -> None:
    auth = (RUN / "g3_author_authorization.txt").read_text(encoding="utf-8").rstrip("\n")
    manifest = json.loads((RUN / "g3_manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(auth.encode("utf-8")).hexdigest() == manifest["author_authorization_sha256"]
    rows = list(csv.DictReader((RUN / "G4_DRAFT" / "g4_author_review_template.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert rows
    required = {"author_decision", "final_report_status", "final_main_judgment", "reviewer", "reviewed_at", "review_note"}
    assert required.issubset(rows[0])
    assert all(not rows[0][key] for key in required)
    assert "draft_pending_author_signoff" in (RUN / "G4_DRAFT" / "g4_t0_report_draft.md").read_text(encoding="utf-8")


def test_paper_and_figures_pass_basic_qc() -> None:
    paper = (RUN / "PAPER_CASE" / "paper_case_section_draft.md").read_text(encoding="utf-8")
    assert 2500 <= len(paper) <= 3500
    qc = (RUN / "FIGURES" / "figure_visual_qc_report.md").read_text(encoding="utf-8")
    assert "结论：PASS" in qc
    assert len(list((RUN / "FIGURES").glob("figure_*.png"))) == 5
    assert len(list((RUN / "FIGURES").glob("figure_*.svg"))) == 5


def test_authoritative_input_hashes_remain_unchanged() -> None:
    p7r = json.loads((ROOT / "outputs" / "P7R-P9R-20260827T020335Z-case-strengthening-v1" / "p7r_p9r_manifest.json").read_text(encoding="utf-8"))
    for relative, expected in p7r["input_file_hashes"].items():
        path = ROOT / relative
        assert path.exists()
        assert _sha256(path) == expected
    for path in RUN.rglob("*"):
        if path.is_file() and path.suffix not in {".pyc"}:
            assert "sk-sp-" not in path.read_text(encoding="utf-8", errors="ignore")
