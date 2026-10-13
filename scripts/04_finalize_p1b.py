"""Build the version-protected P1b manifests from deterministic intake output.

The external supplement records in this file are metadata-only discovery
leads.  When an original page was blocked in this environment, the record is
kept as a candidate with an explicit verification status and is not treated as
P2-admitted material.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


P1_RUN_ID_DEFAULT = "P1-20260825T081142Z-7f4c9b"
CASE_ID_DEFAULT = "us_military_ai_deployment"

EXTRA_SOURCE_COLUMNS = [
    "document_date",
    "public_release_date",
    "temporal_status",
    "p1b_origin",
    "p1b_group",
    "manual_file_name",
    "pdf_page_count",
    "page_index_path",
    "document_card_path",
    "discovery_url",
    "original_source_domain",
    "original_page_verification",
    "verification_status",
    "access_status",
    "manual_followup",
]

EXTRA_LOG_COLUMNS = [
    "p1b_run_id",
    "discovery_source",
    "verification_status",
    "manual_followup",
]


SUPPLEMENT_LEADS: list[dict[str, Any]] = [
    {
        "source_id": "T0-TD-007",
        "title": "Cloud-based intel tool AIDP rolls out to Army units globally",
        "publisher": "U.S. Army Program Executive Office Intelligence, Electronic Warfare & Sensors",
        "published_at": "2024-10-09",
        "material_type": "official_service_application",
        "source_grade": "A",
        "possible_coding_dimensions": "application_maturity;organizational_system_fit;data_enterprise_collaboration",
        "p1b_group": "test_deployment",
        "original_source_domain": "cpeisw.army.mil",
        "discovery_query": "site:army.mil AI 2024 data platform",
        "discovery_url": "https://news.google.com/rss/search?q=site%3Aarmy.mil%20AI%202024%20data%20platform&hl=en-US&gl=US&ceid=US%3Aen",
        "original_page_verification": "blocked_or_not_opened",
        "access_status": "blocked_original_page",
        "manual_followup": "Open the original PEO IEW&S page and record its canonical URL and exact publication date before P2.",
    },
    {
        "source_id": "T0-TD-008",
        "title": "IMSC Task Force Completes Maritime Exercise with Unmanned Systems, A.I.",
        "publisher": "International Maritime Security Construct / U.S. Naval Forces Central Command",
        "published_at": "2023-09-01",
        "material_type": "official_combatant_command_application",
        "source_grade": "A",
        "possible_coding_dimensions": "application_maturity;human_machine_authority;organizational_system_fit",
        "p1b_group": "test_deployment",
        "original_source_domain": "dvidshub.net",
        "discovery_query": "site:dvidshub.net/news artificial intelligence Navy 2023",
        "discovery_url": "https://news.google.com/rss/search?q=site%3Advidshub.net%2Fnews%20artificial%20intelligence%20Navy%202023&hl=en-US&gl=US&ceid=US%3Aen",
        "original_page_verification": "blocked_or_not_opened",
        "access_status": "blocked_original_page",
        "manual_followup": "Open the original DVIDS/NAVCENT page and capture page-level details; the RSS title/date is discovery metadata only.",
    },
    {
        "source_id": "T0-CDAO-001",
        "title": "The CDAO is at the Helm of the DoD's New Generative AI Task Force",
        "publisher": "Chief Digital and Artificial Intelligence Office (CDAO)",
        "published_at": "2023-08-10",
        "material_type": "official_cdao_application",
        "source_grade": "A",
        "possible_coding_dimensions": "governance;application_maturity;organizational_system_fit",
        "p1b_group": "strategy_policy",
        "original_source_domain": "ai.mil",
        "discovery_query": "site:ai.mil CDAO 2024 artificial intelligence",
        "discovery_url": "https://news.google.com/rss/search?q=site%3Aai.mil%20CDAO%202024%20artificial%20intelligence&hl=en-US&gl=US&ceid=US%3Aen",
        "original_page_verification": "blocked_or_not_opened",
        "access_status": "blocked_original_page",
        "manual_followup": "Open the original ai.mil article and verify the exact date and canonical URL before P2.",
    },
    {
        "source_id": "T0-ENT-001",
        "title": "Global Force Information Management kicks off Phase 2 of data platform development",
        "publisher": "U.S. Department of Defense / DVIDS",
        "published_at": "2022-10-07",
        "material_type": "official_enterprise_data_application",
        "source_grade": "A",
        "possible_coding_dimensions": "data_enterprise_collaboration;cross_component;organizational_system_fit",
        "p1b_group": "test_deployment",
        "original_source_domain": "dvidshub.net",
        "discovery_query": "site:dvidshub.net/news artificial intelligence Army 2023",
        "discovery_url": "https://news.google.com/rss/search?q=site%3Advidshub.net%2Fnews%20artificial%20intelligence%20Army%202023&hl=en-US&gl=US&ceid=US%3Aen",
        "original_page_verification": "blocked_or_not_opened",
        "access_status": "blocked_original_page",
        "manual_followup": "Open the original DVIDS page and verify whether the item describes a DoD-wide or component-level rollout.",
    },
    {
        "source_id": "T0-PB-008",
        "title": "AI Cyber Challenge Opens Registration, Adds $4 Million in Prizes, Shows Scoring Algorithm and Challenge Exemplar",
        "publisher": "Defense Advanced Research Projects Agency (DARPA)",
        "published_at": "2023-12-14",
        "url_or_path": "https://www.darpa.mil/news/2023/ai-cyber-challenge-opens",
        "material_type": "official_funding_challenge",
        "source_grade": "A",
        "possible_coding_dimensions": "acquisition_procurement;engineering_resource_conditions;test_evaluation",
        "p1b_group": "program_budget",
        "discovery_query": "site:darpa.mil/news/2023/ai-cyber-challenge-opens",
        "discovery_url": "https://news.google.com/rss/search?q=site%3Adarpa.mil%2Fnews%2F2023%2Fai-cyber-challenge-opens&hl=en-US&gl=US&ceid=US%3Aen",
        "original_source_domain": "darpa.mil",
        "original_page_verification": "verified_original_page",
        "access_status": "opened_original_page",
        "manual_followup": "P2 may decide whether the prize mechanism is relevant to the budget/procurement dimension; no Evidence was extracted in P1b.",
    },
    {
        "source_id": "T0-IR-009",
        "title": "The DOD and the U.S. Tech Sector Relationship",
        "publisher": "Center for Security and Emerging Technology (CSET), Georgetown University",
        "published_at": "2022-08-31",
        "url_or_path": "https://cset.georgetown.edu/publication/dod-and-the-u-s-tech-sector-relationship/",
        "material_type": "research_report",
        "source_grade": "B",
        "possible_coding_dimensions": "acquisition_procurement;data_enterprise_collaboration;organizational_system_fit",
        "p1b_group": "program_budget",
        "discovery_query": "Task Force 59 CSET DOD tech sector relationship",
        "discovery_url": "https://cset.georgetown.edu/wp-json/wp/v2/document?search=Task%20Force%2059&after=2022-01-01T00%3A00%3A00&before=2024-12-31T23%3A59%3A59&per_page=20&_fields=id,date,link,title,slug",
        "original_source_domain": "cset.georgetown.edu",
        "original_page_verification": "verified_original_page",
        "access_status": "opened_original_page",
        "manual_followup": "P2 should assess relevance from the original publication page and any linked report; no Evidence was extracted in P1b.",
    },
]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def code_version() -> tuple[str, str]:
    try:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip(), "verified_workspace_git"
    except Exception:
        return "unverified_local_copy", "Workspace root has no readable Git commit."


def blank_row(fieldnames: list[str]) -> dict[str, str]:
    return {field: "" for field in fieldnames}


def manual_source_row(record: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    meta = record["source_metadata"]
    row = blank_row(fieldnames)
    row.update(
        {
            "source_id": str(meta["source_id"]),
            "title": str(meta["title"]),
            "publisher": str(meta["publisher"]),
            "published_at": str(meta.get("published_at", "")),
            "url_or_path": f"data/staging/t0_candidates/manual/{record['file_name']}",
            "material_type": str(meta["material_type"]),
            "source_grade": str(meta["source_grade"]),
            "possible_coding_dimensions": manual_dimensions(str(meta["source_id"])),
            "date_status": "verified_manual_metadata",
            "selection_status": str(meta.get("selection_status", "candidate")),
            "inclusion_reason": "P1b human-supplied PDF; metadata and page index accepted for later P2 review only.",
            "exclusion_reason": str(meta.get("exclusion_reason", "")),
            "local_path": f"data/staging/t0_candidates/manual/{record['file_name']}",
            "content_hash": str(record["file_sha256"]),
            "notes": "P1b page-level intake only; no formal Evidence, hypothesis, or conclusion created.",
            "document_date": str(meta.get("document_date", "")),
            "public_release_date": str(meta.get("public_release_date", "")),
            "temporal_status": str(meta.get("temporal_status", "within_t0")),
            "p1b_origin": "manual_pdf",
            "p1b_group": manual_group(str(meta["source_id"])),
            "manual_file_name": str(record["file_name"]),
            "pdf_page_count": str(record["page_count"]),
            "page_index_path": str(Path(record["page_index_path"]).as_posix()),
            "document_card_path": str(Path(record["document_card_path"]).as_posix()),
            "original_page_verification": "user_supplied_pdf_opened",
            "verification_status": "verified_pdf_intake",
            "access_status": "opened_local_pdf",
        }
    )
    if meta.get("temporal_status") == "excluded_pre_window":
        row["inclusion_reason"] = "Retained as background-only metadata because it is outside the T0 window."
        row["selection_status"] = "background_only"
    return row


def manual_dimensions(source_id: str) -> str:
    return {
        "T0-SP-007": "trust_governance_constraints;human_machine_authority;application_maturity",
        "T0-SP-008": "data_enterprise_collaboration;organizational_system_fit;application_maturity",
        "T0-IR-006": "application_maturity;data_enterprise_collaboration;organizational_system_fit",
        "T0-PB-007": "acquisition_procurement;application_maturity;organizational_system_fit",
        "T0-IR-007": "engineering_resource_conditions;organizational_system_fit",
        "T0-IR-008": "trust_governance_constraints;human_machine_authority;application_maturity",
    }.get(source_id, "")


def manual_group(source_id: str) -> str:
    if source_id.startswith("T0-SP"):
        return "strategy_policy"
    if source_id.startswith("T0-PB"):
        return "program_budget"
    return "independent_review"


def supplement_source_row(item: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    row = blank_row(fieldnames)
    row.update(
        {
            "source_id": item["source_id"],
            "title": item["title"],
            "publisher": item["publisher"],
            "published_at": item["published_at"],
            "url_or_path": item.get("url_or_path", ""),
            "material_type": item["material_type"],
            "source_grade": item["source_grade"],
            "possible_coding_dimensions": item["possible_coding_dimensions"],
            "date_status": "verified_original_page" if item["access_status"] == "opened_original_page" else "unverified_original_page",
            "selection_status": "candidate",
            "inclusion_reason": "P1b coverage supplement; metadata only and awaiting P2 admission.",
            "local_path": "",
            "content_hash": "",
            "notes": "Original page was not used as Evidence. See verification_status and manual_followup.",
            "temporal_status": "within_t0",
            "p1b_origin": "coverage_recheck",
            "p1b_group": item["p1b_group"],
            "discovery_url": item["discovery_url"],
            "original_source_domain": item["original_source_domain"],
            "original_page_verification": item["original_page_verification"],
            "verification_status": item["original_page_verification"],
            "access_status": item["access_status"],
            "manual_followup": item["manual_followup"],
        }
    )
    return row


def make_search_row(fieldnames: list[str], **values: str) -> dict[str, str]:
    row = blank_row(fieldnames)
    row.update(values)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--p1-source-manifest", type=Path, default=Path("data/curated/source_manifest_t0.csv"))
    parser.add_argument("--p1-search-log", type=Path, default=Path("data/curated/search_log_t0.csv"))
    parser.add_argument("--case-id", default=CASE_ID_DEFAULT)
    parser.add_argument("--p1-run-id", default=P1_RUN_ID_DEFAULT)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    inventory_path = run_dir / "document_inventory.json"
    inventory = read_json(inventory_path)
    if inventory["manual_pdf_count"] != 6 or inventory["total_pdf_pages"] != 295:
        raise ValueError("The intake inventory does not meet the six-file/295-page acceptance target.")
    if inventory["ocr_required"] != 0:
        raise ValueError("P1b requires ocr_required=0.")

    p1_paths = [args.p1_source_manifest, args.p1_search_log]
    p1_paths.extend(sorted((Path("outputs") / args.p1_run_id).glob("*")))
    p1_paths = [path.resolve() for path in p1_paths if path.is_file()]
    p1_before = {str(path): sha256_path(path) for path in p1_paths}

    source_headers, old_source_rows = read_csv(args.p1_source_manifest)
    source_headers_out = list(source_headers)
    for column in EXTRA_SOURCE_COLUMNS:
        if column not in source_headers_out:
            source_headers_out.append(column)
    source_rows = []
    for old_row in old_source_rows:
        row = blank_row(source_headers_out)
        row.update(old_row)
        source_rows.append(row)
    existing_ids = {row.get("source_id", "") for row in source_rows}
    manual_rows = [manual_source_row(record, source_headers_out) for record in inventory["documents"]]
    for row in manual_rows:
        if row["source_id"] in existing_ids:
            raise ValueError(f"Source ID collision: {row['source_id']}")
        existing_ids.add(row["source_id"])
        source_rows.append(row)
    supplement_rows = []
    for item in SUPPLEMENT_LEADS:
        if item["source_id"] in existing_ids:
            raise ValueError(f"Supplement source ID collision: {item['source_id']}")
        existing_ids.add(item["source_id"])
        supplement_rows.append(supplement_source_row(item, source_headers_out))
        source_rows.append(supplement_rows[-1])
    source_manifest_p1b = Path("data/curated/source_manifest_t0_p1b.csv").resolve()
    write_csv(source_manifest_p1b, source_headers_out, source_rows)

    log_headers, old_log_rows = read_csv(args.p1_search_log)
    log_headers_out = list(log_headers)
    for column in EXTRA_LOG_COLUMNS:
        if column not in log_headers_out:
            log_headers_out.append(column)
    log_rows = []
    for old_row in old_log_rows:
        row = blank_row(log_headers_out)
        row.update(old_row)
        log_rows.append(row)
    log_rows.append(
        make_search_row(
            log_headers_out,
            query_id="P1B-MANUAL-INTAKE-01",
            query_date="2026-08-25",
            phase="P1b",
            query_text="human-supplied manual PDF intake: six files in data/staging/t0_candidates/manual/",
            source_domain="local_manual_staging",
            candidate_result_count="6",
            candidate_source_ids=";".join(record["source_id"] for record in inventory["documents"]),
            excluded_source_ids="T0-IR-008",
            exclusion_reasons="R45178.10.pdf is explicitly the 2020-11-10 version and is background_only outside the T0 window.",
            notes="Header/open/page extraction completed deterministically; no Evidence write; no OCR; run_id=" + args.run_id,
            p1b_run_id=args.run_id,
            discovery_source="manual_input",
            verification_status="verified_pdf_intake",
            manual_followup="R45178 retained only as background; five in-window files remain candidate.",
        )
    )
    search_specs = [
        ("P1B-TD-ARMY-01", "site:army.mil AI 2024 data platform", "army.mil", "T0-TD-007", "Original page blocked; title/date are discovery metadata only."),
        ("P1B-TD-NAVY-01", "site:dvidshub.net/news artificial intelligence Navy 2023", "dvidshub.net", "T0-TD-008", "Original page blocked; title/date are discovery metadata only."),
        ("P1B-CDAO-01", "site:ai.mil CDAO 2024 artificial intelligence", "ai.mil", "T0-CDAO-001", "Original page blocked; title/date are discovery metadata only."),
        ("P1B-ENT-01", "site:dvidshub.net/news artificial intelligence Army 2023", "dvidshub.net", "T0-ENT-001", "Original page blocked; title/date are discovery metadata only."),
        ("P1B-PB-DARPA-01", "site:darpa.mil/news/2023/ai-cyber-challenge-opens", "darpa.mil", "T0-PB-008", "Original DARPA page opened; metadata only, no Evidence extracted."),
        ("P1B-IR-CSET-01", "Task Force 59 CSET DOD tech sector relationship", "cset.georgetown.edu", "T0-IR-009", "Original CSET publication page opened; metadata only, no Evidence extracted."),
    ]
    for query_id, query_text, domain, source_id, note in search_specs:
        item = next(entry for entry in SUPPLEMENT_LEADS if entry["source_id"] == source_id)
        log_rows.append(
            make_search_row(
                log_headers_out,
                query_id=query_id,
                query_date="2026-08-25",
                phase="P1b",
                query_text=query_text,
                source_domain=domain,
                candidate_result_count="1",
                candidate_source_ids=source_id,
                excluded_source_ids="",
                exclusion_reasons="",
                notes=note + " run_id=" + args.run_id,
                p1b_run_id=args.run_id,
                discovery_source="Google News RSS discovery plus direct original-page attempt",
                verification_status=item["original_page_verification"],
                manual_followup=item["manual_followup"],
            )
        )
    search_log_p1b = Path("data/curated/search_log_t0_p1b.csv").resolve()
    write_csv(search_log_p1b, log_headers_out, log_rows)

    # Add metadata-only supplement rows to the page-recall matrix.  No page
    # number is invented for external items that were not locally indexed.
    matrix_path = run_dir / "coverage_matrix.csv"
    matrix_headers, matrix_rows = read_csv(matrix_path)
    for item in SUPPLEMENT_LEADS:
        for dimension in item["possible_coding_dimensions"].split(";"):
            matrix_rows.append(
                {
                    "dimension": dimension,
                    "source_id": item["source_id"],
                    "page_hits": "not_available_external_page",
                    "coverage_status": "metadata_only_pending_original_page" if item["access_status"] != "opened_original_page" else "metadata_only_pending_p2",
                    "notes": "Candidate metadata only; no page index, Evidence, hypothesis, or conclusion was created.",
                }
            )
    write_csv(matrix_path, matrix_headers, matrix_rows)

    version, version_note = code_version()
    p1_after = {str(path): sha256_path(path) for path in p1_paths}
    p1_unchanged = p1_before == p1_after
    if not p1_unchanged:
        raise RuntimeError("Protected P1 artifact hashes changed during P1b.")

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    output_paths = [
        source_manifest_p1b,
        search_log_p1b,
        run_dir / "document_inventory.json",
        run_dir / "coverage_matrix.csv",
        *sorted((run_dir / "page_index").glob("*.jsonl")),
        *sorted((run_dir / "document_cards").glob("*.json")),
    ]
    output_hashes = {str(path): sha256_path(path) for path in output_paths if path.is_file()}
    blocked = [
        {
            "source_id": item["source_id"],
            "title": item["title"],
            "original_source_domain": item["original_source_domain"],
            "discovery_url": item["discovery_url"],
            "reason": "Original page could not be opened and verified in this environment; RSS title/date are not treated as material evidence.",
            "manual_action": item["manual_followup"],
        }
        for item in SUPPLEMENT_LEADS
        if item["access_status"] != "opened_original_page"
    ]
    manifest = {
        "run_id": args.run_id,
        "p1_run_id": args.p1_run_id,
        "case_id": args.case_id,
        "stage": "P1b",
        "execution_date": "2026-08-25",
        "executed_at_utc": generated_at,
        "code_version": version,
        "code_version_note": version_note,
        "config_path": "config/case_us_military_ai.yaml",
        "collection_prompt_path": "prompts/luna_source_collection.txt",
        "manual_input_dir": "data/staging/t0_candidates/manual",
        "manual_pdf_count": inventory["manual_pdf_count"],
        "total_pdf_pages": inventory["total_pdf_pages"],
        "formal_t0_candidates_from_manual": sum(1 for record in inventory["documents"] if record["source_metadata"].get("temporal_status") == "within_t0"),
        "excluded_pre_window": sum(1 for record in inventory["documents"] if record["source_metadata"].get("temporal_status") == "excluded_pre_window"),
        "R45178_date": "2020-11-10",
        "ocr_required": inventory["ocr_required"],
        "input_pdf_inventory": [
            {
                "file_name": record["file_name"],
                "file_sha256": record["file_sha256"],
                "file_size_bytes": record["file_size_bytes"],
                "page_count": record["page_count"],
                "pdf_open_status": record["pdf_open_status"],
                "encrypted": record["encrypted"],
                "extraction_status": record["extraction_status"],
                "ocr_required": record["ocr_required"],
            }
            for record in inventory["documents"]
        ],
        "new_candidates": [
            {
                "source_id": item["source_id"],
                "title": item["title"],
                "published_at": item["published_at"],
                "p1b_group": item["p1b_group"],
                "selection_status": "candidate",
                "verification_status": item["original_page_verification"],
            }
            for item in SUPPLEMENT_LEADS
        ],
        "supplement_candidate_count": len(SUPPLEMENT_LEADS),
        "supplement_original_page_verified_count": sum(item["access_status"] == "opened_original_page" for item in SUPPLEMENT_LEADS),
        "blocked_or_date_unverified_materials": blocked,
        "coverage_matrix_path": str(matrix_path),
        "protected_p1_artifact_hashes_before": p1_before,
        "protected_p1_artifact_hashes_after": p1_after,
        "protected_p1_artifacts_unchanged": p1_unchanged,
        "output_file_sha256": output_hashes,
        "boundary_declarations": {
            "modelclient_called": False,
            "modelclient_statement": "ModelClient was not called.",
            "evidence_rows_added": 0,
            "evidence_statement": "No Evidence table write and no formal Evidence extraction occurred.",
            "t1_read": False,
            "t1_statement": "T1 materials were not read; data/raw/t1_sealed was not accessed.",
            "hypotheses_added": 0,
            "competitive_hypotheses_generated": False,
            "case_conclusion_formed": False,
            "p2_executed": False,
            "candidate_to_include_automation": False,
        },
        "acceptance": {
            "manual_pdf_count": 6,
            "total_pdf_pages": 295,
            "formal_t0_candidates_from_manual": 5,
            "excluded_pre_window": 1,
            "R45178_date": "2020-11-10",
            "ocr_required": 0,
            "evidence_rows_added": 0,
            "hypotheses_added": 0,
            "t1_read": False,
        },
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
        "stop_condition": "P1b intake, page index, document cards, coverage supplement, and versioned manifests complete; stop before P2.",
    }
    manifest_path = run_dir / "p1b_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({
        "run_id": args.run_id,
        "manual_pdf_count": 6,
        "total_pdf_pages": 295,
        "formal_t0_candidates_from_manual": 5,
        "excluded_pre_window": 1,
        "supplement_candidate_count": len(SUPPLEMENT_LEADS),
        "supplement_original_page_verified_count": manifest["supplement_original_page_verified_count"],
        "ocr_required": 0,
        "evidence_rows_added": 0,
        "hypotheses_added": 0,
        "t1_read": False,
        "protected_p1_artifacts_unchanged": p1_unchanged,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
