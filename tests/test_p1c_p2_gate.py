import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
P1C_SOURCE = ROOT / "data/curated/source_manifest_t0_p1c.csv"
P2_TEMPLATE = ROOT / "data/curated/p2_review_template.csv"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _latest_manifest() -> tuple[Path, dict]:
    manifests = sorted((ROOT / "outputs").glob("P1C-*/p1c_manifest.json"))
    assert manifests, "P1c manifest is required before the P2 gate can open"
    path = manifests[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_p2_template_requires_human_decision_for_every_row():
    source_rows = _rows(P1C_SOURCE)
    review_rows = _rows(P2_TEMPLATE)
    assert len(source_rows) == len(review_rows) == 35
    assert all(row["review_decision"] == "" for row in review_rows)
    assert all(row["reviewer"] == "" and row["reviewed_at"] == "" for row in review_rows)
    assert all(row["selection_status"] not in {"include", "exclude"} for row in review_rows)


def test_blocked_cdao_is_not_p2_eligible_and_needs_no_manual_retrieval():
    rows = {row["source_id"]: row for row in _rows(P1C_SOURCE)}
    cdao = rows["T0-CDAO-001"]
    assert cdao["verification_status"] == "blocked_original_page"
    assert cdao["p2_eligibility"] == "false"
    assert cdao["manual_followup"] == ""
    assert cdao["selection_status"] == "candidate"


def test_t0_td_008_date_revision_is_auditable():
    row = next(row for row in _rows(P1C_SOURCE) if row["source_id"] == "T0-TD-008")
    assert row["published_at"] == "2023-01-09"
    assert row["date_revision_old"] == "2023-09-01"
    assert row["date_revision_new"] == "2023-01-09"
    assert row["date_revision_reason"]


def test_web_receipts_have_hash_canonical_and_capture_type():
    rows = {row["source_id"]: row for row in _rows(P1C_SOURCE)}
    for source_id in ("T0-TD-007", "T0-TD-008"):
        row = rows[source_id]
        assert len(row["web_content_sha256"]) == 64
        assert row["canonical_url"].startswith("https://")
        assert row["capture_type"] == "html_saved_page_with_assets"
        assert row["body_text_path"]
    ent = rows["T0-ENT-001"]
    assert ent["published_at"] == "2022-10-07"
    assert ent["capture_type"] == "direct_printable_html_fetch"
    assert len(ent["web_content_sha256"]) == 64


def test_p1c_manifest_keeps_stage_boundary_and_protected_inputs():
    manifest_path, manifest = _latest_manifest()
    assert manifest_path.parent.name.startswith("P1C-")
    assert manifest["stage"] == "P1c"
    assert manifest["boundary_declarations"]["modelclient_called"] is False
    assert manifest["boundary_declarations"]["evidence_rows_added"] == 0
    assert manifest["boundary_declarations"]["t1_read"] is False
    assert manifest["boundary_declarations"]["p2_executed"] is False
    assert manifest["boundary_declarations"]["p3_executed"] is False
    assert manifest["acceptance"]["p2_review_decisions_populated"] is False
    for path_string, expected in manifest["protected_p1_hashes_after"].items():
        path = Path(path_string)
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
