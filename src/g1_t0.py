"""Formal G1-T0 author sign-off and Evidence insertion.

The runner is deliberately narrower than later pipeline stages.  It reads the
immutable P3BR2A/P3BR source artifacts, applies the author sign-off supplied in
the request, validates all final objects, and commits the audit rows plus
formal T0 Evidence in one SQLite transaction.  It never calls a model, reads
T1, creates a snapshot, writes hypotheses, or executes P4.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import (
    EVIDENCE_NATURE_VALUES,
    CODING_DIMENSION_VALUES,
    Evidence,
    EvidenceStatus,
    SnapshotTag,
    model_dump,
    model_validate,
    with_evidence_hash,
)


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
P3BR2A_RUN_ID = "P3BR2A-20260826T063553Z-content-completeness-v1"
P3BR2A_DIR = ROOT / "outputs" / P3BR2A_RUN_ID
SOURCE_RUN_ID = "P3BR-20260826T064000Z-recovery-v3"
SOURCE_DIR = ROOT / "outputs" / SOURCE_RUN_ID
DB_PATH = ROOT / "data" / "research.sqlite3"

REVIEWER_ID = "author_01"
REVIEW_MODE = "ai_preannotation_author_batch_signoff_with_focus_adjudication"
LOW_AUTO_REVIEW_MODE = "protocol_low_risk_auto_accept_not_human_reviewed"

AUTHORIZATION_TEXT = (
    "我已审阅P3b-R2a作者签核摘要，同意其余50项建议，并同意上述5项修订；"
    "授权以‘AI预标注＋作者批量签核、重点项逐条裁决’的方式执行正式G1-T0。"
)

FOCUS_OVERRIDES: dict[str, dict[str, Any]] = {
    "EV-T0-CA999183D4D473C4": {
        "final_decision": "revise",
        "final_normalized_claim": "2019年，JAIC开始开发联合通用基金会。",
        "final_nature": "resource_input",
        "final_dimensions": ["engineering_resource_conditions", "integration_level"],
        "review_note": "重点裁决：仅保留已经发生的开发启动事实，不重复写入数据共享平台目标。",
    },
    "EV-T0-794C24A620FE0968": {
        "final_decision": "split",
        "final_nature": "split",
        "final_dimensions": ["application_maturity"],
        "final_normalized_claim": "父项按两个子事实拆分：开发周期启动与预计展示。",
        "review_note": "重点裁决：父项保留split，由两个子Evidence替代。",
    },
    "EV-T0-79B653E22A3D4EF4": {
        "final_decision": "revise",
        "final_normalized_claim": "据2024年10月9日报道，约在2022年10月，AIDP曾在美国本土情报部队中接受压力测试和微调。",
        "final_nature": "test_event",
        "final_dimensions": ["application_maturity"],
        "review_note": "重点裁决：修改为test_event，仅记录压力测试和微调活动，不推断测试结果。",
    },
    "EV-T0-F0773C8E2ECB5C8D": {
        "final_decision": "revise",
        "final_normalized_claim": "2023年1月9日，IMSC在阿拉伯湾完成为期三天的海上演习，整合了无人系统和人工智能，这是六个月内第二次此类演习。",
        "final_nature": "test_event",
        "final_dimensions": ["integration_level", "application_maturity"],
        "review_note": "重点裁决：演习发生标记为test_event，不将演习自动解释为作战效能结果。",
    },
    "EV-T0-00FCD33BA877C8BA": {
        "final_decision": "revise",
        "final_nature": "governance_event",
        "final_dimensions": ["application_maturity", "organizational_system_fit"],
        "final_normalized_claim": None,
        "review_note": "重点裁决：保留P3BR2A关于AI与数据加速计划的治理事件建议，并补充组织系统适配维度。",
    },
}

SPLIT_REASON = "作者重点裁决要求将一个包含两个可核查事实的父项拆分为两个独立Evidence。"
FINAL_FIELDS = (
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


class G1T0Error(RuntimeError):
    """Raised before a G1-T0 transaction is committed."""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fields})


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {_relative(path): _sha256_file(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def _protected_hashes(exclude_dir: Path | None = None) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve() if exclude_dir else None
    return {
        _relative(path): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and not (excluded and (path.resolve() == excluded or excluded in path.resolve().parents))
    }


def _digest(values: dict[str, str]) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable_not_a_git_repository"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _source_records() -> dict[str, dict[str, Any]]:
    combined = _read_jsonl(SOURCE_DIR / "evidence_candidates_p3br_combined.jsonl")
    recovered = _read_jsonl(SOURCE_DIR / "recovered_candidates.jsonl")
    visual = _read_csv(SOURCE_DIR / "g1_visual_rescue_queue.csv")
    records = {row["evidence_id"]: row for row in combined}
    records.update({row["evidence_id"]: row for row in recovered})
    by_source = {row["source_id"]: row for row in [*combined, *recovered]}
    for row in visual:
        metadata = dict(by_source.get(row["source_id"], {}))
        metadata.update(row)
        metadata["coding_dimensions"] = _as_list(row.get("coding_dimensions"))
        records[row["evidence_id"]] = metadata
    return records


def _finalize_author_row(row: dict[str, str], source: dict[str, Any], reviewed_at: str) -> dict[str, Any]:
    result: dict[str, Any] = dict(row)
    result["proposed_dimensions"] = _as_list(row.get("proposed_dimensions"))
    result["source_locator"] = row.get("source_locator", "")
    result["proposed_source_locator"] = row.get("proposed_source_locator") or result["source_locator"]
    result["excerpt_original"] = row.get("excerpt_original") or source.get("excerpt_original", "")
    result["proposed_normalized_claim"] = row.get("proposed_normalized_claim") or source.get("normalized_claim", "")
    result["proposed_excerpt_zh"] = row.get("proposed_excerpt_zh") or source.get("excerpt_zh", "")
    result["proposed_nature"] = row.get("proposed_nature") or source.get("evidence_nature", "")
    result["final_decision"] = row.get("proposed_decision", "")
    result["final_nature"] = result["proposed_nature"]
    result["final_dimensions"] = list(result["proposed_dimensions"])
    result["final_normalized_claim"] = result["proposed_normalized_claim"]
    result["human_corrected_excerpt"] = ""
    result["final_excerpt_zh"] = result["proposed_excerpt_zh"]
    result["reviewer"] = REVIEWER_ID
    result["reviewed_at"] = reviewed_at
    result["review_mode"] = REVIEW_MODE
    result["human_reviewed"] = True
    result["focus_adjudication"] = row["evidence_id"] in FOCUS_OVERRIDES
    result["ai_assisted_visual_pre_review"] = row.get("ai_assisted", "false").lower() == "true"
    result["review_note"] = "作者授权批量签核；重点项逐条裁决。"
    override = FOCUS_OVERRIDES.get(row["evidence_id"])
    if override:
        result.update({key: value for key, value in override.items() if value is not None})
        if override.get("final_normalized_claim") is None:
            result["final_normalized_claim"] = result["proposed_normalized_claim"]
        result["review_note"] = override["review_note"]
    if result["final_decision"] == "split":
        result["final_nature"] = "split"
    return result


def _low_auto_rows(author_ids: set[str], reviewed_at: str, sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pre_rows = _read_csv(P3BR2A_DIR.parent / "P3BR2-20260826T050236Z-gate-correction-v1" / "g1_ai_preannotation_p3br2.csv")
    low_rows = [row for row in pre_rows if row.get("risk_level") == "low" and row.get("evidence_id") not in author_ids]
    rows: list[dict[str, Any]] = []
    for row in low_rows:
        source = sources[row["evidence_id"]]
        result = {
            "evidence_id": row["evidence_id"],
            "source_id": row.get("source_id", source.get("source_id", "")),
            "source_locator": row.get("source_locator", source.get("source_locator", "")),
            "proposed_source_locator": row.get("proposed_source_locator") or row.get("source_locator", ""),
            "excerpt_original": source.get("excerpt_original", ""),
            "proposed_excerpt_zh": row.get("proposed_excerpt_zh") or source.get("excerpt_zh", ""),
            "proposed_normalized_claim": row.get("proposed_normalized_claim") or source.get("normalized_claim", ""),
            "proposed_nature": row.get("proposed_nature") or source.get("evidence_nature", ""),
            "proposed_dimensions": _as_list(row.get("proposed_dimensions")) or source.get("coding_dimensions", []),
            "proposed_decision": "accept",
            "final_decision": "accept",
            "final_nature": row.get("proposed_nature") or source.get("evidence_nature", ""),
            "final_dimensions": _as_list(row.get("proposed_dimensions")) or source.get("coding_dimensions", []),
            "final_normalized_claim": row.get("proposed_normalized_claim") or source.get("normalized_claim", ""),
            "human_corrected_excerpt": "",
            "final_excerpt_zh": row.get("proposed_excerpt_zh") or source.get("excerpt_zh", ""),
            "risk_level": "low",
            "human_reviewed": False,
            "reviewer": "",
            "reviewed_at": "",
            "review_mode": LOW_AUTO_REVIEW_MODE,
            "focus_adjudication": False,
            "ai_assisted_visual_pre_review": False,
            "review_note": "按协议自动接受；不是人工审核。",
        }
        rows.append(result)
    return rows


def _validate_source_fields(row: dict[str, Any]) -> None:
    required = ("source_id", "source_locator", "excerpt_original", "proposed_excerpt_zh", "proposed_normalized_claim", "proposed_nature", "proposed_dimensions")
    if any(not row.get(field) for field in required):
        raise G1T0Error(f"incomplete source field for {row.get('evidence_id')}: {required}")
    dimensions = row["final_dimensions"] if "final_dimensions" in row else row["proposed_dimensions"]
    if not dimensions or any(value not in CODING_DIMENSION_VALUES for value in dimensions):
        raise G1T0Error(f"invalid dimensions for {row.get('evidence_id')}: {dimensions}")


def _metadata_for(source: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    for field in ("title", "publisher", "source_grade", "published_at", "published_at_precision", "source_type", "url_or_path"):
        if not source.get(field):
            raise G1T0Error(f"missing metadata {field} for {evidence_id}")
    return source


def _date_value(value: str) -> date:
    return date.fromisoformat(value[:10])


def _make_evidence(
    row: dict[str, Any],
    source: dict[str, Any],
    run_id: str,
    *,
    evidence_id: str | None = None,
    parent_evidence_id: str | None = None,
    proposal_child_id: str | None = None,
    part_index: int | None = None,
) -> Evidence:
    _validate_source_fields(row)
    metadata = _metadata_for(source, row["evidence_id"])
    final_locator = row.get("proposed_source_locator") or row.get("source_locator")
    nature = row.get("final_nature") or row.get("proposed_nature")
    if nature not in EVIDENCE_NATURE_VALUES or "+" in nature:
        raise G1T0Error(f"formal Evidence nature must be a single legal value: {nature}")
    dimensions = row.get("final_dimensions") or row.get("proposed_dimensions")
    data = Evidence(
        case_id=CASE_ID,
        evidence_id=evidence_id or row["evidence_id"],
        version=1,
        snapshot_tag=SnapshotTag.T0,
        title=metadata["title"],
        source_type=metadata["source_type"],
        source_grade=metadata["source_grade"],
        publisher=metadata["publisher"],
        published_at=_date_value(metadata["published_at"]),
        published_at_precision=metadata.get("published_at_precision", "day"),
        url_or_path=metadata["url_or_path"],
        page_or_section=final_locator,
        source_id=row.get("source_id") or metadata.get("source_id", ""),
        source_locator=final_locator,
        excerpt=row["excerpt_original"],
        excerpt_original=row["excerpt_original"],
        excerpt_zh=row["final_excerpt_zh"] or row["proposed_excerpt_zh"],
        normalized_claim=row["final_normalized_claim"] or row["proposed_normalized_claim"],
        evidence_nature=nature,
        coding_dimensions=list(dimensions),
        topics=metadata.get("topics", []),
        status=EvidenceStatus.ACTIVE,
        reviewed_by=row.get("reviewer") or "protocol_low_risk_auto_accept",
        run_id=run_id,
    )
    data = with_evidence_hash(data)
    # Parent/child lineage is stored in g1_t0_split_lineage.  It is not added
    # as an extra Pydantic field to Evidence, preserving the strict schema.
    return data


def _child_row(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    row = dict(child)
    row["evidence_id"] = parent["evidence_id"]
    row["source_id"] = child["source_id"]
    row["source_locator"] = child["source_locator"]
    row["proposed_source_locator"] = child["source_locator"]
    row["final_decision"] = "accept"
    row["final_nature"] = child["proposed_nature"]
    row["final_dimensions"] = list(child["proposed_dimensions"])
    row["final_normalized_claim"] = child["proposed_normalized_claim"]
    row["final_excerpt_zh"] = child["proposed_excerpt_zh"]
    row["human_reviewed"] = True
    row["reviewer"] = REVIEWER_ID
    row["reviewed_at"] = parent.get("reviewed_at", "")
    return row


def _ensure_audit_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS g1_t0_audit (
            authorization_sha256 TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            author_signoff_count INTEGER NOT NULL,
            batch_confirmed_count INTEGER NOT NULL,
            focus_adjudication_count INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS g1_t0_author_decision (
            run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            final_decision TEXT NOT NULL,
            final_nature TEXT NOT NULL,
            final_dimensions_json TEXT NOT NULL,
            final_normalized_claim TEXT NOT NULL,
            final_excerpt_zh TEXT NOT NULL,
            excerpt_original TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            human_reviewed INTEGER NOT NULL CHECK(human_reviewed IN (0, 1)),
            reviewer TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS g1_t0_low_risk_auto_accept (
            run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS g1_t0_exclusion (
            run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS g1_t0_split_lineage (
            run_id TEXT NOT NULL,
            parent_evidence_id TEXT NOT NULL,
            proposal_child_id TEXT NOT NULL,
            formal_child_evidence_id TEXT NOT NULL UNIQUE,
            part_index INTEGER NOT NULL,
            case_id TEXT NOT NULL,
            split_reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, parent_evidence_id, part_index)
        );
        """
    )


def _ensure_legacy_evidence_columns(conn: sqlite3.Connection) -> None:
    """Add only missing append-only payload columns without reading T1 rows.

    The checked-in database predates the Phase A+ column additions.  SQLite
    permits these DDL statements inside the same transaction as the G1 write;
    no existing evidence payload is selected or rewritten here.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)").fetchall()}
    additions = {
        "case_id": "TEXT NOT NULL DEFAULT '__legacy__'",
        "published_at_precision": "TEXT NOT NULL DEFAULT 'day'",
        "evidence_nature": "TEXT NOT NULL DEFAULT 'legacy_unclassified'",
        "excerpt_original": "TEXT NOT NULL DEFAULT ''",
        "excerpt_zh": "TEXT NOT NULL DEFAULT ''",
        "coding_dimensions_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE evidence ADD COLUMN {name} {definition}")


def _insert_formal_evidence(conn: sqlite3.Connection, evidence: Evidence) -> None:
    payload = model_dump(evidence)
    conn.execute(
        "INSERT INTO evidence(case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence.case_id,
            evidence.evidence_id,
            evidence.version,
            evidence.snapshot_tag.value,
            evidence.status.value,
            evidence.published_at.isoformat(),
            evidence.published_at_precision,
            evidence.content_hash,
            evidence.evidence_nature,
            evidence.excerpt_original,
            evidence.excerpt_zh,
            json.dumps(evidence.coding_dimensions, ensure_ascii=False, separators=(",", ":")),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def _insert_pipeline_audit(conn: sqlite3.Connection, run_id: str, executed_at: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO pipeline_run(run_id, case_id, phase, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
        (run_id, CASE_ID, "G1-T0", executed_at, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
    conn.execute(
        "INSERT INTO pipeline_stage(stage_id, run_id, case_id, phase, stage_name, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"STAGE-{run_id}",
            run_id,
            CASE_ID,
            "G1-T0",
            "formal_g1_t0_commit",
            executed_at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def _transaction_write(
    database_path: Path,
    run_id: str,
    authorization_sha256: str,
    author_rows: list[dict[str, Any]],
    low_rows: list[dict[str, Any]],
    evidence_objects: list[Evidence],
    split_lineage: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    executed_at: str,
    *,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_legacy_evidence_columns(conn)
        _ensure_audit_tables(conn)
        existing = conn.execute(
            "SELECT run_id FROM g1_t0_audit WHERE authorization_sha256=?",
            (authorization_sha256,),
        ).fetchone()
        if existing:
            conn.rollback()
            return {"replayed": True, "existing_run_id": existing[0], "evidence_rows_added": 0}

        ids = [item.evidence_id for item in evidence_objects]
        placeholders = ",".join("?" for _ in ids)
        existing_evidence = conn.execute(
            f"SELECT evidence_id, version FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})",
            [CASE_ID, *ids],
        ).fetchall()
        if existing_evidence:
            raise G1T0Error(f"target Evidence IDs already exist: {existing_evidence[:5]}")

        audit_payload = {
            "authorization_sha256": authorization_sha256,
            "run_id": run_id,
            "case_id": CASE_ID,
            "reviewer_id": REVIEWER_ID,
            "review_mode": REVIEW_MODE,
            "author_signoff_count": len(author_rows),
            "batch_confirmed_count": 50,
            "focus_adjudication_count": 5,
            "human_source_visual_review_count": 0,
            "ai_assisted_visual_pre_review_count": 7,
            "human_reviewed_means_author_confirmation_not_full_source_read": True,
            "formal_evidence_db_rows_added": len(evidence_objects),
            "hypothesis_rows_added": 0,
            "t1_read": False,
            "p4_executed": False,
            "snapshot_frozen": False,
        }
        _insert_pipeline_audit(conn, run_id, executed_at, audit_payload)

        for index, row in enumerate(author_rows):
            conn.execute(
                "INSERT INTO g1_t0_author_decision(run_id, evidence_id, case_id, final_decision, final_nature, final_dimensions_json, final_normalized_claim, final_excerpt_zh, excerpt_original, source_id, source_locator, human_reviewed, reviewer, reviewed_at, review_mode, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    row["evidence_id"],
                    CASE_ID,
                    row["final_decision"],
                    row["final_nature"],
                    json.dumps(row["final_dimensions"], ensure_ascii=False, separators=(",", ":")),
                    row["final_normalized_claim"],
                    row["final_excerpt_zh"],
                    row["excerpt_original"],
                    row["source_id"],
                    row["source_locator"],
                    1,
                    row["reviewer"],
                    row["reviewed_at"],
                    row["review_mode"],
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO human_review(review_id, gate, case_id, approved, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    f"G1-T0-{row['evidence_id']}",
                    "G1-T0",
                    CASE_ID,
                    int(row["final_decision"] != "exclude"),
                    json.dumps(
                        {
                            "review_id": f"G1-T0-{row['evidence_id']}",
                            "gate": "G1-T0",
                            "case_id": CASE_ID,
                            "approved": row["final_decision"] != "exclude",
                            "reviewer": REVIEWER_ID,
                            "reviewed_at": row["reviewed_at"],
                            "artifact_path": str(P3BR2A_DIR / "g1_author_signoff_summary.md"),
                            "artifact_hash": _sha256_file(P3BR2A_DIR / "g1_author_signoff_summary.md"),
                            "notes": row["review_note"],
                            "review_mode": REVIEW_MODE,
                            "human_reviewed_semantics": "author_confirmation_of_signoff_summary_and_final_decision",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise G1T0Error("injected transaction failure for rollback test")

        for row in low_rows:
            conn.execute(
                "INSERT INTO g1_t0_low_risk_auto_accept(run_id, evidence_id, case_id, review_mode, payload_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, row["evidence_id"], CASE_ID, LOW_AUTO_REVIEW_MODE, json.dumps(row, ensure_ascii=False, sort_keys=True)),
            )

        for row in exclusions:
            conn.execute(
                "INSERT INTO g1_t0_exclusion(run_id, evidence_id, case_id, reason, payload_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, row["evidence_id"], CASE_ID, row["review_note"], json.dumps(row, ensure_ascii=False, sort_keys=True)),
            )

        for row in split_lineage:
            conn.execute(
                "INSERT INTO g1_t0_split_lineage(run_id, parent_evidence_id, proposal_child_id, formal_child_evidence_id, part_index, case_id, split_reason, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    row["parent_evidence_id"],
                    row["proposal_child_id"],
                    row["formal_child_evidence_id"],
                    row["part_index"],
                    CASE_ID,
                    SPLIT_REASON,
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                ),
            )

        for evidence in evidence_objects:
            _insert_formal_evidence(conn, evidence)

        actual = conn.execute(
            f"SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})",
            [CASE_ID, *ids],
        ).fetchone()[0]
        if actual != len(evidence_objects):
            raise G1T0Error(f"transaction evidence count mismatch: {actual} != {len(evidence_objects)}")
        if conn.execute("SELECT COUNT(*) FROM g1_t0_author_decision WHERE run_id=?", (run_id,)).fetchone()[0] != 55:
            raise G1T0Error("author decision audit count mismatch")
        if conn.execute("SELECT COUNT(*) FROM g1_t0_split_lineage WHERE run_id=?", (run_id,)).fetchone()[0] != 6:
            raise G1T0Error("split lineage count mismatch")

        conn.execute(
            "INSERT INTO g1_t0_audit(authorization_sha256, run_id, case_id, reviewer_id, review_mode, author_signoff_count, batch_confirmed_count, focus_adjudication_count, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                authorization_sha256,
                run_id,
                CASE_ID,
                REVIEWER_ID,
                REVIEW_MODE,
                55,
                50,
                5,
                json.dumps(audit_payload, ensure_ascii=False, sort_keys=True),
                executed_at,
            ),
        )
        conn.commit()
        return {"replayed": False, "evidence_rows_added": len(evidence_objects), "author_rows_added": 55}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _formal_evidence_payloads(
    author_rows: list[dict[str, Any]],
    low_rows: list[dict[str, Any]],
    split_children: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    run_id: str,
) -> tuple[list[Evidence], list[dict[str, Any]]]:
    evidence: list[Evidence] = []
    lineage: list[dict[str, Any]] = []
    for row in author_rows:
        if row["final_decision"] in {"exclude", "split"}:
            continue
        evidence.append(_make_evidence(row, sources[row["evidence_id"]], run_id))
    for row in low_rows:
        evidence.append(_make_evidence(row, sources[row["evidence_id"]], run_id))
    child_parent_map = {row["evidence_id"]: row for row in author_rows}
    for child in split_children:
        parent = child_parent_map[child["parent_evidence_id"]]
        child_id = "EV-T0-" + hashlib.sha256(
            f"{child['proposal_child_id']}|{child['parent_evidence_id']}|{child['part_index']}".encode("utf-8")
        ).hexdigest()[:16].upper()
        child_row = _child_row(child, parent)
        evidence.append(
            _make_evidence(
                child_row,
                sources[child["parent_evidence_id"]],
                run_id,
                evidence_id=child_id,
                parent_evidence_id=child["parent_evidence_id"],
                proposal_child_id=child["proposal_child_id"],
                part_index=child["part_index"],
            )
        )
        lineage.append(
            {
                "parent_evidence_id": child["parent_evidence_id"],
                "proposal_child_id": child["proposal_child_id"],
                "formal_child_evidence_id": child_id,
                "part_index": child["part_index"],
                "source_id": child["source_id"],
                "source_locator": child["source_locator"],
                "split_reason": SPLIT_REASON,
            }
        )
    if len(evidence) != 60:
        raise G1T0Error(f"formal Evidence expected 60, got {len(evidence)}")
    if len({item.evidence_id for item in evidence}) != 60:
        raise G1T0Error("formal Evidence IDs are not unique")
    return evidence, lineage


def _output_files(
    output_dir: Path,
    authorization: dict[str, Any],
    author_rows: list[dict[str, Any]],
    low_rows: list[dict[str, Any]],
    evidence: list[Evidence],
    lineage: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    input_hashes: dict[str, str],
    protected_before: dict[str, str],
    protected_after: dict[str, str],
    run_id: str,
    transaction_result: dict[str, Any],
    executed_at: str,
) -> dict[str, Any]:
    authorization_path = output_dir / "g1_t0_authorization.json"
    decisions_path = output_dir / "g1_t0_author_decisions.csv"
    low_path = output_dir / "g1_t0_low_risk_auto_accept.csv"
    evidence_path = output_dir / "g1_t0_final_evidence.jsonl"
    lineage_path = output_dir / "g1_t0_split_lineage.jsonl"
    exclusions_path = output_dir / "g1_t0_exclusions.csv"
    quality_path = output_dir / "g1_t0_quality_report.md"

    _write_json(authorization_path, authorization)
    decision_fields = [
        "evidence_id", "source_id", "source_locator", "proposed_source_locator", "excerpt_original",
        "proposed_excerpt_zh", "proposed_normalized_claim", "proposed_nature", "proposed_dimensions",
        "proposed_decision", "final_decision", "final_nature", "final_dimensions", "final_normalized_claim",
        "human_corrected_excerpt", "final_excerpt_zh", "human_reviewed", "reviewer", "reviewed_at",
        "review_mode", "focus_adjudication", "ai_assisted_visual_pre_review", "review_note", "risk_level",
    ]
    _write_csv(decisions_path, author_rows, decision_fields)
    _write_csv(low_path, low_rows, decision_fields)
    _write_jsonl(evidence_path, [model_dump(item) for item in evidence])
    _write_jsonl(lineage_path, lineage)
    _write_csv(exclusions_path, exclusions, ["evidence_id", "source_id", "source_locator", "excerpt_original", "proposed_normalized_claim", "proposed_nature", "proposed_dimensions", "final_decision", "review_note", "human_reviewed", "reviewer", "reviewed_at"])

    decision_counts = Counter(row["final_decision"] for row in author_rows)
    nature_counts = Counter(item.evidence_nature for item in evidence)
    quality = [
        "# G1-T0质量报告",
        "",
        "本报告记录正式G1-T0签核与Evidence写入，不代表作者逐条阅读了全部原始材料，也不代表作者逐页进行了视觉核验。",
        "",
        f"- author_signoff_count：{len(author_rows)}",
        f"- batch_confirmed_count：50",
        f"- focus_adjudication_count：5",
        f"- human_source_visual_review_count：0",
        f"- ai_assisted_visual_pre_review_count：7",
        f"- 决策分布：accept={decision_counts['accept']}，revise={decision_counts['revise']}，split={decision_counts['split']}，exclude={decision_counts['exclude']}",
        f"- 低风险协议自动接受：{len(low_rows)}，human_reviewed=false",
        f"- 正式Evidence写入：{len(evidence)}",
        f"- Evidence性质分布：{dict(nature_counts)}",
        f"- split父项：3；split子谱系：{len(lineage)}",
        "- Evidence正式数据库新增：60",
        "- hypothesis新增：0；T1读取：false；P4执行：false；snapshot_frozen：false；model/API调用：0",
        "- 所有正式Evidence均为T0、version=1、status=active，并保留case_id、source_id、source_locator、原文摘录、中文摘录、规范化声明、性质、维度、发布日期精度和content_hash。",
    ]
    quality_path.write_text("\n".join(quality) + "\n", encoding="utf-8")

    output_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "G1-T0",
        "executed_at_utc": executed_at,
        "authorization_sha256": authorization["authorization_sha256"],
        "input_run_id": P3BR2A_RUN_ID,
        "input_p3br2a_hashes": input_hashes,
        "protected_prior_outputs_unchanged": protected_before == protected_after,
        "protected_outputs_before_digest": _digest(protected_before),
        "protected_outputs_after_digest": _digest(protected_after),
        "git_head": _git_head(),
        "python_version": platform.python_version(),
        "implementation_file_hashes": {
            "src/g1_t0.py": _sha256_file(ROOT / "src/g1_t0.py"),
            "scripts/16_run_g1_t0.py": _sha256_file(ROOT / "scripts/16_run_g1_t0.py"),
            "tests/test_g1_t0.py": _sha256_file(ROOT / "tests/test_g1_t0.py"),
        },
        "author_signoff_count": len(author_rows),
        "batch_confirmed_count": 50,
        "focus_adjudication_count": 5,
        "human_source_visual_review_count": 0,
        "ai_assisted_visual_pre_review_count": 7,
        "decision_counts": dict(decision_counts),
        "low_risk_auto_accept_count": len(low_rows),
        "formal_evidence_count": len(evidence),
        "formal_evidence_db_rows_added": transaction_result.get("evidence_rows_added", 0),
        "hypothesis_rows_added": 0,
        "split_parent_count": 3,
        "split_lineage_count": len(lineage),
        "formal_evidence_ids_unique": len({item.evidence_id for item in evidence}) == len(evidence),
        "formal_evidence_has_no_composite_nature": all("+" not in item.evidence_nature for item in evidence),
        "excluded_evidence_ids": [row["evidence_id"] for row in exclusions],
        "t1_read": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "model_api_call_count": 0,
        "api_key_written": False,
        "author_final_fields_signed": True,
        "author_review_semantics": "author_confirmation_of_signoff_summary_and_final_decision_not_full_source_read",
        "replay_protection": "authorization_sha256_unique_no_duplicate_evidence_insert",
        "transaction_result": transaction_result,
        "output_file_hashes": output_hashes,
    }
    manifest_path = output_dir / "g1_t0_manifest.json"
    _write_json(manifest_path, manifest)
    manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    _write_json(manifest_path, manifest)
    return manifest


def run_g1_t0(
    *,
    database_path: Path = DB_PATH,
    output_dir: Path | None = None,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    executed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    authorization_sha256 = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()
    run_id = f"G1-T0-{authorization_sha256[:16].upper()}-formal-v1"
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    protected_before = _protected_hashes(output_dir)
    input_hashes = _tree_hashes(P3BR2A_DIR)
    sources = _source_records()
    input_rows = _read_csv(P3BR2A_DIR / "g1_author_decision_sheet_p3br2a.csv")
    author_ids = {row["evidence_id"] for row in input_rows}
    author_rows = [_finalize_author_row(row, sources[row["evidence_id"]], executed_at) for row in input_rows]
    low_rows = _low_auto_rows(author_ids, executed_at, sources)
    split_children = _read_jsonl(P3BR2A_DIR / "split_child_proposals_p3br2a.jsonl")
    evidence, lineage = _formal_evidence_payloads(author_rows, low_rows, split_children, sources, run_id)
    exclusions = [row for row in author_rows if row["final_decision"] == "exclude"]
    if {row["evidence_id"] for row in exclusions} != {"EV-T0-246C331615E11FB3", "EV-T0-1C84CB9C7F271D13"}:
        raise G1T0Error("exclude set does not match the two authorized exclusions")
    if Counter(row["final_decision"] for row in author_rows) != Counter({"accept": 13, "revise": 37, "split": 3, "exclude": 2}):
        raise G1T0Error(f"final decision distribution mismatch: {Counter(row['final_decision'] for row in author_rows)}")
    if len(low_rows) != 4:
        raise G1T0Error(f"low-risk auto-accept count mismatch: {len(low_rows)}")
    transaction_result = _transaction_write(
        database_path,
        run_id,
        authorization_sha256,
        author_rows,
        low_rows,
        evidence,
        lineage,
        exclusions,
        executed_at,
        inject_failure_after=inject_failure_after,
    )
    if transaction_result.get("replayed"):
        existing_dir = ROOT / "outputs" / transaction_result["existing_run_id"]
        existing_manifest = existing_dir / "g1_t0_manifest.json"
        if existing_manifest.exists():
            existing_data = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if existing_data.get("protected_prior_outputs_unchanged") is True:
                return existing_data | {"replayed": True}
        # A prior process may have committed the SQLite transaction and then
        # failed while materializing filesystem artifacts.  Reconstruct those
        # artifacts from the same immutable inputs without issuing any second
        # database write.  The audit row remains the idempotency authority.
        # The initially requested output_dir can be a new version while the
        # existing authorization points at an older directory.  Hash the
        # protected tree excluding the actual existing directory in both
        # directions, so reconstruction itself is not reported as a prior
        # artifact mutation.
        protected_before = _protected_hashes(existing_dir)
        protected_after = _protected_hashes(existing_dir)
        authorization = {
            "authorization_text": AUTHORIZATION_TEXT,
            "authorization_sha256": authorization_sha256,
            "reviewer_id": REVIEWER_ID,
            "review_mode": REVIEW_MODE,
            "author_signoff_count": 55,
            "batch_confirmed_count": 50,
            "focus_adjudication_count": 5,
            "human_source_visual_review_count": 0,
            "ai_assisted_visual_pre_review_count": 7,
            "signed_at_utc": executed_at,
            "human_reviewed_semantics": "作者确认签核摘要和最终决定；不表示逐条阅读全部原始材料或逐页视觉核验。",
        }
        reconstructed = dict(transaction_result)
        reconstructed["evidence_rows_added"] = len(evidence)
        reconstructed["reconstructed_after_committed_transaction"] = True
        return _output_files(
            existing_dir,
            authorization,
            author_rows,
            low_rows,
            evidence,
            lineage,
            exclusions,
            input_hashes,
            protected_before,
            protected_after,
            transaction_result["existing_run_id"],
            reconstructed,
            executed_at,
        ) | {"replayed": True, "reconstructed_after_committed_transaction": True}
    protected_after = _protected_hashes(output_dir)
    authorization = {
        "authorization_text": AUTHORIZATION_TEXT,
        "authorization_sha256": authorization_sha256,
        "reviewer_id": REVIEWER_ID,
        "review_mode": REVIEW_MODE,
        "author_signoff_count": 55,
        "batch_confirmed_count": 50,
        "focus_adjudication_count": 5,
        "human_source_visual_review_count": 0,
        "ai_assisted_visual_pre_review_count": 7,
        "signed_at_utc": executed_at,
        "human_reviewed_semantics": "作者确认签核摘要和最终决定；不表示逐条阅读全部原始材料或逐页视觉核验。",
    }
    return _output_files(
        output_dir,
        authorization,
        author_rows,
        low_rows,
        evidence,
        lineage,
        exclusions,
        input_hashes,
        protected_before,
        protected_after,
        run_id,
        transaction_result,
        executed_at,
    )


def new_run_dir() -> Path:
    auth = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()[:16].upper()
    base = ROOT / "outputs" / f"G1-T0-{auth}-formal-v1"
    if not base.exists():
        return base
    suffix = 2
    candidate = ROOT / "outputs" / f"G1-T0-{auth}-formal-v{suffix}"
    while candidate.exists():
        suffix += 1
        candidate = ROOT / "outputs" / f"G1-T0-{auth}-formal-v{suffix}"
    return candidate
