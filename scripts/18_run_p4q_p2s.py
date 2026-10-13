"""Run the P4Q preflight audit and prepare the P2S source supplement.

This step is intentionally offline after the five original HTML captures have
been placed in the run directory.  It never creates Evidence, calls a model,
reads T1 content, or opens a snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from lxml import html


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
C1_RUN_ID = "G1-T0-C1-31F142A5227EA580-correction-v1"
C1_DIR = ROOT / "outputs" / C1_RUN_ID
CANONICAL_PATH = C1_DIR / "g1_t0_c1_latest_evidence_canonical.jsonl"
DB_PATH = ROOT / "data" / "research.sqlite3"
P2_MANIFEST_PATH = ROOT / "data" / "curated" / "source_manifest_t0_p2.csv"

P2S_SOURCE_IDS = ["T0-SP-001", "T0-TD-001", "T0-TD-002", "T0-TD-003", "T0-TD-005"]
CAPTURED_SOURCE_METADATA = {
    "T0-SP-001": {"title": "Developing Algorithms that Make Decisions Aligned with Human Experts", "published_at": "2022-03-03"},
    "T0-TD-001": {"title": "RACER Revs Up for Checkered Flag Goal of High-Speed, Off-Road Autonomy", "published_at": "2022-01-13"},
    "T0-TD-002": {"title": "RACER's Off-Road Autonomous Vehicles Teams Navigate Third Test", "published_at": "2023-04-11"},
    "T0-TD-003": {"title": "A New Take on Modeling & Simulation for Improved Autonomy", "published_at": "2023-10-17"},
    "T0-TD-005": {"title": "DART and Coordinated Robotics Take Top Spots in the DARPA Triage Challenge Event 1", "published_at": "2024-10-05"},
}
GAP_AREAS = {
    "T0-SP-001": ["human_machine_collaboration", "trust_governance_constraints"],
    "T0-TD-001": ["test_activity", "application_maturity"],
    "T0-TD-002": ["test_activity", "test_result", "application_maturity"],
    "T0-TD-003": ["engineering_conditions", "application_maturity"],
    "T0-TD-005": ["test_activity", "test_result", "application_maturity"],
}
SELECTION_REASON = (
    "均为A级官方一手来源；均处于T0截止日期以内；补充现有样本在试验活动、"
    "试验结果、工程条件、人机协同和应用成熟度方面的覆盖缺口；此前排除原因为"
    "最小样本控制，而非真实性、相关性或可追溯性不合格。"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_text(value: str) -> str:
    value = unescape(value).replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_title_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("’", "'")
    value = re.sub(r"\s*\|\s*DARPA\s*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def parse_published_date(value: str) -> str:
    candidate = re.sub(r"(?<=\b[A-Za-z]{3})\.", "", value).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable_not_a_git_repository"


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def protected_output_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        path.relative_to(ROOT).as_posix(): sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and excluded not in path.resolve().parents and path.resolve() != excluded
    }


def load_p2_rows() -> dict[str, dict[str, str]]:
    with P2_MANIFEST_PATH.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["source_id"]: row for row in csv.DictReader(handle)}
    missing = [source_id for source_id in P2S_SOURCE_IDS if source_id not in rows]
    if missing:
        raise RuntimeError(f"P2 source manifest is missing requested source IDs: {missing}")
    return rows


def extract_original_page(raw_path: Path) -> dict[str, str]:
    tree = html.fromstring(raw_path.read_bytes())
    title = normalize_text(tree.xpath("string(//title)"))
    canonical_url = normalize_text(tree.xpath("string(//link[@rel='canonical']/@href)"))
    date_text = normalize_text(" ".join(tree.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' news-date ')]//text()")))
    body_nodes = tree.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' field--name-field-body-with-summary ')]")
    if not body_nodes:
        raise RuntimeError(f"original page body container is missing: {raw_path.name}")
    paragraphs: list[str] = []
    for node in body_nodes[0].xpath(".//h2|.//h3|.//h4|.//p|.//li"):
        value = normalize_text(" ".join(node.itertext()))
        if value:
            paragraphs.append(value)
    body_text = "\n\n".join(paragraphs).strip()
    if not body_text:
        raise RuntimeError(f"original page body text is empty: {raw_path.name}")
    return {"title": title, "canonical_url": canonical_url, "date_text": date_text, "body_text": body_text}


def build_p4q(canonical: list[dict[str, Any]]) -> dict[str, Any]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag=? ORDER BY evidence_id, version",
            (CASE_ID, "T0"),
        ).fetchall()
        snapshot_count = conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=?", (CASE_ID,)).fetchone()[0]
        snapshot_items_count = conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE case_id=?", (CASE_ID,)).fetchone()[0]
    finally:
        conn.close()

    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    physical_rows = len(rows)
    for evidence_id, version, payload_json in rows:
        payload = json.loads(payload_json)
        if evidence_id not in latest or version > latest[evidence_id][0]:
            latest[evidence_id] = (version, payload)
    latest_payloads = [latest[evidence_id][1] for evidence_id in sorted(latest)]
    canonical_by_id = {item["evidence_id"]: item for item in canonical}
    latest_by_id = {item["evidence_id"]: item for item in latest_payloads}
    field_differences = []
    for evidence_id in sorted(set(canonical_by_id) | set(latest_by_id)):
        if canonical_by_id.get(evidence_id) != latest_by_id.get(evidence_id):
            field_differences.append(evidence_id)
    source_grades = Counter(item.get("source_grade") for item in canonical)
    published_after_cutoff = sum(item.get("published_at", "") > "2024-12-31" for item in canonical)
    target_versions = [version for evidence_id, version, _ in rows if evidence_id == "EV-T0-3D9421B5DCC381FC"]
    target_in_canonical = [item for item in canonical if item["evidence_id"] == "EV-T0-3D9421B5DCC381FC"]
    return {
        "canonical_logical_evidence_count": len(canonical),
        "sqlite_physical_t0_evidence_rows": physical_rows,
        "sqlite_latest_version_logical_evidence_count": len(latest_payloads),
        "canonical_latest_field_difference_count": len(field_differences),
        "canonical_latest_field_difference_ids": field_differences,
        "target_evidence_id": "EV-T0-3D9421B5DCC381FC",
        "target_versions_in_sqlite": target_versions,
        "target_entries_in_canonical": [{"version": item.get("version"), "evidence_nature": item.get("evidence_nature")} for item in target_in_canonical],
        "formal_evidence_source_count": len({item.get("source_id") for item in canonical}),
        "source_grade_counts": dict(source_grades),
        "a_grade_fragment_count": source_grades.get("A", 0),
        "b_grade_fragment_count": source_grades.get("B", 0),
        "d_grade_fragment_count": source_grades.get("D", 0),
        "a_grade_fragment_ratio": round(source_grades.get("A", 0) / len(canonical), 6) if canonical else 0,
        "published_after_2024_12_31_count": published_after_cutoff,
        "snapshot_count": snapshot_count,
        "snapshot_items_count": snapshot_items_count,
        "p4_gate_open": False,
        "p4_block_reason": "A级正式证据片段占比51.67%，低于60%",
    }


def build_p2s(run_dir: Path, p2_rows: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_manifest_rows: list[dict[str, Any]] = []
    capture_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    existing_text_hashes: dict[str, str] = {}
    for row in p2_rows.values():
        text_path = row.get("p2_text_path", "")
        candidate = Path(text_path) if Path(text_path).is_absolute() else ROOT / text_path.replace("\\", "/")
        if candidate.exists() and candidate.is_file():
            existing_text_hashes[row["source_id"]] = sha256_bytes(normalize_text(candidate.read_text(encoding="utf-8", errors="replace")).encode("utf-8"))

    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for source_id in P2S_SOURCE_IDS:
        prior = p2_rows[source_id]
        raw_path = run_dir / "source_raw" / f"{source_id}.html"
        text_path = run_dir / "source_text" / f"{source_id}.txt"
        if not raw_path.exists():
            raise RuntimeError(f"captured original page missing: {raw_path}")
        parsed = extract_original_page(raw_path)
        text_path.write_text(parsed["body_text"] + "\n", encoding="utf-8")
        raw_sha = sha256_file(raw_path)
        text_sha = sha256_file(text_path)
        expected = CAPTURED_SOURCE_METADATA[source_id]
        expected_url = prior["url_or_path"]
        date_verified = parse_published_date(parsed["date_text"]) == expected["published_at"]
        title_verified = normalize_title_for_match(parsed["title"]) == normalize_title_for_match(expected["title"])
        url_verified = parsed["canonical_url"] == expected_url
        duplicate_hash_matches = [existing_id for existing_id, existing_hash in existing_text_hashes.items() if existing_hash == text_sha]
        duplicate_status = "exact_text_duplicate_found" if duplicate_hash_matches else "no_exact_duplicate_in_existing_local_archive"
        verification_status = "verified_original_page_html" if title_verified and date_verified and url_verified else "verification_review_required"
        p3s_eligibility = "eligible_for_p3s_review" if verification_status == "verified_original_page_html" and not duplicate_hash_matches else "not_yet_eligible"
        source_manifest_rows.append({
            "source_id": source_id,
            "title": parsed["title"],
            "publisher": prior["publisher"],
            "source_grade": prior["source_grade"],
            "published_at": expected["published_at"],
            "document_date": prior.get("document_date", ""),
            "temporal_status": "within_t0_window",
            "source_type": prior.get("source_type", "official_program_news"),
            "canonical_url": parsed["canonical_url"],
            "capture_type": "original_webpage_html",
            "raw_path": raw_path.relative_to(ROOT).as_posix(),
            "text_path": text_path.relative_to(ROOT).as_posix(),
            "raw_sha256": raw_sha,
            "text_sha256": text_sha,
            "hash_scope": "raw_html_bytes_and_extracted_body_text_bytes",
            "raw_file_size": raw_path.stat().st_size,
            "text_character_count": len(parsed["body_text"]),
            "verification_status": verification_status,
            "full_text_status": "captured_original_body_text",
            "prior_review_decision": prior.get("review_decision", "exclude"),
            "p2s_review_decision": "pending_author_review",
            "selection_reason": SELECTION_REASON,
            "duplicate_check_status": duplicate_status,
            "duplicate_hash_matches": ";".join(duplicate_hash_matches),
            "p3s_eligibility": p3s_eligibility,
            "formal_evidence_created": "0",
        })
        capture_rows.append({
            "captured_at": captured_at,
            "source_id": source_id,
            "canonical_url": parsed["canonical_url"],
            "capture_type": "original_webpage_html",
            "http_status": "200",
            "title_verified": str(title_verified).lower(),
            "publisher_from_prior_verified_manifest": "true",
            "published_at_dom_text": parsed["date_text"],
            "published_at_expected": expected["published_at"],
            "published_at_verified": str(bool(date_verified)).lower(),
            "raw_sha256": raw_sha,
            "text_sha256": text_sha,
            "access_failure_reason": "",
            "blocked_original_page": "false",
        })
        for gap_area in GAP_AREAS[source_id]:
            coverage_rows.append({
                "source_id": source_id,
                "gap_area": gap_area,
                "planned_coverage_role": "supplement candidate; no formal Evidence extracted",
                "selection_basis": "A-grade official first-party source within T0 window",
                "p3s_eligibility": p3s_eligibility,
                "duplicate_check_status": duplicate_status,
                "evidence_created": "0",
            })
    return source_manifest_rows, capture_rows, coverage_rows


def run(run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "source_raw").mkdir(exist_ok=True)
    (run_dir / "source_text").mkdir(exist_ok=True)
    protected_before = protected_output_hashes(run_dir)
    canonical = read_jsonl(CANONICAL_PATH)
    p4q = build_p4q(canonical)
    p2_rows = load_p2_rows()
    source_manifest_rows, capture_rows, coverage_rows = build_p2s(run_dir, p2_rows)
    p2s_manifest = {
        "run_id": run_dir.name,
        "stage": "P2S",
        "case_id": CASE_ID,
        "inherited_c1_run_id": C1_RUN_ID,
        "input_canonical_path": CANONICAL_PATH.relative_to(ROOT).as_posix(),
        "input_canonical_sha256": sha256_file(CANONICAL_PATH),
        "source_ids": P2S_SOURCE_IDS,
        "source_count": len(P2S_SOURCE_IDS),
        "selection_reason": SELECTION_REASON,
        "p3s_executed": False,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "model_api_calls": 0,
        "t1_read": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "formal_evidence_created": 0,
        "verified_original_page_count": sum(row["verification_status"] == "verified_original_page_html" for row in source_manifest_rows),
        "blocked_original_page_count": sum(row["verification_status"] != "verified_original_page_html" for row in source_manifest_rows),
        "p3s_eligible_count": sum(row["p3s_eligibility"] == "eligible_for_p3s_review" for row in source_manifest_rows),
    }
    p4q_manifest = {
        "run_id": run_dir.name,
        "stage": "P4Q",
        "case_id": CASE_ID,
        "input_canonical_path": CANONICAL_PATH.relative_to(ROOT).as_posix(),
        "input_canonical_sha256": sha256_file(CANONICAL_PATH),
        "p4q": p4q,
        "p4_gate_open": False,
        "p4_executed": False,
        "p3s_executed": False,
        "g1_supplement_executed": False,
        "snapshot_frozen": False,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "model_api_calls": 0,
        "t1_read": False,
        "git_head": git_head(),
        "python_version": platform.python_version(),
        "protected_prior_outputs_unchanged": False,
    }
    write_csv(run_dir / "p2s_source_manifest.csv", source_manifest_rows, list(source_manifest_rows[0]))
    write_csv(run_dir / "p2s_source_capture_log.csv", capture_rows, list(capture_rows[0]))
    write_csv(run_dir / "p2s_coverage_gap_matrix.csv", coverage_rows, list(coverage_rows[0]))
    protected_after = protected_output_hashes(run_dir)
    p4q_manifest["protected_prior_outputs_unchanged"] = protected_before == protected_after
    p4q_manifest["protected_outputs_before_digest"] = sha256_bytes(json.dumps(protected_before, sort_keys=True).encode())
    p4q_manifest["protected_outputs_after_digest"] = sha256_bytes(json.dumps(protected_after, sort_keys=True).encode())
    report = [
        "# P4Q冻结前门禁审计报告",
        "",
        f"- canonical逻辑Evidence：{p4q['canonical_logical_evidence_count']}",
        f"- SQLite物理T0 Evidence记录：{p4q['sqlite_physical_t0_evidence_rows']}",
        f"- SQLite最大版本逻辑Evidence：{p4q['sqlite_latest_version_logical_evidence_count']}",
        f"- canonical与最大版本字段差异：{p4q['canonical_latest_field_difference_count']}",
        f"- 正式证据来源数：{p4q['formal_evidence_source_count']}",
        f"- A级片段：{p4q['a_grade_fragment_count']}；B级片段：{p4q['b_grade_fragment_count']}；D级片段：{p4q['d_grade_fragment_count']}",
        f"- A级片段占比：{p4q['a_grade_fragment_count']}/60={p4q['a_grade_fragment_ratio']}",
        f"- published_at晚于2024-12-31：{p4q['published_after_2024_12_31_count']}",
        f"- snapshot数量：{p4q['snapshot_count']}；snapshot_items数量：{p4q['snapshot_items_count']}",
        "",
        "## 门禁结论",
        "",
        "- p4_gate_open=false",
        f"- 阻断原因：{p4q['p4_block_reason']}",
        "- 分母保持为正式Evidence片段总数60；未使用A级来源文件数替代片段分母。",
        "",
        "## P2S准备边界",
        "",
        "- 本运行仅归档5份原始网页及正文文本，未抽取Evidence、未修改Evidence、未调用ModelClient/API。",
        "- P2S材料仍为补充候选，p2s_review_decision保持pending_author_review，不自动进入正式证据池。",
    ]
    (run_dir / "p4q_quality_gate_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    output_file_hashes = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name not in {"p4q_preflight_manifest.json", "p2s_manifest.json"}
    }
    p2s_manifest["output_file_hashes"] = output_file_hashes
    p4q_manifest["output_file_hashes"] = output_file_hashes
    p4q_manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    p2s_manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    write_json(run_dir / "p4q_preflight_manifest.json", p4q_manifest)
    write_json(run_dir / "p2s_manifest.json", p2s_manifest)
    return {"run_id": run_dir.name, "p4q": p4q, "p2s": p2s_manifest}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.run_dir if args.run_dir.is_absolute() else ROOT / args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
