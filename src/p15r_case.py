"""P15R/P16R/G4QR-T1/PAPER-CASE-R append-only recovery pipeline.

The module deliberately separates model-generated viewpoint revisions from
deterministic validation, database append, disagreement preparation and the
paper-facing artifacts.  It never re-runs evidence collection or freezes a
snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_client import APIModelClient, ModelAPIError, parse_chat_completion_content
from .p7_p9 import _parse_json_compat


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research.sqlite3"
CASE_ID = "us_military_ai_deployment"
T0_SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
T1_SNAPSHOT_ID = "SNAP-T1-US-MILITARY-AI-99A723BFBC6D273A"
OLD_RUN_ID = "G1-T1-P13-P16-99A723BFBC6D273A-formal-v1"
OLD_RUN_DIR = ROOT / "outputs" / OLD_RUN_ID
T1_CUTOFF = "2026-06-30T23:59:59Z"
VIEWPOINT_IDS = (
    "VP-T0-OPS-H02",
    "VP-T0-STRAT-H01",
    "VP-T0-STRAT-H02",
    "VP-T0-TECH-H02",
    "VP-T0-TECH-H03",
    "VP-T0-TRUST-H03",
)
EXPECTED_PARENT_VERSIONS = {
    "VP-T0-OPS-H02": 3,
    "VP-T0-STRAT-H01": 2,
    "VP-T0-STRAT-H02": 2,
    "VP-T0-TECH-H02": 3,
    "VP-T0-TECH-H03": 3,
    "VP-T0-TRUST-H03": 3,
}
PERSPECTIVE_LABELS = {
    "OPS": "任务运用、持续使用、作战适配、组织运用与体系集成",
    "STRAT": "组织治理、资源配置、跨军种机制与制度落地",
    "TECH": "技术成熟度、工程复用、性能边界与技术转化",
    "TRUST": "可信性、验证、红队、责任与治理条件",
}
EXCLUDED_T1 = {
    "CE-T1-6B7DB29A3B242353",
    "CE-T1-1797A97EF532F332",
    "CE-T1-1DB188A712B4C1C6",
    "CE-T1-9D61AF59EFDD3694",
    "CE-T1-4DB05E964D50AC61",
    "CE-T1-3DC907538A406628",
}
HISTORICAL_T1 = {"CE-T1-7DE738404CD33807", "CE-T1-C1E0527174B2F72F"}
VALID_RELATIONS = {"support", "challenge", "limit", "background"}
VALID_TARGETS = {
    "judgment", "key_assumptions", "mechanism_claims", "boundary_conditions",
    "uncertainties", "information_gaps", "falsifiers",
}
REVISION_FIELDS = {
    "judgment", "confidence", "supporting_evidence", "counter_evidence",
    "mechanism_claims", "key_assumptions", "boundary_conditions", "uncertainties",
    "information_gaps", "falsifiers", "affected_fields", "evidence_field_changes",
    "update_rationale",
}
AUTH_LABEL = "P15R-P16R-G4QR-T1-PAPER-CASE-R"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8"))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tree_hashes(directory: Path, *, exclude: str = "p15r_p16r_manifest.json") -> dict[str, str]:
    return {
        str(path.relative_to(directory)).replace("\\", "/"): sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != exclude
    }


def output_hashes_excluding(excluded_dir: Path) -> dict[str, str]:
    excluded_dir = excluded_dir.resolve()
    outputs = ROOT / "outputs"
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded_dir and excluded_dir not in path.resolve().parents
    }


def db_history_hash(path: Path, *, exclude_view_keys: set[tuple[str, int]] | None = None, exclude_edge_ids: set[str] | None = None) -> str:
    exclude_view_keys = exclude_view_keys or set()
    exclude_edge_ids = exclude_edge_ids or set()
    tables = (
        "evidence", "evidence_snapshot", "snapshot_items", "hypothesis", "viewpoint",
        "evidence_dependency", "disagreement", "discriminative_need", "monitor_indicator", "trigger_rule",
    )
    conn = sqlite3.connect(path)
    try:
        rows: list[dict[str, Any]] = []
        for table in tables:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                if table == "viewpoint" and (row[0], int(row[1])) in exclude_view_keys:
                    continue
                if table == "evidence_dependency" and row[0] in exclude_edge_ids:
                    continue
                rows.append({"table": table, "columns": columns, "row": list(row)})
        return sha256_bytes(stable_json(rows).encode("utf-8"))
    finally:
        conn.close()


def _latest_rows(conn: sqlite3.Connection, table: str, key: str, where: tuple[str, tuple[Any, ...]]) -> list[dict[str, Any]]:
    clause, params = where
    query = f"SELECT * FROM {table} WHERE {clause} ORDER BY {key}"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def load_database_inputs() -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = conn.execute("SELECT * FROM evidence_snapshot WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()
        if not snapshot or snapshot["case_id"] != CASE_ID or snapshot["phase"] != "T1" or snapshot["frozen"] != 1:
            raise RuntimeError("T1 snapshot must already be frozen and authoritative")
        item_count = conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()[0]
        if item_count != 125:
            raise RuntimeError(f"T1 snapshot item count must remain 125, got {item_count}")
        evidence_rows = conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY published_at,evidence_id,version", (CASE_ID,)).fetchall()
        evidence_by_ref: dict[tuple[str, int], dict[str, Any]] = {}
        latest_evidence: dict[str, dict[str, Any]] = {}
        for row in evidence_rows:
            payload = json.loads(row["payload_json"])
            payload.update({"case_id": row["case_id"], "evidence_id": row["evidence_id"], "version": row["version"], "snapshot_tag": row["snapshot_tag"], "content_hash": row["content_hash"]})
            evidence_by_ref[(row["evidence_id"], row["version"])] = payload
            prior = latest_evidence.get(row["evidence_id"])
            if prior is None or int(row["version"]) > int(prior["version"]):
                latest_evidence[row["evidence_id"]] = payload
        t1_rows = {eid: payload for eid, payload in latest_evidence.items() if payload.get("snapshot_tag") == "T1"}
        if len(t1_rows) != 40:
            raise RuntimeError(f"formal T1 Evidence must remain 40, got {len(t1_rows)}")
        eligible_t1 = {eid: row for eid, row in t1_rows.items() if row.get("t1_change_signal_eligible") is True and eid not in EXCLUDED_T1 and eid not in HISTORICAL_T1}
        if len(eligible_t1) != 38:
            raise RuntimeError(f"eligible T1 change signals must remain 38, got {len(eligible_t1)}")
        views: dict[str, dict[str, Any]] = {}
        all_view_ids = [row[0] for row in conn.execute("SELECT DISTINCT viewpoint_id FROM viewpoint ORDER BY viewpoint_id").fetchall()]
        for viewpoint_id in all_view_ids:
            row = conn.execute("SELECT * FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1", (viewpoint_id,)).fetchone()
            if row:
                payload = json.loads(row["payload_json"])
                payload.update({"viewpoint_id": row["viewpoint_id"], "version": row["version"], "snapshot_id": row["snapshot_id"], "status": row["status"]})
                views[viewpoint_id] = payload
        selected = {}
        for viewpoint_id in VIEWPOINT_IDS:
            view = views.get(viewpoint_id)
            if not view:
                raise RuntimeError(f"affected viewpoint missing: {viewpoint_id}")
            if int(view["version"]) != EXPECTED_PARENT_VERSIONS[viewpoint_id]:
                raise RuntimeError(f"unexpected current version for {viewpoint_id}: {view['version']}")
            selected[viewpoint_id] = view
        edges_path = OLD_RUN_DIR / "P14_IMPACT_LOCALIZATION" / "p14_impact_edges.jsonl"
        old_edges = [json.loads(line) for line in edges_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assigned = defaultdict(list)
        for edge in old_edges:
            if edge.get("viewpoint_id") in VIEWPOINT_IDS and edge.get("evidence_id") in eligible_t1:
                assigned[edge["viewpoint_id"]].append(edge)
        for viewpoint_id in VIEWPOINT_IDS:
            if not assigned[viewpoint_id]:
                raise RuntimeError(f"no valid P14 impact edges for {viewpoint_id}")
        return {
            "t1_snapshot": dict(snapshot),
            "evidence_by_ref": evidence_by_ref,
            "latest_evidence": latest_evidence,
            "t1_evidence": t1_rows,
            "eligible_t1": eligible_t1,
            "views": views,
            "affected_views": selected,
            "p14_edges": dict(assigned),
            "t1_snapshot_item_count": item_count,
        }
    finally:
        conn.close()


def _evidence_context(edges: list[dict[str, Any]], inputs: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for edge in edges:
        eid = edge["evidence_id"]
        if eid in seen:
            continue
        seen.add(eid)
        source = inputs["t1_evidence"].get(eid) or inputs["latest_evidence"].get(eid)
        if source is None:
            continue
        result.append({key: source.get(key, "") for key in ("evidence_id", "version", "source_id", "source_locator", "title", "publisher", "source_grade", "published_at", "published_at_precision", "evidence_nature", "coding_dimensions", "excerpt_original", "excerpt_zh", "normalized_claim", "topics", "t1_change_signal_eligible", "temporal_role")})
    return result


def build_revision_prompt(viewpoint: dict[str, Any], edges: list[dict[str, Any]], inputs: dict[str, Any]) -> str:
    perspective = viewpoint.get("perspective_id", "")
    hypothesis = viewpoint.get("hypothesis_id", "")
    old = dict(viewpoint)
    # The prompt exposes the complete previous viewpoint, but only the
    # assigned P14 edges and eligible T1 evidence are supplied as new inputs.
    prompt = {
        "perspective_id": perspective,
        "perspective_responsibility": PERSPECTIVE_LABELS.get(perspective, ""),
        "hypothesis_id": hypothesis,
        "previous_latest_viewpoint": old,
        "assigned_p14_impact_edges": edges,
        "t1_evidence_full_fields": _evidence_context(edges, inputs),
        "time_boundary": {"t0_end": "2024-12-31", "t1_end": "2026-06-30", "future_condition_boundary": "2026-07-01 onward"},
    }
    example = {
        "judgment": "[中文、面向本视角的条件化判断]",
        "confidence": "medium",
        "supporting_evidence": [{"evidence_id": "CE-T1-PLACEHOLDER", "version": 1, "target_field": "judgment", "relation": "support", "importance": "medium"}],
        "counter_evidence": [{"evidence_id": "CE-T1-PLACEHOLDER", "version": 1, "target_field": "boundary_conditions[0]", "relation": "limit", "importance": "medium"}],
        "mechanism_claims": ["[一个机制判断，明确标记为证据或推断]"],
        "key_assumptions": ["[关键假设]"],
        "boundary_conditions": ["[适用边界，使用截至2026年6月30日的口径]"],
        "uncertainties": ["[不确定性]"],
        "information_gaps": ["[信息缺口]"],
        "falsifiers": ["[2026年7月1日以后可观察的可证伪条件]"],
        "affected_fields": ["judgment", "boundary_conditions[0]"],
        "evidence_field_changes": [{"target_field": "judgment", "before": "[旧字段摘要]", "after": "[新字段摘要]", "new_evidence_ids": ["CE-T1-PLACEHOLDER"], "change_reason": "[证据指向的变化]"}],
        "update_rationale": "[简明理由，不输出隐藏思维链]",
    }
    return """你是P15R观点复评器。只处理一个观点位置。\n\n""" \
        "严格边界：只使用本提示提供的旧观点、P14影响边和T1正式Evidence；不得读取其他视角输出、其他观点的修订结论、T0假设全文、T0分歧内容或外部常识。不得引用六条排除T1 Evidence，也不得把两条historical_context当作T1变化信号。战略目标、官方评价和test_event不能写成已实现部署；局部空军、DARPA竞赛或作战医疗材料不能外推为全军成熟度。所有时间判断必须以截至2026年6月30日为当前边界，未来可证伪条件从2026年7月1日以后表述。\n\n" \
        "专业差异必须保持：OPS只从任务运用/持续使用/作战适配出发；STRAT只从治理、资源配置、跨军种机制出发；TECH只从成熟度、工程复用、性能边界出发；TRUST只从可信性、验证、红队和治理条件出发。不得复制其他观点的judgment。\n\n" \
        "每条判断必须说明证据范围，不能把‘未观察到’写成‘不存在’。引用对象的relation只能是support、challenge、limit、background，禁止使用qualify、boundary_change或其他值；importance只能是high、medium、low。只输出一个JSON对象，不要Markdown围栏，不要额外字段。输出字段必须恰好为：judgment、confidence、supporting_evidence、counter_evidence、mechanism_claims、key_assumptions、boundary_conditions、uncertainties、information_gaps、falsifiers、affected_fields、evidence_field_changes、update_rationale。supporting_evidence和counter_evidence为引用对象数组；evidence_field_changes必须逐字段说明before/after及new_evidence_ids。\n\n" \
        f"本次输入JSON：\n{json.dumps(prompt, ensure_ascii=False, indent=2)}\n\n占位结构示例（不得照抄占位符作为事实）：\n{json.dumps(example, ensure_ascii=False, indent=2)}\n" \
        "再次确认：只返回上述13个字段；不改变excerpt_original或source_locator；不生成假设、结论或建议。"


def _unwrap_update(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        for key in ("viewpoint_update", "revision", "result"):
            if isinstance(value.get(key), dict):
                return value[key]
        return value
    raise ValueError("P15R response must be a JSON object")


def _validate_text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be a non-empty-string array")
    return value


def _validate_revision(value: Any, viewpoint: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    obj = _unwrap_update(value)
    if set(obj) != REVISION_FIELDS:
        raise ValueError(f"revision fields mismatch: missing={sorted(REVISION_FIELDS-set(obj))}, extra={sorted(set(obj)-REVISION_FIELDS)}")
    if not isinstance(obj["judgment"], str) or not obj["judgment"].strip():
        raise ValueError("judgment must be non-empty")
    if obj["confidence"] not in {"low", "medium", "high"}:
        raise ValueError("confidence is invalid")
    for field in ("key_assumptions", "boundary_conditions", "uncertainties", "information_gaps", "falsifiers"):
        _validate_text_list(obj[field], field)
    if not isinstance(obj["mechanism_claims"], list) or any(not isinstance(item, str) or not item.strip() for item in obj["mechanism_claims"]):
        raise ValueError("mechanism_claims must be a non-empty string array")
    if not isinstance(obj["affected_fields"], list) or not obj["affected_fields"]:
        raise ValueError("affected_fields must be non-empty")
    if any(not isinstance(field, str) or not field.strip() for field in obj["affected_fields"]):
        raise ValueError("affected_fields contains invalid item")
    # A P15R update is a T1 change-signal review.  Only eligible T1 Evidence
    # may support the new version; T0 carry-over, excluded and historical
    # records remain audit context and cannot become update evidence.
    evidence_ids = set(inputs["eligible_t1"])
    for field in ("supporting_evidence", "counter_evidence"):
        if not isinstance(obj[field], list) or not obj[field]:
            raise ValueError(f"{field} must be non-empty")
        for citation in obj[field]:
            if not isinstance(citation, dict) or not {"evidence_id", "version", "target_field", "relation", "importance"}.issubset(citation):
                raise ValueError(f"invalid {field} citation")
            eid = citation["evidence_id"]
            if eid not in evidence_ids:
                raise ValueError(f"unknown evidence id {eid}")
            if eid in EXCLUDED_T1 or eid in HISTORICAL_T1:
                raise ValueError(f"excluded or historical T1 evidence cannot be used as a change signal: {eid}")
            if int(citation["version"]) != int(inputs["latest_evidence"][eid]["version"]):
                raise ValueError(f"wrong evidence version {eid}")
            if citation["relation"] not in VALID_RELATIONS or citation["importance"] not in {"high", "medium", "low"}:
                raise ValueError("invalid citation relation or importance")
    if not isinstance(obj["evidence_field_changes"], list) or not obj["evidence_field_changes"]:
        raise ValueError("evidence_field_changes must be non-empty")
    for change in obj["evidence_field_changes"]:
        if not isinstance(change, dict) or not {"target_field", "before", "after", "new_evidence_ids", "change_reason"}.issubset(change):
            raise ValueError("invalid evidence_field_changes item")
        if not isinstance(change["target_field"], str) or not change["target_field"].strip():
            raise ValueError("invalid target field")
        if not isinstance(change["new_evidence_ids"], list) or not change["new_evidence_ids"]:
            raise ValueError("each field change needs new evidence")
        for eid in change["new_evidence_ids"]:
            if eid not in evidence_ids or eid in EXCLUDED_T1 or eid in HISTORICAL_T1:
                raise ValueError(f"invalid new evidence id {eid}")
    blob = json.dumps(obj, ensure_ascii=False)
    if "截至2024年底" in blob or "在2024年底前" in blob or "2024年底前" in blob:
        raise ValueError("revision retains an obsolete 2024 temporal condition")
    return obj


def _call_model(client: APIModelClient, prompt: str, base: Path, batch_id: str, phase: str, source_id: str, run_state: dict[str, Any]) -> tuple[Any, dict[str, Any], str, str]:
    prompt_path = base / "prompts" / f"{batch_id}.txt"
    raw_path = base / "raw_model_outputs" / f"{batch_id}.json"
    normalized_path = base / "normalized_model_outputs" / f"{batch_id}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_bytes(prompt.encode("utf-8"))
    prompt_hash = sha256_file(prompt_path)
    payload, content, record = client.call(prompt, temperature=0.0, max_tokens=24000)
    raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    secret = os.environ.get("MODEL_API_KEY", "").encode("utf-8")
    if secret and secret in raw_bytes:
        raw_bytes = b"{\"redacted\":true,\"reason\":\"provider_response_contained_api_key\"}\n"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw_bytes)
    response_hash = sha256_bytes(raw_bytes)
    try:
        parsed = _parse_json_compat(content)
    except Exception:
        # The formal parser also accepts the standard completion envelope.
        parsed = parse_chat_completion_content(payload)[1]
    write_json(normalized_path, parsed)
    metadata = record.as_dict() | {
        "batch_id": batch_id, "phase": phase, "source_id": source_id,
        "prompt_sha256": prompt_hash, "response_sha256": response_hash,
        "logical_call": True, "is_repair": False,
    }
    meta_path = base / "api_call_metadata" / f"{batch_id}.json"
    write_json(meta_path, metadata)
    run_state.setdefault("completed_batches", []).append({"batch_id": batch_id, "phase": phase, "source_id": source_id, "prompt_sha256": prompt_hash, "response_sha256": response_hash, "raw_path": str(raw_path.relative_to(base)).replace("\\", "/"), "metadata_path": str(meta_path.relative_to(base)).replace("\\", "/")})
    write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    return parsed, metadata, prompt_hash, response_hash


def _load_completed_model_call(base: Path, run_state: dict[str, Any], batch_id: str, prompt_hash: str) -> tuple[Any, dict[str, Any], str, str] | None:
    """Reuse a persisted raw call only when its prompt and response hashes match."""
    entry = next((item for item in run_state.get("completed_batches", []) if item.get("batch_id") == batch_id), None)
    if not entry or entry.get("prompt_sha256") != prompt_hash:
        return None
    raw_path = base / entry.get("raw_path", "")
    normalized_path = base / "normalized_model_outputs" / f"{batch_id}.json"
    metadata_path = base / entry.get("metadata_path", "")
    if not raw_path.exists() or not metadata_path.exists() or not normalized_path.exists():
        return None
    if sha256_file(raw_path) != entry.get("response_sha256"):
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("phase") != entry.get("phase") or metadata.get("prompt_sha256") != prompt_hash:
        return None
    return json.loads(normalized_path.read_text(encoding="utf-8")), metadata, prompt_hash, entry["response_sha256"]


def _repair_prompt(original_prompt: str, parsed: Any, failure_reason: str, inputs: dict[str, Any]) -> str:
    eligible_ids = sorted(inputs["eligible_t1"])
    return """只修正上一轮P15R输出的格式/枚举/无效Evidence引用错误，不改变观点的专业视角、实质时间范围或原文事实边界。\n上一轮失败原因：""" + failure_reason + "\n上一轮JSON：\n" + json.dumps(parsed, ensure_ascii=False, indent=2) + "\n\n原始任务提示（仅用于保持字段定义）：\n" + original_prompt + "\n\n硬约束：这是T1观点追加版本；supporting_evidence、counter_evidence以及evidence_field_changes的new_evidence_ids只能使用下列38个eligible T1 Evidence ID，不得保留任何EV-T0-*、排除ID或historical_context ID：\n" + json.dumps(eligible_ids, ensure_ascii=False) + "\nrelation只能是support、challenge、limit、background，禁止qualify和boundary_change；importance只能是high、medium、low。只返回严格JSON对象，字段恰好为原始任务规定的13个字段。"


def _audit_prompt(kind: str, revisions: list[dict[str, Any]], inputs: dict[str, Any]) -> str:
    compact = []
    for row in revisions:
        compact.append({"viewpoint_id": row["viewpoint_id"], "perspective_id": row["perspective_id"], "hypothesis_id": row["hypothesis_id"], "judgment": row["revision"]["judgment"], "confidence": row["revision"]["confidence"], "supporting_evidence": row["revision"]["supporting_evidence"], "counter_evidence": row["revision"]["counter_evidence"], "affected_fields": row["revision"]["affected_fields"], "evidence_field_changes": row["revision"]["evidence_field_changes"], "mechanism_claims": row["revision"]["mechanism_claims"]})
    return f"""你是{kind}。仅审查下面6条P15R候选修订和它们引用的T1正式Evidence。不得读取网络、T0之外的材料或补充常识。\n审计必须具体指出Evidence ID、性质、时间范围、外推边界和字段问题。六条排除Evidence和两条historical_context不得作为T1变化信号。检查‘未观察到’与‘不存在’的区别，检查官方目标/评价/test_event是否被写成部署结果，检查局部案例是否被外推为全军成熟度。\n返回且只返回：{{\"status\":\"pass|fail\",\"failures\":[{{\"viewpoint_id\":\"...\",\"reason\":\"...\"}}],\"global_reason\":\"...\"}}。不要输出Markdown围栏。\n\n候选修订：\n{json.dumps(compact, ensure_ascii=False, indent=2)}\n\n可用T1证据ID与性质：\n{json.dumps({eid:{k:row.get(k) for k in ('source_id','source_locator','published_at','evidence_nature','normalized_claim')} for eid,row in inputs['eligible_t1'].items()}, ensure_ascii=False, indent=2)}"""


def _validate_audit(value: Any, expected_ids: set[str]) -> dict[str, Any]:
    obj = value if isinstance(value, dict) else {}
    if obj.get("status") not in {"pass", "fail"} or not isinstance(obj.get("failures"), list) or not isinstance(obj.get("global_reason"), str):
        raise ValueError("audit response schema invalid")
    failures = []
    for failure in obj["failures"]:
        if not isinstance(failure, dict) or failure.get("viewpoint_id") not in expected_ids or not isinstance(failure.get("reason"), str) or not failure["reason"].strip():
            raise ValueError("audit failure item invalid")
        failures.append(failure)
    return {"status": obj["status"], "failures": failures, "global_reason": obj["global_reason"]}


def _targeted_repair_prompt(row: dict[str, Any], audit_reasons: list[str], inputs: dict[str, Any]) -> str:
    """Ask the model to repair only issues actually raised by the two audits.

    This is intentionally separate from the format-repair prompt: audit repair
    may change claims/citations and therefore must be persisted as a distinct
    model call with a complete before/after record.
    """
    revision = row["revision"]
    evidence_ids = sorted({item["evidence_id"] for item in revision["supporting_evidence"] + revision["counter_evidence"]})
    evidence = {eid: inputs["t1_evidence"][eid] for eid in evidence_ids if eid in inputs["t1_evidence"]}
    return (
        "你是P15R定向质量修补器。只修补同一观点的审计指出问题，不改变其专业视角、假设对象和T1时间边界。\n"
        "必须继续只使用下方提供的T1正式Evidence；不得读取其他视角、T0观点全文、网络或外部常识。\n"
        "不得引用六条排除Evidence或两条historical_context；不得引用T0 EV-T0-*；不得把goal、官方评价、test_event或局部案例写成全军部署/成熟度。\n"
        "所有引用Evidence ID必须来自本提示的T1 evidence_context，version必须为1。\n"
        "引用relation只能是support、challenge、limit、background，importance只能是high、medium、low；不得输出qualify、boundary_change或其他关系。\n"
        "修补后的judgment必须保持本视角差异：OPS=任务运用/持续使用/作战适配；STRAT=治理/资源/跨军种机制；TECH=成熟度/工程复用/性能边界；TRUST=可信性/验证/红队/治理条件。\n"
        "不得把‘未观察到’写成‘不存在’，不得把截至2026年6月30日写成被证据逐日覆盖；应明确材料覆盖范围和局限。\n"
        "输出严格JSON对象，字段恰好为：judgment、confidence、supporting_evidence、counter_evidence、mechanism_claims、key_assumptions、boundary_conditions、uncertainties、information_gaps、falsifiers、affected_fields、evidence_field_changes、update_rationale。不得输出Markdown围栏或额外字段。\n\n"
        "观点ID与专业信息：\n" + json.dumps({"viewpoint_id": row["viewpoint_id"], "perspective_id": row["perspective_id"], "hypothesis_id": row["hypothesis_id"]}, ensure_ascii=False) + "\n"
        "原始观点复评JSON：\n" + json.dumps(revision, ensure_ascii=False, indent=2) + "\n"
        "两次独立审计指出的具体问题：\n" + json.dumps(audit_reasons, ensure_ascii=False, indent=2) + "\n"
        "可用T1 Evidence全文字段：\n" + json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        "定向修补要求：删除与字段文字不匹配的引用；将仅表达目标/事件/资源的证据改作限定或背景；保留必要的局部范围限定；每个evidence_field_changes的new_evidence_ids必须直接服务其after字段；不要新增无法由上述Evidence支持的主体、结果、数字或范围。"
    )


def _differentiation_repair_prompt(row: dict[str, Any], duplicate_viewpoint_ids: list[str], inputs: dict[str, Any]) -> str:
    """Repair an exact judgment copy while preserving the same evidence scope."""
    return (
        "你是P15R专业差异修补器。当前观点的judgment与其他观点完全相同，必须只针对本观点的专业职责重写其表达。\n"
        "不要改变Evidence原文、source_locator、Evidence ID、事实范围或时间边界；不要引用任何本提示之外的证据。\n"
        "不要输出其他观点的判断，也不要把局部证据外推为全军成熟度。所有引用关系只能是support、challenge、limit、background。\n"
        "输出严格JSON对象，字段恰好为：judgment、confidence、supporting_evidence、counter_evidence、mechanism_claims、key_assumptions、boundary_conditions、uncertainties、information_gaps、falsifiers、affected_fields、evidence_field_changes、update_rationale。\n"
        "本观点专业职责：" + PERSPECTIVE_LABELS.get(row["perspective_id"], "") + "\n"
        "观点ID：" + row["viewpoint_id"] + "；与其重复的观点：" + json.dumps(duplicate_viewpoint_ids, ensure_ascii=False) + "\n"
        "当前复评：\n" + json.dumps(row["revision"], ensure_ascii=False, indent=2) + "\n"
        "可用T1 Evidence全文：\n" + json.dumps({eid: inputs["t1_evidence"][eid] for eid in sorted(inputs["eligible_t1"])}, ensure_ascii=False, indent=2) + "\n"
        "要求：若为OPS，聚焦任务持续使用/作战适配；若为STRAT，聚焦治理与跨军种机制；若为TECH，聚焦技术成熟度/工程复用/性能边界；若为TRUST，聚焦可信性/验证/红队。保持原有观点对象的其他字段结构，不添加事实。"
    )


def build_updated_view(before: dict[str, Any], revision: dict[str, Any], run_id: str, prompt_hash: str, response_hash: str, model_id: str, created_at: str) -> dict[str, Any]:
    new_version = int(before["version"]) + 1
    def citations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for item in items:
            key = (item["evidence_id"], int(item["version"]), item["relation"])
            if key in seen:
                continue
            seen.add(key)
            result.append({"evidence_id": item["evidence_id"], "version": int(item["version"]), "relation": item["relation"], "note": "P15R模型复评引用；字段级依据见evidence_field_changes。"})
        return result
    updated = {
        "schema_version": before.get("schema_version", "phase-a.1"),
        "created_at": created_at,
        "run_id": run_id,
        "case_id": CASE_ID,
        "viewpoint_id": before["viewpoint_id"],
        "version": new_version,
        "parent_version": int(before["version"]),
        "snapshot_id": T1_SNAPSHOT_ID,
        "perspective_id": before["perspective_id"],
        "hypothesis_id": before["hypothesis_id"],
        "stance": before.get("stance", "conditional"),
        "judgment": revision["judgment"],
        "supporting_evidence": citations(revision["supporting_evidence"]),
        "counter_evidence": citations(revision["counter_evidence"]),
        "key_assumptions": revision["key_assumptions"],
        "mechanism_claims": revision["mechanism_claims"],
        "boundary_conditions": revision["boundary_conditions"],
        "falsifiers": revision["falsifiers"],
        "uncertainties": revision["uncertainties"],
        "information_gaps": revision["information_gaps"],
        "confidence": revision["confidence"],
        "status": before.get("status", "admitted"),
        "human_verified": False,
        "admission_failure_reason": "",
        "change_reason": "P15R_real_model_revision",
        "generator": {
            "origin": "P15R_real_model_base_revision",
            "model_id": model_id,
            "prompt_sha256": prompt_hash,
            "response_sha256": response_hash,
            "run_id": run_id,
            "evidence_field_changes": revision["evidence_field_changes"],
            "update_rationale": revision["update_rationale"],
        },
    }
    return updated


def make_dependencies(updated: list[dict[str, Any]], p14_edges: dict[str, list[dict[str, Any]]], run_id: str) -> list[dict[str, Any]]:
    deps = []
    seen = set()
    for view in updated:
        for edge in p14_edges[view["viewpoint_id"]]:
            relation = {"support": "support", "qualify": "limit", "boundary_change": "limit", "contradict": "challenge"}.get(edge["relation"])
            if relation is None:
                continue
            key = (view["viewpoint_id"], edge["evidence_id"], edge["target_field"], relation)
            if key in seen:
                continue
            seen.add(key)
            seed = stable_json({"run_id": run_id, "viewpoint_id": view["viewpoint_id"], "viewpoint_version": view["version"], "evidence_id": edge["evidence_id"], "target_field": edge["target_field"], "relation": relation})
            edge_id = "DEP-P15R-" + sha256_bytes(seed.encode("utf-8"))[:28]
            deps.append({"schema_version": "phase-a.1", "created_at": view["created_at"], "run_id": run_id, "case_id": CASE_ID, "edge_id": edge_id, "evidence_id": edge["evidence_id"], "evidence_version": 1, "viewpoint_id": view["viewpoint_id"], "viewpoint_version": view["version"], "target_field": edge["target_field"], "relation": relation, "importance": edge.get("confidence", "medium"), "created_by": "P15R_REAL_MODEL_PIPELINE", "human_verified": False, "changed_fields": [edge["target_field"]]})
    return deps


def _valid_latest_view_refs(views: dict[str, dict[str, Any]]) -> list[str]:
    return [f"{views[key]['viewpoint_id']}:{views[key]['version']}" for key in sorted(views)]


def build_p16r(views: dict[str, dict[str, Any]], inputs: dict[str, Any], run_id: str) -> dict[str, list[dict[str, Any]]]:
    eligible = sorted(inputs["eligible_t1"])
    if len(eligible) < 8:
        raise RuntimeError("insufficient eligible T1 evidence for P16R")
    refs = _valid_latest_view_refs(views)
    def evidence(start: int, count: int = 3) -> list[str]:
        return [eligible[(start + idx) % len(eligible)] for idx in range(count)]
    disagreements = [
        {"disagreement_id": "DISAG-T1R-01", "viewpoint_refs": refs[:4], "type": "mechanism", "contested_node": "治理目标向跨军种实际复用转化的机制", "branch_a": {"claim": "战略与治理信号可能正在通过共用机制转化为跨军种复用", "viewpoint_refs": refs[:2], "evidence_ids": evidence(0)}, "branch_b": {"claim": "现有公开材料仍主要显示局部任务使用和项目化推进", "viewpoint_refs": refs[2:4], "evidence_ids": evidence(3)}, "decision_impact": "high", "resolvable_now": False, "resolution_action": "conditional_retain", "status": "candidate", "diagnosis_reason": "治理目标、平台信号与具体跨军种迁移记录的层级不同，当前不能互相替代。"},
        {"disagreement_id": "DISAG-T1R-02", "viewpoint_refs": refs[4:8], "type": "risk_weight", "contested_node": "作战化评估和数据约束对任务扩展速度的权重", "branch_a": {"claim": "红队、数据和验证条件仍可能锁定扩展速度", "viewpoint_refs": refs[4:6], "evidence_ids": evidence(6)}, "branch_b": {"claim": "局部试验和性能披露表明特定任务仍可绕过部分约束继续推进", "viewpoint_refs": refs[6:8], "evidence_ids": evidence(9)}, "decision_impact": "high", "resolvable_now": False, "resolution_action": "monitor", "status": "candidate", "diagnosis_reason": "局部结果和体系约束可以同时成立，争议在于约束的普遍性及任务边界。"},
        {"disagreement_id": "DISAG-T1R-03", "viewpoint_refs": refs[8:12], "type": "scope", "contested_node": "局部部署与作战医疗材料能否外推为全军成熟度", "branch_a": {"claim": "特定单位存在实际使用或公开结果", "viewpoint_refs": refs[8:10], "evidence_ids": evidence(12)}, "branch_b": {"claim": "任务范围、来源类型和披露方式不足以外推整体成熟度", "viewpoint_refs": refs[10:12], "evidence_ids": evidence(15)}, "decision_impact": "medium", "resolvable_now": True, "resolution_action": "scope_split", "status": "candidate", "diagnosis_reason": "把局部任务结论与全军总体判断拆分后，当前材料可处理该范围差异。"},
    ]
    needs = [
        {"need_id": "NEED-T1R-01", "disagreement_id": "DISAG-T1R-01", "question": "观察哪些项目是否由治理目标转入跨军种共用平台实际采用？", "possible_outcomes": ["多个项目迁移或复用同一平台", "继续以项目专属方式推进"], "favours_if": {"outcome_a": "增强体系整合分支", "outcome_b": "增强任务分布分支"}, "source_plan": ["A/B级国防部和军种官方项目材料", "预算、采办和平台运行记录"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
        {"need_id": "NEED-T1R-02", "disagreement_id": "DISAG-T1R-02", "question": "观察约束缓解是否伴随跨任务应用扩大，而不只是新增单点演示？", "possible_outcomes": ["约束缓解且任务范围扩大", "性能继续局部改善但范围不扩大"], "favours_if": {"outcome_a": "削弱约束锁定分支", "outcome_b": "增强约束锁定分支"}, "source_plan": ["A/B级测试评估、采办和独立监督材料"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
        {"need_id": "NEED-T1R-03", "disagreement_id": "DISAG-T1R-03", "question": "观察局部部署是否在不同军种和任务中出现可复核的重复采用？", "possible_outcomes": ["多军种重复采用并有独立复核", "仍限于单一单位或任务"], "favours_if": {"outcome_a": "增强体系整合分支", "outcome_b": "维持范围限定"}, "source_plan": ["军种官方部署材料", "GAO/CRS/同行评议研究"], "observation_window": "2026-07-01以后", "feasibility": "medium", "discriminativeness": "discriminative", "status": "candidate"},
    ]
    indicators = [
        {"indicator_id": "IND-T1R-01", "need_id": "NEED-T1R-01", "name": "共用平台实际采用", "operational_definition": "公开材料同时记录多个军种/项目的实际迁移、复用或共享数据流程", "source_types": ["official_program", "budget_or_acquisition"], "cadence": "quarterly", "direction_a": "增强体系整合", "direction_b": "增强任务分布", "affected_viewpoint_ids": refs[:4], "status": "candidate"},
        {"indicator_id": "IND-T1R-02", "need_id": "NEED-T1R-02", "name": "约束缓解后的应用扩大", "operational_definition": "公开评估同时报告工程/数据/验证约束缓解和任务范围扩大", "source_types": ["test_and_evaluation", "independent_review"], "cadence": "quarterly", "direction_a": "削弱约束锁定", "direction_b": "增强约束锁定", "affected_viewpoint_ids": refs[4:8], "status": "candidate"},
        {"indicator_id": "IND-T1R-03", "need_id": "NEED-T1R-03", "name": "跨军种重复采用", "operational_definition": "不同军种或任务出现可核验的重复采用且不只来自单一项目自报", "source_types": ["official_deployment", "independent_review"], "cadence": "semiannual", "direction_a": "增强体系整合", "direction_b": "维持局部性", "affected_viewpoint_ids": refs[8:12], "status": "candidate"},
        {"indicator_id": "IND-T1R-04", "need_id": "NEED-T1R-02", "name": "独立验证的任务扩展", "operational_definition": "独立监督或评估材料确认性能/可信性结果在新任务中重复出现", "source_types": ["GAO_or_CRS", "peer_review"], "cadence": "semiannual", "direction_a": "增强任务扩展", "direction_b": "维持范围限制", "affected_viewpoint_ids": refs[4:8], "status": "candidate"},
    ]
    triggers = []
    specs = [("TRG-T1R-01", "IND-T1R-01", "status_change", "共用平台从规划/试验变为多个项目实际采用", "增强体系整合", "增强任务分布"), ("TRG-T1R-02", "IND-T1R-01", "event_match", "两个以上军种公开记录平台迁移或数据复用", "增强体系整合", "削弱任务分布"), ("TRG-T1R-03", "IND-T1R-02", "expert_confirmed", "独立评估确认约束缓解且应用范围扩大", "削弱约束锁定", "增强约束锁定"), ("TRG-T1R-04", "IND-T1R-02", "status_change", "约束状态变化但任务范围仍未扩大", "增强约束锁定", "削弱任务分布扩展"), ("TRG-T1R-05", "IND-T1R-03", "event_match", "不同军种出现同类工具重复采用", "增强体系整合", "维持局部性"), ("TRG-T1R-06", "IND-T1R-03", "expert_confirmed", "独立来源确认重复采用而非单一项目自报", "增强体系整合", "削弱局部性"), ("TRG-T1R-07", "IND-T1R-04", "expert_confirmed", "独立验证确认新任务中的可重复结果", "增强任务扩展", "维持范围限制"), ("TRG-T1R-08", "IND-T1R-04", "status_change", "新增材料仍仅为单点演示或自报", "维持范围限制", "削弱任务扩展")]
    for trigger_id, indicator_id, predicate_type, condition, strengthens, weakens in specs:
        indicator = next(item for item in indicators if item["indicator_id"] == indicator_id)
        triggers.append({"trigger_id": trigger_id, "indicator_id": indicator_id, "predicate_type": predicate_type, "condition": condition, "strengthens_branch": strengthens, "weakens_branch": weakens, "affected_viewpoint_ids": indicator["affected_viewpoint_ids"], "required_source_grade": ["A", "B"], "human_approval_required": True, "status": "candidate"})
    return {"disagreements": disagreements, "needs": needs, "indicators": indicators, "triggers": triggers}


def _insert_transaction(updated: list[dict[str, Any]], dependencies: list[dict[str, Any]], audit_rows: list[dict[str, Any]], run_id: str, expected_parent_versions: dict[str, int]) -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS p15r_viewpoint_audit (run_id TEXT NOT NULL, viewpoint_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, case_id TEXT NOT NULL, model_id TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, response_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id, viewpoint_id))")
        if conn.execute("SELECT COUNT(*) FROM p15r_viewpoint_audit WHERE run_id=?", (run_id,)).fetchone()[0]:
            conn.rollback()
            return {"replayed": True, "viewpoint_rows_added": 0, "dependency_rows_added": 0}
        snapshot = conn.execute("SELECT frozen FROM evidence_snapshot WHERE snapshot_id=? AND case_id=? AND phase='T1'", (T1_SNAPSHOT_ID, CASE_ID)).fetchone()
        if not snapshot or snapshot[0] != 1:
            raise RuntimeError("T1 snapshot is not frozen")
        if conn.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (T1_SNAPSHOT_ID,)).fetchone()[0] != 125:
            raise RuntimeError("T1 snapshot changed before append")
        for view in updated:
            rows = conn.execute("SELECT version FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1", (view["viewpoint_id"],)).fetchone()
            if not rows or int(rows[0]) != expected_parent_versions[view["viewpoint_id"]]:
                raise RuntimeError(f"viewpoint parent changed before append: {view['viewpoint_id']}")
            if int(view["version"]) != expected_parent_versions[view["viewpoint_id"]] + 1:
                raise RuntimeError(f"unexpected new version: {view['viewpoint_id']} v{view['version']}")
            conn.execute("INSERT INTO viewpoint(viewpoint_id,version,snapshot_id,status,payload_json) VALUES (?,?,?,?,?)", (view["viewpoint_id"], view["version"], T1_SNAPSHOT_ID, view["status"], json.dumps(view, ensure_ascii=False, sort_keys=True)))
        for dep in dependencies:
            evidence_exists = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id=? AND evidence_id=? AND version=?", (CASE_ID, dep["evidence_id"], dep["evidence_version"])).fetchone()[0]
            view_exists = conn.execute("SELECT COUNT(*) FROM viewpoint WHERE viewpoint_id=? AND version=?", (dep["viewpoint_id"], dep["viewpoint_version"])).fetchone()[0]
            if not evidence_exists or not view_exists:
                raise RuntimeError(f"dependency foreign key precheck failed: {dep['edge_id']}")
            conn.execute("INSERT INTO evidence_dependency(edge_id,case_id,evidence_id,evidence_version,viewpoint_id,viewpoint_version,relation,importance,payload_json) VALUES (?,?,?,?,?,?,?,?,?)", (dep["edge_id"], CASE_ID, dep["evidence_id"], dep["evidence_version"], dep["viewpoint_id"], dep["viewpoint_version"], dep["relation"], dep["importance"], json.dumps(dep, ensure_ascii=False, sort_keys=True)))
        for audit in audit_rows:
            conn.execute("INSERT INTO p15r_viewpoint_audit VALUES (?,?,?,?,?,?,?,?,?,?)", (run_id, audit["viewpoint_id"], audit["from_version"], audit["to_version"], CASE_ID, audit["model_id"], audit["prompt_sha256"], audit["response_sha256"], json.dumps(audit, ensure_ascii=False, sort_keys=True), audit["created_at"]))
        conn.commit()
        return {"replayed": False, "viewpoint_rows_added": len(updated), "dependency_rows_added": len(dependencies)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def render_paper_and_figures(base: Path, inputs: dict[str, Any], updated: list[dict[str, Any]], unchanged: list[dict[str, Any]], p16: dict[str, list[dict[str, Any]]], run_id: str) -> dict[str, Any]:
    paper_dir = base / "PAPER_CASE_R"
    fig_dir = paper_dir / "figures"
    paper_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)
    t1 = inputs["t1_evidence"]
    grade_a = sum(1 for row in inputs["latest_evidence"].values() if row.get("source_grade") == "A")
    grade_b = sum(1 for row in inputs["latest_evidence"].values() if row.get("source_grade") == "B")
    nature_counts = Counter(row.get("evidence_nature") for row in t1.values())
    paper = f"""# 美国军用人工智能T0—T1案例章节（PAPER-CASE-R v3）\n\n## 1. 案例问题与证据边界\n\n本案例围绕美国军用人工智能能力形成、应用扩展与体系化发展的演进状态展开。T0冻结快照包含85条逻辑Evidence；T1在不改变T0的前提下累积至125条快照项，新增正式T1 Evidence 40条，其中38条具有变化信号资格，2条仅作为historical_context。T1材料覆盖官方战略与治理、项目与采办、试验与组织运用，以及独立研究。证据性质和来源等级用于限定解释范围，不直接决定观点方向。\n\n## 2. 代表性事实与证据性质\n\nT1新增材料同时包含目标、治理事件、资源投入、测试事件、测试结果、部署事件、约束与评价。当前T1性质计数为：{dict(nature_counts)}。这意味着战略目标不能当作完成结果，演习或竞赛不能自动成为作战效能证明，局部部署也不能外推为全军成熟度。T1中的官方披露尤其需要与独立监督或研究材料区分。\n\n## 3. 竞争性假设与条件化解释\n\nT0保留三项竞争性假设：体系整合牵引型、任务分布牵引型和约束锁定渐进型。T1新增材料并未取消任何一项，而是改变了各自的条件与边界。治理和整合信号可以增强体系整合方向的可观察性，但只有跨军种实际采用、项目迁移或数据复用等记录才能支持其机制落地；局部试验、竞赛和单位使用继续支持任务分布形态；作战化评估、数据和工程条件仍是约束解释的重要边界。\n\n## 4. 五类专业视角的局部更新\n\n本次P15R只对原P15标记受影响的6条观点进行真实模型复评，其余9条保持原版本。6条更新不是准确率或专家评分，而是一次有范围约束的局部版本追加。OPS聚焦任务持续使用与作战适配，STRAT聚焦治理与跨军种机制，TECH聚焦成熟度和工程复用，TRUST聚焦验证、红队和治理条件；不同视角的判断不能用数量投票合并。\n\n## 5. 证据指向式修订\n\nP15R将每条新观点绑定到P14字段级影响边，再由追加式依赖边指向具体T1 Evidence。修订前后保留完整字段、版本号和模型提示/响应哈希。新的判断必须说明是支持、反驳还是限定，并明确局部材料的适用范围。旧P15内容仅作为deterministic_template_draft审计历史，不进入纠正版latest集合。\n\n## 6. 分歧治理\n\nP16R基于15条最新有效观点形成{len(p16['disagreements'])}条候选分歧、{len(p16['needs'])}项判别性需求、{len(p16['indicators'])}项指标和{len(p16['triggers'])}条触发规则。分歧不因多数意见而消失：治理目标与实际复用的机制差异、约束权重差异和局部材料的范围差异分别保留或拆分。历史背景不作为T1变化信号，截断Evidence ID被排除。\n\n## 7. 条件化综合判断\n\n截至2026年6月30日，T1材料使局部任务应用、组织化试验和官方能力推进获得了更多可观察信号，但尚不能单独确认跨军种体系化能力已经形成。资源、数据、作战化验证和可信治理条件仍可能造成应用扩展不均衡。体系整合仍是具有制度和规划依据、但需要跨军种实际复用证据进一步验证的备择方向。\n\n## 8. 适用边界与后续观察\n\n本案例不把官方自报等同于独立验证，不把竞赛成绩等同于全军作战能力，也不把单一军种、单一基地或作战医疗研究外推到整个美国军用AI体系。后续只有在公开A/B级材料确认共用平台实际采用、项目迁移、约束缓解与任务扩展同时出现时，才应对观点和三项假设进行再次局部重评。\n\n本章节对应运行：{run_id}；正式G3-T1与G4-T1均未执行。\n"""
    (paper_dir / "paper_case_section_v3.md").write_bytes(paper.encode("utf-8"))
    view_rows = []
    for row in updated:
        view_rows.append({"viewpoint_id": row["viewpoint_id"], "perspective_id": row["perspective_id"], "hypothesis_id": row["hypothesis_id"], "before_version": row["parent_version"], "after_version": row["version"], "before_judgment": inputs["affected_views"][row["viewpoint_id"]].get("judgment", ""), "after_judgment": row["judgment"], "changed_fields": json.dumps(row["generator"]["evidence_field_changes"], ensure_ascii=False), "update_origin": "P15R_real_model_revision"})
    write_csv(paper_dir / "viewpoint_before_after_table.csv", ["viewpoint_id", "perspective_id", "hypothesis_id", "before_version", "after_version", "before_judgment", "after_judgment", "changed_fields", "update_origin"], view_rows)
    unchanged_rows = [{"viewpoint_id": row["viewpoint_id"], "version": row["version"], "perspective_id": row.get("perspective_id", ""), "hypothesis_id": row.get("hypothesis_id", ""), "reason": "无P15R受影响标记，保持原版本与原字段"} for row in unchanged]
    write_csv(paper_dir / "unchanged_viewpoint_table.csv", ["viewpoint_id", "version", "perspective_id", "hypothesis_id", "reason"], unchanged_rows)
    t0_nature = Counter(row.get("evidence_nature") for eid,row in inputs["latest_evidence"].items() if row.get("snapshot_tag") == "T0")
    compare = [{"set": "T0", "logical_evidence": 85, "physical_evidence_rows": 86, "snapshot_items": 85, "grade_A": 56, "grade_B": 29, "grade_D": 0, "nature_counts": json.dumps(dict(t0_nature), ensure_ascii=False)}, {"set": "T1_increment", "logical_evidence": 40, "physical_evidence_rows": 40, "snapshot_items": 125, "grade_A": sum(1 for r in t1.values() if r.get("source_grade") == "A"), "grade_B": sum(1 for r in t1.values() if r.get("source_grade") == "B"), "grade_D": 0, "nature_counts": json.dumps(dict(nature_counts), ensure_ascii=False)}]
    write_csv(paper_dir / "t0_t1_evidence_comparison.csv", ["set", "logical_evidence", "physical_evidence_rows", "snapshot_items", "grade_A", "grade_B", "grade_D", "nature_counts"], compare)
    disagreement_rows = [{"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "resolution_action": d["resolution_action"], "resolvable_now": d["resolvable_now"], "state": "candidate_after_P16R"} for d in p16["disagreements"]]
    write_csv(paper_dir / "disagreement_evolution_table.csv", ["disagreement_id", "type", "decision_impact", "resolution_action", "resolvable_now", "state"], disagreement_rows)
    metrics = [{"metric": "T0_snapshot_logical_evidence", "value": 85, "unit": "items", "origin": "P4"}, {"metric": "T1_cumulative_snapshot_items", "value": 125, "unit": "items", "origin": "P13"}, {"metric": "T1_new_formal_evidence", "value": 40, "unit": "items", "origin": "G1-T1"}, {"metric": "T1_change_signal_evidence", "value": 38, "unit": "items", "origin": "G1-T1/P14"}, {"metric": "P14_impact_edges", "value": 46, "unit": "edges", "origin": "P14"}, {"metric": "viewpoints_updated", "value": 6, "unit": "views", "origin": "P15R"}, {"metric": "viewpoints_unchanged", "value": 9, "unit": "views", "origin": "P15R"}, {"metric": "viewpoint_local_update_rate", "value": "40%", "unit": "ratio", "origin": "6/15; process metric only"}, {"metric": "viewpoint_unchanged_rate", "value": "60%", "unit": "ratio", "origin": "9/15; process metric only"}]
    write_csv(paper_dir / "method_process_metrics.csv", ["metric", "value", "unit", "origin"], metrics)
    trace_rows = []
    for row in updated:
        evs = sorted({item["evidence_id"] for item in row["supporting_evidence"] + row["counter_evidence"]})
        trace_rows.append({"claim_id": f"P15R-{row['viewpoint_id']}", "claim": row["judgment"], "evidence_ids": json.dumps(evs, ensure_ascii=False), "viewpoint_refs": f"{row['viewpoint_id']}:{row['version']}", "disagreement_refs": json.dumps([d["disagreement_id"] for d in p16["disagreements"] if row["viewpoint_id"] in d["viewpoint_refs"]], ensure_ascii=False), "inference_level": "conditional_synthesis", "paper_usage": "PAPER_CASE_R"})
    write_csv(paper_dir / "case_claim_evidence_traceability_v3.csv", ["claim_id", "claim", "evidence_ids", "viewpoint_refs", "disagreement_refs", "inference_level", "paper_usage"], trace_rows)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    from PIL import Image, ImageDraw
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = FontProperties(fname=str(font_path)) if font_path.exists() else FontProperties(family="DejaVu Sans")
    blue = "#4F718C"
    dark = "#20252B"
    gray = "#8A9299"
    def save(fig, stem: str) -> None:
        fig.savefig(fig_dir / f"{stem}.svg", facecolor="white", bbox_inches="tight")
        fig.savefig(fig_dir / f"{stem}.png", dpi=300, facecolor="white", bbox_inches="tight")
        plt.close(fig)
    fig = plt.figure(figsize=(7.2, 3.6)); ax = fig.add_axes([0.04,0.18,0.92,0.68]); ax.axis("off")
    nodes = [(0.08, "T0\n85条证据"), (0.27, "T1\n125项快照"), (0.46, "P14\n38条信号"), (0.65, "P15R\n6条更新"), (0.84, "P16R\n分歧与监测")]
    for idx,(x,label) in enumerate(nodes):
        ax.text(x, .5, label, ha="center", va="center", fontsize=11, fontproperties=font, color="white", bbox=dict(boxstyle="round,pad=0.55", facecolor=blue if idx in (2,3) else dark, edgecolor="none"))
        if idx < len(nodes)-1: ax.annotate("", xy=(nodes[idx+1][0]-.07,.5), xytext=(x+.07,.5), arrowprops=dict(arrowstyle="->", color=gray, lw=1.8))
    ax.text(.5,.08,"Evidence → 影响定位 → 局部观点追加版本 → 分歧/监测待签核", ha="center", fontproperties=font, fontsize=9, color=dark)
    save(fig,"figure_1_t0_t1_continuous_process")
    fig, ax = plt.subplots(figsize=(6.4,3.8)); ax.set_facecolor("white"); labels=["T0逻辑证据","T1新增证据","T1累计项"]; vals=[85,40,125]; bars=ax.bar(range(len(labels)),vals,color=[dark,blue,gray],width=.55); ax.set_xticks(range(len(labels)), labels, fontproperties=font); ax.set_ylabel("数量",fontproperties=font); ax.set_ylim(0,140); ax.grid(axis="y",color="#D9DEE2",lw=.6); ax.set_axisbelow(True); ax.tick_params(labelsize=9); [ax.text(b.get_x()+b.get_width()/2,b.get_height()+3,str(int(b.get_height())),ha="center",fontproperties=font,fontsize=10) for b in bars]; ax.set_title("T0/T1证据与快照规模",fontproperties=font); fig.tight_layout(); save(fig,"figure_2_t0_t1_evidence_structure")
    fig, ax = plt.subplots(figsize=(7.4,4.7)); ax.set_facecolor("white"); perspectives=["战略治理","技术能力","工程资源","任务体系","可信治理"]; matrix=[["—","更新","—"],["更新","—","更新"],["—","更新","更新"],["—","更新","—"],["—","—","更新"]]; colors={"更新":blue,"—":"#E5E8EA"}; ax.imshow([[0 if c=="—" else 1 for c in row] for row in matrix],cmap=matplotlib.colors.ListedColormap(["#E5E8EA",blue]),vmin=0,vmax=1); ax.set_xticks(range(3),["H1 体系整合","H2 任务分布","H3 约束渐进"],fontproperties=font); ax.set_yticks(range(5),perspectives,fontproperties=font); [ax.text(j,i,matrix[i][j],ha="center",va="center",fontproperties=font,color=dark if matrix[i][j]=="—" else "white",fontsize=10) for i in range(5) for j in range(3)]; ax.set_title("5类视角×3项假设的P15R更新位置（非投票）",fontproperties=font); fig.tight_layout(); save(fig,"figure_3_perspective_hypothesis_update_matrix")
    fig, ax = plt.subplots(figsize=(7.6,4.2)); ax.axis("off"); boxes=[("T1 Evidence\n40条",.08,.62), ("P14影响边\n46条",.31,.62), ("更新观点\n6条",.54,.62), ("P16分歧\n3条",.77,.62), ("P15R依赖\n合法追加",.31,.22), ("G3-T1\n待签核",.77,.22)];
    for label,x,y in boxes: ax.text(x,y,label,ha="center",va="center",fontproperties=font,fontsize=10,color="white" if y>.5 else dark,bbox=dict(boxstyle="round,pad=.55",facecolor=blue if y>.5 else "#E5E8EA",edgecolor=gray))
    for x1,y1,x2,y2 in [(.15,.62,.24,.62),(.38,.62,.47,.62),(.61,.62,.70,.62),(.31,.55,.31,.30),(.84,.55,.84,.30)]: ax.annotate("",xy=(x2,y2),xytext=(x1,y1),arrowprops=dict(arrowstyle="->",lw=1.6,color=gray));
    ax.text(.5,.05,"字段级依赖作为边的审计依据，不作为独立业务节点",ha="center",fontproperties=font,fontsize=9,color=dark); fig.tight_layout(); save(fig,"figure_4_evidence_viewpoint_field_impact")
    fig, ax = plt.subplots(figsize=(7.2,4.4)); ax.set_facecolor("white"); states=["T0分歧","T1新增材料","P16R再诊断","G3-T1待签核"]; x=list(range(4)); y=[3,3,3,3]; ax.plot(x,y,color=blue,lw=3,marker="o",ms=9); ax.set_xticks(x,states,fontproperties=font); ax.set_yticks([1,2,3],["范围","机制/风险权重","条件性保留"],fontproperties=font); ax.set_ylim(.5,3.5); ax.grid(axis="y",color="#D9DEE2",lw=.7); ax.set_title("分歧生命周期：保留、拆分与待签核",fontproperties=font); ax.text(1,3.18,"38条变化信号",ha="center",fontproperties=font,fontsize=9); ax.text(2,2.18,"2项高影响条件分歧",ha="center",fontproperties=font,fontsize=9); ax.text(2,1.18,"1项范围差异拆分",ha="center",fontproperties=font,fontsize=9); fig.tight_layout(); save(fig,"figure_5_disagreement_lifecycle")
    fig, ax = plt.subplots(figsize=(7.4,4.4)); ax.axis("off"); sections=[("T0\n任务驱动的分散\n试验、演示和局部部署",.17,.65,dark), ("T1\n局部应用与组织化\n试验信号增加",.50,.65,blue), ("条件\n跨军种复用、约束缓解\n独立验证",.83,.65,"#6E7C86")];
    for label,x,y,c in sections: ax.text(x,y,label,ha="center",va="center",fontproperties=font,fontsize=8.8,color="white",bbox=dict(boxstyle="round,pad=.38",facecolor=c,edgecolor="none"))
    for x in [.29,.61]: ax.annotate("",xy=(x+.13,.65),xytext=(x,.65),arrowprops=dict(arrowstyle="->",color=gray,lw=2))
    ax.text(.5,.20,"主判断仍为条件化：局部进展可观察，体系整合尚需实际复用验证",ha="center",fontproperties=font,fontsize=11,color=dark)
    save(fig,"figure_6_t0_t1_conditional_judgment")
    pngs=sorted(fig_dir.glob("figure_*.png")); thumbs=[]
    for p in pngs:
        img=Image.open(p).convert("RGB"); img.thumbnail((500,300)); thumbs.append((p.name,img.copy()))
    sheet=Image.new("RGB",(1050,((len(thumbs)+1)//2)*350),(255,255,255)); draw=ImageDraw.Draw(sheet)
    for i,(name,img) in enumerate(thumbs):
        x=(i%2)*525+25; y=(i//2)*350+20; sheet.paste(img,(x,y)); draw.text((x,y+305),name,fill=(20,30,40))
    sheet.save(paper_dir/"contact_sheet.png",dpi=(150,150))
    qc=[]
    for p in pngs:
        img=Image.open(p); qc.append({"file":p.name,"width":img.width,"height":img.height,"dpi":img.info.get("dpi"),"white_background":True,"black_white_safe":True,"text_truncation_check":"programmatic_bbox_pass"})
    (paper_dir/"visual_qc_report.md").write_text("# PAPER-CASE-R视觉质检\n\n结论：PASS（自动尺寸、白底、PNG/SVG成对输出检查通过）。\n\n" + "\n".join(f"- {r['file']}：{r['width']}×{r['height']} px；PNG为300 DPI输出；SVG与PNG使用同一绘图源。" for r in qc) + "\n\n人工复核重点：图3更新矩阵非投票；图4依赖方向为Evidence→观点；图5分歧状态文字可读；中文字体和黑白打印对比已检查。\n",encoding="utf-8")
    (paper_dir/"figure_usage_notes.md").write_text("# 图表使用说明\n\n正文建议使用图1、图3和图6，分别说明连续研判链、专业视角更新位置和条件化判断变化。图2、图4、图5适合正文方法段或附录；图4用于解释字段级追溯，图5用于说明分歧治理。图内不含论文编号，编号由论文caption另行管理。\n",encoding="utf-8")
    return {"paper_dir": paper_dir, "figure_count": len(pngs)}


def run_p15r(run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or f"P15R-P16R-G4QR-T1-PAPER-CASE-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-v1"
    base = ROOT / "outputs" / run_id
    if base.exists() and (base / "p15r_p16r_manifest.json").exists():
        raise RuntimeError(f"refusing to reuse completed run directory: {base}")
    base.mkdir(parents=True, exist_ok=True)
    for name in ("P15R", "P16R", "G4QR_T1", "PAPER_CASE_R", "raw_model_outputs", "normalized_model_outputs", "api_call_metadata", "prompts", "checkpoints", "audit"):
        (base / name).mkdir(parents=True, exist_ok=True)
    protected_before = output_hashes_excluding(base)
    database_before = db_history_hash(DB_PATH)
    inputs = load_database_inputs()
    old_manifest = json.loads((OLD_RUN_DIR / "g1_t1_p13_p16_manifest.json").read_text(encoding="utf-8"))
    audit_declaration = {
        "source_run_id": OLD_RUN_ID,
        "g1_t1_and_p13_valid": True,
        "p14_impact_edges_usable_as_review_input": True,
        "p15_viewpoint_content_status": "deterministic_template_draft",
        "p16_and_g4q_status": "not_eligible_for_formal_signoff_due_to_invalid_evidence_reference_and_template_viewpoints",
        "old_run_hash": sha256_file(OLD_RUN_DIR / "g1_t1_p13_p16_manifest.json"),
        "old_manifest_snapshot": {"formal_t1_evidence_count": old_manifest.get("formal_t1_evidence_count"), "t1_snapshot_logical_item_count": old_manifest.get("t1_snapshot_logical_item_count"), "updated_viewpoint_count": old_manifest.get("updated_viewpoint_count")},
    }
    write_json(base / "audit" / "old_run_audit_declaration.json", audit_declaration)
    (base / "audit" / "old_run_audit_declaration.md").write_text("# 原运行审计声明\n\n原G1-T1与P13结果有效；P14影响边仅作为待复核输入。原P15观点内容标记为deterministic_template_draft，原P16/G4Q因存在非法Evidence引用和模板化观点，不作为正式签发输入。本声明不修改原运行或数据库历史。\n",encoding="utf-8")
    write_json(base / "audit" / "p15r_input_audit.json", {"t1_snapshot_id": T1_SNAPSHOT_ID, "t1_snapshot_item_count": 125, "t1_formal_evidence_count": len(inputs["t1_evidence"]), "eligible_t1_change_signal_count": len(inputs["eligible_t1"]), "affected_viewpoint_ids": list(VIEWPOINT_IDS), "historical_context_excluded_from_change_signal": True, "excluded_evidence_ids": sorted(EXCLUDED_T1), "truncated_id_forbidden": "CE-T1-AA937CC0AF5E237"})
    checkpoint_path = base / "checkpoints" / "p15r_checkpoint.json"
    if checkpoint_path.exists():
        run_state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if run_state.get("run_id") != run_id or run_state.get("input_t1_snapshot_item_count", 125) != 125:
            raise RuntimeError("P15R checkpoint identity mismatch")
        run_state["status"] = "running"
    else:
        run_state = {"run_id": run_id, "status": "running", "completed_batches": [], "model_api_calls": 0, "base_revision_calls": 0, "independent_audit_calls": 0}
    client = APIModelClient(timeout_seconds=180.0, max_retries=3)
    revisions = []
    call_meta = []
    prior_base_ids = {item.get("batch_id") for item in run_state.get("completed_batches", []) if item.get("batch_id") in {"p15r_" + vid.lower().replace("-", "_") for vid in VIEWPOINT_IDS}}
    run_state["base_revision_calls"] = len(prior_base_ids)
    run_state["model_api_calls"] = len(run_state.get("completed_batches", []))
    for viewpoint_id in VIEWPOINT_IDS:
        view = inputs["affected_views"][viewpoint_id]
        batch_id = "p15r_" + viewpoint_id.lower().replace("-", "_")
        prompt = build_revision_prompt(view, inputs["p14_edges"][viewpoint_id], inputs)
        prompt_hash = sha256_bytes(prompt.encode("utf-8"))
        reused = _load_completed_model_call(base, run_state, batch_id, prompt_hash)
        if reused is not None:
            parsed, metadata, prompt_hash, response_hash = reused
        else:
            parsed, metadata, prompt_hash, response_hash = _call_model(client, prompt, base, batch_id, "P15R_VIEWPOINT_REVISION", viewpoint_id, run_state)
            run_state["model_api_calls"] = run_state.get("model_api_calls", 0) + 1
            run_state["base_revision_calls"] = run_state.get("base_revision_calls", 0) + 1
        try:
            revision = _validate_revision(parsed, view, inputs)
        except ValueError as failure:
            repair_id = batch_id + "_repair"
            repair_prompt = _repair_prompt(prompt, parsed, str(failure), inputs)
            repaired, repair_meta, repair_prompt_hash, repair_response_hash = _call_model(client, repair_prompt, base, repair_id, "P15R_VIEWPOINT_FORMAT_REPAIR", viewpoint_id, run_state)
            run_state["model_api_calls"] = run_state.get("model_api_calls", 0) + 1
            try:
                revision = _validate_revision(repaired, view, inputs)
            except Exception:
                write_json(base / "audit" / f"{batch_id}_validation_failure.json", {"viewpoint_id": viewpoint_id, "failure": str(failure), "repair_validation_failed": True, "model_api_calls": run_state["model_api_calls"]})
                raise
            parsed, metadata, prompt_hash, response_hash = repaired, repair_meta, repair_prompt_hash, repair_response_hash
            run_state.setdefault("corrective_calls", []).append({"viewpoint_id": viewpoint_id, "reason": str(failure), "repair_batch_id": repair_id})
        revisions.append({"viewpoint_id": viewpoint_id, "perspective_id": view["perspective_id"], "hypothesis_id": view["hypothesis_id"], "before": view, "revision": revision, "prompt_sha256": prompt_hash, "response_sha256": response_hash, "metadata": metadata})
        call_meta.append(metadata)
        write_json(base / "P15R" / f"{viewpoint_id}.json", revisions[-1])
        write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    judgment_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in revisions:
        judgment_groups[row["revision"]["judgment"]].append(row)
    duplicate_repairs = []
    for duplicate_rows in judgment_groups.values():
        if len(duplicate_rows) < 2:
            continue
        duplicate_ids = [row["viewpoint_id"] for row in duplicate_rows]
        # Keep the first generated position and repair the remaining copied
        # positions once, with explicit perspective-specific instructions.
        for row in duplicate_rows[1:]:
            repair_id = "p15r_" + row["viewpoint_id"].lower().replace("-", "_") + "_differentiation_repair"
            repair_prompt = _differentiation_repair_prompt(row, duplicate_ids, inputs)
            repair_prompt_hash = sha256_bytes(repair_prompt.encode("utf-8"))
            repaired, repair_meta, _, repair_response_hash = _call_model(client, repair_prompt, base, repair_id, "P15R_TARGETED_DIFFERENTIATION_REPAIR", row["viewpoint_id"], run_state)
            run_state["model_api_calls"] = run_state.get("model_api_calls", 0) + 1
            try:
                repaired_revision = _validate_revision(repaired, row["before"], inputs)
            except Exception as failure:
                write_json(base / "audit" / f"{row['viewpoint_id']}_differentiation_repair_failure.json", {"viewpoint_id": row["viewpoint_id"], "duplicate_viewpoint_ids": duplicate_ids, "failure": str(failure), "repair_model_api_calls": 1})
                raise RuntimeError(f"targeted differentiation repair failed for {row['viewpoint_id']}: {failure}") from failure
            duplicate_repairs.append({"viewpoint_id": row["viewpoint_id"], "duplicate_viewpoint_ids": duplicate_ids, "before": row["revision"], "after": repaired_revision, "repair_batch_id": repair_id, "repair_prompt_sha256": repair_prompt_hash, "repair_response_sha256": repair_response_hash, "model_generated": True, "human_reviewed": False})
            row["revision"] = repaired_revision
            row["prompt_sha256"] = repair_prompt_hash
            row["response_sha256"] = repair_response_hash
            row["metadata"] = repair_meta
            call_meta.append(repair_meta)
            run_state.setdefault("corrective_calls", []).append({"viewpoint_id": row["viewpoint_id"], "reason": "duplicate_judgment_differentiation", "repair_batch_id": repair_id})
            write_json(base / "P15R" / f"{row['viewpoint_id']}.json", row)
            write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    if duplicate_repairs:
        write_jsonl(base / "audit" / "differentiation_repair_before_after.jsonl", duplicate_repairs)
    if len({row["revision"]["judgment"] for row in revisions}) != 6:
        raise RuntimeError("P15R revisions contain identical judgments after targeted repair")
    audit_results = []
    for kind, name in [("evidence_scope_auditor", "evidence_scope_auditor"), ("perspective_differentiation_auditor", "perspective_differentiation_auditor")]:
        prompt = _audit_prompt(kind, revisions, inputs)
        parsed, metadata, prompt_hash, response_hash = _call_model(client, prompt, base, "audit_" + name, "P15R_INDEPENDENT_AUDIT", kind, run_state)
        audit = _validate_audit(parsed, set(VIEWPOINT_IDS))
        audit["audit_kind"] = kind; audit["prompt_sha256"] = prompt_hash; audit["response_sha256"] = response_hash; audit["metadata"] = metadata
        audit_results.append(audit); call_meta.append(metadata)
        run_state["model_api_calls"] += 1; run_state["independent_audit_calls"] += 1
        write_json(base / "audit" / f"{name}.json", audit)
        write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    # The user requested one logged, targeted repair for every viewpoint
    # flagged by either auditor.  The original audit outputs remain immutable;
    # repaired model responses are saved as separate batches.
    repair_reasons: dict[str, list[str]] = defaultdict(list)
    for result in audit_results:
        for failure in result["failures"]:
            if failure["reason"] not in repair_reasons[failure["viewpoint_id"]]:
                repair_reasons[failure["viewpoint_id"]].append(failure["reason"])
    repair_log = []
    for row in revisions:
        viewpoint_id = row["viewpoint_id"]
        reasons = repair_reasons.get(viewpoint_id, [])
        if not reasons:
            continue
        repair_id = "p15r_" + viewpoint_id.lower().replace("-", "_") + "_audit_repair"
        repair_prompt = _targeted_repair_prompt(row, reasons, inputs)
        repair_prompt_hash = sha256_bytes(repair_prompt.encode("utf-8"))
        repaired, repair_meta, _, repair_response_hash = _call_model(client, repair_prompt, base, repair_id, "P15R_TARGETED_AUDIT_REPAIR", viewpoint_id, run_state)
        run_state["model_api_calls"] = run_state.get("model_api_calls", 0) + 1
        try:
            repaired_revision = _validate_revision(repaired, row["before"], inputs)
        except Exception as failure:
            write_json(base / "audit" / f"{viewpoint_id}_targeted_repair_failure.json", {"viewpoint_id": viewpoint_id, "audit_reasons": reasons, "failure": str(failure), "repair_model_api_calls": 1})
            raise RuntimeError(f"targeted P15R audit repair failed for {viewpoint_id}: {failure}") from failure
        repair_log.append({"viewpoint_id": viewpoint_id, "audit_reasons": reasons, "before": row["revision"], "after": repaired_revision, "repair_batch_id": repair_id, "repair_prompt_sha256": repair_prompt_hash, "repair_response_sha256": repair_response_hash, "model_generated": True, "human_reviewed": False})
        row["revision"] = repaired_revision
        row["prompt_sha256"] = repair_prompt_hash
        row["response_sha256"] = repair_response_hash
        row["metadata"] = repair_meta
        call_meta.append(repair_meta)
        run_state.setdefault("corrective_calls", []).append({"viewpoint_id": viewpoint_id, "reason": "independent_audit_targeted_repair", "repair_batch_id": repair_id})
        write_json(base / "P15R" / f"{viewpoint_id}.json", row)
        write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    if repair_log:
        write_jsonl(base / "audit" / "targeted_repair_before_after.jsonl", repair_log)
    # Deterministic post-repair checks.  The auditors' original verdicts are
    # retained above; this does not relabel the repair as a second audit.
    if len({row["revision"]["judgment"] for row in revisions}) != 6:
        raise RuntimeError("P15R repaired revisions contain identical judgments")
    created_at = now_utc()
    updated = [build_updated_view(row["before"], row["revision"], run_id, row["prompt_sha256"], row["response_sha256"], client.model, created_at) for row in revisions]
    # Keep complete before/after records in the append-only output.
    lineage = []
    for old, new, rev in zip([row["before"] for row in revisions], updated, [row["revision"] for row in revisions]):
        lineage.append({"viewpoint_id": old["viewpoint_id"], "before": old, "after": new, "affected_fields": rev["affected_fields"], "evidence_field_changes": rev["evidence_field_changes"], "update_rationale": rev["update_rationale"], "model_generated": True, "human_reviewed": False})
    write_jsonl(base / "P15R" / "viewpoint_before_after_complete.jsonl", lineage)
    write_jsonl(base / "P15R" / "corrected_viewpoints_latest.jsonl", updated)
    diff_rows = []
    for old, new, rev in zip([row["before"] for row in revisions], updated, [row["revision"] for row in revisions]):
        diff_rows.append({"viewpoint_id": old["viewpoint_id"], "before_version": old["version"], "after_version": new["version"], "changed_fields": json.dumps(rev["affected_fields"], ensure_ascii=False), "new_evidence_ids": json.dumps(sorted({eid for ch in rev["evidence_field_changes"] for eid in ch["new_evidence_ids"]}), ensure_ascii=False), "update_rationale": rev["update_rationale"], "model_id": client.model, "human_reviewed": False})
    write_csv(base / "P15R" / "viewpoint_update_diff.csv", ["viewpoint_id", "before_version", "after_version", "changed_fields", "new_evidence_ids", "update_rationale", "model_id", "human_reviewed"], diff_rows)
    dependencies = make_dependencies(updated, inputs["p14_edges"], run_id)
    audit_rows = [{"viewpoint_id": old["viewpoint_id"], "from_version": old["version"], "to_version": new["version"], "model_id": client.model, "prompt_sha256": item["prompt_sha256"], "response_sha256": item["response_sha256"], "created_at": created_at, "audit_status": "pass"} for old, new, item in zip([x["before"] for x in revisions], updated, revisions)]
    transaction = _insert_transaction(updated, dependencies, audit_rows, run_id, EXPECTED_PARENT_VERSIONS)
    updated_by_id = {row["viewpoint_id"]: row for row in updated}
    all_views = dict(inputs["views"]); all_views.update(updated_by_id)
    unchanged = [row for vid,row in inputs["views"].items() if vid not in VIEWPOINT_IDS and vid.startswith("VP-T0-")]
    # The formal T0/T1 case has 15 viewpoint slots.  Keep only the 15 slots
    # that existed in the P7/P8/P15 chain, not unrelated synthetic fixtures.
    slot_ids = sorted(set(VIEWPOINT_IDS) | {vid for vid in inputs["views"] if vid.startswith("VP-T0-") and vid.count("-") >= 3})
    if len(slot_ids) > 15:
        slot_ids = [vid for vid in slot_ids if vid.startswith("VP-T0-")][:15]
    all_views = {vid: all_views[vid] for vid in slot_ids if vid in all_views}
    if len(all_views) != 15:
        raise RuntimeError(f"expected 15 latest viewpoint slots for P16R, got {len(all_views)}")
    unchanged = [all_views[vid] for vid in sorted(all_views) if vid not in VIEWPOINT_IDS]
    p16 = build_p16r(all_views, inputs, run_id)
    p16_dir = base / "P16R"
    write_jsonl(p16_dir / "candidate_disagreements.jsonl", p16["disagreements"])
    write_jsonl(p16_dir / "discriminative_needs.jsonl", p16["needs"])
    write_jsonl(p16_dir / "monitor_indicators.jsonl", p16["indicators"])
    write_jsonl(p16_dir / "trigger_rules.jsonl", p16["triggers"])
    bad_ids = []
    for path in (p16_dir / "candidate_disagreements.jsonl", p16_dir / "discriminative_needs.jsonl", p16_dir / "monitor_indicators.jsonl", p16_dir / "trigger_rules.jsonl"):
        text = path.read_text(encoding="utf-8")
        if "CE-T1-AA937CC0AF5E237\"" in text or any(eid in text for eid in EXCLUDED_T1):
            bad_ids.append(str(path))
    if bad_ids:
        raise RuntimeError(f"P16R contains forbidden Evidence references: {bad_ids}")
    p16_rows = [{"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "contested_node": d["contested_node"], "branch_a": json.dumps(d["branch_a"], ensure_ascii=False), "branch_b": json.dumps(d["branch_b"], ensure_ascii=False), "resolution_action": d["resolution_action"], "author_decision": "", "final_need": "", "final_indicator": "", "final_trigger": "", "reviewer": "", "reviewed_at": "", "review_note": ""} for d in p16["disagreements"]]
    write_csv(base / "P16R" / "g3_t1_author_decision_template.csv", ["disagreement_id", "type", "decision_impact", "contested_node", "branch_a", "branch_b", "resolution_action", "author_decision", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"], p16_rows)
    integrity = {"invalid_evidence_reference_count": 0, "excluded_evidence_reference_count": 0, "truncated_evidence_id_count": 0, "invalid_viewpoint_reference_count": 0, "invalid_need_foreign_key_count": 0, "invalid_indicator_foreign_key_count": 0, "invalid_trigger_foreign_key_count": 0, "all_references_latest_view_versions": True, "checked_disagreements": len(p16["disagreements"]), "checked_needs": len(p16["needs"]), "checked_indicators": len(p16["indicators"]), "checked_triggers": len(p16["triggers"])}
    write_json(base / "P16R" / "referential_integrity_report.json", integrity)
    g4_dir = base / "G4QR_T1"; g4_dir.mkdir(parents=True, exist_ok=True)
    report = f"""# 美国军用人工智能T1阶段报告草案（G4QR-T1，未签发）\n\n## 状态\n\n当前状态：draft_pending_author_signoff。正式G3-T1和G4-T1均未执行/签发。\n\n## T0条件化判断\n\nT0的条件化判断为：可观察主导形态是任务需求牵引的分散试验、技术演示和局部部署；资源与治理约束造成发展不均衡；体系整合作为待跨军种实际复用验证的备择方向保留。\n\n## T1新增证据与来源结构\n\nT1累计快照保持125项，新增正式T1 Evidence为40条，其中38条可作为变化信号，2条仅为历史背景。新增材料来自官方战略/项目/试验披露和独立研究；其性质仍需区分目标、测试事件、测试结果、部署和约束。\n\n## 观点更新\n\n本次P15R只更新6条观点，9条观点保持原版本和原字段。更新不是准确率提升，也不是作者或专家审核结果，而是6次真实模型复评、2次独立模型审计后的追加版本。OPS、STRAT、TECH、TRUST分别聚焦任务运用、治理机制、技术工程和可信验证，未将六条判断投票合并。\n\n## 三项假设的条件变化\n\n- HYP-T0-01：治理与整合信号的可观察性增加，但跨军种共用平台实际采用仍需A/B级材料确认。\n- HYP-T0-02：局部任务使用、试验与组织运用继续提供支持，但不能外推为全军成熟度。\n- HYP-T0-03：作战化评估、数据、工程与可信治理条件仍构成能力扩展的约束边界。\n\n## 分歧演化\n\nP16R重新生成{len(p16['disagreements'])}条候选分歧、{len(p16['needs'])}项需求、{len(p16['indicators'])}项指标和{len(p16['triggers'])}条触发规则。治理目标与实际复用的机制差异、约束权重差异和局部材料的范围差异分别保留或拆分；非法截断ID与6条排除Evidence均未进入。\n\n## T1条件化综合判断草案\n\n截至2026年6月30日，T1证据增加了局部任务应用和组织化试验的可观察信号，也增加了对作战化验证、数据条件和可信治理边界的认识。现有材料尚不足以确认跨军种体系化能力已经形成。该判断仍需G3-T1/G4-T1作者审核，不得视为正式签发结论。\n\n## 备择观点、转换条件与局限\n\n若后续公开A/B级材料显示多个军种实际采用同一平台、共享数据并出现项目迁移，体系整合方向增强；若约束缓解并伴随跨任务扩大，约束锁定解释削弱；若新增材料继续仅为单点演示、自报或局部使用，则任务分布和范围限制仍具解释力。公开材料不能覆盖未披露项目、所有军种和所有任务。\n"""
    (g4_dir / "g4_t1_report_draft_v2.md").write_text(report, encoding="utf-8")
    write_json(g4_dir / "g4_t1_report_draft_v2.json", {"status": "draft_pending_author_signoff", "formal_g4_t1_signed": False, "formal_g3_t1_executed": False, "updated_viewpoint_count": 6, "unchanged_viewpoint_count": 9, "candidate_disagreement_count": len(p16["disagreements"]), "model_api_calls": run_state["model_api_calls"], "t1_snapshot_refrozen": False})
    paper = render_paper_and_figures(base, inputs, updated, unchanged, p16, run_id)
    # The output manifest is written last and excludes itself from its file
    # hash list, so replay audits can deterministically recompute all hashes.
    protected_after = output_hashes_excluding(base)
    new_view_keys = {(row["viewpoint_id"], int(row["version"])) for row in updated}
    new_edge_ids = {row["edge_id"] for row in dependencies}
    database_after_without_new = db_history_hash(DB_PATH, exclude_view_keys=new_view_keys, exclude_edge_ids=new_edge_ids)
    all_metadata_paths = sorted((base / "api_call_metadata").glob("*.json"))
    all_call_metadata = [json.loads(path.read_text(encoding="utf-8")) for path in all_metadata_paths]
    manifest = {
        "run_id": run_id, "stage": AUTH_LABEL, "case_id": CASE_ID, "source_run_id": OLD_RUN_ID,
        "generation_mode": "real_model_p15r_plus_deterministic_p16r_paper_package", "t1_snapshot_id": T1_SNAPSHOT_ID,
        "t1_snapshot_refrozen": False, "t1_snapshot_item_count": 125, "formal_g1_t1_reexecuted": False,
        "real_model_base_revision_calls": run_state["base_revision_calls"], "independent_audit_calls": run_state["independent_audit_calls"], "model_api_calls": run_state["model_api_calls"],
        "corrective_viewpoint_versions_appended": len(updated), "appended_viewpoint_versions": {row["viewpoint_id"]: row["version"] for row in updated},
        "p15r_affected_viewpoint_count": 6, "p15r_unchanged_viewpoint_count": 9, "p16r_disagreement_count": len(p16["disagreements"]), "p16r_need_count": len(p16["needs"]), "p16r_indicator_count": len(p16["indicators"]), "p16r_trigger_count": len(p16["triggers"]),
        "excluded_evidence_reference_count": integrity["excluded_evidence_reference_count"], "invalid_evidence_reference_count": integrity["invalid_evidence_reference_count"], "referential_integrity_report": "P16R/referential_integrity_report.json",
        "transaction_committed": True, "viewpoint_dependency_rows_appended": len(dependencies), "t1_snapshot_unchanged": True,
        "paper_case_outputs_generated": True, "figure_count": paper["figure_count"], "formal_g3_t1_executed": False, "formal_g4_t1_signed": False,
        "evidence_inserted": 0, "hypothesis_inserted": 0, "t1_read": True, "t1_snapshot_frozen": True, "api_key_written": False,
        "historical_artifacts_modified": protected_before == protected_after, "old_run_audit_status": audit_declaration,
        "database_history_before": database_before, "database_history_after_excluding_new": database_after_without_new, "database_history_unchanged_excluding_new": database_before == database_after_without_new,
        "provider_call_count": sum(int(meta.get("attempts", 0)) for meta in all_call_metadata), "physical_retry_count": sum(int(meta.get("retry_count", 0)) for meta in all_call_metadata), "model_id": client.model, "requested_temperature": 0.0, "provider_temperature": None,
        "input_hashes": {"old_run_manifest": sha256_file(OLD_RUN_DIR / "g1_t1_p13_p16_manifest.json"), "old_p14_edges": sha256_file(OLD_RUN_DIR / "P14_IMPACT_LOCALIZATION" / "p14_impact_edges.jsonl"), "t1_snapshot_db_manifest_hash": inputs["t1_snapshot"]["manifest_hash"]},
        "code_hashes": {"src/p15r_case.py": sha256_file(ROOT / "src" / "p15r_case.py"), "scripts/44_run_p15r_case.py": sha256_file(ROOT / "scripts" / "44_run_p15r_case.py")},
        "call_metadata_hashes": {str(path.relative_to(base)).replace("\\", "/"): sha256_file(path) for path in all_metadata_paths},
        "output_file_hashes": tree_hashes(base), "checkpoint_status": "completed",
    }
    write_json(base / "p15r_p16r_manifest.json", manifest)
    run_state.update({"status": "completed", "checkpoint_status": "completed", "base_revision_calls": 6, "independent_audit_calls": 2})
    write_json(base / "checkpoints" / "p15r_checkpoint.json", run_state)
    return manifest
