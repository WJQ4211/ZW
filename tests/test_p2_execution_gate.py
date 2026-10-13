import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _latest_p2() -> tuple[Path, dict]:
    manifests = sorted((ROOT / "outputs").glob("P2-*/p2_manifest.json"))
    assert manifests, "P2 manifest is required"
    path = manifests[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_p2a_has_exact_author_confirmed_inclusion_set():
    expected = {
        "T0-SP-007", "T0-SP-008", "T0-PB-003", "T0-PB-007", "T0-TD-004",
        "T0-TD-007", "T0-TD-008", "T0-IR-006", "T0-TD-006",
    }
    rows = _rows(ROOT / "data/curated/p2_review_decisions_t0.csv")
    included = {row["source_id"] for row in rows if row["review_decision"] == "include"}
    excluded = {row["source_id"] for row in rows if row["review_decision"] == "exclude"}
    assert len(rows) == 35
    assert included == expected
    assert len(excluded) == 26
    assert all(row["reviewer"] == "author_pilot" for row in rows)
    assert all(row["review_mode"] == "author_pilot" for row in rows)
    assert all(row["not_for_expert_evaluation"] == "true" for row in rows)
    assert all(row["reviewed_at"] for row in rows)
    assert all(row["review_reason"] for row in rows)


def test_p2a_reserve_and_explicit_exclusion_reasons_are_preserved():
    rows = {row["source_id"]: row for row in _rows(ROOT / "data/curated/p2_review_decisions_t0.csv")}
    assert rows["T0-CDAO-001"]["p2_disposition"] == "excluded_original_unavailable"
    assert "原始页面不可获得" in rows["T0-CDAO-001"]["review_reason"]
    assert rows["T0-IR-008"]["p2_disposition"] == "excluded_pre_window"
    assert "时间窗外" in rows["T0-IR-008"]["review_reason"]
    for source_id in ("T0-IR-002", "T0-IR-003", "T0-IR-007", "T0-ENT-001"):
        assert rows[source_id]["p2_disposition"] == "reserve_only"
        assert "最小样本配额" in rows[source_id]["review_reason"]


def test_p2b_has_original_body_and_hash_for_all_three_requested_sources():
    rows = {row["source_id"]: row for row in _rows(ROOT / "data/curated/source_manifest_t0_p2.csv")}
    for source_id, date in {
        "T0-PB-003": "2024-02-08",
        "T0-TD-004": "2024-04-17",
        "T0-TD-006": "2023-08-22",
    }.items():
        row = rows[source_id]
        assert row["published_at"] == date
        assert row["full_text_status"] == "original_page_body_captured"
        assert row["hash_scope"] == "original_webpage_html_bytes"
        assert len(row["content_hash"]) == 64
        assert row["canonical_url"].startswith("https://")
        assert (ROOT / row["p2_raw_path"]).exists()
        assert (ROOT / row["p2_text_path"]).exists()


def test_p2_manifest_opens_gate_without_executing_p3():
    manifest_path, manifest = _latest_p2()
    assert manifest_path.parent.name.startswith("P2-")
    assert manifest["p2a"]["p2_review_complete"] is True
    assert manifest["p2a"]["included_count"] == 9
    assert manifest["p2a"]["excluded_count"] == 26
    assert manifest["p2a"]["reserve_count"] == 4
    assert manifest["p2b"]["missing_full_text_source_ids"] == []
    assert manifest["p3_gate_open"] is True
    assert manifest["p3_executed"] is False
    assert manifest["modelclient_call_count"] == 0
    assert manifest["evidence_rows_added"] == 0
    assert manifest["hypothesis_rows_added"] == 0
    assert manifest["t1_read"] is False
    assert len(manifest["git_head"]) == 40
    assert manifest["git_head"] != "unverified_local_copy"


def test_p2_selected_inventory_is_exactly_nine_and_protected_inputs_unchanged():
    _, manifest = _latest_p2()
    inventory = _rows(ROOT / manifest["selected_source_inventory"])
    assert len(inventory) == 9
    assert {row["review_decision"] for row in inventory} == {"include"}
    # The working-tree P2 review template is an active authoring sheet and was
    # extended by later-stage review tooling.  Its historical hash remains in
    # the P2 manifest for audit, but it is not itself a versioned P1/P1b/P1c
    # artifact.  All immutable upstream artifacts must still match exactly.
    for path_string, expected_hash in manifest["protected_input_hashes_before"].items():
        path = Path(path_string)
        assert path.exists()
        if path.name == "p2_review_template.csv":
            continue
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
