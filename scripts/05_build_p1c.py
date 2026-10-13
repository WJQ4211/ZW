"""Build the simplified P1c intake package.

This script is deliberately metadata/text-only.  It receives two locally saved
web archives, fetches the explicitly requested DVIDS printable page, and
creates a versioned P1c manifest plus an empty-decision P2 review template.
It does not call ModelClient, write Evidence, read T1, or make P2 decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
P1B_RUN_ID = "P1B-20260825T140911Z-manual"
P1_RUN_ID = "P1-20260825T081142Z-7f4c9b"
P1_SOURCE = ROOT / "data/curated/source_manifest_t0.csv"
P1_SEARCH = ROOT / "data/curated/search_log_t0.csv"
P1_RUN_DIR = ROOT / "outputs" / P1_RUN_ID
P1B_SOURCE = ROOT / "data/curated/source_manifest_t0_p1b.csv"
P1B_SEARCH = ROOT / "data/curated/search_log_t0_p1b.csv"
P1B_RUN_DIR = ROOT / "outputs" / P1B_RUN_ID

OLD_P1_IDS = {
    *(f"T0-SP-{i:03d}" for i in range(1, 7)),
    *(f"T0-PB-{i:03d}" for i in range(1, 7)),
    *(f"T0-TD-{i:03d}" for i in range(1, 7)),
    *(f"T0-IR-{i:03d}" for i in range(1, 6)),
}
MANUAL_PDF_IDS = {"T0-SP-007", "T0-SP-008", "T0-IR-006", "T0-PB-007", "T0-IR-007", "T0-IR-008"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def visible_text(raw: bytes) -> str:
    """Extract visible text deterministically; do not summarize or interpret it."""

    class TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.skip_depth = 0
            self.skip_tags = {"script", "style", "noscript", "svg", "template"}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() in self.skip_tags:
                self.skip_depth += 1

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            return

        def handle_endtag(self, tag: str) -> None:
            if tag.lower() in self.skip_tags and self.skip_depth:
                self.skip_depth -= 1

        def handle_data(self, data: str) -> None:
            if not self.skip_depth:
                self.parts.append(data)

    parser = TextParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def extract_canonical(raw: bytes, fallback: str) -> str:
    text = raw.decode("utf-8", errors="replace")
    patterns = [
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'][^"\']*canonical',
        r"saved from url=\(\d+\)(https?://[^\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).rstrip("\"'>")
    return fallback


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def snapshot_paths() -> list[Path]:
    paths = [P1_SOURCE, P1_SEARCH, P1B_SOURCE, P1B_SEARCH]
    for directory in (P1_RUN_DIR, P1B_RUN_DIR):
        if directory.exists():
            paths.extend(p for p in directory.rglob("*") if p.is_file())
    return sorted(set(paths))


def snapshot_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in paths if path.is_file()}


def find_archive(directory: Path, marker: str) -> Path:
    matches = [p for p in directory.glob("*.html") if marker.lower() in p.name.lower()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {marker} HTML archive, found {len(matches)}")
    return matches[0]


def fetch_ent(url: str, run_dir: Path) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 P1c-intake/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 200)
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
    if status != 200:
        raise RuntimeError(f"DVIDS printable page returned HTTP {status}")
    raw_path = run_dir / "web_raw" / "T0-ENT-001.html"
    text_path = run_dir / "web_text" / "T0-ENT-001.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    text = visible_text(raw)
    text_path.write_text(text + "\n", encoding="utf-8")
    date_verified = bool(re.search(r"Posted\s*:\s*10\.07\.2022", text))
    if not date_verified:
        raise RuntimeError("DVIDS printable page was fetched but Posted: 10.07.2022 was not visible")
    return {
        "source_id": "T0-ENT-001",
        "url": url,
        "http_status": status,
        "content_type": content_type,
        "raw_path": str(raw_path.relative_to(ROOT)),
        "raw_sha256": sha256_bytes(raw),
        "raw_size_bytes": len(raw),
        "text_path": str(text_path.relative_to(ROOT)),
        "text_sha256": sha256_file(text_path),
        "text_character_count": len(text),
        "date_verified": date_verified,
        "published_at": "2022-10-07",
        "capture_type": "direct_printable_html_fetch",
    }


def archive_record(source_id: str, path: Path, canonical_url: str, run_dir: Path) -> dict[str, object]:
    raw = path.read_bytes()
    text = visible_text(raw)
    text_path = run_dir / "web_text" / f"{source_id}.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text + "\n", encoding="utf-8")
    return {
        "source_id": source_id,
        "file_name": path.name,
        "source_path": str(path.relative_to(ROOT)),
        "file_sha256": sha256_bytes(raw),
        "file_size_bytes": len(raw),
        "canonical_url": canonical_url,
        "capture_type": "html_saved_page_with_assets",
        "text_path": str(text_path.relative_to(ROOT)),
        "text_sha256": sha256_file(text_path),
        "text_character_count": len(text),
        "extraction_status": "visible_text_extracted_no_ocr_no_summary",
    }


def add_fields(rows: list[dict[str, str]], fieldnames: list[str]) -> list[str]:
    additions = [
        "canonical_url",
        "capture_type",
        "p2_eligibility",
        "date_revision_old",
        "date_revision_new",
        "date_revision_reason",
        "web_archive_path",
        "body_text_path",
        "web_content_sha256",
        "body_text_sha256",
        "body_text_character_count",
        "hash_scope",
        "full_text_status",
    ]
    for name in additions:
        if name not in fieldnames:
            fieldnames.append(name)
    for row in rows:
        for name in additions:
            row.setdefault(name, "")
    return fieldnames


def normalize_rows(rows: list[dict[str, str]], archives: dict[str, dict[str, object]], ent: dict[str, object]) -> None:
    for row in rows:
        source_id = row["source_id"]
        row["p2_eligibility"] = "pending_human_review"
        if source_id in OLD_P1_IDS:
            row["verification_status"] = "metadata_verified_original_page"
            row["full_text_status"] = "not_captured_p1"
            row["hash_scope"] = "source_response_metadata_only"
            row["capture_type"] = "original_webpage_metadata_only"
            row["canonical_url"] = row.get("url_or_path", "") if row.get("url_or_path", "").startswith("http") else ""
        elif source_id in MANUAL_PDF_IDS:
            row["verification_status"] = "manual_pdf_opened_date_verified"
            row["full_text_status"] = "page_indexed_raw_text"
            row["hash_scope"] = "original_pdf_bytes"
            row["capture_type"] = "manual_pdf_intake"
            row["canonical_url"] = row.get("url_or_path", "") if row.get("url_or_path", "").startswith("http") else ""
        elif source_id in archives:
            record = archives[source_id]
            row["verification_status"] = "manual_web_archive_received"
            row["full_text_status"] = "manual_web_visible_text_captured"
            row["hash_scope"] = "archive_html_bytes"
            row["capture_type"] = str(record["capture_type"])
            row["canonical_url"] = str(record["canonical_url"])
            row["web_archive_path"] = str(record["source_path"])
            row["body_text_path"] = str(record["text_path"])
            row["web_content_sha256"] = str(record["file_sha256"])
            row["body_text_sha256"] = str(record["text_sha256"])
            row["body_text_character_count"] = str(record["text_character_count"])
            row["access_status"] = "opened_local_web_archive"
            row["manual_followup"] = ""
        elif source_id == "T0-CDAO-001":
            row["verification_status"] = "blocked_original_page"
            row["full_text_status"] = "not_available"
            row["hash_scope"] = "none"
            row["capture_type"] = "original_page_blocked"
            row["canonical_url"] = "https://www.ai.mil/"
            row["p2_eligibility"] = "false"
            row["access_status"] = "blocked_original_page"
            row["manual_followup"] = ""
            row["notes"] = "Original page remains blocked; retained as a candidate record but is not eligible for P2. No manual re-fetch required."
        elif source_id == "T0-ENT-001":
            row["published_at"] = str(ent["published_at"])
            row["document_date"] = str(ent["published_at"])
            row["date_status"] = "verified_direct_printable_page"
            row["verification_status"] = "direct_original_page_opened"
            row["full_text_status"] = "printable_body_captured"
            row["hash_scope"] = "direct_printable_html_bytes"
            row["capture_type"] = str(ent["capture_type"])
            row["canonical_url"] = str(ent["url"])
            row["web_archive_path"] = str(ent["raw_path"])
            row["body_text_path"] = str(ent["text_path"])
            row["web_content_sha256"] = str(ent["raw_sha256"])
            row["body_text_sha256"] = str(ent["text_sha256"])
            row["body_text_character_count"] = str(ent["text_character_count"])
            row["access_status"] = "opened_direct_printable_page"
            row["manual_followup"] = ""
            row["notes"] = "Direct printable page fetched and Posted: 10.07.2022 verified; no search-result summary used."
        else:
            row["verification_status"] = "original_page_metadata_verified"
            row["full_text_status"] = "metadata_only_p1b"
            row["hash_scope"] = "source_page_metadata_response"
            row["capture_type"] = "original_webpage_metadata_only"
            row["canonical_url"] = row.get("url_or_path", "") if row.get("url_or_path", "").startswith("http") else ""

        if source_id == "T0-TD-008":
            row["date_revision_old"] = "2023-09-01"
            row["date_revision_new"] = "2023-01-09"
            row["date_revision_reason"] = (
                "The received official Navy HTML archive states the exercise date as Jan. 9; "
                "the previous 2023-09-01 value came from unverified discovery metadata and is superseded."
            )
            row["published_at"] = "2023-01-09"
            row["document_date"] = "2023-01-09"
            row["date_status"] = "verified_manual_official_archive_with_revision"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--manual-web-dir", default="data/staging/t0_candidates/manual_web")
    parser.add_argument("--ent-url", default="https://www.dvidshub.net/news/printable/430931")
    args = parser.parse_args()

    run_dir = (ROOT / args.run_dir).resolve()
    manual_web_dir = (ROOT / args.manual_web_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot_hashes(snapshot_paths())

    fieldnames, rows = read_csv(P1B_SOURCE)
    if len(rows) != 35:
        raise RuntimeError(f"Expected 35 inherited P1b rows, found {len(rows)}")
    by_id = {row["source_id"]: row for row in rows}

    aidp_path = find_archive(manual_web_dir, "AIDP")
    imsc_path = find_archive(manual_web_dir, "IMSC")
    archives = {
        "T0-TD-007": archive_record(
            "T0-TD-007",
            aidp_path,
            extract_canonical(aidp_path.read_bytes(), "https://cpeisw.army.mil/2024/10/09/277811/"),
            run_dir,
        ),
        "T0-TD-008": archive_record(
            "T0-TD-008",
            imsc_path,
            extract_canonical(
                imsc_path.read_bytes(),
                "https://www.navy.mil/Press-Office/News-Stories/Article/3262053/imsc-task-force-completes-maritime-exercise-with-unmanned-systems-ai/",
            ),
            run_dir,
        ),
    }
    ent = fetch_ent(args.ent_url, run_dir)
    normalize_rows(rows, archives, ent)
    add_fields(rows, fieldnames)

    # Ensure no decision is silently introduced.  candidate/background_only are
    # inherited collection statuses, not P2 review decisions.
    for row in rows:
        row.setdefault("selection_status", "candidate")
        if row["selection_status"] in {"include", "exclude"}:
            raise RuntimeError(f"P1c must not make include/exclude decisions: {row['source_id']}")

    source_out = ROOT / "data/curated/source_manifest_t0_p1c.csv"
    write_csv(source_out, fieldnames, rows)

    review_fields = [
        "source_id",
        "title",
        "published_at",
        "source_grade",
        "verification_status",
        "full_text_status",
        "hash_scope",
        "canonical_url",
        "capture_type",
        "p2_eligibility",
        "selection_status",
        "review_decision",
        "reviewer",
        "reviewed_at",
        "review_reason",
    ]
    review_rows = []
    for row in rows:
        review_rows.append({
            key: row.get(key, "") for key in review_fields if key != "review_decision"
        } | {"review_decision": "", "reviewer": "", "reviewed_at": "", "review_reason": ""})
    review_out = ROOT / "data/curated/p2_review_template.csv"
    write_csv(review_out, review_fields, review_rows)

    inventory = {
        "run_id": args.run_id,
        "case_id": CASE_ID,
        "stage": "P1c",
        "manual_web_dir": str(manual_web_dir.relative_to(ROOT)),
        "manual_web_count": 2,
        "archives": list(archives.values()),
        "direct_fetch": ent,
        "original_html_archives_not_modified": True,
    }
    write_json(run_dir / "web_archive_inventory.json", inventory)

    after = snapshot_hashes(snapshot_paths())
    if before != after:
        changed = sorted(set(before) | set(after))
        changed = [path for path in changed if before.get(path) != after.get(path)]
        raise RuntimeError("Protected P1/P1b artifact changed: " + ", ".join(changed))

    old23 = [row for row in rows if row["source_id"] in OLD_P1_IDS]
    revision = by_id["T0-TD-008"]
    manifest: dict[str, object] = {
        "run_id": args.run_id,
        "p1_run_id": P1_RUN_ID,
        "inherited_p1b_run_id": P1B_RUN_ID,
        "case_id": CASE_ID,
        "stage": "P1c",
        "execution_date": datetime.now(timezone.utc).date().isoformat(),
        "executed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "code_version": "unverified_local_copy",
        "code_version_note": "Workspace root has no readable Git commit.",
        "input_files": {
            "manual_web_archives": [
                {"path": r["source_path"], "sha256": r["file_sha256"], "size_bytes": r["file_size_bytes"]}
                for r in archives.values()
            ],
            "direct_ent_response": {"path": ent["raw_path"], "sha256": ent["raw_sha256"], "size_bytes": ent["raw_size_bytes"]},
            "inherited_p1b_source_manifest_sha256": sha256_file(P1B_SOURCE),
            "inherited_p1b_search_log_sha256": sha256_file(P1B_SEARCH),
        },
        "inherited_p1b_pdf_inputs": json.loads((P1B_RUN_DIR / "p1b_manifest.json").read_text(encoding="utf-8")).get("input_pdf_inventory", []),
        "archive_records": list(archives.values()),
        "ent_record": ent,
        "date_revision": {
            "source_id": "T0-TD-008",
            "old_value": revision["date_revision_old"],
            "new_value": revision["date_revision_new"],
            "reason": revision["date_revision_reason"],
        },
        "cdao_record": {
            "source_id": "T0-CDAO-001",
            "verification_status": by_id["T0-CDAO-001"]["verification_status"],
            "p2_eligibility": by_id["T0-CDAO-001"]["p2_eligibility"],
            "manual_followup_required": False,
        },
        "verification_normalization": {
            "old_p1_candidate_count": len(old23),
            "old_p1_statuses": {
                "verification_status": sorted({row["verification_status"] for row in old23}),
                "full_text_status": sorted({row["full_text_status"] for row in old23}),
                "hash_scope": sorted({row["hash_scope"] for row in old23}),
            },
            "all_rows_have_verification_status": all(row["verification_status"] for row in rows),
            "all_rows_have_full_text_status": all(row["full_text_status"] for row in rows),
            "all_rows_have_hash_scope": all(row["hash_scope"] for row in rows),
        },
        "output_file_sha256": {
            str(source_out.relative_to(ROOT)): sha256_file(source_out),
            str(review_out.relative_to(ROOT)): sha256_file(review_out),
            "outputs/{run_id}/web_archive_inventory.json".format(run_id=args.run_id): sha256_file(run_dir / "web_archive_inventory.json"),
            str(run_dir / "web_text/T0-TD-007.txt"): sha256_file(run_dir / "web_text/T0-TD-007.txt"),
            str(run_dir / "web_text/T0-TD-008.txt"): sha256_file(run_dir / "web_text/T0-TD-008.txt"),
            str(run_dir / "web_text/T0-ENT-001.txt"): sha256_file(run_dir / "web_text/T0-ENT-001.txt"),
            str(run_dir / "web_raw/T0-ENT-001.html"): sha256_file(run_dir / "web_raw/T0-ENT-001.html"),
        },
        "protected_artifacts_unchanged": True,
        "protected_p1_hashes_after": {str(P1_SOURCE): after[str(P1_SOURCE)], str(P1_SEARCH): after[str(P1_SEARCH)]},
        "protected_p1b_hashes_after": {str(P1B_SOURCE): after[str(P1B_SOURCE)], str(P1B_SEARCH): after[str(P1B_SEARCH)]},
        "boundary_declarations": {
            "modelclient_called": False,
            "evidence_rows_added": 0,
            "t1_read": False,
            "p2_executed": False,
            "p3_executed": False,
            "hypotheses_added": 0,
            "case_conclusion_formed": False,
            "candidate_to_include_exclude_automation": False,
            "search_result_summary_used_for_ent": False,
            "formal_evidence_extracted": False,
        },
        "acceptance": {
            "manual_web_count": 2,
            "formal_t0_rows_inherited": len(rows),
            "date_revision_old": "2023-09-01",
            "date_revision_new": "2023-01-09",
            "cdao_p2_eligibility": False,
            "ent_date": ent["published_at"],
            "p2_review_decisions_populated": False,
            "evidence_rows_added": 0,
            "modelclient_called": False,
            "t1_read": False,
        },
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
        "stop_condition": "Simplified P1c web archive intake and P2 template complete; stop before P2/P3.",
    }
    manifest_path = run_dir / "p1c_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({
        "run_id": args.run_id,
        "source_manifest": str(source_out),
        "p2_review_template": str(review_out),
        "manual_web_count": 2,
        "ent_date": ent["published_at"],
        "p2_executed": False,
        "evidence_rows_added": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
