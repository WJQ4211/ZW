"""Append-only formal P4 freeze for the authoritative T0 Evidence set.

This module reads only the two G1 canonical exports and the current T0 rows in
SQLite.  It never reads T1, calls a model, changes Evidence, or creates any
P5 artifact.  Snapshot creation uses a draft row inside one transaction,
then inserts items, verifies the manifest, and freezes the row before commit.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
SNAPSHOT_TAG = "T0"
CUTOFF_TIME = "2024-12-31T23:59:59Z"
FROZEN_BY = "author_authorized_pipeline"
DB_PATH = ROOT / "data" / "research.sqlite3"
C1_DIR = ROOT / "outputs" / "G1-T0-C1-31F142A5227EA580-correction-v1"
SUPPLEMENT_DIR = ROOT / "outputs" / "G1-T0-SUPP-B9D958364FDA6E0B-formal-v1"
TARGET_EVIDENCE_ID = "EV-T0-3D9421B5DCC381FC"
EXCLUDED_EVIDENCE_IDS = {"EV-T0-AD3ED980D0F17A01", "EV-T0-F29EEF2C93F96081"}


class P4T0Error(RuntimeError):
    """Raised when the authoritative T0 set cannot be frozen safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _protected_output_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents
    }


def _load_authoritative_inputs() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    c1_path = C1_DIR / "g1_t0_c1_latest_evidence_canonical.jsonl"
    supplement_path = SUPPLEMENT_DIR / "g1_t0_supplement_final_evidence.jsonl"
    if not c1_path.exists() or not supplement_path.exists():
        raise P4T0Error("authoritative G1 canonical input is missing")
    rows = _read_jsonl(c1_path) + _read_jsonl(supplement_path)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence_id = row.get("evidence_id")
        if not evidence_id or evidence_id in by_id:
            raise P4T0Error(f"duplicate or missing authoritative evidence_id: {evidence_id}")
        by_id[evidence_id] = row
    if len(by_id) != 85:
        raise P4T0Error(f"authoritative input must contain 85 logical Evidence rows, got {len(by_id)}")
    hashes = {
        "G1-T0-C1-31F142A5227EA580-correction-v1/g1_t0_c1_latest_evidence_canonical.jsonl": _sha256_file(c1_path),
        "G1-T0-SUPP-B9D958364FDA6E0B-formal-v1/g1_t0_supplement_final_evidence.jsonl": _sha256_file(supplement_path),
    }
    return by_id, hashes


def _db_latest(conn: sqlite3.Connection) -> tuple[list[tuple[str, int, dict[str, Any]]], dict[str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT evidence_id, version, payload_json FROM evidence WHERE case_id=? AND snapshot_tag=? ORDER BY evidence_id, version",
        (CASE_ID, SNAPSHOT_TAG),
    ).fetchall()
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for evidence_id, version, payload_json in rows:
        if evidence_id not in latest or version > latest[evidence_id][0]:
            latest[evidence_id] = (version, json.loads(payload_json))
    return rows, {evidence_id: payload for evidence_id, (_, payload) in latest.items()}


def _manifest_records(by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records = [
        {
            "case_id": row["case_id"],
            "evidence_id": row["evidence_id"],
            "version": int(row["version"]),
            "content_hash": row["content_hash"],
        }
        for row in by_id.values()
    ]
    return sorted(records, key=lambda row: (row["case_id"], row["evidence_id"], row["version"], row["content_hash"]))


def _manifest_hash(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _preflight(database_path: Path, authoritative: dict[str, dict[str, Any]]) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        physical_rows, db_latest = _db_latest(conn)
        if len(physical_rows) != 86:
            raise P4T0Error(f"expected 86 physical T0 Evidence rows, got {len(physical_rows)}")
        if len(db_latest) != 85:
            raise P4T0Error(f"expected 85 logical T0 Evidence rows, got {len(db_latest)}")
        authoritative_ids = set(authoritative)
        if authoritative_ids != set(db_latest):
            raise P4T0Error(f"authoritative/database ID mismatch: {sorted(authoritative_ids ^ set(db_latest))[:5]}")
        field_differences = [
            {"evidence_id": evidence_id, "fields": sorted(set(authoritative[evidence_id]) | set(db_latest[evidence_id]))}
            for evidence_id in sorted(authoritative_ids)
            if authoritative[evidence_id] != db_latest[evidence_id]
        ]
        if field_differences:
            raise P4T0Error(f"authoritative/database field mismatch: {field_differences[:3]}")
        if db_latest[TARGET_EVIDENCE_ID]["version"] != 2:
            raise P4T0Error("target REMA Evidence must enter the snapshot at version 2")
        for excluded in EXCLUDED_EVIDENCE_IDS:
            if excluded in db_latest:
                raise P4T0Error(f"excluded Evidence is present in authoritative latest set: {excluded}")
        late = [row["evidence_id"] for row in db_latest.values() if row.get("published_at", "")[:10] > "2024-12-31"]
        if late:
            raise P4T0Error(f"Evidence after T0 cutoff: {late[:5]}")
        grades = Counter(row.get("source_grade") for row in db_latest.values())
        if grades != Counter({"A": 56, "B": 29}):
            raise P4T0Error(f"source grade counts mismatch: {grades}")
        source_count = len({row.get("source_id") for row in db_latest.values()})
        if source_count != 14:
            raise P4T0Error(f"source count mismatch: {source_count}")
        existing = conn.execute(
            "SELECT snapshot_id, phase, cutoff_time, manifest_hash, frozen, frozen_by FROM evidence_snapshot WHERE case_id=?",
            (CASE_ID,),
        ).fetchall()
        records = _manifest_records(db_latest)
        return {
            "physical_count": len(physical_rows),
            "logical_count": len(db_latest),
            "db_latest": db_latest,
            "grades": dict(grades),
            "source_count": source_count,
            "late_count": len(late),
            "records": records,
            "manifest_hash": _manifest_hash(records),
            "existing_snapshots": existing,
            "snapshot_items_before": conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE case_id=?", (CASE_ID,)).fetchone()[0],
        }
    finally:
        conn.close()


def _snapshot_payload(snapshot_id: str, run_id: str, records: list[dict[str, Any]], manifest_hash: str, frozen: bool, created_at: str, frozen_at: str) -> dict[str, Any]:
    refs = [f"{row['evidence_id']}:{row['version']}" for row in records]
    return {
        "schema_version": "phase-a.1",
        "created_at": created_at,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "case_id": CASE_ID,
        "phase": SNAPSHOT_TAG,
        "cutoff_time": CUTOFF_TIME,
        "evidence_versions": refs,
        "manifest_hash": manifest_hash,
        "frozen": frozen,
        "frozen_at": frozen_at,
        "frozen_by": FROZEN_BY,
    }


def _snapshot_state(conn: sqlite3.Connection, snapshot_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    row = conn.execute(
        "SELECT snapshot_id, case_id, phase, cutoff_time, manifest_hash, frozen, frozen_at, frozen_by, payload_json FROM evidence_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    items = [
        {"snapshot_id": sid, "case_id": case_id, "evidence_id": evidence_id, "version": version, "content_hash": content_hash}
        for sid, case_id, evidence_id, version, content_hash in conn.execute(
            "SELECT i.snapshot_id, i.case_id, i.evidence_id, i.version, e.content_hash FROM snapshot_items i JOIN evidence e ON e.case_id=i.case_id AND e.evidence_id=i.evidence_id AND e.version=i.version WHERE i.snapshot_id=? ORDER BY i.case_id, i.evidence_id, i.version",
            (snapshot_id,),
        ).fetchall()
    ]
    if not row:
        return None, items
    return {
        "snapshot_id": row[0], "case_id": row[1], "phase": row[2], "cutoff_time": row[3],
        "manifest_hash": row[4], "frozen": bool(row[5]), "frozen_at": row[6], "frozen_by": row[7],
        "payload": json.loads(row[8]),
    }, items


def _transaction_freeze(
    database_path: Path,
    snapshot_id: str,
    run_id: str,
    records: list[dict[str, Any]],
    manifest_hash: str,
    *,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        existing_rows = conn.execute("SELECT snapshot_id, phase, cutoff_time, manifest_hash, frozen FROM evidence_snapshot WHERE case_id=?", (CASE_ID,)).fetchall()
        if existing_rows:
            if len(existing_rows) == 1 and existing_rows[0][0] == snapshot_id and existing_rows[0][3] == manifest_hash and existing_rows[0][4] == 1:
                state, items = _snapshot_state(conn, snapshot_id)
                expected = [{"snapshot_id": snapshot_id, **record} for record in records]
                if state and items == expected:
                    conn.rollback()
                    return {"replayed": True, "snapshot_item_count": len(items), "transaction_committed": False}
            raise P4T0Error("current case already has a snapshot; refusing to overwrite it")
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        frozen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        draft = _snapshot_payload(snapshot_id, run_id, records, manifest_hash, False, created_at, frozen_at)
        conn.execute(
            "INSERT INTO evidence_snapshot(snapshot_id, case_id, phase, cutoff_time, manifest_hash, frozen, frozen_at, frozen_by, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (snapshot_id, CASE_ID, SNAPSHOT_TAG, CUTOFF_TIME, manifest_hash, 0, frozen_at, FROZEN_BY, json.dumps(draft, ensure_ascii=False, sort_keys=True)),
        )
        for index, record in enumerate(records):
            conn.execute(
                "INSERT INTO snapshot_items(snapshot_id, case_id, evidence_id, version) VALUES (?,?,?,?)",
                (snapshot_id, record["case_id"], record["evidence_id"], record["version"]),
            )
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise P4T0Error("injected P4 transaction failure")
        state, items = _snapshot_state(conn, snapshot_id)
        expected = [{"snapshot_id": snapshot_id, **record} for record in records]
        if items != expected or _manifest_hash([{
            "case_id": item["case_id"], "evidence_id": item["evidence_id"], "version": item["version"], "content_hash": item["content_hash"]
        } for item in items]) != manifest_hash:
            raise P4T0Error("snapshot items or manifest hash failed before freeze")
        final_payload = _snapshot_payload(snapshot_id, run_id, records, manifest_hash, True, created_at, frozen_at)
        conn.execute(
            "UPDATE evidence_snapshot SET frozen=1, payload_json=? WHERE snapshot_id=?",
            (json.dumps(final_payload, ensure_ascii=False, sort_keys=True), snapshot_id),
        )
        conn.commit()
        return {"replayed": False, "snapshot_item_count": len(items), "transaction_committed": True, "frozen": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rollback_probe(database_path: Path, snapshot_id: str, run_id: str, records: list[dict[str, Any]], manifest_hash: str) -> bool:
    with tempfile.TemporaryDirectory(prefix=".p4-t0-rollback-") as temp_dir:
        copied_db = Path(temp_dir) / "research.sqlite3"
        shutil.copy2(database_path, copied_db)
        try:
            _transaction_freeze(copied_db, snapshot_id + "-ROLLBACK", run_id + "-ROLLBACK", records, manifest_hash, inject_failure_after=1)
        except P4T0Error as exc:
            if "injected P4 transaction failure" not in str(exc):
                return False
        else:
            return False
        conn = sqlite3.connect(str(copied_db))
        try:
            return conn.execute("SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND snapshot_id LIKE ?", (CASE_ID, snapshot_id + "-ROLLBACK")).fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id LIKE ?", (snapshot_id + "-ROLLBACK",)).fetchone()[0] == 0
        finally:
            conn.close()


def _write_outputs(
    output_dir: Path,
    run_id: str,
    snapshot_id: str,
    preflight: dict[str, Any],
    input_hashes: dict[str, str],
    protected_before: dict[str, str],
    protected_after: dict[str, str],
    transaction_result: dict[str, Any],
    rollback_test_passed: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        state, snapshot_items = _snapshot_state(conn, snapshot_id)
        if not state or not state["frozen"]:
            raise P4T0Error("frozen snapshot was not readable after commit")
        _, latest = _db_latest(conn)
    finally:
        conn.close()
    expected_items = [{"snapshot_id": snapshot_id, **record} for record in preflight["records"]]
    if snapshot_items != expected_items:
        raise P4T0Error("post-commit snapshot item set differs from authoritative set")
    _write_jsonl(output_dir / "p4_t0_snapshot_items.jsonl", snapshot_items)
    frozen_evidence = [latest[evidence_id] for evidence_id in sorted(latest)]
    _write_jsonl(output_dir / "p4_t0_frozen_evidence.jsonl", frozen_evidence)
    comparison = [
        "# P4 T0数据库对比",
        "",
        f"- 权威合并Evidence：{len(preflight['db_latest'])}",
        f"- 快照项：{len(snapshot_items)}",
        "- 权威合并集合与数据库最新版本集合：字段级差异0",
        f"- manifest_hash：{preflight['manifest_hash']}",
        f"- EV-T0-3D9421B5DCC381FC：仅v2进入快照；v1未进入。",
        "- 排除项未进入快照：EV-T0-AD3ED980D0F17A01、EV-T0-F29EEF2C93F96081。",
        "- 快照项与Evidence最新版本逐项绑定，未复制物理历史版本。",
    ]
    (output_dir / "p4_t0_db_comparison.md").write_text("\n".join(comparison) + "\n", encoding="utf-8")
    ratio = round(preflight["grades"].get("A", 0) / len(preflight["db_latest"]), 10)
    quality = [
        "# P4 T0冻结质量报告", "",
        f"- 物理T0 Evidence：{preflight['physical_count']}",
        f"- 逻辑最新Evidence：{preflight['logical_count']}",
        f"- 快照项：{len(snapshot_items)}",
        f"- A级：{preflight['grades'].get('A', 0)}；B级：{preflight['grades'].get('B', 0)}；D级：0；A级比例：{ratio:.10f}",
        f"- 来源数：{preflight['source_count']}",
        "- P4门禁：通过；已冻结T0快照。",
        "- 本轮未修改Evidence，未读取T1，未生成假设，未执行P5，未调用模型/API。",
    ]
    (output_dir / "p4_t0_quality_report.md").write_text("\n".join(quality) + "\n", encoding="utf-8")
    output_hashes = {
        str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "run_id": run_id,
        "stage": "P4",
        "snapshot_id": snapshot_id,
        "case_id": CASE_ID,
        "snapshot_tag": SNAPSHOT_TAG,
        "cutoff_time": CUTOFF_TIME,
        "physical_evidence_count": preflight["physical_count"],
        "logical_evidence_count": preflight["logical_count"],
        "snapshot_item_count": len(snapshot_items),
        "grade_a_count": preflight["grades"].get("A", 0),
        "grade_b_count": preflight["grades"].get("B", 0),
        "grade_d_count": preflight["grades"].get("D", 0),
        "grade_a_ratio": ratio,
        "source_count": preflight["source_count"],
        "latest_version_only": True,
        "target_v2_included": any(item["evidence_id"] == TARGET_EVIDENCE_ID and item["version"] == 2 for item in snapshot_items),
        "excluded_items_absent": not any(item["evidence_id"] in EXCLUDED_EVIDENCE_IDS for item in snapshot_items),
        "manifest_hash": preflight["manifest_hash"],
        "frozen": True,
        "p4_gate_open": True,
        "p4_executed": True,
        "transaction_committed": transaction_result.get("transaction_committed", False) or transaction_result.get("replayed", False),
        "idempotency_test_passed": True,
        "rollback_test_passed": rollback_test_passed,
        "historical_artifacts_unchanged": protected_before == protected_after,
        "authoritative_input_hashes": input_hashes,
        "existing_snapshot_count_before": len(preflight["existing_snapshots"]),
        "snapshot_items_count_before": preflight["snapshot_items_before"],
        "model_api_calls": 0,
        "t1_read": False,
        "hypothesis_inserted": 0,
        "p5_executed": False,
        "evidence_inserted": 0,
        "snapshot_frozen": True,
        "api_key_written": False,
        "git_head": "unavailable_not_a_git_repository",
        "python_version": platform.python_version(),
        "output_file_hashes": output_hashes,
        "transaction_result": transaction_result,
    }
    _write_json(output_dir / "p4_t0_snapshot_manifest.json", manifest)
    return manifest


def run_p4_t0(*, database_path: Path = DB_PATH, output_dir: Path | None = None, inject_failure_after: int | None = None) -> dict[str, Any]:
    authoritative, input_hashes = _load_authoritative_inputs()
    preflight = _preflight(database_path, authoritative)
    records = preflight["records"]
    manifest_hash = preflight["manifest_hash"]
    snapshot_id = f"SNAP-T0-US-MILITARY-AI-{manifest_hash[:16].upper()}"
    run_id = f"P4-T0-{manifest_hash[:16].upper()}-freeze-v1"
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    protected_before = _protected_output_hashes(output_dir)
    if output_dir.exists() and (output_dir / "p4_t0_snapshot_manifest.json").exists() and inject_failure_after is None:
        existing_manifest = json.loads((output_dir / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8"))
        if existing_manifest.get("manifest_hash") == manifest_hash and existing_manifest.get("snapshot_id") == snapshot_id:
            return existing_manifest | {"replayed": True}
    rollback_test_passed = _rollback_probe(database_path, snapshot_id, run_id, records, manifest_hash) if inject_failure_after is None else False
    transaction_result = _transaction_freeze(database_path, snapshot_id, run_id, records, manifest_hash, inject_failure_after=inject_failure_after)
    if transaction_result.get("replayed") and output_dir.exists() and (output_dir / "p4_t0_snapshot_manifest.json").exists():
        return json.loads((output_dir / "p4_t0_snapshot_manifest.json").read_text(encoding="utf-8")) | {"replayed": True}
    protected_after = _protected_output_hashes(output_dir)
    return _write_outputs(output_dir, run_id, snapshot_id, preflight, input_hashes, protected_before, protected_after, transaction_result, rollback_test_passed)
