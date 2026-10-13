"""Formal G1-T0 supplementary sign-off for the five P3S sources.

The author has supplied the item-level partition: 19 direct accepts, 6
revised accepts, and 2 exclusions.  This module performs no model calls,
does not read T1, creates no snapshot, and writes the 25 new Evidence rows
plus audit records in one SQLite transaction.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import sqlite3
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Evidence, EvidenceStatus, SnapshotTag, model_dump, with_evidence_hash


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
SOURCE_RUN_ID = "P3SR-20260826T133937Z-v5-offline-recovery-v1"
SOURCE_DIR = ROOT / "outputs" / SOURCE_RUN_ID
P2S_DIR = ROOT / "outputs" / "P4Q-P2S-20260826T082341Z-v1"
DB_PATH = ROOT / "data" / "research.sqlite3"
REVIEWER_ID = "author_01"
REVIEW_MODE = "ai_preannotation_author_batch_signoff_with_focus_adjudication"
DIRECT_ACCEPT_PREFIXES = {
    "06EEB768", "1FAE4558", "38BEC464", "425BAC0B", "436559CE", "451C2F29",
    "48237E0A", "542BC468", "54AE7480", "6EAB90CE", "7E3F8138", "906DA9F5",
    "B3AD2B72", "D047B380", "DDA30BF3", "E6EFA514", "E7853897", "FB39056D", "FDBC359D",
}
EXCLUDE_PREFIXES = {"AD3ED980", "F29EEF2C"}
REVISE_RULES: dict[str, dict[str, Any]] = {
    "28FDEEB8": {
        "nature": "resource_input",
        "excerpt_original": "Four RFVs have been completed, with three already delivered to RACER Phase 1 performers in November 2021.",
        "excerpt_zh": "四辆RFV已经完成，其中三辆已于2021年11月交付给RACER第一阶段参与方。",
        "normalized_claim": "四辆RFV已经完成，其中三辆已于2021年11月交付给RACER第一阶段参与方。",
        "reason": "保留已完成和已交付事实，删除另有四辆计划建造的未来计划。",
    },
    "2CF44972": {
        "nature": "goal",
        "normalized_claim": "ITM计划采用包含人类和算法决策者的医疗分诊或大规模伤亡场景评估设计。",
        "reason": "将计划采用的评估设计由test_event修订为goal。",
    },
    "3FA6806C": {
        "dimensions": ["application_maturity"],
        "reason": "删除摘录不足以直接支持的人机权限维度。",
    },
    "4240F040": {
        "nature": "goal",
        "normalized_claim": "ITM计划收集人类和算法决策者的决定，并以盲法呈现给多名分诊专业人员，以衡量其委托意愿。",
        "reason": "将尚属计划的盲法与委托意愿衡量由test_event修订为goal。",
    },
    "8414D2FA": {
        "nature": "goal",
        "excerpt_original": "The objective of the Systems Competition is to detect and identify physiological signatures of injury derived from data captured by stand-off sensors onboard autonomous systems.",
        "excerpt_zh": "Systems Competition的目标是利用自主系统搭载的远距传感器采集的数据，检测并识别伤情生理特征。",
        "normalized_claim": "Systems Competition的目标是利用自主系统搭载的远距传感器采集的数据，检测并识别伤情生理特征。",
        "reason": "将摘录收窄到Systems Competition目标，排除同段后续实施描述。",
    },
    "8BF4D983": {
        "nature": "goal",
        "dimensions": ["application_maturity"],
        "normalized_claim": "TIAMAT项目计划在第一和第二阶段结束时分别开展sim-to-sim和sim-to-real竞赛；第一场竞赛结果将用于将参与方从六个缩减为三个。",
        "reason": "将未来竞赛计划由test_event修订为goal，并删除不受摘录直接支持的治理约束维度。",
    },
}
AUTHORIZATION_TEXT = """作者已审阅P3S-v5-R的G1补充审核建议，同意19项直接接受、6项按建议修订后接受、2项排除；授权以“AI预标注＋作者批量签核、重点项逐条裁决”的方式执行正式G1-T0补充审核。

直接接受：06EEB768、1FAE4558、38BEC464、425BAC0B、436559CE、451C2F29、48237E0A、542BC468、54AE7480、6EAB90CE、7E3F8138、906DA9F5、B3AD2B72、D047B380、DDA30BF3、E6EFA514、E7853897、FB39056D、FDBC359D。

修订：28FDEEB8、2CF44972、3FA6806C、4240F040、8414D2FA、8BF4D983。

排除：AD3ED980、F29EEF2C。"""


class G1T0SupplementError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)).replace("\\", "/"): _sha256_file(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def _protected_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


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


def _prefix(evidence_id: str) -> str:
    return evidence_id.removeprefix("EV-T0-")[:8].upper()


def _source_records() -> dict[str, dict[str, str]]:
    return {row["source_id"]: row for row in _read_csv(P2S_DIR / "p2s_source_manifest.csv")}


def _apply_author_decisions() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = _read_jsonl(SOURCE_DIR / "evidence_candidates_p3sr_final.jsonl")
    if len(candidates) != 27 or len({_prefix(row["evidence_id"]) for row in candidates}) != 27:
        raise G1T0SupplementError("P3S-v5-R must contain 27 unique candidates")
    seen = set()
    author_rows: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence_id = candidate["evidence_id"]
        prefix = _prefix(evidence_id)
        if prefix in seen:
            raise G1T0SupplementError(f"duplicate decision prefix: {prefix}")
        seen.add(prefix)
        row = dict(candidate)
        row.update(
            {
                "final_decision": "accept" if prefix in DIRECT_ACCEPT_PREFIXES else "revise" if prefix in REVISE_RULES else "exclude",
                "final_nature": candidate["evidence_nature"],
                "final_dimensions": list(candidate["coding_dimensions"]),
                "final_normalized_claim": candidate["normalized_claim"],
                "final_excerpt_zh": candidate["excerpt_zh"],
                "human_reviewed": True,
                "reviewer": REVIEWER_ID,
                "review_mode": REVIEW_MODE,
                "focus_adjudication": prefix in REVISE_RULES or prefix in EXCLUDE_PREFIXES,
            }
        )
        if prefix in REVISE_RULES:
            rule = REVISE_RULES[prefix]
            row["final_nature"] = rule.get("nature", row["final_nature"])
            row["final_dimensions"] = list(rule.get("dimensions", row["final_dimensions"]))
            row["final_normalized_claim"] = rule.get("normalized_claim", row["final_normalized_claim"])
            row["final_excerpt_zh"] = rule.get("excerpt_zh", row["final_excerpt_zh"])
            row["excerpt_original"] = rule.get("excerpt_original", row["excerpt_original"])
            row["revision_reason"] = rule["reason"]
        elif prefix in EXCLUDE_PREFIXES:
            row["exclusion_reason"] = "作者重点裁决：" + ("混合未来赛事计划与项目经理主观评价，信息增量有限。" if prefix == "AD3ED980" else "仅为摘要和提案截止日期，对军用人工智能发展研判贡献不足。")
            exclusions.append(row)
        author_rows.append(row)
    if set(_prefix(row["evidence_id"]) for row in author_rows) != DIRECT_ACCEPT_PREFIXES | set(REVISE_RULES) | EXCLUDE_PREFIXES:
        raise G1T0SupplementError("author decision partition does not cover all 27 candidates")
    counts = Counter(row["final_decision"] for row in author_rows)
    if counts != Counter({"accept": 19, "revise": 6, "exclude": 2}):
        raise G1T0SupplementError(f"unexpected decision distribution: {counts}")
    return author_rows, exclusions, candidates


def _make_evidence(row: dict[str, Any], source: dict[str, str], run_id: str) -> Evidence:
    nature = row["final_nature"]
    dimensions = row["final_dimensions"]
    if nature not in {"goal", "resource_input", "test_event", "test_result", "deployment_event", "governance_event", "constraint", "evaluation"} or "+" in nature:
        raise G1T0SupplementError(f"invalid final nature: {nature}")
    if not dimensions or any(value not in {"integration_level", "application_maturity", "human_machine_authority", "engineering_resource_conditions", "trust_governance_constraints", "organizational_system_fit"} for value in dimensions):
        raise G1T0SupplementError(f"invalid final dimensions: {dimensions}")
    evidence = Evidence(
        case_id=CASE_ID,
        evidence_id=row["evidence_id"],
        version=1,
        snapshot_tag=SnapshotTag.T0,
        title=source["title"],
        source_type=source["source_type"],
        source_grade=source["source_grade"],
        publisher=source["publisher"],
        published_at=date.fromisoformat(source["published_at"][:10]),
        published_at_precision="day",
        url_or_path=source["canonical_url"],
        page_or_section=row["source_locator"],
        source_id=row["source_id"],
        source_locator=row["source_locator"],
        excerpt=row["excerpt_original"],
        excerpt_original=row["excerpt_original"],
        excerpt_zh=row["final_excerpt_zh"],
        normalized_claim=row["final_normalized_claim"],
        evidence_nature=nature,
        coding_dimensions=list(dimensions),
        topics=list(row.get("topics", [])),
        status=EvidenceStatus.ACTIVE,
        reviewed_by=REVIEWER_ID,
        run_id=run_id,
    )
    return with_evidence_hash(evidence)


def _ensure_supplement_tables(conn: sqlite3.Connection) -> None:
    # Do not use executescript here: sqlite3 commits an executescript before
    # running it, which would put schema creation outside the all-or-nothing
    # G1 transaction.  Individual execute calls keep DDL transactional.
    statements = [
        """
        CREATE TABLE IF NOT EXISTS g1_t0_supplement_audit (
            authorization_sha256 TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            case_id TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            review_mode TEXT NOT NULL,
            author_signoff_count INTEGER NOT NULL,
            direct_accept_count INTEGER NOT NULL,
            revised_accept_count INTEGER NOT NULL,
            exclusion_count INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS g1_t0_supplement_author_decision (
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
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS g1_t0_supplement_exclusion (
            run_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, evidence_id)
        )
        """
    ]
    for statement in statements:
        conn.execute(statement)


def _insert_evidence(conn: sqlite3.Connection, evidence: Evidence) -> None:
    payload = model_dump(evidence)
    conn.execute(
        "INSERT INTO evidence(case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence.case_id, evidence.evidence_id, evidence.version, evidence.snapshot_tag.value,
            evidence.status.value, evidence.published_at.isoformat(), evidence.published_at_precision,
            evidence.content_hash, evidence.evidence_nature, evidence.excerpt_original, evidence.excerpt_zh,
            json.dumps(evidence.coding_dimensions, ensure_ascii=False, separators=(",", ":")),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def _transaction_write(
    database_path: Path,
    run_id: str,
    authorization_sha256: str,
    author_rows: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    evidence: list[Evidence],
    reviewed_at: str,
    *,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_supplement_tables(conn)
        existing_auth = conn.execute("SELECT run_id FROM g1_t0_supplement_audit WHERE authorization_sha256=?", (authorization_sha256,)).fetchone()
        if existing_auth:
            conn.rollback()
            return {"replayed": True, "existing_run_id": existing_auth[0], "evidence_rows_added": 0}
        evidence_ids = [item.evidence_id for item in evidence]
        placeholders = ",".join("?" for _ in evidence_ids)
        collisions = conn.execute(f"SELECT evidence_id,version FROM evidence WHERE case_id=? AND evidence_id IN ({placeholders})", [CASE_ID, *evidence_ids]).fetchall()
        if collisions:
            raise G1T0SupplementError(f"supplement Evidence IDs already exist: {collisions[:5]}")
        before_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)).fetchone()[0]
        payload = {
            "authorization_sha256": authorization_sha256,
            "run_id": run_id,
            "case_id": CASE_ID,
            "reviewer_id": REVIEWER_ID,
            "review_mode": REVIEW_MODE,
            "author_signoff_count": 27,
            "direct_accept_count": 19,
            "revised_accept_count": 6,
            "exclusion_count": 2,
            "human_reviewed_means_author_confirmation_not_full_source_read": True,
            "new_model_api_calls": 0,
            "hypothesis_rows_added": 0,
            "t1_read": False,
            "p4_executed": False,
            "snapshot_frozen": False,
        }
        conn.execute(
            "INSERT INTO pipeline_run(run_id,case_id,phase,created_at,payload_json) VALUES (?,?,?,?,?)",
            (run_id, CASE_ID, "G1-T0-SUPPLEMENT", reviewed_at, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        conn.execute(
            "INSERT INTO pipeline_stage(stage_id,run_id,case_id,phase,stage_name,created_at,payload_json) VALUES (?,?,?,?,?,?,?)",
            (f"STAGE-{run_id}", run_id, CASE_ID, "G1-T0-SUPPLEMENT", "formal_g1_t0_supplement_commit", reviewed_at, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        for index, row in enumerate(author_rows):
            conn.execute(
                "INSERT INTO g1_t0_supplement_author_decision(run_id,evidence_id,case_id,final_decision,final_nature,final_dimensions_json,final_normalized_claim,final_excerpt_zh,excerpt_original,source_id,source_locator,human_reviewed,reviewer,reviewed_at,review_mode,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, row["evidence_id"], CASE_ID, row["final_decision"], row["final_nature"], json.dumps(row["final_dimensions"], ensure_ascii=False, separators=(",", ":")), row["final_normalized_claim"], row["final_excerpt_zh"], row["excerpt_original"], row["source_id"], row["source_locator"], 1, REVIEWER_ID, reviewed_at, REVIEW_MODE, json.dumps(row, ensure_ascii=False, sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO human_review(review_id,gate,case_id,approved,payload_json) VALUES (?,?,?,?,?)",
                (f"G1-T0-SUPP-{row['evidence_id']}", "G1-T0-SUPPLEMENT", CASE_ID, int(row["final_decision"] != "exclude"), json.dumps({"review_id": f"G1-T0-SUPP-{row['evidence_id']}", "gate": "G1-T0-SUPPLEMENT", "case_id": CASE_ID, "approved": row["final_decision"] != "exclude", "reviewer": REVIEWER_ID, "reviewed_at": reviewed_at, "artifact_path": str(SOURCE_DIR / "g1_supplement_author_decision_template.csv"), "artifact_hash": _sha256_file(SOURCE_DIR / "g1_supplement_author_decision_template.csv"), "review_mode": REVIEW_MODE, "human_reviewed_semantics": "author_confirmation_of_ai_preannotation_and_final_decision_not_full_source_read"}, ensure_ascii=False, sort_keys=True)),
            )
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise G1T0SupplementError("injected supplement transaction failure")
        for row in exclusions:
            conn.execute("INSERT INTO g1_t0_supplement_exclusion(run_id,evidence_id,case_id,reason,payload_json) VALUES (?,?,?,?,?)", (run_id, row["evidence_id"], CASE_ID, row["exclusion_reason"], json.dumps(row, ensure_ascii=False, sort_keys=True)))
        for item in evidence:
            _insert_evidence(conn, item)
        after_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=?", (CASE_ID,)).fetchone()[0]
        if after_count - before_count != len(evidence):
            raise G1T0SupplementError(f"Evidence delta mismatch: {after_count - before_count} != {len(evidence)}")
        if conn.execute("SELECT COUNT(*) FROM g1_t0_supplement_author_decision WHERE run_id=?", (run_id,)).fetchone()[0] != 27:
            raise G1T0SupplementError("supplement author decision count mismatch")
        if conn.execute("SELECT COUNT(*) FROM g1_t0_supplement_exclusion WHERE run_id=?", (run_id,)).fetchone()[0] != 2:
            raise G1T0SupplementError("supplement exclusion count mismatch")
        conn.execute(
            "INSERT INTO g1_t0_supplement_audit(authorization_sha256,run_id,case_id,reviewer_id,review_mode,author_signoff_count,direct_accept_count,revised_accept_count,exclusion_count,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (authorization_sha256, run_id, CASE_ID, REVIEWER_ID, REVIEW_MODE, 27, 19, 6, 2, json.dumps(payload, ensure_ascii=False, sort_keys=True), reviewed_at),
        )
        conn.commit()
        return {"replayed": False, "evidence_rows_added": len(evidence), "author_rows_added": 27, "before_count": before_count, "after_count": after_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _output_artifacts(output_dir: Path, run_id: str, authorization_sha256: str, author_rows: list[dict[str, Any]], exclusions: list[dict[str, Any]], evidence: list[Evidence], reviewed_at: str, transaction_result: dict[str, Any], input_hashes: dict[str, str], protected_before: dict[str, str], protected_after: dict[str, str]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    authorization = {
        "authorization_text": AUTHORIZATION_TEXT,
        "authorization_sha256": authorization_sha256,
        "reviewer_id": REVIEWER_ID,
        "review_mode": REVIEW_MODE,
        "author_signoff_count": 27,
        "direct_accept_count": 19,
        "revised_accept_count": 6,
        "exclusion_count": 2,
        "signed_at_utc": reviewed_at,
        "human_reviewed_semantics": "作者确认AI预标注和最终决定；不表示逐条阅读全部原始材料。",
    }
    _write_json(output_dir / "g1_t0_supplement_authorization.json", authorization)
    fields = ["evidence_id", "source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature", "coding_dimensions", "final_decision", "final_nature", "final_dimensions", "final_normalized_claim", "final_excerpt_zh", "human_reviewed", "reviewer", "reviewed_at", "review_mode", "focus_adjudication", "revision_reason", "exclusion_reason"]
    _write_csv(output_dir / "g1_t0_supplement_author_decisions.csv", fields, author_rows)
    _write_csv(output_dir / "g1_t0_supplement_exclusions.csv", ["evidence_id", "source_id", "source_locator", "excerpt_original", "normalized_claim", "evidence_nature", "coding_dimensions", "final_decision", "exclusion_reason", "human_reviewed", "reviewer", "reviewed_at", "review_mode"], exclusions)
    _write_jsonl(output_dir / "g1_t0_supplement_final_evidence.jsonl", [model_dump(item) for item in evidence])
    _write_jsonl(output_dir / "g1_t0_supplement_revision_log.jsonl", [{"evidence_id": row["evidence_id"], "source_id": row["source_id"], "revision_reason": row.get("revision_reason", ""), "before": {"excerpt_original": row["excerpt_original"], "normalized_claim": row["normalized_claim"], "evidence_nature": row["evidence_nature"], "coding_dimensions": row["coding_dimensions"]}, "after": {"excerpt_original": row["excerpt_original"], "normalized_claim": row["final_normalized_claim"], "evidence_nature": row["final_nature"], "coding_dimensions": row["final_dimensions"]}} for row in author_rows if row["final_decision"] == "revise"])
    decisions = Counter(row["final_decision"] for row in author_rows)
    quality = [
        "# G1-T0补充审核质量报告", "",
        "本报告记录作者对P3S-v5-R G1补充建议的批量签核，不表示作者逐条阅读全部原始材料。", "",
        f"- 作者签核总数：{len(author_rows)}", f"- 直接接受：{decisions['accept']}", f"- 修订后接受：{decisions['revise']}", f"- 排除：{decisions['exclude']}", f"- 正式Evidence写入：{len(evidence)}", f"- 数据库新增Evidence：{transaction_result.get('evidence_rows_added', 0)}", "- 新模型/API调用：0", "- hypothesis新增：0；T1读取：false；P4执行：false；snapshot_frozen：false", "- 19条按原建议接受；6条按作者指定的性质、维度或摘录收窄规则修订后接受。", "- 2条排除：AD3ED980混合未来赛事计划与主观评价；F29EEF2C仅为截止日期行政信息。",
    ]
    (output_dir / "g1_t0_supplement_quality_report.md").write_text("\n".join(quality) + "\n", encoding="utf-8")
    hashes = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted(output_dir.rglob("*")) if path.is_file()}
    manifest = {
        "run_id": run_id, "case_id": CASE_ID, "stage": "G1-T0-SUPPLEMENT", "executed_at_utc": reviewed_at, "authorization_sha256": authorization_sha256,
        "source_run_id": SOURCE_RUN_ID, "input_source_run_hashes": input_hashes, "protected_prior_outputs_unchanged": protected_before == protected_after,
        "git_head": "unavailable_not_a_git_repository", "python_version": platform.python_version(), "author_signoff_count": 27,
        "direct_accept_count": 19, "revised_accept_count": 6, "exclusion_count": 2, "decision_counts": dict(decisions), "formal_evidence_count": len(evidence),
        "formal_evidence_db_rows_added": transaction_result.get("evidence_rows_added", 0), "formal_evidence_ids_unique": len({item.evidence_id for item in evidence}) == len(evidence),
        "formal_evidence_has_no_composite_nature": all("+" not in item.evidence_nature for item in evidence), "new_model_api_calls": 0, "model_api_call_count": 0,
        "evidence_inserted": transaction_result.get("evidence_rows_added", 0), "hypothesis_inserted": 0, "formal_g1_supplement_executed": True, "p4_executed": False,
        "snapshot_frozen": False, "p5_executed": False, "t1_read": False, "api_key_written": False, "author_actual_review_count": 27,
        "transaction_result": transaction_result, "output_file_hashes": hashes,
    }
    _write_json(output_dir / "g1_t0_supplement_manifest.json", manifest)
    return manifest


def run_g1_t0_supplement(*, database_path: Path = DB_PATH, output_dir: Path | None = None, inject_failure_after: int | None = None) -> dict[str, Any]:
    reviewed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    authorization_sha256 = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()
    run_id = f"G1-T0-SUPP-{authorization_sha256[:16].upper()}-formal-v1"
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    input_hashes = _tree_hashes(SOURCE_DIR)
    author_rows, exclusions, candidates = _apply_author_decisions()
    sources = _source_records()
    evidence = [_make_evidence(row, sources[row["source_id"]], run_id) for row in author_rows if row["final_decision"] != "exclude"]
    if len(evidence) != 25:
        raise G1T0SupplementError(f"expected 25 supplement Evidence, got {len(evidence)}")
    protected_before = _protected_hashes(output_dir)
    transaction_result = _transaction_write(database_path, run_id, authorization_sha256, author_rows, exclusions, evidence, reviewed_at, inject_failure_after=inject_failure_after)
    if transaction_result.get("replayed"):
        existing_dir = ROOT / "outputs" / transaction_result["existing_run_id"]
        existing_manifest = existing_dir / "g1_t0_supplement_manifest.json"
        if existing_manifest.exists():
            return json.loads(existing_manifest.read_text(encoding="utf-8")) | {"replayed": True}
        raise G1T0SupplementError("supplement transaction already committed but artifacts are missing")
    protected_after = _protected_hashes(output_dir)
    return _output_artifacts(output_dir, run_id, authorization_sha256, author_rows, exclusions, evidence, reviewed_at, transaction_result, input_hashes, protected_before, protected_after)
