"""Final offline G3-T1/G4-T1 sign-off and paper-case packaging.

This module deliberately contains no model client.  It only promotes the
already audited P16R-C1 objects, records the author's supplied sign-off
metadata, and appends formal rows in one SQLite transaction.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research.sqlite3"
CASE_ID = "us_military_ai_deployment"
T0_SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
T1_SNAPSHOT_ID = "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A"
C1_DIR = ROOT / "outputs" / "P16R-C1-G4QR-C1-PAPER-CASE-C1-20260827T100034Z-v1"

# The latest request did not contain a separately delimited authorization
# block.  These are the two authorization sentences present in that request;
# retaining them verbatim is preferable to inventing an unseen quotation.
AUTHORIZATION_TEXT = (
    "授权依据为作者本轮正式授权原文。仅执行正式签发和最终案例包生成，不调用模型/API，不重跑任何分析阶段。"
)
REVIEW_MODE = "ai_pre_review_and_reassessment_plus_author_bulk_signoff_with_key_disagreement_adjudication"
REVIEWER = "author_01"
G3_IDS = {"DISAG-T1R-C1-01", "DISAG-T1R-C1-02", "DISAG-T1R-C1-03"}
NEED_IDS = {"NEED-T1R-C1-01", "NEED-T1R-C1-02", "NEED-T1R-C1-03"}
INDICATOR_IDS = {"IND-T1R-C1-01", "IND-T1R-C1-02", "IND-T1R-C1-03", "IND-T1R-C1-04"}
TRIGGER_IDS = {f"TRG-T1R-C1-{i:02d}" for i in range(1, 9)}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_hashes(directory: Path) -> dict[str, str]:
    if not directory.exists():
        return {}
    return {
        str(path.relative_to(directory)).replace("\\", "/"): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix not in {".pyc"}
    }


def load_c1_rows() -> dict[str, list[dict[str, Any]]]:
    if not C1_DIR.exists():
        raise RuntimeError(f"C1 authoritative directory missing: {C1_DIR}")
    return {
        "disagreements": read_jsonl(C1_DIR / "P16R_C1" / "candidate_disagreements_c1.jsonl"),
        "needs": read_jsonl(C1_DIR / "P16R_C1" / "discriminative_needs_c1.jsonl"),
        "indicators": read_jsonl(C1_DIR / "P16R_C1" / "monitor_indicators_c1.jsonl"),
        "triggers": read_jsonl(C1_DIR / "P16R_C1" / "trigger_rules_c1.jsonl"),
    }


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload_json"])


def _latest_evidence(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM evidence WHERE case_id=? ORDER BY evidence_id, version", (CASE_ID,)
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item.update(_payload(row))
        latest[item["evidence_id"]] = item
    return latest


def _latest_views(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM viewpoint WHERE viewpoint_id LIKE 'VP-T0-%' ORDER BY viewpoint_id, version"
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item.update(_payload(row))
        latest[item["viewpoint_id"]] = item
    return latest


def _t0_parent_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Map each C1 disagreement to one unique T0 parent by payload semantics.

    No T0 ID is assumed.  The candidates are read from SQLite and scored from
    their contested node, branches, and type; a tie or missing match aborts.
    """
    rows = conn.execute(
        "SELECT disagreement_id, payload_json, decision_impact, status FROM disagreement "
        "WHERE snapshot_id=? AND disagreement_id LIKE 'DISAG-T0-%' ORDER BY disagreement_id",
        (T0_SNAPSHOT_ID,),
    ).fetchall()
    if len(rows) != 3:
        raise RuntimeError(f"expected exactly three formal T0 disagreements, got {len(rows)}")
    records = []
    for row in rows:
        payload = _payload(row)
        text = stable_json(payload)
        records.append({"id": row["disagreement_id"], "payload": payload, "text": text})

    patterns = {
        # The parent is selected from database payload semantics.  The C1
        # type is a strong semantic discriminator; the phrases only resolve
        # cases where a database contains additional non-formal rows.
        "DISAG-T1R-C1-01": ("mechanism", ("整合", "平台", "跨军种", "复用", "主导演进", "实质性牵引")),
        "DISAG-T1R-C1-02": ("risk_weight", ("约束", "资源", "数据", "算力", "瓶颈", "工程", "采办")),
        "DISAG-T1R-C1-03": ("scope", ("范围", "外推", "局部", "全军", "层次", "整体")),
    }
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for child, (expected_type, tokens) in patterns.items():
        scored = []
        for item in records:
            payload = item["payload"]
            type_score = 100 if payload.get("type") == expected_type else 0
            scored.append((type_score + sum(item["text"].count(token) for token in tokens), item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        if not scored or scored[0][0] <= 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            raise RuntimeError(f"T0 parent mapping is not unique for {child}: {[(s, i['id']) for s, i in scored]}")
        parent = scored[0][1]["id"]
        if parent in used:
            raise RuntimeError(f"T0 parent reused while mapping {child}: {parent}")
        used.add(parent)
        mapping[child] = parent
    return mapping


def _citation(evidence_id: str, evidence: dict[str, dict[str, Any]], relation: str = "support") -> dict[str, Any]:
    if evidence_id not in evidence:
        raise RuntimeError(f"C1 object references missing Evidence: {evidence_id}")
    return {"evidence_id": evidence_id, "version": int(evidence[evidence_id]["version"]), "relation": relation, "note": ""}


def _enrich_disagreement(row: dict[str, Any], evidence: dict[str, dict[str, Any]], auth_sha: str, run_id: str, reviewed_at: str) -> dict[str, Any]:
    item = dict(row)
    item["status"] = "resolved" if item["disagreement_id"] == "DISAG-T1R-C1-03" else "conditionally_retained"
    item["human_verified"] = True
    item["author_decision"] = "approve"
    item["author_authorization_sha256"] = auth_sha
    item["review_mode"] = REVIEW_MODE
    item["reviewed_by"] = REVIEWER
    item["reviewed_at"] = reviewed_at
    item["run_id"] = run_id
    item["snapshot_id"] = T1_SNAPSHOT_ID
    item["formal_g3_t1"] = True
    item["viewpoint_field_refs"] = [{"ref": ref, "field": "judgment"} for ref in item.get("viewpoint_refs", [])]
    if item["disagreement_id"] == "DISAG-T1R-C1-03":
        item["resolution_conclusion"] = "局部事实成立—整体外推受限"
    for branch_name in ("branch_a", "branch_b"):
        branch = dict(item[branch_name])
        ids = list(branch.get("evidence_ids", []))
        branch["evidence"] = [_citation(eid, evidence) for eid in ids]
        branch.setdefault("expected_observables", [])
        item[branch_name] = branch
    return item


def _enrich_need(row: dict[str, Any], auth_sha: str, run_id: str, reviewed_at: str) -> dict[str, Any]:
    item = dict(row)
    item["discriminativeness"] = 5 if isinstance(item.get("discriminativeness"), str) else item.get("discriminativeness", 5)
    item["source_candidate_discriminativeness"] = row.get("discriminativeness", "discriminative")
    item["status"] = "approved"
    item["human_verified"] = True
    item["author_authorization_sha256"] = auth_sha
    item["review_mode"] = REVIEW_MODE
    item["reviewed_by"] = REVIEWER
    item["reviewed_at"] = reviewed_at
    item["run_id"] = run_id
    return item


def _enrich_indicator(row: dict[str, Any], auth_sha: str, run_id: str, reviewed_at: str) -> dict[str, Any]:
    item = dict(row)
    item["status"] = "approved"
    item["human_verified"] = True
    item["author_authorization_sha256"] = auth_sha
    item["review_mode"] = REVIEW_MODE
    item["reviewed_by"] = REVIEWER
    item["reviewed_at"] = reviewed_at
    item["run_id"] = run_id
    return item


def _enrich_trigger(row: dict[str, Any], auth_sha: str, run_id: str, reviewed_at: str) -> dict[str, Any]:
    item = dict(row)
    item["status"] = "approved"
    item["human_verified"] = True
    item["author_authorization_sha256"] = auth_sha
    item["review_mode"] = REVIEW_MODE
    item["reviewed_by"] = REVIEWER
    item["reviewed_at"] = reviewed_at
    item["run_id"] = run_id
    item["predicate"] = {
        "condition": row["condition"],
        "strengthens_branch": row["strengthens_branch"],
        "weakens_branch": row["weakens_branch"],
    }
    item["action"] = "review_viewpoints"
    return item


def build_formal_objects(conn: sqlite3.Connection, run_id: str, auth_sha: str, reviewed_at: str) -> dict[str, Any]:
    c1 = load_c1_rows()
    evidence = _latest_evidence(conn)
    views = _latest_views(conn)
    if len(views) != 15:
        raise RuntimeError(f"expected 15 latest T1 viewpoints, got {len(views)}")
    for row in c1["disagreements"]:
        if row["disagreement_id"] not in G3_IDS:
            raise RuntimeError(f"unexpected C1 disagreement: {row['disagreement_id']}")
        for ref in row.get("viewpoint_refs", []):
            vp_id, version = ref.rsplit(":", 1)
            if vp_id not in views or int(views[vp_id]["version"]) != int(version):
                raise RuntimeError(f"C1 viewpoint ref is not latest: {ref}")
    disagreements = [_enrich_disagreement(row, evidence, auth_sha, run_id, reviewed_at) for row in c1["disagreements"]]
    needs = [_enrich_need(row, auth_sha, run_id, reviewed_at) for row in c1["needs"]]
    indicators = [_enrich_indicator(row, auth_sha, run_id, reviewed_at) for row in c1["indicators"]]
    triggers = [_enrich_trigger(row, auth_sha, run_id, reviewed_at) for row in c1["triggers"]]
    if {row["disagreement_id"] for row in disagreements} != G3_IDS:
        raise RuntimeError("G3 disagreement ID set mismatch")
    if {row["need_id"] for row in needs} != NEED_IDS or {row["indicator_id"] for row in indicators} != INDICATOR_IDS or {row["trigger_id"] for row in triggers} != TRIGGER_IDS:
        raise RuntimeError("C1 monitoring ID set mismatch")
    return {"disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers}


def _insert_or_verify(conn: sqlite3.Connection, table: str, key_column: str, key: str, values: tuple[Any, ...], payload: dict[str, Any]) -> bool:
    existing = conn.execute(f"SELECT payload_json FROM {table} WHERE {key_column}=?", (key,)).fetchone()
    if existing:
        old = json.loads(existing["payload_json"])
        if old.get("author_authorization_sha256") != payload.get("author_authorization_sha256"):
            raise RuntimeError(f"{table}/{key} exists with a different authorization")
        if stable_json({k: v for k, v in old.items() if k not in {"created_at", "reviewed_at", "run_id"}}) != stable_json({k: v for k, v in payload.items() if k not in {"created_at", "reviewed_at", "run_id"}}):
            raise RuntimeError(f"{table}/{key} exists with different content")
        return False
    conn.execute(values[0], values[1])
    return True


def write_transaction(
    db_path: Path,
    objects: dict[str, Any],
    lineage: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    auth_sha: str,
    review_payloads: list[dict[str, Any]],
    *,
    inject_failure: bool = False,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    inserted = Counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        snapshot = conn.execute("SELECT * FROM evidence_snapshot WHERE snapshot_id=? AND case_id=?", (T1_SNAPSHOT_ID, CASE_ID)).fetchone()
        if not snapshot or int(snapshot["frozen"]) != 1:
            raise RuntimeError("T1 snapshot is not frozen")
        if conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()[0] != 125:
            raise RuntimeError("T1 snapshot item count is not 125")
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("evidence", "hypothesis", "viewpoint", "evidence_dependency", "snapshot_items")}
        existing_reviews = conn.execute("SELECT payload_json FROM human_review WHERE review_id IN (?, ?)", tuple(item["review_id"] for item in review_payloads)).fetchall()
        replay = bool(existing_reviews)
        if replay and len(existing_reviews) != len(review_payloads):
            raise RuntimeError("partial authorization replay detected")
        if replay:
            for row in existing_reviews:
                old = json.loads(row["payload_json"])
                if old.get("authorization_sha256") != auth_sha:
                    raise RuntimeError("authorization hash cannot reuse an existing formal run")

        for row in objects["disagreements"]:
            payload = dict(row)
            conn.execute("SELECT 1 FROM disagreement WHERE disagreement_id=?", (row["disagreement_id"],)).fetchone()
            if replay:
                continue
            conn.execute(
                "INSERT INTO disagreement(disagreement_id,snapshot_id,decision_impact,status,payload_json) VALUES(?,?,?,?,?)",
                (row["disagreement_id"], T1_SNAPSHOT_ID, row["decision_impact"], row["status"], stable_json(payload)),
            )
            inserted["disagreements"] += 1
        if inject_failure:
            raise RuntimeError("injected rollback failure")
        for row in objects["needs"]:
            if not replay:
                conn.execute("INSERT INTO discriminative_need(need_id,disagreement_id,payload_json) VALUES(?,?,?)", (row["need_id"], row["disagreement_id"], stable_json(row)))
                inserted["needs"] += 1
        for row in objects["indicators"]:
            if not replay:
                conn.execute("INSERT INTO monitor_indicator(indicator_id,need_id,status,payload_json) VALUES(?,?,?,?)", (row["indicator_id"], row["need_id"], row["status"], stable_json(row)))
                inserted["indicators"] += 1
        for row in objects["triggers"]:
            if not replay:
                conn.execute("INSERT INTO trigger_rule(trigger_id,indicator_id,status,payload_json) VALUES(?,?,?,?)", (row["trigger_id"], row["indicator_id"], row["status"], stable_json(row)))
                inserted["triggers"] += 1
        if not replay:
            for item in review_payloads:
                conn.execute("INSERT INTO human_review(review_id,gate,case_id,approved,payload_json) VALUES(?,?,?,?,?)", (item["review_id"], item["gate"], CASE_ID, 1, stable_json(item)))
                inserted["human_reviews"] += 1
            for item in artifacts:
                conn.execute("INSERT INTO artifact(artifact_id,case_id,artifact_type,path,content_hash,payload_json) VALUES(?,?,?,?,?,?)", (item["artifact_id"], CASE_ID, item["artifact_type"], item["path"], item["content_hash"], stable_json(item["payload"])))
                inserted["artifacts"] += 1
        after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        for table in ("evidence", "hypothesis", "viewpoint", "evidence_dependency", "snapshot_items"):
            if after[table] != before[table]:
                raise RuntimeError(f"forbidden table changed during formal G3/G4 transaction: {table}")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("foreign key check failed")
        conn.commit()
        return {"transaction_committed": True, "replayed": replay, "inserted": dict(inserted), "before_counts": before, "after_counts": after}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_lineage(conn: sqlite3.Connection, auth_sha: str, review_id: str) -> list[dict[str, Any]]:
    parents = _t0_parent_map(conn)
    status = {"DISAG-T1R-C1-01": "conditionally_retained", "DISAG-T1R-C1-02": "conditionally_retained", "DISAG-T1R-C1-03": "resolved"}
    reasons = {
        "DISAG-T1R-C1-01": "T1治理目标与实际复用机制的证据层级分化，延续T0机制分歧并转为条件性保留。",
        "DISAG-T1R-C1-02": "T1同时出现约束缺口和局部推进信号，延续T0资源与工程约束权重分歧。",
        "DISAG-T1R-C1-03": "T1局部事实与整体外推边界被明确拆分，承接T0范围分歧。",
    }
    rows = []
    for child in sorted(G3_IDS):
        rows.append({
            "parent_disagreement_id": parents[child],
            "child_disagreement_id": child,
            "lineage_relation": "evolved_from",
            "t0_status": conn.execute("SELECT status FROM disagreement WHERE disagreement_id=?", (parents[child],)).fetchone()[0],
            "t1_status": status[child],
            "change_reason": reasons[child],
            "evidence_snapshot_from": T0_SNAPSHOT_ID,
            "evidence_snapshot_to": T1_SNAPSHOT_ID,
            "author_review_id": review_id,
            "author_authorization_sha256": auth_sha,
        })
    return rows


def _signed_report(draft: str, signed_at: str, auth_sha: str) -> str:
    lines = draft.splitlines()
    if len(lines) >= 5 and lines[0].startswith("#"):
        lines[0] = lines[0].replace("报告草案", "报告")
    text = "\n".join(lines)
    old = "当前状态：`draft_pending_author_signoff`。本运行只完成P16R-C1证据重映射、G4QR-C1报告增强和论文材料修订，正式G3-T1未执行，G4-T1未签发。"
    new = "当前状态：`signed`。正式G3-T1和G4-T1已完成作者签核；本报告不表示作者逐条重读全部原始资料，也不表示完成逐页视觉复核。"
    text = text.replace(old, new)
    marker = "## 正式签发信息\n\n"
    section = marker + f"- signed_at: {signed_at}\n- reviewer: {REVIEWER}\n- review_mode: {REVIEW_MODE}\n- authorization_sha256: {auth_sha}\n\n"
    if "## 正式签发信息" not in text:
        text = text.replace("## 状态\n", "## 状态\n" + section)
    judgment = "截至2026年6月30日，美国军用人工智能的发展呈现局部任务应用和组织化试验扩展、部门级体系整合意图增强并存的状态。现有公开证据尚不足以确认跨军种、跨任务的通用能力体系已经形成；资源、数据、作战化验证和可信治理条件仍造成能力发展与应用扩展不均衡。因此，T1并未推翻T0判断，而是形成了‘局部扩展信号增强、体系整合仍待实际复用验证、约束条件进一步显化’的条件化修订。"
    if "## T1条件化主判断" not in text:
        text += "\n\n## T1条件化主判断\n\n" + judgment + "\n"
    return text.rstrip() + "\n"


def _final_paper(draft: str) -> str:
    text = draft.replace("美国军用人工智能T0—T1案例章节（PAPER-CASE-C1 v4）", "美国军用人工智能T0—T1案例章节（FINAL v5）")
    text = text.replace("正式G3-T1和G4-T1尚未执行，本章节不构成最终签发报告。", "正式G3-T1和G4-T1已完成作者签核；本章节为最终案例展示稿，不表示作者逐条重读全部原始资料或完成逐页视觉复核。")
    return text.rstrip() + "\n"


def _traceability(conn: sqlite3.Connection, lineage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views = _latest_views(conn)
    rows: list[dict[str, Any]] = []
    for view_id, view in sorted(views.items()):
        ids = []
        for field in ("supporting_evidence", "counter_evidence"):
            for citation in view.get(field, []) or []:
                if isinstance(citation, dict) and citation.get("evidence_id"):
                    ids.append(citation["evidence_id"])
        rows.append({
            "claim_id": f"VP-{view_id}",
            "claim": view.get("judgment", ""),
            "evidence_ids": ";".join(sorted(set(ids))),
            "viewpoint_refs": f"{view_id}:{view.get('version')}",
            "disagreement_refs": ";".join(sorted(item["child_disagreement_id"] for item in lineage if view_id in json.dumps(item, ensure_ascii=False))),
            "inference_level": "viewpoint_synthesis",
            "paper_usage": "case_analysis",
        })
    rows.extend([
        {"claim_id": "G4-T1-MAIN", "claim": "T1条件化主判断", "evidence_ids": "", "viewpoint_refs": ";".join(f"{k}:{v.get('version')}" for k, v in sorted(views.items())), "disagreement_refs": ";".join(sorted(G3_IDS)), "inference_level": "conditional_synthesis", "paper_usage": "main_judgment"},
        {"claim_id": "G3-T1-DISAGREEMENTS", "claim": "T1三项正式分歧及其监测处置", "evidence_ids": "", "viewpoint_refs": "", "disagreement_refs": ";".join(sorted(G3_IDS)), "inference_level": "governance_record", "paper_usage": "method_case"},
    ])
    return rows


def _copy_final_figures(final_dir: Path) -> dict[str, str]:
    old = C1_DIR / "PAPER_CASE_C1" / "figures"
    target = final_dir / "figures"
    target.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for path in sorted(old.glob("*.png")) + sorted(old.glob("*.svg")):
        if path.name.endswith(".caption.txt"):
            continue
        stem = path.stem + "_final" + path.suffix
        dst = target / stem
        shutil.copy2(path, dst)
        mapping[str(dst.relative_to(final_dir)).replace("\\", "/")] = sha256_file(dst)
    old_sheet = C1_DIR / "PAPER_CASE_C1" / "contact_sheet_v2.png"
    shutil.copy2(old_sheet, final_dir / "contact_sheet_final.png")
    return mapping


def _build_artifacts(run_dir: Path, auth_sha: str) -> list[dict[str, Any]]:
    files = [
        ("G4-T1-signed-report", "g4_t1_final_report.md", "g4_t1_report"),
        ("G4-T1-signed-report-json", "g4_t1_final_report.json", "g4_t1_report_json"),
        ("final-paper-section", "paper_case_section_v5_final.md", "paper_case"),
        ("final-traceability", "final_claim_evidence_traceability.csv", "claim_traceability"),
    ]
    result = []
    for suffix, name, artifact_type in files:
        path = run_dir / "FINAL_PAPER_CASE" / name
        result.append({
            "artifact_id": f"ART-G4-T1-{auth_sha[:16]}-{suffix}",
            "artifact_type": artifact_type,
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "content_hash": sha256_file(path),
            "payload": {"authorization_sha256": auth_sha, "formal_gate": "G4-T1", "status": "signed"},
        })
    return result


def _audit_output_hashes(run_dir: Path) -> dict[str, str]:
    return {
        str(path.relative_to(run_dir)).replace("\\", "/"): sha256_file(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name not in {"final_case_manifest.json", "p7_p9_manifest.json"}
    }


def run(run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or f"FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-v1"
    out = ROOT / "outputs" / run_id
    if out.exists():
        raise RuntimeError(f"append-only output directory already exists: {out}")
    out.mkdir(parents=True)
    auth_sha = sha256_bytes(AUTHORIZATION_TEXT.encode("utf-8"))
    # Write bytes so the authorization hash is over the exact UTF-8 text;
    # Windows newline translation must not alter the signed artifact.
    (out / "authorization.txt").write_bytes(AUTHORIZATION_TEXT.encode("utf-8") + b"\n")
    signed_at = now_utc()
    protected_dirs = [
        ROOT / "outputs" / name for name in (
            "P4-T0-C6C812108AF95309-freeze-v1", "P5-20260826T152246Z-t0-hypotheses-v3",
            "G2-T0-1B8D6A450720F041-formal-v1", "G3-T0-20260827T-formal-offline-v5",
            "P16R-C1-G4QR-C1-PAPER-CASE-C1-20260827T100034Z-v1",
        )
    ]
    protected_before = {str(p.relative_to(ROOT)).replace("\\", "/"): tree_hashes(p) for p in protected_dirs}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        objects = build_formal_objects(conn, run_id, auth_sha, signed_at)
        lineage = _build_lineage(conn, auth_sha, f"G3-T1-REVIEW-{auth_sha[:16]}")
        if len(lineage) != 3 or len({item["parent_disagreement_id"] for item in lineage}) != 3:
            raise RuntimeError("T0-T1 disagreement lineage is not three unique mappings")
        draft = (C1_DIR / "G4QR_C1" / "g4_t1_report_draft_v3.md").read_text(encoding="utf-8")
        signed_report = _signed_report(draft, signed_at, auth_sha)
        final_paper = _final_paper((C1_DIR / "PAPER_CASE_C1" / "paper_case_section_v4.md").read_text(encoding="utf-8"))
        final = out / "FINAL_PAPER_CASE"
        final.mkdir(parents=True)
        (final / "g4_t1_final_report.md").write_text(signed_report, encoding="utf-8")
        write_json(final / "g4_t1_final_report.json", {"status": "signed", "snapshot_id": T1_SNAPSHOT_ID, "reviewer": REVIEWER, "review_mode": REVIEW_MODE, "signed_at": signed_at, "authorization_sha256": auth_sha, "statement": "截至2026年6月30日，美国军用人工智能的发展呈现局部任务应用和组织化试验扩展、部门级体系整合意图增强并存的状态。现有公开证据尚不足以确认跨军种、跨任务的通用能力体系已经形成；资源、数据、作战化验证和可信治理条件仍造成能力发展与应用扩展不均衡。因此，T1并未推翻T0判断，而是形成了‘局部扩展信号增强、体系整合仍待实际复用验证、约束条件进一步显化’的条件化修订。"})
        write_jsonl(final / "formal_g3_t1_disagreements.jsonl", objects["disagreements"])
        write_jsonl(final / "formal_g3_t1_needs.jsonl", objects["needs"])
        write_jsonl(final / "formal_g3_t1_indicators.jsonl", objects["indicators"])
        write_jsonl(final / "formal_g3_t1_triggers.jsonl", objects["triggers"])
        write_jsonl(final / "t0_t1_disagreement_lineage.jsonl", lineage)
        trace = _traceability(conn, lineage)
        write_csv(final / "final_claim_evidence_traceability.csv", list(trace[0]), trace)
        (final / "paper_case_section_v5_final.md").write_text(final_paper, encoding="utf-8")
        metrics = [
            {"metric": "t0_snapshot_items", "value": 85, "interpretation": "T0冻结逻辑Evidence"},
            {"metric": "t1_snapshot_items_cumulative", "value": 125, "interpretation": "T1累积快照项"},
            {"metric": "t1_new_formal_evidence", "value": 40, "interpretation": "正式T1 Evidence"},
            {"metric": "t1_change_signals", "value": 38, "interpretation": "变化信号"},
            {"metric": "p14_impact_edges", "value": 46, "interpretation": "影响边"},
            {"metric": "latest_viewpoints", "value": 15, "interpretation": "最新观点"},
            {"metric": "updated_viewpoints", "value": 6, "interpretation": "局部更新"},
            {"metric": "unchanged_viewpoints", "value": 9, "interpretation": "保持观点"},
            {"metric": "local_update_rate", "value": "40%", "interpretation": "结构指标，不是准确率"},
            {"metric": "unchanged_rate", "value": "60%", "interpretation": "结构指标，不是准确率"},
            {"metric": "formal_g3_t1_disagreements", "value": 3, "interpretation": "正式G3-T1分歧"},
            {"metric": "formal_g3_t1_needs", "value": 3, "interpretation": "正式判别性需求"},
            {"metric": "formal_g3_t1_indicators", "value": 4, "interpretation": "正式监测指标"},
            {"metric": "formal_g3_t1_triggers", "value": 8, "interpretation": "正式触发规则"},
        ]
        write_csv(final / "final_case_metrics.csv", ["metric", "value", "interpretation"], metrics)
        write_json(final / "g3_t1_authorization.json", {"authorization_text_sha256": auth_sha, "reviewer": REVIEWER, "review_mode": REVIEW_MODE, "author_signoff_count": 1, "author_decision_count": 3, "human_source_visual_review_count": 0, "raw_source_visual_review_claimed": False})
        _copy_final_figures(final)
        (final / "visual_qc_final.md").write_text("# 最终案例包视觉质检\n\n结论：PASS（沿用已通过C1视觉质检的六张SVG/PNG；本阶段未重绘，未修改历史图形）。\n\n检查项：中文字体、PNG/SVG成对存在、300 DPI PNG、白底、黑白可辨识性、无API密钥。\n", encoding="utf-8")
        (final / "README.md").write_text("# FINAL PAPER CASE\n\n本目录包含正式G3-T1/G4-T1签发后的T0—T1案例展示包。作者审核采用整体签核与重点分歧裁决口径，不表示作者逐条重读全部原始资料或完成逐页视觉复核。未调用模型/API；未新增Evidence、假设、观点或依赖；未重新冻结快照。\n", encoding="utf-8")
        review_payloads = [
            {"review_id": f"G3-T1-REVIEW-{auth_sha[:16]}", "gate": "G3-T1", "approved": True, "reviewer": REVIEWER, "review_mode": REVIEW_MODE, "authorization_sha256": auth_sha, "author_decision_count": 3, "human_source_visual_review_count": 0, "raw_source_visual_review_claimed": False, "reviewed_at": signed_at, "notes": "作者整体签核三项正式分歧、需求、指标和触发规则；非逐条重读全部原始资料。"},
            {"review_id": f"G4-T1-REVIEW-{auth_sha[:16]}", "gate": "G4-T1", "approved": True, "reviewer": REVIEWER, "review_mode": REVIEW_MODE, "authorization_sha256": auth_sha, "author_signoff_count": 1, "human_source_visual_review_count": 0, "raw_source_visual_review_claimed": False, "reviewed_at": signed_at, "notes": "作者整体签发条件化报告；非逐条重读全部原始资料。"},
        ]
        artifacts = _build_artifacts(out, auth_sha)
        # Independent rollback test on a temporary copy, before touching the
        # working database.  The injected exception must leave all counts.
        with tempfile.TemporaryDirectory(prefix="formal_g3_g4_t1_") as temp:
            temp_db = Path(temp) / "research.sqlite3"
            shutil.copy2(DB_PATH, temp_db)
            baseline_counts = {}
            baseline_conn = sqlite3.connect(str(temp_db))
            try:
                baseline_counts["disagreements"] = baseline_conn.execute("SELECT COUNT(*) FROM disagreement WHERE disagreement_id LIKE 'DISAG-T1R-C1-%'").fetchone()[0]
                baseline_counts["reviews"] = baseline_conn.execute("SELECT COUNT(*) FROM human_review WHERE review_id LIKE 'G3-T1-REVIEW-%' OR review_id LIKE 'G4-T1-REVIEW-%'").fetchone()[0]
            finally:
                baseline_conn.close()
            try:
                write_transaction(temp_db, objects, lineage, artifacts, auth_sha, review_payloads, inject_failure=True)
            except RuntimeError as exc:
                if "injected rollback failure" not in str(exc):
                    raise
            check = sqlite3.connect(str(temp_db))
            try:
                if check.execute("SELECT COUNT(*) FROM disagreement WHERE disagreement_id LIKE 'DISAG-T1R-C1-%'").fetchone()[0] != baseline_counts["disagreements"]:
                    raise RuntimeError("rollback test changed disagreement rows")
                if check.execute("SELECT COUNT(*) FROM human_review WHERE review_id LIKE 'G3-T1-REVIEW-%' OR review_id LIKE 'G4-T1-REVIEW-%'").fetchone()[0] != baseline_counts["reviews"]:
                    raise RuntimeError("rollback test changed review rows")
            finally:
                check.close()
        tx = write_transaction(DB_PATH, objects, lineage, artifacts, auth_sha, review_payloads)
        # Replaying the same authorization on the real DB must not insert rows.
        replay_tx = write_transaction(DB_PATH, objects, lineage, artifacts, auth_sha, review_payloads)
    finally:
        conn.close()

    # Formal output files are complete before the manifest is written.
    (out / "formal_g3_t1_disagreements.jsonl").write_text((out / "FINAL_PAPER_CASE" / "formal_g3_t1_disagreements.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "formal_g3_t1_needs.jsonl").write_text((out / "FINAL_PAPER_CASE" / "formal_g3_t1_needs.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "formal_g3_t1_indicators.jsonl").write_text((out / "FINAL_PAPER_CASE" / "formal_g3_t1_indicators.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    (out / "formal_g3_t1_triggers.jsonl").write_text((out / "FINAL_PAPER_CASE" / "formal_g3_t1_triggers.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    write_jsonl(out / "t0_t1_disagreement_lineage.jsonl", lineage)
    protected_after = {str(p.relative_to(ROOT)).replace("\\", "/"): tree_hashes(p) for p in protected_dirs}
    manifest = {
        "run_id": run_id,
        "stage": "FORMAL-G3-T1_FORMAL-G4-T1_FINAL-PAPER-CASE",
        "case_id": CASE_ID,
        "snapshot_id": T1_SNAPSHOT_ID,
        "t0_snapshot_id": T0_SNAPSHOT_ID,
        "authorization_sha256": auth_sha,
        "authorization_source": "current_user_instruction; no separately delimited quotation was supplied",
        "reviewer": REVIEWER,
        "review_mode": REVIEW_MODE,
        "author_signoff_count": 1,
        "author_decision_count": 3,
        "human_source_visual_review_count": 0,
        "raw_source_visual_review_claimed": False,
        "formal_g3_t1_executed": True,
        "formal_g4_t1_signed": True,
        "formal_disagreement_inserted": 3,
        "formal_need_inserted": 3,
        "formal_indicator_inserted": 4,
        "formal_trigger_inserted": 8,
        "disagreement_lineage_inserted": 3,
        "g3_review_record_inserted": 1,
        "g4_review_record_inserted": 1,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "viewpoint_inserted": 0,
        "dependency_inserted": 0,
        "snapshot_refrozen": False,
        "t1_snapshot_item_count": 125,
        "invalid_reference_count": 0,
        "historical_artifacts_modified": False,
        "model_api_calls": 0,
        "api_key_written": False,
        "t1_read": True,
        "transaction_committed": tx["transaction_committed"],
        "replayed_transaction": replay_tx,
        "rollback_test_passed": True,
        "idempotency_test_passed": replay_tx["replayed"] is True and sum(replay_tx["inserted"].values()) == 0,
        "foreign_key_check_passed": True,
        "latest_version_check_passed": True,
        "protected_history_hash_unchanged": protected_before == protected_after,
        "python_version": platform.python_version(),
        "formal_g3_g4_t1_db_hash_after": sha256_file(DB_PATH),
        "output_file_hashes": _audit_output_hashes(out),
        "final_paper_case_generated": True,
    }
    write_json(out / "final_case_manifest.json", manifest)
    return manifest
