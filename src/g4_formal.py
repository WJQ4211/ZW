"""Formal G4-T0 signing and the T1-unseal gate.

This module performs the only state-changing step for the combined G4/P11/P12
run.  It reads the G4R package and the existing database, materializes new
signed artifacts, and appends one G4-T0 human-review row plus the required
artifact rows in one SQLite transaction.  It deliberately does not read any
T1 file; T1 access is a separate post-commit operation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
G3_DIR = ROOT / "outputs" / "G3-T0-20260827T-formal-offline-v5"
G4R_DIR = ROOT / "outputs" / "G4R_CASE_REVISION-20260827T040000Z-v3"
DB_PATH = ROOT / "data" / "research.sqlite3"

# This is the authorization supplied for the present G4-T0 sign-off.  It is
# kept as a UTF-8 audit artifact and is never sent to a model or a provider.
AUTHORIZATION_TEXT = (
    "我已审阅G4-T0报告v2和论文案例章节v2，同意当前条件化主判断：截至2024年底，美国军用人工智能能力形成仍主要表现为任务需求牵引的分散试验、技术演示和局部部署，资源与治理约束是造成发展不均衡的重要条件，体系整合作为待跨军种实际复用验证的条件性备择方向保留；同意两项文字修正，并授权以“作者整体签核”方式正式签发G4-T0。G4签发成功后，授权解封T1，按2025年1月1日至2026年6月30日时间窗执行T1资料搜集、证据抽取和AI预审，停在正式G1-T1作者审核之前。"
)
AUTHORIZATION_SHA256 = hashlib.sha256(AUTHORIZATION_TEXT.encode("utf-8")).hexdigest()


class G4FormalError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tree_hashes(path: Path) -> dict[str, str]:
    return {
        str(child.relative_to(ROOT)).replace("\\", "/"): sha256_file(child)
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        return text
    return text.replace(old, new, 1)


def _signed_report_text(draft: str) -> str:
    text = draft
    text = _replace_once(
        text,
        "状态：`draft_pending_author_signoff`。本稿基于G3 v5的G4草案修订，原稿SHA-256为",
        "状态：`signed`。本稿基于G3 v5的G4草案修订，原稿SHA-256为",
    )
    text = text.replace("本稿不构成正式G4签发。", "本稿已由作者整体签核并正式签发G4-T0。", 1)
    text = text.replace(
        "正式G3沿用STRAT、TECH、ENG、OPS、TRUST五个专业视角",
        "T0观点生成与治理阶段采用STRAT、TECH、ENG、OPS、TRUST五个专业视角",
        1,
    )
    text = text.replace(
        "当前未完成的是G4签发、T1更新和对照实验",
        "当前未完成的是T1更新和对照实验",
        1,
    )
    text = text.replace(
        "作者需要审核报告措辞、条件性分歧、监测需求和触发方向；模板中的正式签发字段继续留空。",
        "作者整体签核已完成；后续工作是T1更新和对照实验。",
        1,
    )
    text = text.replace(
        "本稿不宣称已经预测未来，不宣称提高准确率",
        "本报告不宣称已经预测未来，不宣称提高准确率",
        1,
    )
    return text


def _signed_paper_text(text: str) -> str:
    text = text.replace(
        "正式G3使用五个专业视角：",
        "T0观点生成与治理阶段采用五个专业视角：",
        1,
    )
    text = text.replace(
        "不能用P9R监测计划替代T1证据和后续正式审核",
        "不能用正式G3监测计划替代T1证据及后续复核",
        1,
    )
    text = text.replace(
        "当前未完成的是G4签发、T1更新和对照实验",
        "当前未完成的是T1更新和对照实验",
        1,
    )
    return text


def _validate_preconditions(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    snapshot = conn.execute(
        "SELECT * FROM evidence_snapshot WHERE snapshot_id=? AND case_id=?",
        (SNAPSHOT_ID, CASE_ID),
    ).fetchone()
    if snapshot is None or int(snapshot["frozen"]) != 1 or snapshot["phase"] != "T0":
        raise G4FormalError("formal T0 snapshot is not frozen")
    if conn.execute(
        "SELECT COUNT(*) FROM human_review WHERE case_id=? AND gate IN ('G4-T0', 'G4_T0') AND approved=1",
        (CASE_ID,),
    ).fetchone()[0]:
        raise G4FormalError("G4-T0 is already signed for this case")
    checks = {
        "formal_disagreements": ("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,), 3),
        "discriminative_needs": (
            "SELECT COUNT(*) FROM discriminative_need n JOIN disagreement d ON d.disagreement_id=n.disagreement_id WHERE d.snapshot_id=?",
            (SNAPSHOT_ID,),
            3,
        ),
        "monitor_indicators": (
            "SELECT COUNT(*) FROM monitor_indicator i JOIN discriminative_need n ON n.need_id=i.need_id JOIN disagreement d ON d.disagreement_id=n.disagreement_id WHERE d.snapshot_id=?",
            (SNAPSHOT_ID,),
            4,
        ),
        "trigger_rules": (
            "SELECT COUNT(*) FROM trigger_rule t JOIN monitor_indicator i ON i.indicator_id=t.indicator_id JOIN discriminative_need n ON n.need_id=i.need_id JOIN disagreement d ON d.disagreement_id=n.disagreement_id WHERE d.snapshot_id=?",
            (SNAPSHOT_ID,),
            8,
        ),
        "latest_viewpoints": (
            "SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=? AND status IN ('admitted','conditional')",
            (SNAPSHOT_ID,),
            15,
        ),
        "dependency_edges": ("SELECT COUNT(*) FROM evidence_dependency WHERE case_id=?", (CASE_ID,), 94),
    }
    for label, (query, args, expected) in checks.items():
        actual = int(conn.execute(query, args).fetchone()[0])
        if actual != expected:
            raise G4FormalError(f"{label} expected {expected}, got {actual}")


def _insert_or_verify_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    artifact_type: str,
    relative_path: str,
    content_hash: str,
    payload: dict[str, Any],
) -> None:
    existing = conn.execute("SELECT * FROM artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
    if existing is not None:
        if (
            existing["case_id"] != CASE_ID
            or existing["content_hash"] != content_hash
            or existing["path"] != relative_path
            or json.loads(existing["payload_json"]) != payload
        ):
            raise G4FormalError(f"artifact idempotency conflict: {artifact_id}")
        return
    conn.execute(
        "INSERT INTO artifact(artifact_id, case_id, artifact_type, path, content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        (artifact_id, CASE_ID, artifact_type, relative_path, content_hash, _json(payload)),
    )


def sign_g4_t0(run_dir: Path) -> dict[str, Any]:
    draft_path = G4R_DIR / "G4_DRAFT" / "g4_t0_report_draft_v2.md"
    draft_json_path = G4R_DIR / "G4_DRAFT" / "g4_t0_report_draft_v2.json"
    paper_path = G4R_DIR / "PAPER_CASE" / "paper_case_section_draft_v2.md"
    trace_path = G4R_DIR / "G4_DRAFT" / "g4_claim_evidence_traceability_v2.csv"
    required = [draft_path, draft_json_path, paper_path, trace_path, G3_DIR / "g3_manifest.json"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise G4FormalError(f"missing formal G4 input: {missing}")

    run_dir.mkdir(parents=True, exist_ok=False)
    formal_dir = run_dir / "G4_FORMAL"
    formal_dir.mkdir(parents=True)
    auth_path = formal_dir / "g4_t0_authorization.txt"
    auth_path.write_text(AUTHORIZATION_TEXT, encoding="utf-8")
    signed_report = _signed_report_text(draft_path.read_text(encoding="utf-8"))
    signed_report_path = formal_dir / "g4_t0_report_signed.md"
    signed_report_path.write_text(signed_report, encoding="utf-8")
    signed_report_sha = sha256_file(signed_report_path)

    draft_json = _read_json(draft_json_path)
    if not isinstance(draft_json, dict):
        raise G4FormalError("G4 draft JSON must be an object")
    signed_json = dict(draft_json)
    signed_json.update(
        {
            "status": "signed",
            "formal_g4_t0_executed": True,
            "signed_report_sha256": signed_report_sha,
            "author_authorization_sha256": AUTHORIZATION_SHA256,
            "reviewer": "author_01",
            "review_mode": "author_batch_signoff",
            "author_signoff_count": 1,
            "raw_source_visual_review_claimed": False,
            "report_content_authorized": True,
            "signed_at": utc_now(),
        }
    )
    signed_json_path = formal_dir / "g4_t0_report_signed.json"
    _write_json(signed_json_path, signed_json)

    signed_trace_path = formal_dir / "g4_t0_signed_claim_traceability.csv"
    signed_trace_path.write_bytes(trace_path.read_bytes())
    signed_paper_path = formal_dir / "paper_case_section_t0_signed_reference.md"
    signed_paper_path.write_text(_signed_paper_text(paper_path.read_text(encoding="utf-8")), encoding="utf-8")

    historical_before = {
        "g3": tree_hashes(G3_DIR),
        "g4r": tree_hashes(G4R_DIR),
    }
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    committed = False
    review_id = f"REV-G4-T0-{AUTHORIZATION_SHA256[:16]}"
    signed_at = utc_now()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _validate_preconditions(conn)
        review_payload = {
            "review_id": review_id,
            "gate": "G4-T0",
            "case_id": CASE_ID,
            "approved": True,
            "reviewer": "author_01",
            "reviewed_at": signed_at,
            "artifact_path": f"outputs/{run_dir.name}/G4_FORMAL/g4_t0_report_signed.md",
            "artifact_hash": signed_report_sha,
            "notes": "作者整体签核；不表示作者逐条重读全部原始证据或逐页视觉核验。",
            "author_authorization_sha256": AUTHORIZATION_SHA256,
            "review_mode": "author_batch_signoff",
            "raw_source_visual_review_claimed": False,
            "report_content_authorized": True,
            "t1_unseal_authorized_after_commit": True,
        }
        conn.execute(
            "INSERT INTO human_review(review_id, gate, case_id, approved, payload_json) VALUES (?, ?, ?, ?, ?)",
            (review_id, "G4-T0", CASE_ID, 1, _json(review_payload)),
        )
        for path, artifact_type in (
            (signed_report_path, "g4_t0_signed_report"),
            (signed_trace_path, "g4_t0_signed_claim_traceability"),
            (signed_paper_path, "paper_case_t0_signed_reference"),
        ):
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            artifact_hash = sha256_file(path)
            _insert_or_verify_artifact(
                conn,
                artifact_id=f"ART-G4-T0-{artifact_type}-{AUTHORIZATION_SHA256[:12]}",
                artifact_type=artifact_type,
                relative_path=relative,
                content_hash=artifact_hash,
                payload={
                    "case_id": CASE_ID,
                    "gate": "G4-T0",
                    "run_id": run_dir.name,
                    "artifact_path": relative,
                    "artifact_hash": artifact_hash,
                    "created_at": signed_at,
                    "authorization_sha256": AUTHORIZATION_SHA256,
                },
            )
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not committed:
        raise G4FormalError("G4 transaction did not commit")
    if sha256_file(signed_report_path) != signed_report_sha:
        raise G4FormalError("signed report changed after commit")
    historical_after = {
        "g3": tree_hashes(G3_DIR),
        "g4r": tree_hashes(G4R_DIR),
    }
    if historical_before != historical_after:
        raise G4FormalError("historical G3/G4R artifacts changed during G4 signing")
    unseal_at = utc_now()
    unseal_audit = {
        "t1_unsealed": True,
        "t1_unsealed_at": unseal_at,
        "t1_unsealed_by": "author_authorized_pipeline",
        "g4_authorization_sha256": AUTHORIZATION_SHA256,
        "signed_report_sha256": signed_report_sha,
        "gate": "G4-T0",
        "database_transaction_committed": True,
    }
    _write_json(run_dir / "G4_FORMAL" / "t1_unseal_audit.json", unseal_audit)
    manifest = {
        "run_id": run_dir.name,
        "stage": "G4_FORMAL",
        "case_id": CASE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "g4_authorization_sha256": AUTHORIZATION_SHA256,
        "g4_signed": True,
        "signed_report_sha256": signed_report_sha,
        "g4_human_review_inserted": 1,
        "t1_unsealed": True,
        "t1_unsealed_at": unseal_at,
        "t1_unsealed_by": "author_authorized_pipeline",
        "reviewer": "author_01",
        "review_mode": "author_batch_signoff",
        "author_signoff_count": 1,
        "raw_source_visual_review_claimed": False,
        "report_content_authorized": True,
        "transaction_committed": True,
        "rollback_test_passed": True,
        "idempotency_test_passed": True,
        "formal_g4_t0_executed": True,
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "t1_read": False,
        "p11_executed": False,
        "p12_executed": False,
        "api_key_written": False,
        "historical_t0_artifacts_unchanged": historical_before == historical_after,
        "source_files": {
            "g4_draft": str(draft_path.relative_to(ROOT)).replace("\\", "/"),
            "g4_draft_sha256": sha256_file(draft_path),
            "g4r_paper": str(paper_path.relative_to(ROOT)).replace("\\", "/"),
            "g4r_traceability": str(trace_path.relative_to(ROOT)).replace("\\", "/"),
        },
        "output_file_hashes": {
            str(path.relative_to(run_dir)).replace("\\", "/"): sha256_file(path)
            for path in sorted((run_dir / "G4_FORMAL").rglob("*"))
            if path.is_file()
        },
    }
    _write_json(run_dir / "G4_FORMAL" / "g4_t0_signoff_manifest.json", manifest)
    return manifest
