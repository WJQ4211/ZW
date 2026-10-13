"""Deterministic, non-semantic intake of manually supplied T0 PDFs.

This script is deliberately limited to P1b intake work.  It never imports
Evidence, calls ModelClient, reads T1 material, or makes a P2 admission
decision.  The source PDFs are read in place and are never rewritten.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader


CASE_ID_DEFAULT = "us_military_ai_deployment"
P1_RUN_ID_DEFAULT = "P1-20260825T081142Z-7f4c9b"

MANUAL_SOURCES: dict[str, dict[str, Any]] = {
    "2024-06-RAI-STRATEGY-IMPLEMENTATION-PATHWAY.pdf": {
        "source_id": "T0-SP-007",
        "title": "Responsible AI Strategy and Implementation Pathway",
        "publisher": "U.S. Department of Defense",
        "document_date": "2022-06",
        "published_at": "2022-06",
        "source_grade": "A",
        "material_type": "official_strategy",
        "temporal_status": "within_t0",
        "date_note": "User-supplied document date; filename year was not used.",
    },
    "DOD_DATA_ANALYTICS_AI_ADOPTION_STRATEGY.pdf": {
        "source_id": "T0-SP-008",
        "title": "Data, Analytics, and Artificial Intelligence Adoption Strategy",
        "publisher": "U.S. Department of Defense",
        "document_date": "2023-06-27",
        "public_release_date": "2023-11-02",
        "published_at": "2023-06-27",
        "source_grade": "A",
        "material_type": "official_strategy",
        "temporal_status": "within_t0",
        "date_note": "Document date and public release date are recorded separately.",
    },
    "gao-22-105834.pdf": {
        "source_id": "T0-IR-006",
        "title": "Artificial Intelligence: DOD Should Improve Strategies, Inventory Process, and Collaboration Guidance",
        "publisher": "U.S. Government Accountability Office",
        "published_at": "2022-03",
        "source_grade": "B",
        "material_type": "oversight_report",
        "temporal_status": "within_t0",
    },
    "gao-23-105850.pdf": {
        "source_id": "T0-PB-007",
        "title": "Artificial Intelligence: DOD Needs Department-Wide Guidance to Inform Acquisitions",
        "publisher": "U.S. Government Accountability Office",
        "published_at": "2023-06",
        "source_grade": "B",
        "material_type": "oversight_report",
        "temporal_status": "within_t0",
    },
    "gao-24-105645.pdf": {
        "source_id": "T0-IR-007",
        "title": "Artificial Intelligence: Actions Needed to Improve DOD's Workforce Management",
        "publisher": "U.S. Government Accountability Office",
        "published_at": "2023-12",
        "source_grade": "B",
        "material_type": "oversight_report",
        "temporal_status": "within_t0",
    },
    "R45178.10.pdf": {
        "source_id": "T0-IR-008",
        "title": "Artificial Intelligence and National Security",
        "publisher": "Congressional Research Service",
        "published_at": "2020-11-10",
        "source_grade": "B",
        "material_type": "background_report",
        "temporal_status": "excluded_pre_window",
        "selection_status": "background_only",
        "exclusion_reason": "Explicitly identified as the 2020-11-10 version, outside the 2022-01-01 to 2024-12-31 T0 window.",
    },
}

RESEARCH_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "deployment_fielding_operations": ("deployment", "fielding", "operational"),
    "adoption_scaling": ("adoption", "scaling"),
    "acquisition_procurement": ("acquisition", "procurement", "inventory", "budget"),
    "test_evaluation": ("test and evaluation", "verification", "validation"),
    "workforce_organization": ("workforce",),
    "governance_trust_responsibility": ("governance", "trust", "responsibility"),
    "data_enterprise_collaboration": ("data", "enterprise", "cross-component", "collaboration"),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    """Normalize only for deterministic retrieval; retain raw text unchanged."""

    text = unicodedata.normalize("NFKC", text.replace("\u00ad", ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def text_pages(reader: PdfReader) -> tuple[list[dict[str, Any]], int]:
    pages: list[dict[str, Any]] = []
    ocr_required = 0
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
            if not raw.strip():
                # A page can be a decorative/image-only page while extraction
                # itself succeeds.  This is not an OCR requirement: OCR is
                # reserved for an extraction exception, not for a page with no
                # text objects.
                status = "extracted_empty"
            else:
                status = "extracted"
        except Exception as exc:  # pypdf can fail at page granularity
            raw = ""
            status = "ocr_pending"
            ocr_required += 1
            extraction_error = str(exc)
        row: dict[str, Any] = {
            "pdf_page": page_number,
            "printed_page_or_section": "page_pending_review",
            "text_raw": raw,
            "text_normalized": normalize_text(raw),
            "character_count": len(raw),
            "extraction_status": status,
        }
        if status == "ocr_pending" and "extraction_error" in locals():
            row["extraction_error"] = extraction_error
            del extraction_error
        pages.append(row)
    return pages, ocr_required


def matching_pages(pages: list[dict[str, Any]], patterns: Iterable[str]) -> list[int]:
    terms = tuple(pattern.lower() for pattern in patterns)
    return [
        int(row["pdf_page"])
        for row in pages
        if any(term in row["text_normalized"] for term in terms)
    ]


def selected_text_pages(pages: list[dict[str, Any]], patterns: Iterable[str], limit: int = 5) -> list[dict[str, Any]]:
    matches = matching_pages(pages, patterns)[:limit]
    by_page = {int(row["pdf_page"]): row for row in pages}
    return [
        {"pdf_page": page, "text_raw": by_page[page]["text_raw"]}
        for page in matches
    ]


def make_document_card(metadata: dict[str, Any], pages: list[dict[str, Any]]) -> dict[str, Any]:
    cover = pages[:1]
    executive = selected_text_pages(
        pages,
        ("executive summary", "highlights", "highlights at a glance", "summary"),
    )
    contents = selected_text_pages(pages, ("table of contents", "contents"))
    conclusions = selected_text_pages(
        pages,
        ("conclusion", "conclusions", "recommendation", "recommendations", "agency comments"),
    )
    dimension_hits = {
        name: matching_pages(pages, patterns)[:3]
        for name, patterns in RESEARCH_DIMENSIONS.items()
    }
    hit_dimension_count = sum(bool(page_numbers) for page_numbers in dimension_hits.values())
    return {
        "document_metadata": metadata,
        "cover_page": cover,
        "executive_summary_or_gao_highlights": executive,
        "table_of_contents": contents,
        "conclusions_or_recommendations": conclusions,
        "dimension_hits": dimension_hits,
        "neutral_relevance_note": (
            f"Deterministic keyword recall found pages in {hit_dimension_count} research dimensions; "
            "this card contains no semantic summary or P2 admission decision."
        ),
        "possible_information_increment": [
            "Adds a manually supplied primary or oversight document to the P1b coverage map.",
            "May provide page-locatable material for later human P2 review in the dimensions listed above.",
        ],
        "p2_admission_pending": True,
        "formal_evidence_created": False,
        "competitive_hypotheses_created": False,
        "case_conclusion_created": False,
    }


def intake_one(path: Path, case_id: str, page_index_dir: Path, cards_dir: Path) -> dict[str, Any]:
    file_name = path.name
    if file_name not in MANUAL_SOURCES:
        raise ValueError(f"Unexpected manual PDF: {file_name}")
    metadata = dict(MANUAL_SOURCES[file_name])
    raw_bytes = path.read_bytes()
    file_hash = sha256_bytes(raw_bytes)
    record: dict[str, Any] = {
        "case_id": case_id,
        "source_id": metadata["source_id"],
        "file_name": file_name,
        "file_path": str(path),
        "file_sha256": file_hash,
        "file_size_bytes": len(raw_bytes),
        "header_valid": raw_bytes.startswith(b"%PDF"),
        "pdf_open_status": "not_attempted",
        "encrypted": None,
        "page_count": 0,
        "pdf_metadata": {},
        "extraction_status": "not_attempted",
        "ocr_required": 0,
        "source_metadata": metadata,
    }
    if not record["header_valid"]:
        record["pdf_open_status"] = "invalid_header"
        record["extraction_status"] = "not_run"
        raise ValueError(f"{file_name} does not start with %PDF")

    reader = PdfReader(path)
    record["pdf_open_status"] = "opened"
    record["encrypted"] = bool(reader.is_encrypted)
    if reader.is_encrypted:
        record["pdf_open_status"] = "encrypted"
        record["extraction_status"] = "encrypted"
        raise ValueError(f"{file_name} is encrypted")

    record["page_count"] = len(reader.pages)
    record["pdf_metadata"] = json_safe(reader.metadata or {})
    pages, ocr_required = text_pages(reader)
    record["ocr_required"] = ocr_required
    record["extraction_status"] = "extracted" if ocr_required == 0 else "ocr_pending"
    page_rows = []
    for page in pages:
        page_rows.append(
            {
                "case_id": case_id,
                "source_id": metadata["source_id"],
                "file_name": file_name,
                "file_sha256": file_hash,
                **page,
            }
        )
    page_index_path = page_index_dir / f"{metadata['source_id']}.jsonl"
    write_jsonl(page_index_path, page_rows)

    card_metadata = {
        **metadata,
        "case_id": case_id,
        "source_id": metadata["source_id"],
        "file_name": file_name,
        "file_sha256": file_hash,
        "file_size_bytes": len(raw_bytes),
        "pdf_page_count": len(pages),
        "pdf_metadata": json_safe(reader.metadata or {}),
        "pdf_open_status": record["pdf_open_status"],
        "extraction_status": record["extraction_status"],
    }
    card_path = cards_dir / f"{metadata['source_id']}.json"
    write_json(card_path, make_document_card(card_metadata, pages))
    record["page_index_path"] = str(page_index_path)
    record["document_card_path"] = str(card_path)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("data/staging/t0_candidates/manual"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case-id", default=CASE_ID_DEFAULT)
    parser.add_argument("--p1-run-id", default=P1_RUN_ID_DEFAULT)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    page_index_dir = output_dir / "page_index"
    cards_dir = output_dir / "document_cards"
    pdfs = sorted(input_dir.glob("*.pdf"), key=lambda item: item.name.lower())
    expected_names = set(MANUAL_SOURCES)
    actual_names = {path.name for path in pdfs}
    if actual_names != expected_names:
        raise ValueError(f"Manual PDF set mismatch. expected={sorted(expected_names)} actual={sorted(actual_names)}")
    if len(pdfs) != 6:
        raise ValueError(f"Expected 6 manual PDFs, found {len(pdfs)}")

    records = [intake_one(path, args.case_id, page_index_dir, cards_dir) for path in pdfs]
    total_pages = sum(int(record["page_count"]) for record in records)
    total_ocr = sum(int(record["ocr_required"]) for record in records)
    if total_pages != 295:
        raise ValueError(f"Acceptance page count mismatch: expected 295, found {total_pages}")
    if total_ocr != 0:
        raise ValueError(f"OCR is required for {total_ocr} pages; P1b acceptance requires 0")

    inventory = {
        "run_id": args.run_id,
        "p1_run_id": args.p1_run_id,
        "case_id": args.case_id,
        "stage": "P1b",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_dir": str(input_dir),
        "manual_pdf_count": len(records),
        "total_pdf_pages": total_pages,
        "ocr_required": total_ocr,
        "documents": records,
        "boundary_declarations": {
            "modelclient_called": False,
            "evidence_rows_added": 0,
            "formal_evidence_created": False,
            "t1_read": False,
            "p2_executed": False,
            "hypotheses_added": 0,
            "case_conclusion_created": False,
        },
    }
    write_json(output_dir / "document_inventory.json", inventory)

    # The matrix is a page-recall map only. It intentionally has no evidence or hypothesis columns.
    matrix_path = output_dir / "coverage_matrix.csv"
    all_rows: list[dict[str, str]] = []
    for dimension, patterns in RESEARCH_DIMENSIONS.items():
        for record in records:
            page_path = Path(record["page_index_path"])
            hits = []
            with page_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if any(term in row["text_normalized"] for term in patterns):
                        hits.append(str(row["pdf_page"]))
            if hits:
                all_rows.append(
                    {
                        "dimension": dimension,
                        "source_id": record["source_id"],
                        "page_hits": ";".join(hits[:3]),
                        "coverage_status": "page_recall_only",
                        "notes": "Keyword recall only; P2 admission and formal Evidence are pending.",
                    }
                )
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    with matrix_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["dimension", "source_id", "page_hits", "coverage_status", "notes"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(json.dumps({
        "run_id": args.run_id,
        "manual_pdf_count": len(records),
        "total_pdf_pages": total_pages,
        "ocr_required": total_ocr,
        "page_index_files": len(list(page_index_dir.glob("*.jsonl"))),
        "document_cards": len(list(cards_dir.glob("*.json"))),
        "coverage_rows": len(all_rows),
        "evidence_rows_added": 0,
        "hypotheses_added": 0,
        "t1_read": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
