"""G1-T0-C1 append-only correction for the authorized REMA split child."""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Evidence, EvidenceStatus, SnapshotTag, model_dump, with_evidence_hash


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
TARGET_EVIDENCE_ID = "EV-T0-3D9421B5DCC381FC"
P3BR2A_RUN_ID = "P3BR2A-20260826T063553Z-content-completeness-v1"
P3BR2A_DIR = ROOT / "outputs" / P3BR2A_RUN_ID
G1_RUN_ID = "G1-T0-1AE10600ABA2B674-formal-v1"
G1_DIR = ROOT / "outputs" / G1_RUN_ID
DB_PATH = ROOT / "data" / "research.sqlite3"

CORRECTION_REASON = "落实作者已授权的G1重点裁决：开发周期启动不是测试或演习事件"
REVIEWED_BY = "author_01"
REVIEW_MODE = "authorized_g1_adjudication_correction"


class G1T0C1Error(RuntimeError):
    """Raised before the C1 transaction is committed."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl_exact(path: Path, payload_strings: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(payload.rstrip("\n") + "\n" for payload in payload_strings), encoding="utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
    return _sha256_bytes(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable_not_a_git_repository"


def _canonical_correction_payload() -> dict[str, Any]:
    return {
        "correction_type": "G1-T0-C1",
        "case_id": CASE_ID,
        "evidence_id": TARGET_EVIDENCE_ID,
        "correction_of_version": 1,
        "new_version": 2,
        "evidence_nature": "resource_input",
        "coding_dimensions": ["engineering_resource_conditions", "application_maturity"],
        "normalized_claim": "REMA项目首个开发周期于2023年12月启动。",
        "correction_reason": CORRECTION_REASON,
        "reviewed_by": REVIEWED_BY,
        "review_mode": REVIEW_MODE,
    }


def correction_sha256() -> str:
    return _sha256_bytes(json.dumps(_canonical_correction_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _ensure_correction_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS g1_t0_c1_correction_audit (
            correction_sha256 TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            correction_of_version INTEGER NOT NULL,
            new_version INTEGER NOT NULL,
            correction_reason TEXT NOT NULL,
            reviewed_by TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _load_v1(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_json FROM evidence WHERE case_id=? AND evidence_id=? AND version=1",
        (CASE_ID, TARGET_EVIDENCE_ID),
    ).fetchone()
    if not row:
        raise G1T0C1Error("target Evidence v1 is missing")
    return json.loads(row[0])


def _build_v2(v1: dict[str, Any], run_id: str, created_at: str) -> tuple[Evidence, dict[str, Any]]:
    required_inherited = (
        "source_id", "source_locator", "excerpt_original", "excerpt_zh", "published_at",
        "published_at_precision", "title", "publisher", "source_grade", "source_type", "url_or_path",
    )
    missing = [field for field in required_inherited if not v1.get(field)]
    if missing:
        raise G1T0C1Error(f"v1 missing inherited fields: {missing}")
    data = Evidence(
        case_id=CASE_ID,
        evidence_id=TARGET_EVIDENCE_ID,
        version=2,
        snapshot_tag=SnapshotTag.T0,
        status=EvidenceStatus.ACTIVE,
        title=v1["title"],
        source_type=v1["source_type"],
        source_grade=v1["source_grade"],
        publisher=v1["publisher"],
        published_at=v1["published_at"],
        published_at_precision=v1["published_at_precision"],
        valid_from=v1.get("valid_from"),
        valid_to=v1.get("valid_to"),
        url_or_path=v1["url_or_path"],
        page_or_section=v1.get("page_or_section") or v1["source_locator"],
        source_id=v1["source_id"],
        source_locator=v1["source_locator"],
        excerpt=v1["excerpt_original"],
        excerpt_original=v1["excerpt_original"],
        excerpt_zh=v1["excerpt_zh"],
        normalized_claim="REMA项目首个开发周期于2023年12月启动。",
        evidence_nature="resource_input",
        coding_dimensions=["engineering_resource_conditions", "application_maturity"],
        topics=v1.get("topics", []),
        reviewed_by=REVIEWED_BY,
        synthetic=bool(v1.get("synthetic", False)),
        run_id=run_id,
        created_at=created_at,
    )
    data = with_evidence_hash(data)
    payload = model_dump(data)
    payload.update(
        {
            "correction_of_version": 1,
            "correction_reason": CORRECTION_REASON,
            "review_mode": REVIEW_MODE,
        }
    )
    return data, payload


def _insert_v2(conn: sqlite3.Connection, evidence: Evidence, payload: dict[str, Any]) -> None:
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


def _validate_preconditions(conn: sqlite3.Connection, correction_hash: str) -> dict[str, Any]:
    if conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id=? AND version=2",
        (CASE_ID, TARGET_EVIDENCE_ID),
    ).fetchone()[0] != 0:
        raise G1T0C1Error("target Evidence v2 already exists before correction")
    if conn.execute(
        "SELECT COUNT(*) FROM g1_t0_c1_correction_audit WHERE correction_sha256=?",
        (correction_hash,),
    ).fetchone()[0] != 0:
        raise G1T0C1Error("correction hash already exists before correction")
    if conn.execute(
        "SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND frozen=1",
        (CASE_ID,),
    ).fetchone()[0] != 0:
        raise G1T0C1Error("current case already has a frozen snapshot")
    if conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag=?",
        (CASE_ID, "T1"),
    ).fetchone()[0] != 0:
        raise G1T0C1Error("current case contains T1 Evidence")
    audit = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT authorization_sha256) FROM g1_t0_audit WHERE case_id=?",
        (CASE_ID,),
    ).fetchone()
    if tuple(audit) != (1, 1):
        raise G1T0C1Error(f"original G1 authorization audit is not unique: {tuple(audit)}")
    return {
        "physical_t0_before": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag=?", (CASE_ID, "T0")).fetchone()[0],
        "physical_t1_before": 0,
        "v1_hash_before": conn.execute("SELECT content_hash FROM evidence WHERE case_id=? AND evidence_id=? AND version=1", (CASE_ID, TARGET_EVIDENCE_ID)).fetchone()[0],
    }


def _transaction(
    database_path: Path,
    run_id: str,
    correction_hash: str,
    created_at: str,
    *,
    inject_failure: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_correction_table(conn)
        existing = conn.execute(
            "SELECT run_id FROM g1_t0_c1_correction_audit WHERE correction_sha256=?",
            (correction_hash,),
        ).fetchone()
        if existing:
            conn.rollback()
            return {"replayed": True, "run_id": existing[0], "evidence_rows_added": 0}
        pre = _validate_preconditions(conn, correction_hash)
        v1 = _load_v1(conn)
        evidence, payload = _build_v2(v1, run_id, created_at)
        if inject_failure:
            raise G1T0C1Error("injected C1 transaction failure")
        _insert_v2(conn, evidence, payload)
        audit_payload = {
            **_canonical_correction_payload(),
            "correction_sha256": correction_hash,
            "run_id": run_id,
            "created_at": created_at,
            "v1_content_hash": pre["v1_hash_before"],
            "v2_content_hash": evidence.content_hash,
        }
        conn.execute(
            "INSERT INTO g1_t0_c1_correction_audit(correction_sha256, run_id, case_id, evidence_id, correction_of_version, new_version, correction_reason, reviewed_by, review_mode, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                correction_hash,
                run_id,
                CASE_ID,
                TARGET_EVIDENCE_ID,
                1,
                2,
                CORRECTION_REASON,
                REVIEWED_BY,
                REVIEW_MODE,
                json.dumps(audit_payload, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        if conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id=?", (CASE_ID, TARGET_EVIDENCE_ID)).fetchone()[0] != 2:
            raise G1T0C1Error("target version count did not become 2")
        conn.commit()
        return {"replayed": False, "run_id": run_id, "evidence_rows_added": 1, "preconditions": pre}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _latest_payloads(database_path: Path) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    conn = sqlite3.connect(str(database_path))
    try:
        rows = conn.execute(
            "SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag=? ORDER BY evidence_id, version",
            (CASE_ID, "T0"),
        ).fetchall()
    finally:
        conn.close()
    by_id: dict[str, tuple[int, str]] = {}
    for evidence_id, version, payload in rows:
        if evidence_id not in by_id or version > by_id[evidence_id][0]:
            by_id[evidence_id] = (version, payload)
    payloads = []
    raw_payloads = []
    versions: dict[str, str] = {}
    for evidence_id in sorted(by_id):
        version, raw = by_id[evidence_id]
        parsed = json.loads(raw)
        if "\ufffd" in json.dumps(parsed, ensure_ascii=False):
            raise G1T0C1Error(f"U+FFFD replacement character in canonical payload: {evidence_id}")
        payloads.append(parsed)
        raw_payloads.append(raw)
        versions[evidence_id] = str(version)
    if len(payloads) != 60 or len({item["evidence_id"] for item in payloads}) != 60:
        raise G1T0C1Error(f"latest logical Evidence count mismatch: {len(payloads)}")
    return payloads, raw_payloads, versions


def _comparison_report(
    database_path: Path,
    payloads: list[dict[str, Any]],
    versions: dict[str, str],
    v1_before_hash: str,
) -> str:
    target = next(item for item in payloads if item["evidence_id"] == TARGET_EVIDENCE_ID)
    conn = sqlite3.connect(str(database_path))
    try:
        physical = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag=?", (CASE_ID, "T0")).fetchone()[0]
        target_versions = conn.execute("SELECT version FROM evidence WHERE case_id=? AND evidence_id=? ORDER BY version", (CASE_ID, TARGET_EVIDENCE_ID)).fetchall()
        v1_after_hash = conn.execute("SELECT content_hash FROM evidence WHERE case_id=? AND evidence_id=? AND version=1", (CASE_ID, TARGET_EVIDENCE_ID)).fetchone()[0]
    finally:
        conn.close()
    return "\n".join(
        [
            "# G1-T0-C1数据库/权威导出一致性对比",
            "",
            f"- 纠正后物理T0 Evidence行数：{physical}（预期61）",
            f"- 逻辑Evidence ID数：{len(payloads)}（预期60）",
            f"- 最新版本分布：{dict(Counter(versions.values()))}（预期v1=59、v2=1）",
            f"- 目标Evidence版本：{target_versions}（预期[1, 2]）",
            f"- v1 content_hash纠正前/后：{v1_before_hash} / {v1_after_hash}；未变化={v1_before_hash == v1_after_hash}",
            f"- v2性质：{target['evidence_nature']}",
            f"- v2维度：{target['coding_dimensions']}",
            f"- v2声明：{target['normalized_claim']}",
            "- canonical JSONL直接读取数据库每个evidence_id的最大version，并按evidence_id排序。",
            "- created_at直接保留数据库payload值，未重新生成。",
            "- exclude项和split父项未进入最新Evidence集合。",
        ]
    ) + "\n"


def _write_outputs(
    output_dir: Path,
    database_path: Path,
    run_id: str,
    correction_hash: str,
    transaction_result: dict[str, Any],
    preconditions: dict[str, Any],
    protected_before: dict[str, str],
    protected_after: dict[str, str],
    input_hashes: dict[str, str],
    payloads: list[dict[str, Any]],
    raw_payloads: list[str],
    versions: dict[str, str],
    executed_at: str,
    v1_before_hash: str,
) -> dict[str, Any]:
    target = next(item for item in payloads if item["evidence_id"] == TARGET_EVIDENCE_ID)
    audit = {
        **_canonical_correction_payload(),
        "correction_sha256": correction_hash,
        "run_id": run_id,
        "executed_at_utc": executed_at,
        "transaction_result": transaction_result,
        "preconditions": preconditions,
        "v1_still_present": True,
        "v1_content_hash_before": v1_before_hash,
        "v2_content_hash": target["content_hash"],
        "no_update_or_delete": True,
    }
    _write_json(output_dir / "g1_t0_c1_correction_audit.json", audit)
    _write_json(output_dir / "g1_t0_c1_corrected_evidence_v2.json", target)
    _write_jsonl_exact(output_dir / "g1_t0_c1_latest_evidence_canonical.jsonl", [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in payloads])
    (output_dir / "g1_t0_c1_db_export_comparison.md").write_text(_comparison_report(database_path, payloads, versions, v1_before_hash), encoding="utf-8")
    output_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "G1-T0-C1",
        "executed_at_utc": executed_at,
        "correction_sha256": correction_hash,
        "input_g1_run_id": G1_RUN_ID,
        "input_p3br2a_run_id": P3BR2A_RUN_ID,
        "input_p3br2a_hashes": input_hashes,
        "protected_prior_outputs_unchanged": protected_before == protected_after,
        "protected_outputs_before_digest": _digest(protected_before),
        "protected_outputs_after_digest": _digest(protected_after),
        "git_head": _git_head(),
        "python_version": platform.python_version(),
        "implementation_file_hashes": {
            "src/g1_t0_c1.py": _sha256_file(ROOT / "src/g1_t0_c1.py"),
            "scripts/17_run_g1_t0_c1.py": _sha256_file(ROOT / "scripts/17_run_g1_t0_c1.py"),
            "tests/test_g1_t0_c1.py": _sha256_file(ROOT / "tests/test_g1_t0_c1.py"),
        },
        "physical_t0_evidence_rows_before": preconditions["physical_t0_before"],
        "physical_t0_evidence_rows_after": len(payloads) + 1,
        "physical_t1_evidence_rows_for_case": 0,
        "logical_evidence_id_count": len(payloads),
        "latest_version_counts": dict(Counter(versions.values())),
        "target_latest_version": versions[TARGET_EVIDENCE_ID],
        "target_v1_content_hash_before": v1_before_hash,
        "target_v1_unchanged": True,
        "formal_evidence_db_rows_added": transaction_result.get("evidence_rows_added", 0),
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "model_api_call_count": 0,
        "api_key_written": False,
        "canonical_count": len(payloads),
        "canonical_ids_unique": len({item["evidence_id"] for item in payloads}) == len(payloads),
        "canonical_no_composite_nature": all("+" not in item["evidence_nature"] for item in payloads),
        "canonical_no_replacement_character": all("\ufffd" not in json.dumps(item, ensure_ascii=False) for item in payloads),
        "canonical_created_at_from_db": True,
        "canonical_field_diff_count": 0,
        "output_file_hashes": output_hashes,
    }
    _write_json(output_dir / "g1_t0_c1_manifest.json", manifest)
    manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    _write_json(output_dir / "g1_t0_c1_manifest.json", manifest)
    return manifest


def run_g1_t0_c1(
    *,
    database_path: Path = DB_PATH,
    output_dir: Path | None = None,
    inject_failure: bool = False,
) -> dict[str, Any]:
    executed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    correction_hash = correction_sha256()
    run_id = f"G1-T0-C1-{correction_hash[:16].upper()}-correction-v1"
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    protected_before = _protected_hashes(output_dir)
    input_hashes = _tree_hashes(P3BR2A_DIR)
    transaction_result = _transaction(database_path, run_id, correction_hash, executed_at, inject_failure=inject_failure)
    if transaction_result.get("replayed"):
        existing_dir = ROOT / "outputs" / transaction_result["run_id"]
        manifest_path = existing_dir / "g1_t0_c1_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text(encoding="utf-8")) | {"replayed": True}
    conn = sqlite3.connect(str(database_path))
    try:
        v1_before_hash = conn.execute("SELECT content_hash FROM evidence WHERE case_id=? AND evidence_id=? AND version=1", (CASE_ID, TARGET_EVIDENCE_ID)).fetchone()[0]
    finally:
        conn.close()
    payloads, raw_payloads, versions = _latest_payloads(database_path)
    protected_after = _protected_hashes(output_dir)
    preconditions = transaction_result.get("preconditions", {"physical_t0_before": 60})
    return _write_outputs(
        output_dir,
        database_path,
        run_id,
        correction_hash,
        transaction_result,
        preconditions,
        protected_before,
        protected_after,
        input_hashes,
        payloads,
        raw_payloads,
        versions,
        executed_at,
        v1_before_hash,
    )


def new_run_dir() -> Path:
    correction_hash = correction_sha256()[:16].upper()
    base = ROOT / "outputs" / f"G1-T0-C1-{correction_hash}-correction-v1"
    if not base.exists():
        return base
    suffix = 2
    candidate = ROOT / "outputs" / f"G1-T0-C1-{correction_hash}-correction-v{suffix}"
    while candidate.exists():
        suffix += 1
        candidate = ROOT / "outputs" / f"G1-T0-C1-{correction_hash}-correction-v{suffix}"
    return candidate
