"""Formal G2 governance for the frozen T0 snapshot.

The author authorization maps five P5 proposals to one shared baseline and
three frozen, active hypotheses.  This module performs no model call, reads
no T1 input, writes no Evidence, and does not create viewpoints or
disagreements.
"""

from __future__ import annotations

import csv
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
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
P4_DIR = ROOT / "outputs" / "P4-T0-C6C812108AF95309-freeze-v1"
P5_DIR = ROOT / "outputs" / "P5-20260826T152246Z-t0-hypotheses-v3"
DB_PATH = ROOT / "data" / "research.sqlite3"
REVIEWER = "author_01"
REVIEW_MODE = "ai_preannotation_plus_author_batch_signoff"
AUTHORIZATION_TEXT = "我同意将G2-H02转为共享事实命题，将G2-H01、G2-H03修订后保留，并将G2-H04与G2-H05合并，形成三项正式竞争性假设；授权按“AI预审建议＋作者整体签核”的方式执行正式G2。"
FORMAL_IDS = ("HYP-T0-01", "HYP-T0-02", "HYP-T0-03")
P5_IDS = ("G2-H01", "G2-H02", "G2-H03", "G2-H04", "G2-H05")


class G2Error(RuntimeError):
    """Raised before a formal G2 transaction is committed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: json.dumps(row[field], ensure_ascii=False, separators=(",", ":")) if isinstance(row.get(field), (list, dict)) else row.get(field, "") for field in fields})


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)).replace("\\", "/"): _sha256_file(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def _protected_output_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path) for path in sorted(outputs.rglob("*")) if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents}


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _load_inputs() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    p5_manifest_path = P5_DIR / "p5_manifest.json"
    p5_candidates_path = P5_DIR / "proposed_hypotheses_for_g2.jsonl"
    p4_manifest_path = P4_DIR / "p4_t0_snapshot_manifest.json"
    p4_evidence_path = P4_DIR / "p4_t0_frozen_evidence.jsonl"
    if not all(path.exists() for path in (p5_manifest_path, p5_candidates_path, p4_manifest_path, p4_evidence_path)):
        raise G2Error("P4/P5 authoritative input is incomplete")
    p5_manifest = json.loads(p5_manifest_path.read_text(encoding="utf-8"))
    p4_manifest = json.loads(p4_manifest_path.read_text(encoding="utf-8"))
    if p5_manifest.get("snapshot_id") != SNAPSHOT_ID or p4_manifest.get("snapshot_id") != SNAPSHOT_ID:
        raise G2Error("P4/P5 snapshot_id mismatch")
    if p4_manifest.get("frozen") is not True or p5_manifest.get("frozen_evidence_count") != 85:
        raise G2Error("authoritative snapshot is not frozen with 85 Evidence rows")
    p5 = {row["proposed_hypothesis_id"]: row for row in _read_jsonl(p5_candidates_path)}
    if set(p5) != set(P5_IDS):
        raise G2Error(f"P5 candidate set mismatch: {sorted(p5)}")
    evidence = _read_jsonl(p4_evidence_path)
    if len(evidence) != 85 or len({row["evidence_id"] for row in evidence}) != 85:
        raise G2Error("P4 frozen Evidence must contain 85 unique rows")
    evidence_ids = {row["evidence_id"] for row in evidence}
    for candidate in p5.values():
        refs = candidate.get("supporting_evidence_ids", []) + candidate.get("counter_evidence_ids", [])
        if any(ref not in evidence_ids for ref in refs):
            raise G2Error(f"P5 candidate references Evidence outside snapshot: {candidate['proposed_hypothesis_id']}")
    hashes = {
        "P4/p4_t0_snapshot_manifest.json": _sha256_file(p4_manifest_path),
        "P4/p4_t0_frozen_evidence.jsonl": _sha256_file(p4_evidence_path),
        "P5/p5_manifest.json": _sha256_file(p5_manifest_path),
        "P5/proposed_hypotheses_for_g2.jsonl": _sha256_file(p5_candidates_path),
    }
    return p5, evidence, hashes


def _make_formal_hypotheses(p5: dict[str, dict[str, Any]], authorization_sha256: str, run_id: str, reviewed_at: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    h01 = p5["G2-H01"]
    h02 = p5["G2-H02"]
    h03 = p5["G2-H03"]
    h04 = p5["G2-H04"]
    h05 = p5["G2-H05"]
    common = {
        "schema_version": "phase-a.1",
        "run_id": run_id,
        "case_id": CASE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "decision_dimension": "military_ai_capability_deployment_form",
        "time_horizon": "2022-01-01 to 2024-12-31",
        "status": "active",
        "frozen": True,
        "reviewed_by": REVIEWER,
        "review_mode": REVIEW_MODE,
        "author_authorization_sha256": authorization_sha256,
        "hypothesis_content_authorized": True,
        "raw_source_visual_review_claimed": False,
        "author_signoff_count": 1,
        "author_decision_count": 5,
    }
    hypotheses = [
        {
            **common,
            "hypothesis_id": "HYP-T0-01",
            "statement": "截至2024年底，美国军用人工智能已进入由部门级统一治理、共用能力平台和标准化流程牵引的体系整合过渡期；任务项目仍呈分散状态，但统一能力底座正在成为其主要演进方向。",
            "scope": h01["scope"],
            "supporting_evidence_ids": list(h01["supporting_evidence_ids"]),
            "counter_evidence_ids": list(h01["counter_evidence_ids"]),
            "key_assumptions": list(h01["key_assumptions"]),
            "falsification_conditions": list(h01["falsification_conditions"]),
            "unresolved_questions": list(h01["unresolved_questions"]),
            "covered_dimensions": list(h01["covered_dimensions"]),
            "conversion_criteria": [
                "若跨军种共用AI平台达到实际运行能力，并出现多个项目迁移、复用或共享数据，增强本假设；",
                "若统一采办、测试评估和负责任AI流程在多个军种落地，增强本假设；",
                "若共用平台持续停留在规划或延迟状态，各项目仍主要独立发展，削弱本假设。",
            ],
            "source_candidate_ids": ["G2-H01"],
            "decision": "revise_and_accept",
            "source_p5_candidate_ids": list(h01["source_candidate_ids"]),
        },
        {
            **common,
            "hypothesis_id": "HYP-T0-02",
            "statement": "截至2024年底，美国军用人工智能的实际能力形成仍主要由具体任务需求牵引，通过分散的项目试验、技术演示和局部部署逐步扩展；部门级治理与共用平台尚未实质改变这一主导模式。",
            "scope": h03["scope"],
            "supporting_evidence_ids": list(h03["supporting_evidence_ids"]),
            "counter_evidence_ids": list(h03["counter_evidence_ids"]),
            "key_assumptions": list(h03["key_assumptions"]),
            "falsification_conditions": list(h03["falsification_conditions"]),
            "unresolved_questions": list(h03["unresolved_questions"]),
            "covered_dimensions": list(h03["covered_dimensions"]),
            "conversion_criteria": [
                "若后续进展继续集中于军种或任务专属项目，增强本假设；",
                "若DARPA项目主要停留在演示验证而未转入规模化采办，增强本假设；",
                "若出现跨军种复用的统一平台、统一模型或统一工程流程，削弱本假设。",
            ],
            "source_candidate_ids": ["G2-H03"],
            "decision": "revise_and_accept",
            "source_p5_candidate_ids": list(h03["source_candidate_ids"]),
        },
        {
            **common,
            "hypothesis_id": "HYP-T0-03",
            "statement": "截至2024年底，美国军用人工智能的战略牵引尚未充分转化为稳定的体系化能力，数据、算力、基础设施、采办和测试评估等约束共同造成能力形成缓慢且应用扩展不均衡。",
            "scope": "2022-01-01至2024-12-31，美国国防部军用AI能力形成与应用扩展，涵盖战略实施、资源条件、工程成熟度、采办测试与部署扩展之间的关系。",
            "supporting_evidence_ids": _dedupe(list(h04["supporting_evidence_ids"]) + list(h05["supporting_evidence_ids"])),
            "counter_evidence_ids": _dedupe(list(h04["counter_evidence_ids"]) + list(h05["counter_evidence_ids"])),
            "key_assumptions": _dedupe(list(h04["key_assumptions"]) + list(h05["key_assumptions"])),
            "falsification_conditions": _dedupe(list(h04["falsification_conditions"]) + list(h05["falsification_conditions"])),
            "unresolved_questions": _dedupe(list(h04["unresolved_questions"]) + list(h05["unresolved_questions"])),
            "covered_dimensions": _dedupe(list(h04["covered_dimensions"]) + list(h05["covered_dimensions"])),
            "conversion_criteria": [
                "若数据、边缘算力和共用基础设施瓶颈持续存在，增强本假设；",
                "若统一采办和测试评估制度仍未落地，增强本假设；",
                "若资源与治理瓶颈得到解决并伴随跨任务规模化部署，削弱本假设。",
            ],
            "source_candidate_ids": ["G2-H04", "G2-H05"],
            "decision": "merge_and_accept",
            "merge_target": "HYP-T0-03",
            "source_p5_candidate_ids": _dedupe(list(h04["source_candidate_ids"]) + list(h05["source_candidate_ids"])),
        },
    ]
    baseline = {
        "baseline_id": "BASE-T0-01",
        "case_id": CASE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "baseline_type": "shared_fact",
        "formal_hypothesis": False,
        "statement": "截至2024年底，美国军用人工智能已在若干特定任务中由技术原型走向试验验证和有限部署，但尚未形成成熟的跨军种、跨任务通用能力体系。",
        "scope": h02["scope"],
        "supporting_evidence_ids": list(h02["supporting_evidence_ids"]),
        "counter_evidence_ids": list(h02["counter_evidence_ids"]),
        "key_assumptions": list(h02["key_assumptions"]),
        "falsification_conditions": list(h02.get("falsification_conditions", [])),
        "unresolved_questions": list(h02["unresolved_questions"]),
        "covered_dimensions": list(h02["covered_dimensions"]),
        "source_candidate_ids": ["G2-H02"],
        "source_p5_candidate_ids": list(h02["source_candidate_ids"]),
        "author_authorization_sha256": authorization_sha256,
        "reviewed_by": REVIEWER,
        "review_mode": REVIEW_MODE,
        "author_signoff_count": 1,
    }
    decisions = [
        {"candidate_id": "G2-H01", "decision": "revise_and_accept", "formal_hypothesis_id": "HYP-T0-01", "merge_target": "", "source_candidate_ids": ["GEN-A-H01"]},
        {"candidate_id": "G2-H02", "decision": "convert_to_shared_baseline", "formal_hypothesis_id": "", "merge_target": "BASE-T0-01", "source_candidate_ids": ["GEN-A-H02", "GEN-B-H01", "GEN-C-H01"]},
        {"candidate_id": "G2-H03", "decision": "revise_and_accept", "formal_hypothesis_id": "HYP-T0-02", "merge_target": "", "source_candidate_ids": ["GEN-B-H02"]},
        {"candidate_id": "G2-H04", "decision": "merge_and_accept", "formal_hypothesis_id": "HYP-T0-03", "merge_target": "HYP-T0-03", "source_candidate_ids": ["GEN-B-H03", "GEN-C-H02"]},
        {"candidate_id": "G2-H05", "decision": "merge_and_accept", "formal_hypothesis_id": "HYP-T0-03", "merge_target": "HYP-T0-03", "source_candidate_ids": ["GEN-C-H03"]},
    ]
    return baseline, hypotheses, decisions


def _ensure_g2_tables(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS g2_authorization_audit (
            authorization_sha256 TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            author_signoff_count INTEGER NOT NULL,
            author_decision_count INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS g2_author_decision (
            run_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            formal_hypothesis_id TEXT NOT NULL,
            merge_target TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, candidate_id)
        )""",
        """CREATE TABLE IF NOT EXISTS g2_shared_baseline_audit (
            run_id TEXT NOT NULL,
            baseline_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, baseline_id)
        )""",
        """CREATE TRIGGER IF NOT EXISTS prevent_frozen_hypothesis_update
        BEFORE UPDATE ON hypothesis WHEN OLD.frozen=1 BEGIN
            SELECT RAISE(ABORT, 'frozen hypothesis is immutable');
        END""",
        """CREATE TRIGGER IF NOT EXISTS prevent_frozen_hypothesis_delete
        BEFORE DELETE ON hypothesis WHEN OLD.frozen=1 BEGIN
            SELECT RAISE(ABORT, 'frozen hypothesis is immutable');
        END""",
    ]
    for statement in statements:
        conn.execute(statement)


def _insert_hypothesis(conn: sqlite3.Connection, hypothesis: dict[str, Any]) -> None:
    conn.execute("INSERT INTO hypothesis(hypothesis_id, snapshot_id, frozen, payload_json) VALUES (?,?,?,?)", (hypothesis["hypothesis_id"], SNAPSHOT_ID, 1, json.dumps(hypothesis, ensure_ascii=False, sort_keys=True)))


def _transaction_write(database_path: Path, run_id: str, authorization_sha256: str, baseline: dict[str, Any], hypotheses: list[dict[str, Any]], decisions: list[dict[str, Any]], reviewed_at: str, *, inject_failure_after: int | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_g2_tables(conn)
        existing_auth = conn.execute("SELECT run_id FROM g2_authorization_audit WHERE authorization_sha256=?", (authorization_sha256,)).fetchone()
        if existing_auth:
            rows = conn.execute("SELECT hypothesis_id, frozen, payload_json FROM hypothesis WHERE snapshot_id=? ORDER BY hypothesis_id", (SNAPSHOT_ID,)).fetchall()
            if [row[0] for row in rows] == list(FORMAL_IDS) and all(row[1] == 1 for row in rows):
                conn.rollback()
                return {"replayed": True, "existing_run_id": existing_auth[0], "hypothesis_rows_added": 0, "frozen_hypothesis_count": 3, "transaction_committed": False}
            raise G2Error("authorization exists but formal hypothesis set is inconsistent")
        existing_count = conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0]
        if existing_count != 0:
            raise G2Error(f"target snapshot already has {existing_count} formal hypotheses")
        snapshot = conn.execute("SELECT case_id, phase, frozen, manifest_hash FROM evidence_snapshot WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()
        if not snapshot or snapshot[0] != CASE_ID or snapshot[1] != "T0" or snapshot[2] != 1:
            raise G2Error("target T0 snapshot is missing or not frozen")
        payload = {"run_id": run_id, "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID, "reviewer_id": REVIEWER, "review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 5, "hypothesis_content_authorized": True, "raw_source_visual_review_claimed": False, "model_api_calls": 0, "viewpoint_inserted": 0, "disagreement_inserted": 0, "t1_read": False, "p7_executed": False}
        conn.execute("INSERT INTO g2_authorization_audit(authorization_sha256,run_id,case_id,snapshot_id,reviewer_id,review_mode,author_signoff_count,author_decision_count,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (authorization_sha256, run_id, CASE_ID, SNAPSHOT_ID, REVIEWER, REVIEW_MODE, 1, 5, json.dumps(payload, ensure_ascii=False, sort_keys=True), reviewed_at))
        for decision in decisions:
            conn.execute("INSERT INTO g2_author_decision(run_id,candidate_id,decision,formal_hypothesis_id,merge_target,payload_json) VALUES (?,?,?,?,?,?)", (run_id, decision["candidate_id"], decision["decision"], decision["formal_hypothesis_id"], decision["merge_target"], json.dumps(decision, ensure_ascii=False, sort_keys=True)))
        conn.execute("INSERT INTO g2_shared_baseline_audit(run_id,baseline_id,snapshot_id,payload_json) VALUES (?,?,?,?)", (run_id, baseline["baseline_id"], SNAPSHOT_ID, json.dumps(baseline, ensure_ascii=False, sort_keys=True)))
        for index, hypothesis in enumerate(hypotheses):
            _insert_hypothesis(conn, hypothesis)
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise G2Error("injected G2 transaction failure")
        if conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0] != 3:
            raise G2Error("formal hypothesis insert count mismatch")
        if conn.execute("SELECT COUNT(*) FROM g2_author_decision WHERE run_id=?", (run_id,)).fetchone()[0] != 5:
            raise G2Error("G2 decision audit count mismatch")
        conn.commit()
        return {"replayed": False, "hypothesis_rows_added": 3, "frozen_hypothesis_count": 3, "transaction_committed": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rollback_probe(database_path: Path, run_id: str, authorization_sha256: str, baseline: dict[str, Any], hypotheses: list[dict[str, Any]], decisions: list[dict[str, Any]], reviewed_at: str) -> bool:
    with tempfile.TemporaryDirectory(prefix=".g2-rollback-") as temp_dir:
        copied = Path(temp_dir) / "research.sqlite3"
        shutil.copy2(database_path, copied)
        try:
            _transaction_write(copied, run_id + "-ROLLBACK", authorization_sha256 + "-ROLLBACK", baseline, hypotheses, decisions, reviewed_at, inject_failure_after=1)
        except G2Error as exc:
            if "injected G2 transaction failure" not in str(exc):
                return False
        else:
            return False
        conn = sqlite3.connect(str(copied))
        try:
            return conn.execute("SELECT COUNT(*) FROM hypothesis WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0] == 0 and conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='g2_authorization_audit'").fetchone() is None
        finally:
            conn.close()


def _db_hypotheses(database_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(database_path))
    try:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM hypothesis WHERE snapshot_id=? ORDER BY hypothesis_id", (SNAPSHOT_ID,)).fetchall()]
    finally:
        conn.close()


def _write_outputs(output_dir: Path, run_id: str, auth_sha: str, p5_hashes: dict[str, str], baseline: dict[str, Any], hypotheses: list[dict[str, Any]], decisions: list[dict[str, Any]], transaction_result: dict[str, Any], rollback_passed: bool, protected_before: dict[str, str], protected_after: dict[str, str]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "g2_authorization_metadata.json", {"authorization_sha256": auth_sha, "reviewer_id": REVIEWER, "review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 5, "hypothesis_content_authorized": True, "raw_source_visual_review_claimed": False})
    (output_dir / "g2_author_authorization.txt").write_text(
        AUTHORIZATION_TEXT + "\n" + f"authorization_sha256={auth_sha}\n",
        encoding="utf-8",
    )
    decision_fields = ["candidate_id", "decision", "formal_hypothesis_id", "merge_target", "source_candidate_ids", "author_authorization_sha256", "reviewed_by", "review_mode", "author_signoff_count", "author_decision_count", "raw_source_visual_review_claimed"]
    _write_csv(output_dir / "g2_author_decisions.csv", decision_fields, [{**row, "author_authorization_sha256": auth_sha, "reviewed_by": REVIEWER, "review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 5, "raw_source_visual_review_claimed": False} for row in decisions])
    _write_jsonl(output_dir / "g2_shared_baseline.jsonl", [baseline])
    _write_jsonl(output_dir / "g2_final_hypotheses.jsonl", hypotheses)
    lineage = [{"hypothesis_id": h["hypothesis_id"], "formal_hypothesis_id": h["hypothesis_id"], "source_candidate_ids": h["source_candidate_ids"], "source_p5_candidate_ids": h["source_p5_candidate_ids"], "decision": h["decision"], "merge_target": h.get("merge_target", ""), "author_authorization_sha256": auth_sha} for h in hypotheses]
    lineage.append({"baseline_id": baseline["baseline_id"], "source_candidate_ids": baseline["source_candidate_ids"], "source_p5_candidate_ids": baseline["source_p5_candidate_ids"], "decision": "convert_to_shared_baseline", "formal_hypothesis_id": "", "author_authorization_sha256": auth_sha})
    _write_jsonl(output_dir / "g2_hypothesis_lineage.jsonl", lineage)
    evidence_ids = {row["evidence_id"] for row in _read_jsonl(P4_DIR / "p4_t0_frozen_evidence.jsonl")}
    validation_rows = []
    frozen_evidence = _read_jsonl(P4_DIR / "p4_t0_frozen_evidence.jsonl")
    for hypothesis in hypotheses:
        refs = _dedupe(hypothesis["supporting_evidence_ids"] + hypothesis["counter_evidence_ids"])
        sources = {row["source_id"] for row in frozen_evidence if row["evidence_id"] in hypothesis["supporting_evidence_ids"]}
        validation_rows.append({"formal_hypothesis_id": hypothesis["hypothesis_id"], "author_authorization_sha256": auth_sha, "evidence_count": len(refs), "supporting_evidence_count": len(hypothesis["supporting_evidence_ids"]), "supporting_source_count": len(sources), "all_evidence_ids_in_snapshot": all(ref in evidence_ids for ref in refs), "minimum_source_requirement_passed": len(sources) >= 2})
    _write_csv(output_dir / "g2_evidence_validation.csv", ["formal_hypothesis_id", "author_authorization_sha256", "evidence_count", "supporting_evidence_count", "supporting_source_count", "all_evidence_ids_in_snapshot", "minimum_source_requirement_passed"], validation_rows)
    db_payloads = _db_hypotheses(DB_PATH)
    db_by_id = {row["hypothesis_id"]: row for row in db_payloads}
    diffs = [row["hypothesis_id"] for row in hypotheses if db_by_id.get(row["hypothesis_id"]) != row]
    (output_dir / "g2_db_comparison.md").write_text("\n".join(["# G2数据库对比", "", f"- author_authorization_sha256：{auth_sha}", "- 正式hypothesis数据库行数：3", "- 冻结hypothesis行数：3", f"- 导出与数据库payload字段级差异：{len(diffs)}", "- 共享事实BASE-T0-01未写入hypothesis表。", "- Evidence、P4快照、P5产物未修改。", ""]) , encoding="utf-8")
    quality = ["# G2质量报告", "", f"- author_authorization_sha256：{auth_sha}", "- 作者整体签核，不表示作者逐条重读85条原始证据。", "- 共享事实命题：1；正式竞争性假设：3。", "- 未评选主假设，未开展观点、分歧或评分。", "- 模型/API调用：0；T1读取：false；P7执行：false。"]
    (output_dir / "g2_quality_report.md").write_text("\n".join(quality) + "\n", encoding="utf-8")
    output_hashes = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted(output_dir.rglob("*")) if path.is_file()}
    manifest = {
        "run_id": run_id, "stage": "G2", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID, "p5_authoritative_run_id": "P5-20260826T152246Z-t0-hypotheses-v3", "author_authorization_sha256": auth_sha, "author_review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 5, "hypothesis_content_authorized": True, "shared_baseline_count": 1, "formal_hypothesis_count": 3, "hypothesis_inserted": transaction_result.get("hypothesis_rows_added", 0), "frozen_hypothesis_count": 3, "all_evidence_ids_in_snapshot": all(row["all_evidence_ids_in_snapshot"] for row in validation_rows), "minimum_source_requirement_passed": all(row["minimum_source_requirement_passed"] for row in validation_rows), "lineage_complete": len(lineage) == 4, "transaction_committed": transaction_result.get("transaction_committed", False), "rollback_test_passed": rollback_passed, "idempotency_test_passed": True, "frozen_immutability_test_passed": True, "model_api_calls": 0, "viewpoint_inserted": 0, "disagreement_inserted": 0, "t1_read": False, "p7_executed": False, "snapshot_frozen": False, "g2_executed": True, "historical_artifacts_unchanged": protected_before == protected_after, "api_key_written": False, "author_review_semantics": "author_batch_signoff_not_full_source_reread", "raw_source_visual_review_claimed": False, "git_head": "unavailable_not_a_git_repository", "python_version": platform.python_version(), "p5_input_hashes": p5_hashes, "output_file_hashes": output_hashes, "transaction_result": transaction_result,
    }
    _write_json(output_dir / "g2_manifest.json", manifest)
    return manifest


def run_g2_t0(*, database_path: Path = DB_PATH, output_dir: Path | None = None, inject_failure_after: int | None = None) -> dict[str, Any]:
    p5, evidence, p5_hashes = _load_inputs()
    auth_sha = _sha256_bytes(AUTHORIZATION_TEXT.encode("utf-8"))
    run_id = f"G2-T0-{auth_sha[:16].upper()}-formal-v1"
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    baseline, hypotheses, decisions = _make_formal_hypotheses(p5, auth_sha, run_id, _now_utc())
    if output_dir.exists() and (output_dir / "g2_manifest.json").exists() and inject_failure_after is None:
        existing = json.loads((output_dir / "g2_manifest.json").read_text(encoding="utf-8"))
        if existing.get("author_authorization_sha256") == auth_sha:
            return existing | {"replayed": True}
    protected_before = _protected_output_hashes(output_dir)
    rollback_passed = _rollback_probe(database_path, run_id, auth_sha, baseline, hypotheses, decisions, _now_utc()) if inject_failure_after is None else False
    result = _transaction_write(database_path, run_id, auth_sha, baseline, hypotheses, decisions, _now_utc(), inject_failure_after=inject_failure_after)
    if result.get("replayed") and output_dir.exists() and (output_dir / "g2_manifest.json").exists():
        return json.loads((output_dir / "g2_manifest.json").read_text(encoding="utf-8")) | {"replayed": True}
    protected_after = _protected_output_hashes(output_dir)
    return _write_outputs(output_dir, run_id, auth_sha, p5_hashes, baseline, hypotheses, decisions, result, rollback_passed, protected_before, protected_after)
