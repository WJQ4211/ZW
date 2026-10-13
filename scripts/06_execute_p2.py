"""Execute P2a review decisions and P2b full-text intake.

The script is intentionally bounded at P2.  It records human pilot review
decisions, fetches only the three explicitly requested original pages, and
creates versioned P2 artifacts.  It never reads T1, calls ModelClient, writes
Evidence, extracts evidence, or runs P3.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
P1_RUN_ID = "P1-20260825T081142Z-7f4c9b"
P1B_RUN_ID = "P1B-20260825T140911Z-manual"
P1C_RUN_ID = "P1C-20260825T152730Z-manual-web"
P1_SOURCE = ROOT / "data/curated/source_manifest_t0.csv"
P1_SEARCH = ROOT / "data/curated/search_log_t0.csv"
P1B_SOURCE = ROOT / "data/curated/source_manifest_t0_p1b.csv"
P1B_SEARCH = ROOT / "data/curated/search_log_t0_p1b.csv"
P1C_SOURCE = ROOT / "data/curated/source_manifest_t0_p1c.csv"
P1C_TEMPLATE = ROOT / "data/curated/p2_review_template.csv"
P1_DIR = ROOT / "outputs" / P1_RUN_ID
P1B_DIR = ROOT / "outputs" / P1B_RUN_ID
P1C_DIR = ROOT / "outputs" / P1C_RUN_ID

INCLUDED = [
    "T0-SP-007",
    "T0-SP-008",
    "T0-PB-003",
    "T0-PB-007",
    "T0-TD-004",
    "T0-TD-007",
    "T0-TD-008",
    "T0-IR-006",
    "T0-TD-006",
]
INCLUDED_SET = set(INCLUDED)
RESERVE_ONLY = {"T0-IR-002", "T0-IR-003", "T0-IR-007", "T0-ENT-001"}
FETCH_IDS = {"T0-PB-003", "T0-TD-004", "T0-TD-006"}

INCLUDE_REASONS = {
    "T0-SP-007": "官方国防部责任人工智能战略材料，日期可核验，直接提供治理与实施路径信息，并补充既有战略治理候选。",
    "T0-SP-008": "官方国防部数据、分析与人工智能采用战略，日期可核验，直接涉及跨部门采用和企业化推广，具有信息增量。",
    "T0-PB-003": "DARPA官方项目材料，日期和原文可核验，直接描述任务自主能力项目，补充项目层面的能力成熟度信息。",
    "T0-PB-007": "GAO官方监督材料，日期和原文可核验，直接涉及国防部人工智能采购指导，提供独立的采购治理约束信息。",
    "T0-TD-004": "DARPA官方试验/应用材料，日期和原文可核验，直接描述航空航天人工智能试验里程碑，补充试验到应用的信息。",
    "T0-TD-007": "美国陆军官方网页存档，日期、原文和归档哈希可核验，直接描述AIDP向多个陆军单位推广，提供部署扩展信息。",
    "T0-TD-008": "美国海军官方网页存档，日期、原文和归档哈希可核验，直接描述无人系统与人工智能海上演练，提供组织运用信息。",
    "T0-IR-006": "GAO官方监督报告，日期、原文和页级索引可核验，直接涉及国防部人工智能战略、清单和协同缺口，提供独立约束信息。",
    "T0-TD-006": "CSET原始研究网页，日期、原文和内容哈希可核验，直接讨论机载人工智能约束，补充平台部署的工程限制信息。",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def visible_text(raw: bytes) -> str:
    """Extract visible HTML text without summarizing or semantically editing it."""

    class TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.skip_depth = 0
            self.skip_tags = {"script", "style", "noscript", "svg", "template"}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() in self.skip_tags:
                self.skip_depth += 1

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
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).rstrip("\"'>")
    return fallback


def date_tokens(iso_date: str) -> list[str]:
    year, month, day = (int(part) for part in iso_date.split("-"))
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    abbreviations = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    month_name = months[month - 1]
    month_abbreviation = abbreviations[month - 1]
    return [
        iso_date,
        iso_date.replace("-", "/"),
        f"{month_name} {day}, {year}",
        f"{month_name} {day} {year}",
        f"{month_name[:3]} {day}, {year}",
        f"{month_abbreviation}. {day}, {year}",
        f"{month_abbreviation} {day}, {year}",
    ]


def current_git_head() -> dict[str, str]:
    candidates = [ROOT / ".github_upload_staging", ROOT]
    for git_root in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(git_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        head = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return {"commit": head, "short_commit": head[:12], "git_root": str(git_root)}
    raise RuntimeError("Unable to read a real Git HEAD; refusing unverified_local_copy.")


def protected_paths() -> list[Path]:
    paths = [P1_SOURCE, P1_SEARCH, P1B_SOURCE, P1B_SEARCH, P1C_SOURCE, P1C_TEMPLATE]
    for directory in (P1_DIR, P1B_DIR, P1C_DIR):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(set(path for path in paths if path.is_file()))


def protected_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in paths}


def fetch_original(source_id: str, row: dict[str, str], run_dir: Path) -> dict[str, object]:
    url = row.get("canonical_url") or row.get("url_or_path")
    expected_date = row.get("published_at", "")
    record: dict[str, object] = {
        "source_id": source_id,
        "requested_url": url,
        "expected_published_at": expected_date,
        "capture_type": "direct_original_webpage_fetch",
        "hash_scope": "original_webpage_html_bytes",
    }
    if not url.startswith("http"):
        record.update({"status": "full_text_pending", "error": "No HTTP original URL in inherited manifest."})
        return record
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 P2-intake/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        raw_path = run_dir / "source_raw" / f"{source_id}.html"
        text_path = run_dir / "source_text" / f"{source_id}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw)
        text = visible_text(raw)
        text_path.write_text(text + "\n", encoding="utf-8")
        decoded = raw.decode("utf-8", errors="replace")
        date_verified = any(token.lower() in decoded.lower() for token in date_tokens(expected_date))
        canonical = extract_canonical(raw, url)
        if not date_verified:
            raise RuntimeError(f"Published date {expected_date} not found in original response")
        record.update({
            "status": "full_text_captured",
            "http_status": status,
            "content_type": content_type,
            "canonical_url": canonical,
            "published_at": expected_date,
            "raw_path": rel(raw_path),
            "raw_sha256": sha256_bytes(raw),
            "raw_size_bytes": len(raw),
            "text_path": rel(text_path),
            "text_sha256": sha256_file(text_path),
            "text_character_count": len(text),
            "date_verified": date_verified,
            "error": "",
        })
    except Exception as exc:  # P2 must finish with a closed P3 gate on failure.
        record.update({"status": "full_text_pending", "error": f"{type(exc).__name__}: {exc}"})
    return record


def reason_for(source_id: str) -> tuple[str, str]:
    if source_id in INCLUDED_SET:
        return "include", INCLUDE_REASONS[source_id]
    if source_id == "T0-CDAO-001":
        return "exclude", "原始页面不可获得，不满足正式证据可追溯要求。"
    if source_id == "T0-IR-008":
        return "exclude", "时间窗外背景材料，不作为T0当前演进信号。"
    if source_id in RESERVE_ONLY:
        return "exclude", "因最小样本配额和信息重复暂不纳入，可在正式材料缺失时替补。"
    return "exclude", "不代表材料内容错误；因直接性、信息增量或最小样本配额未进入本次正式证据池。"


def disposition(source_id: str) -> str:
    if source_id in INCLUDED_SET:
        return "formal_selected"
    if source_id in RESERVE_ONLY:
        return "reserve_only"
    if source_id == "T0-CDAO-001":
        return "excluded_original_unavailable"
    if source_id == "T0-IR-008":
        return "excluded_pre_window"
    return "not_selected_minimal_sample"


def append_fields(fieldnames: list[str], names: Iterable[str]) -> list[str]:
    for name in names:
        if name not in fieldnames:
            fieldnames.append(name)
    return fieldnames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty P2 run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    head = current_git_head()
    protected_before = protected_hashes(protected_paths())
    fieldnames, rows = read_csv(P1C_SOURCE)
    if len(rows) != 35:
        raise RuntimeError(f"Expected 35 P1c source rows, found {len(rows)}")
    by_id = {row["source_id"]: row for row in rows}
    if set(INCLUDED) - set(by_id):
        raise RuntimeError("Required included source ID is absent from P1c manifest")

    reviewed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fetch_records = {source_id: fetch_original(source_id, by_id[source_id], run_dir) for source_id in FETCH_IDS}

    review_fields = [
        "source_id", "title", "published_at", "source_grade", "verification_status",
        "full_text_status", "hash_scope", "canonical_url", "capture_type", "p2_eligibility",
        "selection_status", "review_decision", "reviewer", "review_mode",
        "not_for_expert_evaluation", "reviewed_at", "review_reason", "p2_disposition",
    ]
    review_rows: list[dict[str, str]] = []
    for row in rows:
        decision, reason = reason_for(row["source_id"])
        review_rows.append({
            "source_id": row["source_id"],
            "title": row["title"],
            "published_at": row["published_at"],
            "source_grade": row["source_grade"],
            "verification_status": row["verification_status"],
            "full_text_status": row["full_text_status"],
            "hash_scope": row["hash_scope"],
            "canonical_url": row.get("canonical_url", ""),
            "capture_type": row.get("capture_type", ""),
            "p2_eligibility": row.get("p2_eligibility", ""),
            "selection_status": row.get("selection_status", ""),
            "review_decision": decision,
            "reviewer": "author_pilot",
            "review_mode": "author_pilot",
            "not_for_expert_evaluation": "true",
            "reviewed_at": reviewed_at,
            "review_reason": reason,
            "p2_disposition": disposition(row["source_id"]),
        })

    decisions_out = ROOT / "data/curated/p2_review_decisions_t0.csv"
    write_csv(decisions_out, review_fields, review_rows)

    manifest_fields = append_fields(fieldnames[:], [
        "review_decision", "reviewer", "review_mode", "not_for_expert_evaluation",
        "reviewed_at", "review_reason", "p2_disposition", "p2_raw_path", "p2_text_path",
        "p2_content_sha256", "p2_text_sha256", "p2_date_verified", "p2_fetch_status",
    ])
    for row in rows:
        decision, reason = reason_for(row["source_id"])
        row.update({
            "review_decision": decision,
            "reviewer": "author_pilot",
            "review_mode": "author_pilot",
            "not_for_expert_evaluation": "true",
            "reviewed_at": reviewed_at,
            "review_reason": reason,
            "p2_disposition": disposition(row["source_id"]),
        })
        fetch = fetch_records.get(row["source_id"])
        if fetch and fetch.get("status") == "full_text_captured":
            row["verification_status"] = "direct_original_page_opened_and_date_verified"
            row["full_text_status"] = "original_page_body_captured"
            row["hash_scope"] = str(fetch["hash_scope"])
            row["canonical_url"] = str(fetch["canonical_url"])
            row["capture_type"] = str(fetch["capture_type"])
            row["local_path"] = str(fetch["raw_path"])
            row["body_text_path"] = str(fetch["text_path"])
            row["web_archive_path"] = str(fetch["raw_path"])
            row["content_hash"] = str(fetch["raw_sha256"])
            row["web_content_sha256"] = str(fetch["raw_sha256"])
            row["body_text_sha256"] = str(fetch["text_sha256"])
            row["body_text_character_count"] = str(fetch["text_character_count"])
            row["p2_raw_path"] = str(fetch["raw_path"])
            row["p2_text_path"] = str(fetch["text_path"])
            row["p2_content_sha256"] = str(fetch["raw_sha256"])
            row["p2_text_sha256"] = str(fetch["text_sha256"])
            row["p2_date_verified"] = "true"
            row["p2_fetch_status"] = "full_text_captured"
        elif fetch:
            row["p2_fetch_status"] = "full_text_pending"
            row["p2_date_verified"] = "false"
            row["p2_raw_path"] = ""
            row["p2_text_path"] = ""
            row["p2_content_sha256"] = ""
            row["p2_text_sha256"] = ""
        else:
            row["p2_fetch_status"] = "inherited_full_text_available"
            row["p2_date_verified"] = "inherited_or_archive_verified"
            row["p2_raw_path"] = row.get("local_path", "") or row.get("web_archive_path", "")
            row["p2_text_path"] = row.get("body_text_path", "") or row.get("page_index_path", "")
            row["p2_content_sha256"] = row.get("content_hash", "") or row.get("web_content_sha256", "")
            row["p2_text_sha256"] = row.get("body_text_sha256", "")

        # P1c's web archives already have a source hash; carry it into the P2
        # manifest so the nine selected records all have a direct content hash.
        if row["source_id"] in {"T0-TD-007", "T0-TD-008"}:
            row["content_hash"] = row.get("web_content_sha256", "")
            row["local_path"] = row.get("web_archive_path", "")
            row["p2_content_sha256"] = row.get("web_content_sha256", "")
            row["p2_text_sha256"] = row.get("body_text_sha256", "")
            row["p2_raw_path"] = row.get("web_archive_path", "")
            row["p2_text_path"] = row.get("body_text_path", "")

    source_out = ROOT / "data/curated/source_manifest_t0_p2.csv"
    write_csv(source_out, manifest_fields, rows)

    selected_rows = []
    for source_id in INCLUDED:
        row = next(row for row in rows if row["source_id"] == source_id)
        locator = row.get("canonical_url") or row.get("local_path") or row.get("url_or_path")
        content_hash = row.get("content_hash", "")
        selected_rows.append({
            "source_id": source_id,
            "title": row["title"],
            "publisher": row["publisher"],
            "source_grade": row["source_grade"],
            "published_at": row["published_at"],
            "canonical_url": row.get("canonical_url", ""),
            "url_or_path": row.get("url_or_path", ""),
            "local_path": row.get("local_path", ""),
            "source_locator": locator,
            "page_index_path": row.get("page_index_path", ""),
            "document_card_path": row.get("document_card_path", ""),
            "body_text_path": row.get("body_text_path", "") or row.get("p2_text_path", ""),
            "verification_status": row["verification_status"],
            "full_text_status": row["full_text_status"],
            "content_hash": content_hash,
            "hash_scope": row["hash_scope"],
            "review_decision": row["review_decision"],
        })
    selected_fields = list(selected_rows[0].keys())
    selected_out = run_dir / "selected_source_inventory.csv"
    write_csv(selected_out, selected_fields, selected_rows)

    missing_full_text = [
        row["source_id"] for row in selected_rows
        if row["full_text_status"] in {"full_text_pending", "not_available", "not_captured_p1"}
        or len(row["content_hash"]) != 64
        or not row["source_locator"]
    ]
    p3_gate_open = len(missing_full_text) == 0 and len(selected_rows) == 9
    output_paths = [decisions_out, source_out, selected_out]
    output_paths.extend(path for path in run_dir.rglob("*") if path.is_file())
    output_hashes = {rel(path): sha256_file(path) for path in sorted(set(output_paths))}

    manifest = {
        "run_id": args.run_id,
        "case_id": CASE_ID,
        "stage": "P2",
        "p1_run_id": P1_RUN_ID,
        "p1b_run_id": P1B_RUN_ID,
        "p1c_run_id": P1C_RUN_ID,
        "executed_at_utc": reviewed_at,
        "reviewed_at": reviewed_at,
        "git_head": head["commit"],
        "git_head_short": head["short_commit"],
        "git_root": head["git_root"],
        "p2a": {
            "p2_review_complete": True,
            "reviewer": "author_pilot",
            "review_mode": "author_pilot",
            "not_for_expert_evaluation": True,
            "included_count": len(INCLUDED_SET),
            "excluded_count": sum(row["review_decision"] == "exclude" for row in review_rows),
            "reserve_count": sum(row["p2_disposition"] == "reserve_only" for row in review_rows),
            "selected_source_ids": INCLUDED,
            "review_decision_file": rel(decisions_out),
        },
        "p2b": {
            "requested_source_ids": sorted(FETCH_IDS),
            "fetch_records": list(fetch_records.values()),
            "missing_full_text_source_ids": missing_full_text,
            "search_result_summaries_not_used": True,
            "cdao_refetch_attempted": False,
        },
        "p3_gate_open": p3_gate_open,
        "p3_executed": False,
        "selected_source_inventory": rel(selected_out),
        "output_file_sha256": output_hashes,
        "protected_input_hashes_before": protected_before,
        "protected_p1_p1b_p1c_unchanged": True,
        "modelclient_call_count": 0,
        "evidence_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "formal_evidence_extracted": False,
        "competitive_hypotheses_generated": False,
        "boundary_declarations": {
            "data_raw_t1_sealed_read": False,
            "modelclient_called": False,
            "evidence_written": False,
            "p3_started": False,
        },
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
        "stop_condition": "P2a review decisions and P2b requested full-text intake complete; stop before P3.",
    }
    manifest_path = run_dir / "p2_manifest.json"
    write_json(manifest_path, manifest)

    protected_after = protected_hashes(protected_paths())
    if protected_before != protected_after:
        changed = sorted(path for path in set(protected_before) | set(protected_after) if protected_before.get(path) != protected_after.get(path))
        raise RuntimeError("Protected P1/P1b/P1c artifact changed: " + ", ".join(changed))

    print(json.dumps({
        "run_id": args.run_id,
        "p2_review_complete": True,
        "included_count": len(INCLUDED_SET),
        "excluded_count": manifest["p2a"]["excluded_count"],
        "reserve_count": manifest["p2a"]["reserve_count"],
        "missing_full_text_source_ids": missing_full_text,
        "p3_gate_open": p3_gate_open,
        "p3_executed": False,
        "git_head": head["commit"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
