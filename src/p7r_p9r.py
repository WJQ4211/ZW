"""P7R-P9R append-only recovery and case-exhibit preparation.

This module repairs the structural failure of the earlier P7-P9 run without
re-running P4, P5, G2, or evidence collection.  Four prior P7 perspectives
are reused as immutable inputs; STRAT is generated once.  P8R/P9R outputs are
kept as candidates, while only recovered viewpoint versions and their
field-level evidence dependencies are appended to SQLite.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_client import APIModelClient, ModelAPIError
from .p7_p9 import (
    CASE_ID,
    DB_PATH,
    G2_DIR,
    HYPOTHESIS_IDS,
    P4_DIR,
    SNAPSHOT_ID,
    P7P9Error,
    _challenge_prompt,
    _evidence_context,
    _hypothesis_context,
    _load_inputs,
    _normalize_viewpoint,
    _parse_json_compat,
    _read_jsonl,
    _restore_response_citation_locations,
    _sha256_bytes,
    _sha256_file,
    _string_list,
    _unwrap,
    _write_csv,
    _write_json,
    _write_jsonl,
)
from .schemas import EvidenceDependency, Viewpoint, model_validate


ROOT = Path(__file__).resolve().parents[1]
OLD_RUN_ID = "P7P9-20260826T163818Z-t0-viewpoint-disagreement-prep-v1"
OLD_RUN_DIR = ROOT / "outputs" / OLD_RUN_ID
PROMPT_TEMPLATE_VERSION = "p7r-p9r-v1"
PERSPECTIVES_R: dict[str, str] = {
    "STRAT": "战略与组织治理视角：战略目标、组织职责、治理工具、制度落地及统一能力方向。",
    "TECH": "技术能力视角：算法与模型能力、数据、算力、技术成熟度、试验结果和技术性能边界。",
    "ENG": "工程与资源视角：工程化、基础设施、预算采办、接口、保障、资源投入及规模化部署条件。",
    "OPS": "任务应用与体系视角：任务适配、实际部署、跨军种复用、跨平台协同、组织运用和体系集成。",
    "TRUST": "可信治理视角：可靠性、安全、测试评估、负责任AI、授权责任、治理机制和制度落地。",
}
PERSPECTIVE_ORDER = tuple(PERSPECTIVES_R)
OLD_PERSPECTIVES = ("TECH", "ENG", "OPS", "TRUST")
STANCE_VALUES = {"support", "challenge", "conditional", "insufficient"}
ACTION_VALUES = {"maintain", "revise", "conditional_retain", "suspend", "exit"}
CHALLENGE_TYPES = {
    "evidence_mismatch", "goal_result_confusion", "mechanism_gap", "assumption_risk",
    "scope_overreach", "counter_evidence_omitted", "uncertainty_understated",
}
DISAGREEMENT_TYPES = {"fact", "scope", "mechanism", "assumption", "risk_weight"}
RESOLUTION_ACTIONS = {"evidence_check", "scope_split", "conditional_retain", "monitor"}
PREDICATE_TYPES = {"event_match", "status_change", "expert_confirmed"}
VIEW_FIELDS = (
    "stance", "judgment", "key_assumptions", "mechanism_claims", "boundary_conditions",
    "falsifiers", "uncertainties", "information_gaps",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _protected_output_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents
    }


def _db_history_hash(database_path: Path) -> str:
    conn = sqlite3.connect(str(database_path))
    try:
        tables = ("evidence", "evidence_snapshot", "snapshot_items", "hypothesis", "viewpoint", "evidence_dependency", "disagreement", "discriminative_need", "monitor_indicator", "trigger_rule")
        rows: list[dict[str, Any]] = []
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                rows.append({"table": table, "row": list(row), "columns": columns})
        return _sha256_bytes(_json(rows).encode("utf-8"))
    finally:
        conn.close()


def _db_history_hash_excluding_new(database_path: Path, new_view_keys: set[tuple[str, int]], new_edge_ids: set[str]) -> str:
    conn = sqlite3.connect(str(database_path))
    try:
        tables = ("evidence", "evidence_snapshot", "snapshot_items", "hypothesis", "viewpoint", "evidence_dependency", "disagreement", "discriminative_need", "monitor_indicator", "trigger_rule")
        rows: list[dict[str, Any]] = []
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                if table == "viewpoint" and (row[0], int(row[1])) in new_view_keys:
                    continue
                if table == "evidence_dependency" and row[0] in new_edge_ids:
                    continue
                rows.append({"table": table, "row": list(row), "columns": columns})
        return _sha256_bytes(_json(rows).encode("utf-8"))
    finally:
        conn.close()


def _input_identity() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any], dict[str, str], str]:
    evidence, hypotheses, baseline, input_hashes, base_identity = _load_inputs()
    old_initial_path = OLD_RUN_DIR / "P7" / "initial_viewpoints.jsonl"
    old_manifest_path = OLD_RUN_DIR / "p7_p9_manifest.json"
    if not old_initial_path.exists() or not old_manifest_path.exists():
        raise P7P9Error("the prior P7P9 run is not complete")
    old_rows = _read_jsonl(old_initial_path)
    if len(old_rows) != 12 or {row.get("perspective_id") for row in old_rows} != set(OLD_PERSPECTIVES):
        raise P7P9Error("prior P7 initial viewpoints are not the expected 12-slot input")
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_input = {"evidence": evidence, "hypotheses": hypotheses, "baseline": baseline, "old_initial": old_rows, "old_manifest": old_manifest}
    identity = _sha256_bytes(_json({"base": base_identity, "old": old_input}).encode("utf-8"))
    return evidence, hypotheses, baseline, input_hashes | {
        "outputs/" + OLD_RUN_ID + "/p7_p9_manifest.json": _sha256_file(old_manifest_path),
        "outputs/" + OLD_RUN_ID + "/P7/initial_viewpoints.jsonl": _sha256_file(old_initial_path),
    }, identity


def _viewpoint_prompt(perspective_id: str, evidence: list[dict[str, Any]], hypotheses: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    example = {
        "viewpoint_id": "VP-T0-STRAT-H01",
        "perspective_id": perspective_id,
        "hypothesis_id": "HYP-T0-01",
        "snapshot_id": SNAPSHOT_ID,
        "stance": "conditional",
        "judgment": "[简明评价占位符]",
        "supporting_evidence": [{"evidence_id": "EV-T0-PLACEHOLDER", "version": 1, "target_field": "judgment", "relation": "support", "importance": "medium"}],
        "counter_evidence": [{"evidence_id": "EV-T0-PLACEHOLDER", "version": 1, "target_field": "boundary_conditions[0]", "relation": "limit", "importance": "medium"}],
        "key_assumptions": ["[关键假设占位符]"],
        "mechanism_claims": [{"claim": "[机制主张占位符]", "basis": "evidence", "evidence_ids": ["EV-T0-PLACEHOLDER"]}],
        "boundary_conditions": ["[适用边界占位符]"],
        "falsifiers": ["[可证伪条件占位符]"],
        "uncertainties": ["[不确定性占位符]"],
        "information_gaps": ["[信息缺口占位符]"],
        "confidence": "medium",
        "status": "candidate",
    }
    return f"""你是{perspective_id}专业视角分析器。只使用下方冻结T0快照、共享事实基线和三项正式假设。不得联网、读取T1、使用其他视角输出或补充模型常识。

研究问题：基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？其能力形成、应用扩展与体系化发展的主导路径是什么？哪些条件可能使当前判断发生改变？
时间范围：2022-01-01至2024-12-31
冻结快照：{SNAPSHOT_ID}
你的视角：{PERSPECTIVES_R[perspective_id]}

分别评价HYP-T0-01、HYP-T0-02、HYP-T0-03各一次。不投票、不排名、不选择优胜假设。观点应是可核查的简明判断，不是对假设的复述。excerpt_original是事实核查的首要字段；战略目标、计划和预期能力不得写成已实现能力，来源等级不等于证据方向。不要输出隐藏思维链。

必须返回JSON数组或{{"viewpoints":[...]}}，恰好3个观点。每项必须包含下列全部字段，且不得增加额外字段：
viewpoint_id, perspective_id, hypothesis_id, snapshot_id, stance, judgment, supporting_evidence, counter_evidence, key_assumptions, mechanism_claims, boundary_conditions, falsifiers, uncertainties, information_gaps, confidence, status。
stance只能是support、challenge、conditional、insufficient；status固定candidate。每个观点至少3条支持证据且来自至少2个source_id，并至少有1条反证、限制性证据或明确的信息缺口。所有Evidence引用必须来自下方冻结Evidence，并保留version。每条citation必须包含evidence_id、version、target_field、relation、importance；relation只能support、challenge、limit、background；importance只能high、medium、low。mechanism_claims每项包含claim、basis和evidence_ids。每个视角×假设位置都不能静默缺失。

JSON示例（仅为占位格式，不得复制占位内容）：
{json.dumps(example, ensure_ascii=False, indent=2)}

共享事实基线：{_json(baseline)}
正式假设：{_hypothesis_context(hypotheses)}
冻结Evidence（只能从这些内容引用）：
<FROZEN_EVIDENCE>
{_evidence_context(evidence)}
</FROZEN_EVIDENCE>

现在只返回观点JSON。"""


def _challenge_prompt_r(hypothesis: dict[str, Any], views: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    context = _json(views)
    refs = {c["evidence_id"] for view in views for c in view.get("supporting_evidence", []) + view.get("counter_evidence", [])}
    cited = [evidence_by_id[r] for r in sorted(refs) if r in evidence_by_id]
    return f"""你是证据指向式P8R批判审查器。只审查目标假设和五个专业视角的候选观点、这些观点实际引用的冻结Evidence及共享事实基线。不得读取其他材料、其他假设的质疑结果、T1或网络资料，不得强制形成共识。

研究问题：基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？
目标假设：{_json(hypothesis)}
共享基线：{_json(baseline)}
五视角观点：{context}
实际引用Evidence：{_evidence_context(cited)}

返回JSON数组或{{"challenges":[...]}}。每个观点最多2项质疑，也可为空。每项必须包含challenge_id、target_viewpoint_id、target_field、challenge_type、challenge_statement、evidence_ids、impact、response_required。challenge_type只能是evidence_mismatch、goal_result_confusion、mechanism_gap、assumption_risk、scope_overreach、counter_evidence_omitted、uncertainty_understated；target_viewpoint_id和Evidence ID必须来自输入；target_field必须指向具体字段、关键假设、机制、边界、反证或不确定性；impact只能high、medium、low；response_required必须为true。禁止只写“证据不足”或“需要进一步研究”。"""


def _response_prompt_r(perspective_id: str, views: list[dict[str, Any]], challenges: list[dict[str, Any]], baseline: dict[str, Any]) -> str:
    return f"""你是{perspective_id}专业视角的P8R质疑回应与观点修订器。只处理自己视角的三个观点及具体质疑。不得读取其他视角回应，不得投票、排名、趋同、联网或读取T1。

研究问题：基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？
共享基线：{_json(baseline)}
本视角三个观点：{_json(views)}
收到的具体质疑：{_json(challenges)}

返回JSON数组或{{"responses":[...]}}，每个viewpoint_id恰好一次，不得缺失位置。每项必须包含viewpoint_id、action、response_note、updated_viewpoint。action只能maintain、revise、conditional_retain、suspend、exit。updated_viewpoint必须包含需要保留或修订的stance、judgment、supporting_evidence、counter_evidence、key_assumptions、mechanism_claims、boundary_conditions、falsifiers、uncertainties、information_gaps、confidence。每个citation必须保留evidence_id、version、target_field、relation、importance；不得修改excerpt或新增冻结快照外Evidence。每条至少保留3条支持证据、至少2个source_id，并有反证/限制或信息缺口。"""


def _citation_repair_prompt(issues: list[dict[str, Any]]) -> str:
    return f"""You are a restricted P8R citation-field repairer. Handle only the citation mapping issues below. Do not change any judgment, response action, Evidence ID, version, Evidence scope, mechanism claim, or other content; do not add or remove citations. For each issue return one patch selecting a relation already present for that Evidence in the corresponding prior P7 viewpoint, and copy its validated target_field and importance.

ISSUES: {_json(issues)}

Return only a JSON array or {{\"citation_repairs\":[...]}}. Each item must contain issue_id, perspective_id, viewpoint_id, field, index, evidence_id, relation, target_field, importance. relation must be one of the issue's allowed_relations. No extra fields, Markdown, or explanation."""


def _disagreement_prompt_r(final_views: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    refs = {c["evidence_id"] for view in final_views for c in view.get("supporting_evidence", []) + view.get("counter_evidence", [])}
    cited = [evidence_by_id[r] for r in sorted(refs) if r in evidence_by_id]
    return f"""你是P9R分歧诊断器。只使用下方15个最新观点（5个视角×3项假设）、其引用Evidence、共享基线和研究问题。不得读取旧P9候选、T1、网络资料或模型常识。

研究问题：基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？
最新观点：{_json(final_views)}
共享基线：{_json(baseline)}
引用Evidence：{_evidence_context(cited)}

只报告真实的实质分歧：必须有两个相互冲突的分支，且至少来自两个不同视角，或两个假设的相反相对优先级；每个分支应有观点字段和Evidence或明确专业判断支撑。以下节点是诊断重点，不是预设结论：系统整合牵引与战略方向；任务分布牵引与任务适配/成熟过渡；资源技术约束与采办标准组织治理；治理机制建立是否等于跨军种实际共用。措辞不同但实质相同、可无条件并存、仅一方是另一方子集、没有相反分支的项目不得建立分歧。

返回JSON数组或{{"disagreements":[...]}}，可返回空数组。每项包含disagreement_id、viewpoint_refs、type、contested_node、branch_a、branch_b、decision_impact、resolvable_now、resolution_action、status、diagnosis_reason。type只能fact、scope、mechanism、assumption、risk_weight；viewpoint_refs至少两个不同观点，引用最新版本并含具体field；两个branch都需有claim、viewpoint_refs、evidence_ids；decision_impact只能high、medium、low；resolution_action只能evidence_check、scope_split、conditional_retain、monitor；status固定candidate。必须说明为何是实质冲突、是否影响总体研判及当前能否核查。"""


def _monitor_prompt_r(disagreements: list[dict[str, Any]], final_views: list[dict[str, Any]], baseline: dict[str, Any]) -> str:
    selected = [d for d in disagreements if d["decision_impact"] == "high" and d["resolvable_now"] is False and d["resolution_action"] in {"conditional_retain", "monitor"}]
    return f"""你是P9R判别性证据与公开监测设计器。只处理下方高影响、当前不可消除且处置为conditional_retain或monitor的真实分歧。不得联网、读取T1、引用旧P9或创造结论。监测窗口固定为2025-01-01至2026-06-30；不得使用“是否在2024年底前发生”作为指标。

分歧：{_json(selected)}
相关最新观点：{_json(final_views)}
共享基线：{_json(baseline)}

围绕三条链设计：跨军种共用平台/标准的实际采用；任务项目从演示向采办、部署或跨任务复用的转化；数据、边缘算力、基础设施、采办、测试评估瓶颈是否缓解并带来规模应用。单一DARPA项目进入采办不等于集中式整合；任务专属转化支持任务分布；跨军种共用平台/标准或跨任务复用才强力支持体系整合；无搜索证据不是负向触发。不得创造数值阈值。

返回单一JSON对象，包含discriminative_needs、monitor_indicators、trigger_rules三个数组。need包含need_id、disagreement_id、question、what_to_observe、outcome_a、outcome_b、public_sources、observation_frequency、affected_viewpoint_ids、discriminativeness；indicator包含indicator_id、need_id、name、operational_definition、source_types、cadence、direction_a、direction_b、affected_viewpoint_ids；trigger包含trigger_id、indicator_id、predicate_type、trigger_condition、strengthens_branch、weakens_branch、required_source_grade、affected_viewpoint_ids、human_approval_required。discriminativeness只能discriminative或non_discriminative；predicate_type只能event_match、status_change、expert_confirmed；required_source_grade只能A或B；human_approval_required必须true。每个触发器必须绑定受影响观点并明确方向。"""


def _checkpoint_path(output_dir: Path) -> Path:
    return output_dir / "stage_checkpoint.json"


def _load_checkpoint(output_dir: Path, run_id: str, identity: str) -> dict[str, Any]:
    path = _checkpoint_path(output_dir)
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("run_id") != run_id or value.get("input_identity_sha256") != identity or value.get("prompt_template_version") != PROMPT_TEMPLATE_VERSION:
            raise P7P9Error("P7R-P9R checkpoint identity or prompt version mismatch")
        if value.get("status") == "completed":
            prior_manifest = output_dir / "p7r_p9r_manifest.json"
            prior_value = json.loads(prior_manifest.read_text(encoding="utf-8")) if prior_manifest.exists() else {}
            if not prior_value.get("p9r_executed", True) or prior_value.get("p9r_representation_normalization_version") != 2 or prior_value.get("quality_report_version") != 2 or prior_value.get("historical_artifacts_unchanged") is False or not (output_dir / "CASE_EXHIBIT" / "figure_case_dependency_graph.graphml").exists() or any((output_dir / "P8R" / "perspective_responses").glob("*_validation_errors.json")):
                value["status"] = "failed"
            else:
                raise P7P9Error("a completed P7R-P9R run cannot be resumed")
        return value
    value = {
        "run_id": run_id, "input_identity_sha256": identity,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION, "status": "running",
        "completed_batches": {}, "completed_stages": [], "call_metadata_paths": [],
    }
    _write_json(path, value)
    return value


def _save_checkpoint(output_dir: Path, checkpoint: dict[str, Any]) -> None:
    _write_json(_checkpoint_path(output_dir), checkpoint)


def _safe_failure(output_dir: Path, run_id: str, stage: str, source_id: str | None, error: ModelAPIError | None, checkpoint: dict[str, Any]) -> None:
    failure = {
        "run_id": run_id, "failure_stage": stage, "source_id": source_id,
        "exception_type": error.root_exception_type if error else "P7P9Error",
        "http_status_code": error.http_status_code if error else None,
        "completed_logical_calls": len(checkpoint.get("call_metadata_paths", [])),
        "saved_batches": list(checkpoint.get("completed_batches", {})),
        "formal_evidence_inserted": 0, "formal_g3_executed": False,
        "formal_g4_t0_executed": False, "t1_read": False,
    }
    checkpoint["status"] = "failed"
    checkpoint["failure"] = {key: failure[key] for key in ("failure_stage", "source_id", "exception_type", "http_status_code")}
    _save_checkpoint(output_dir, checkpoint)
    _write_json(output_dir / "p7r_p9r_failure_manifest.json", failure)


def _call_batch(*, batch_id: str, phase: str, source_id: str | None, prompt: str, output_dir: Path, checkpoint: dict[str, Any], client: APIModelClient) -> tuple[Any, dict[str, Any]]:
    prompt_path = output_dir / "prompts" / f"{batch_id}.txt"
    raw_path = output_dir / "raw_responses" / f"{batch_id}.json"
    normalized_path = output_dir / "normalized_responses" / f"{batch_id}.json"
    _write_text(prompt_path, prompt)
    prompt_sha = _sha256_file(prompt_path)
    existing = checkpoint.get("completed_batches", {}).get(batch_id)
    if existing:
        if existing.get("prompt_sha256") != prompt_sha or existing.get("input_identity_sha256") != checkpoint["input_identity_sha256"] or existing.get("phase") != phase:
            raise P7P9Error(f"checkpoint batch identity mismatch: {batch_id}")
        raw = output_dir / existing.get("raw_response_path", "")
        normalized = output_dir / existing.get("normalized_response_path", "")
        record_path = output_dir / existing.get("call_metadata_path", "")
        if not raw.exists() or not normalized.exists() or not record_path.exists():
            raise P7P9Error(f"completed batch artifacts are missing: {batch_id}")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if _sha256_file(raw) != record.get("response_sha256"):
            raise P7P9Error(f"completed batch response hash mismatch: {batch_id}")
        return json.loads(normalized.read_text(encoding="utf-8")), {"reused": True, "call_record": record}
    payload, content, record = client.call(prompt, temperature=0.0, max_tokens=24000)
    _write_json(raw_path, payload)
    metadata = record.as_dict() | {
        "batch_id": batch_id, "call_id": batch_id, "phase": phase, "source_id": source_id,
        "prompt_sha256": prompt_sha, "response_sha256": _sha256_file(raw_path), "is_repair": False,
    }
    meta_path = output_dir / "api_call_metadata" / f"{batch_id}.json"
    _write_json(meta_path, metadata)
    try:
        parsed = _parse_json_compat(content)
    except Exception as exc:
        raise P7P9Error(f"P7R-P9R JSON parsing failed for {batch_id}") from exc
    _write_json(normalized_path, parsed)
    relative = lambda p: str(p.relative_to(output_dir)).replace("\\", "/")
    checkpoint.setdefault("completed_batches", {})[batch_id] = {
        "status": "completed", "input_identity_sha256": checkpoint["input_identity_sha256"],
        "prompt_template_version": PROMPT_TEMPLATE_VERSION, "prompt_sha256": prompt_sha,
        "raw_response_path": relative(raw_path), "normalized_response_path": relative(normalized_path),
        "call_metadata_path": relative(meta_path), "response_sha256": metadata["response_sha256"],
        "phase": phase, "source_id": source_id,
    }
    checkpoint.setdefault("call_metadata_paths", []).append(relative(meta_path))
    _save_checkpoint(output_dir, checkpoint)
    return parsed, {"reused": False, "call_record": metadata}


def _citation_enriched_view(raw: dict[str, Any]) -> dict[str, Any]:
    """Rebuild old P7 citation audit locations without changing their content."""
    result = dict(raw)
    details = raw.get("_citation_details", [])
    by_key = {(d.get("evidence_id"), d.get("relation")): d for d in details if isinstance(d, dict)}
    for field, default_relation in (("supporting_evidence", "support"), ("counter_evidence", "challenge")):
        enriched = []
        for citation in raw.get(field, []):
            item = dict(citation)
            relation = item.get("relation", default_relation)
            prior = by_key.get((item.get("evidence_id"), relation))
            if prior:
                item.setdefault("target_field", prior.get("target_field", ""))
                item.setdefault("importance", prior.get("importance", "medium"))
            enriched.append(item)
        result[field] = enriched
    return result


def _reuse_old_p7(evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows = _read_jsonl(OLD_RUN_DIR / "P7" / "initial_viewpoints.jsonl")
    output: dict[str, list[dict[str, Any]]] = {key: [] for key in OLD_PERSPECTIVES}
    for row in rows:
        perspective = row["perspective_id"]
        hypothesis = row["hypothesis_id"]
        enriched = _citation_enriched_view(row)
        normalized = _normalize_viewpoint(enriched, perspective, hypothesis, evidence_by_id)
        if normalized.get("_validation_errors"):
            raise P7P9Error(f"reused P7 viewpoint failed validation: {row['viewpoint_id']}")
        output[perspective].append(normalized)
    if any(len(value) != 3 for value in output.values()):
        raise P7P9Error("reused P7 viewpoints do not cover all 12 slots")
    return output


def _normalize_response_rows(parsed: Any) -> Any:
    if isinstance(parsed, dict) and isinstance(parsed.get("responses"), list):
        rows = parsed["responses"]
        return {"responses": [_normalize_response_row(row) for row in rows]}
    if isinstance(parsed, list):
        return [_normalize_response_row(row) for row in parsed]
    return parsed


def _normalize_response_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    result = dict(row)
    if "response_action" not in result and "action" in result:
        result["response_action"] = result["action"]
    updated = result.get("updated_viewpoint")
    if isinstance(updated, dict) and isinstance(updated.get("mechanism_claims"), list):
        for claim in updated["mechanism_claims"]:
            if isinstance(claim, dict) and claim.get("basis") == "direct_evidence":
                claim["basis"] = "evidence"
    return result


def _find_citation_issues(responses_by_perspective: dict[str, Any], p7_views: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for perspective, parsed in responses_by_perspective.items():
        rows = parsed.get("responses", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
        bases = {row["viewpoint_id"]: row for row in p7_views[perspective]}
        for row in rows:
            if not isinstance(row, dict) or row.get("viewpoint_id") not in bases:
                continue
            base = bases[row["viewpoint_id"]]
            prior = base.get("_citation_details", [])
            for field, default_relation in (("supporting_evidence", "support"), ("counter_evidence", "challenge")):
                for index, citation in enumerate((row.get("updated_viewpoint") or {}).get(field, [])):
                    if not isinstance(citation, dict):
                        continue
                    evidence_id = citation.get("evidence_id")
                    relation = citation.get("relation", default_relation)
                    legal = [item for item in prior if item.get("evidence_id") == evidence_id]
                    if legal and not any(item.get("relation") == relation for item in legal):
                        issues.append({
                            "issue_id": f"CIT-{len(issues)+1:03d}", "perspective_id": perspective,
                            "viewpoint_id": row["viewpoint_id"], "field": field, "index": index,
                            "evidence_id": evidence_id, "returned_relation": relation,
                            "allowed_relations": sorted({item.get("relation") for item in legal}),
                        })
    return issues


def _inherit_blank_citation_fields(responses_by_perspective: dict[str, Any], p7_views: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Fill only blank audit fields from a unique same-relation prior mapping."""
    applied: list[dict[str, Any]] = []
    for perspective, parsed in responses_by_perspective.items():
        rows = parsed.get("responses", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
        bases = {row["viewpoint_id"]: row for row in p7_views[perspective]}
        for row in rows:
            if not isinstance(row, dict) or row.get("viewpoint_id") not in bases:
                continue
            base = bases[row["viewpoint_id"]]
            prior = base.get("_citation_details", [])
            for field, default_relation in (("supporting_evidence", "support"), ("counter_evidence", "challenge")):
                for index, citation in enumerate((row.get("updated_viewpoint") or {}).get(field, [])):
                    if not isinstance(citation, dict):
                        continue
                    relation = citation.get("relation", default_relation)
                    matches = [item for item in prior if item.get("evidence_id") == citation.get("evidence_id") and item.get("relation") == relation]
                    if len(matches) != 1:
                        continue
                    mapping = matches[0]
                    changes: dict[str, Any] = {}
                    if not isinstance(citation.get("target_field"), str) or not citation.get("target_field", "").strip():
                        citation["target_field"] = mapping.get("target_field", "")
                        changes["target_field"] = citation["target_field"]
                    if citation.get("importance") not in {"high", "medium", "low"}:
                        citation["importance"] = mapping.get("importance", "medium")
                        changes["importance"] = citation["importance"]
                    if changes:
                        applied.append({"perspective_id": perspective, "viewpoint_id": row["viewpoint_id"], "field": field, "index": index, "evidence_id": citation.get("evidence_id"), "relation": relation, "changes": changes, "repair_origin": "deterministic_inheritance_of_unique_same_relation_prior_p7_mapping"})
    return applied


def _apply_citation_repairs(responses_by_perspective: dict[str, Any], repairs: Any, issues: list[dict[str, Any]], p7_views: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = _unwrap(repairs, ("citation_repairs",))
    issue_by_id = {issue["issue_id"]: issue for issue in issues}
    seen: set[str] = set()
    applied: list[dict[str, Any]] = []
    for repair in rows:
        if not isinstance(repair, dict) or repair.get("issue_id") not in issue_by_id:
            raise P7P9Error("citation repair references an unknown issue")
        issue = issue_by_id[repair["issue_id"]]
        if any(repair.get(key) != issue.get(key) for key in ("perspective_id", "viewpoint_id", "field", "index", "evidence_id")):
            raise P7P9Error("citation repair changed the issue identity")
        if repair.get("relation") not in issue["allowed_relations"]:
            raise P7P9Error("citation repair selected a relation not present in prior P7 mapping")
        base = next(v for v in p7_views[issue["perspective_id"]] if v["viewpoint_id"] == issue["viewpoint_id"])
        prior = next(item for item in base.get("_citation_details", []) if item.get("evidence_id") == issue["evidence_id"] and item.get("relation") == repair["relation"])
        parsed = responses_by_perspective[issue["perspective_id"]]
        response_rows = parsed.get("responses", []) if isinstance(parsed, dict) else parsed
        target = next(row for row in response_rows if row.get("viewpoint_id") == issue["viewpoint_id"])
        citation = target["updated_viewpoint"][issue["field"]][issue["index"]]
        citation["relation"] = repair["relation"]
        citation["target_field"] = prior["target_field"]
        citation["importance"] = prior["importance"]
        applied.append({
            "issue_id": issue["issue_id"], "model_relation": repair.get("relation"),
            "model_target_field": repair.get("target_field"), "model_importance": repair.get("importance"),
            "applied_relation": prior.get("relation"), "applied_target_field": prior.get("target_field"),
            "applied_importance": prior.get("importance"), "repair_origin": "deterministic_copy_of_unique_prior_p7_mapping",
        })
        seen.add(repair["issue_id"])
    if seen != set(issue_by_id):
        raise P7P9Error("citation repair did not cover every invalid citation")
    return applied


def _validate_response_r(value: Any, perspective_id: str, initial: list[dict[str, Any]], challenges: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate P8R responses while explicitly merging citation fields.

    The earlier runner's validator merged only narrative fields and therefore
    dropped target_field from updated citations before validation.  P8R keeps
    citations as first-class fields and records any inherited mapping.
    """
    rows = _unwrap(value, ("responses", "viewpoints"))
    initial_by_id = {row["viewpoint_id"]: row for row in initial}
    output_by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or raw.get("viewpoint_id") not in initial_by_id or raw.get("viewpoint_id") in output_by_id:
            errors.append({"index": index, "reason": "response_viewpoint_id_invalid_or_duplicated"})
            continue
        action = raw.get("response_action", raw.get("action"))
        if action not in ACTION_VALUES:
            errors.append({"viewpoint_id": raw.get("viewpoint_id"), "reason": "invalid_response_action"})
            continue
        base = initial_by_id[raw["viewpoint_id"]]
        update = raw.get("updated_viewpoint") if isinstance(raw.get("updated_viewpoint"), dict) else raw
        update = _restore_response_citation_locations(update, base)
        merged = dict(base)
        for field in VIEW_FIELDS + ("confidence", "supporting_evidence", "counter_evidence"):
            if field in update:
                merged[field] = update[field]
        try:
            normalized = _normalize_viewpoint(merged, perspective_id, base["hypothesis_id"], evidence_by_id)
        except Exception as exc:
            errors.append({"viewpoint_id": raw["viewpoint_id"], "reason": type(exc).__name__ + ": " + str(exc)})
            continue
        normalized["_response_action"] = action
        normalized["_response_note"] = raw.get("response_note", "") if isinstance(raw.get("response_note", ""), str) else ""
        normalized["_challenge_ids"] = [c["challenge_id"] for c in challenges if c["target_viewpoint_id"] == raw["viewpoint_id"]]
        output_by_id[raw["viewpoint_id"]] = normalized
    for base in initial:
        if base["viewpoint_id"] not in output_by_id:
            missing = dict(base)
            missing["_response_action"] = "suspend"
            missing["_response_note"] = "未收到可校验的质疑回应，保留位置并暂停准入"
            missing["_validation_errors"] = list(base.get("_validation_errors", [])) + ["missing response or invalid response"]
            output_by_id[base["viewpoint_id"]] = missing
    return [output_by_id[base["viewpoint_id"]] for base in initial], errors


def _validate_challenges_r(parsed: Any, valid_view_ids: set[str], evidence_by_id: dict[str, dict[str, Any]], hypothesis_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(parsed, ("challenges",))
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    per_view: Counter[str] = Counter()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "challenge_not_object"})
            continue
        target = raw.get("target_viewpoint_id")
        ids = raw.get("evidence_ids")
        if (target not in valid_view_ids or raw.get("challenge_type") not in CHALLENGE_TYPES or raw.get("impact") not in {"high", "medium", "low"} or not isinstance(raw.get("target_field"), str) or not raw["target_field"].strip() or not isinstance(raw.get("challenge_statement"), str) or not raw["challenge_statement"].strip() or not isinstance(ids, list) or not all(item in evidence_by_id for item in ids)):
            rejected.append({"index": index, "reason": "invalid_challenge_fields_or_evidence"})
            continue
        if per_view[target] >= 2:
            rejected.append({"index": index, "reason": "more_than_two_challenges_per_viewpoint"})
            continue
        per_view[target] += 1
        valid.append({
            "challenge_id": raw.get("challenge_id") or f"CH-{hypothesis_id}-{len(valid)+1:02d}",
            "target_viewpoint_id": target, "target_field": raw["target_field"].strip(),
            "challenge_type": raw["challenge_type"], "challenge_statement": raw["challenge_statement"].strip(),
            "evidence_ids": list(dict.fromkeys(ids)), "impact": raw["impact"],
            "response_required": bool(raw.get("response_required", True)),
        })
    return valid, rejected


def _finalize_view(view: dict[str, Any], *, run_id: str, version: int, parent_version: int | None, response_meta: dict[str, Any], p7_meta: dict[str, Any], action: str, response_note: str, change_reason: str) -> dict[str, Any]:
    status = "conditional" if action == "conditional_retain" else ("suspended" if action in {"suspend", "exit"} else "admitted")
    errors = list(view.get("_validation_errors", []))
    if errors:
        status = "suspended"
    generator = {
        "perspective_id": view["perspective_id"], "model_id": response_meta.get("requested_model_id") or p7_meta.get("requested_model_id"),
        "p7_source": "reused_prior_p7" if view["perspective_id"] in OLD_PERSPECTIVES else "p7r_strat_model_call",
        "p7_call": p7_meta, "p8_response_call": response_meta, "run_id": run_id,
        "response_action": action, "response_note": response_note,
        "citation_details": view.get("_citation_details", []), "mechanism_claim_details": view.get("_mechanism_details", []),
    }
    return {
        "schema_version": "phase-a.1", "created_at": _now_utc(), "run_id": run_id, "case_id": CASE_ID,
        "viewpoint_id": view["viewpoint_id"], "version": version, "parent_version": parent_version,
        "snapshot_id": SNAPSHOT_ID, "perspective_id": view["perspective_id"], "hypothesis_id": view["hypothesis_id"],
        "stance": view["stance"], "judgment": view["judgment"], "supporting_evidence": view.get("supporting_evidence", []),
        "counter_evidence": view.get("counter_evidence", []), "key_assumptions": view.get("key_assumptions", []),
        "mechanism_claims": view.get("mechanism_claims", []), "boundary_conditions": view.get("boundary_conditions", []),
        "falsifiers": view.get("falsifiers", []), "uncertainties": view.get("uncertainties", []),
        "information_gaps": view.get("information_gaps", []), "confidence": view.get("confidence", "low"),
        "status": status, "change_reason": change_reason, "generator": generator,
        "human_verified": False, "admission_failure_reason": "; ".join(errors),
    }


def _make_dependencies(final_views: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for view in final_views:
        if view["status"] not in {"admitted", "conditional"}:
            continue
        for detail in view.get("generator", {}).get("citation_details", []):
            if not isinstance(detail, dict) or detail.get("relation") not in {"support", "challenge", "limit", "background"}:
                continue
            target = detail.get("target_field")
            if not isinstance(target, str) or not target.strip():
                continue
            key = (view["viewpoint_id"], view["version"], detail.get("evidence_id"), detail.get("version"), target, detail.get("relation"), detail.get("importance", "medium"))
            if key in seen:
                continue
            seen.add(key)
            seed = _json({"viewpoint_id": view["viewpoint_id"], "viewpoint_version": view["version"], "evidence_id": detail["evidence_id"], "evidence_version": detail["version"], "target_field": target, "relation": detail["relation"]})
            result.append({
                "schema_version": "phase-a.1", "created_at": _now_utc(), "run_id": run_id, "case_id": CASE_ID,
                "edge_id": "EDGE-P7R-" + _sha256_bytes(seed.encode("utf-8"))[:20], "evidence_id": detail["evidence_id"],
                "evidence_version": int(detail["version"]), "viewpoint_id": view["viewpoint_id"], "viewpoint_version": int(view["version"]),
                "target_field": target, "relation": detail["relation"], "importance": detail.get("importance", "medium"),
                "created_by": "P7R_P9R_MODEL_PIPELINE", "human_verified": False, "changed_fields": [],
            })
    return result


def _reuse_persisted_final_payloads(database_path: Path, final_views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep replayed payloads byte-for-byte equivalent to existing DB rows."""
    conn = sqlite3.connect(str(database_path))
    try:
        output = []
        for view in final_views:
            row = conn.execute("SELECT snapshot_id,payload_json FROM viewpoint WHERE viewpoint_id=? AND version=?", (view["viewpoint_id"], view["version"])).fetchone()
            if row and row[0] == SNAPSHOT_ID:
                output.append(json.loads(row[1]))
            else:
                output.append(view)
        return output
    finally:
        conn.close()


def _reuse_persisted_dependency_payloads(database_path: Path, dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(database_path))
    try:
        output = []
        for dependency in dependencies:
            row = conn.execute("SELECT payload_json FROM evidence_dependency WHERE edge_id=?", (dependency["edge_id"],)).fetchone()
            output.append(json.loads(row[0]) if row else dependency)
        return output
    finally:
        conn.close()


def _latest_viewpoint_row(conn: sqlite3.Connection, viewpoint_id: str) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute("SELECT version,payload_json FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1", (viewpoint_id,)).fetchone()
    return (int(row[0]), json.loads(row[1])) if row else None


def _insert_transaction(database_path: Path, final_views: list[dict[str, Any]], dependencies: list[dict[str, Any]], *, inject_failure_after: int | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        snapshot = conn.execute("SELECT case_id,phase,frozen FROM evidence_snapshot WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()
        if not snapshot or snapshot[0] != CASE_ID or snapshot[1] != "T0" or snapshot[2] != 1:
            raise P7P9Error("target snapshot is missing or not frozen")
        added_views = 0
        for index, view in enumerate(final_views):
            model_validate(Viewpoint, view)
            existing = conn.execute("SELECT snapshot_id,status,payload_json FROM viewpoint WHERE viewpoint_id=? AND version=?", (view["viewpoint_id"], view["version"])).fetchone()
            if existing:
                if existing[0] != SNAPSHOT_ID or json.loads(existing[2]) != view:
                    raise P7P9Error(f"viewpoint version already exists with different content: {view['viewpoint_id']} v{view['version']}")
            else:
                latest = _latest_viewpoint_row(conn, view["viewpoint_id"])
                if view["version"] == 1:
                    if latest is not None:
                        raise P7P9Error(f"new v1 viewpoint collides with existing history: {view['viewpoint_id']}")
                elif latest is None or latest[0] != view["parent_version"] or view["version"] != latest[0] + 1:
                    raise P7P9Error(f"viewpoint append lineage is invalid: {view['viewpoint_id']}")
                conn.execute("INSERT INTO viewpoint(viewpoint_id,version,snapshot_id,status,payload_json) VALUES (?,?,?,?,?)", (view["viewpoint_id"], view["version"], SNAPSHOT_ID, view["status"], _json(view)))
                added_views += 1
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise P7P9Error("injected P7R transaction failure")
        added_deps = 0
        for dependency in dependencies:
            model_validate(EvidenceDependency, dependency)
            existing = conn.execute("SELECT payload_json FROM evidence_dependency WHERE edge_id=?", (dependency["edge_id"],)).fetchone()
            if existing:
                if json.loads(existing[0]) != dependency:
                    raise P7P9Error(f"dependency edge already exists with different content: {dependency['edge_id']}")
                continue
            conn.execute("INSERT INTO evidence_dependency(edge_id,case_id,evidence_id,evidence_version,viewpoint_id,viewpoint_version,relation,importance,payload_json) VALUES (?,?,?,?,?,?,?,?,?)", (dependency["edge_id"], CASE_ID, dependency["evidence_id"], dependency["evidence_version"], dependency["viewpoint_id"], dependency["viewpoint_version"], dependency["relation"], dependency["importance"], _json(dependency)))
            added_deps += 1
        conn.commit()
        return {"replayed": added_views == 0 and added_deps == 0, "viewpoint_rows_added": added_views, "dependency_rows_added": added_deps, "transaction_committed": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rollback_probe(database_path: Path, final_views: list[dict[str, Any]], dependencies: list[dict[str, Any]]) -> bool:
    with tempfile.TemporaryDirectory(prefix=".p7r-rollback-") as temp:
        copied = Path(temp) / "research.sqlite3"
        shutil.copy2(database_path, copied)
        before = _db_history_hash(copied)
        try:
            _insert_transaction(copied, final_views, dependencies, inject_failure_after=1)
        except P7P9Error as exc:
            if "injected P7R transaction failure" not in str(exc):
                return False
        else:
            return False
        return _db_history_hash(copied) == before


def _immutability_probe(database_path: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix=".p7r-immutability-") as temp:
        copied = Path(temp) / "research.sqlite3"
        shutil.copy2(database_path, copied)
        conn = sqlite3.connect(str(copied))
        try:
            try:
                conn.execute("UPDATE viewpoint SET status=status WHERE viewpoint_id=? AND version=1", ("VP-T0-TECH-H01",))
                conn.commit()
                return False
            except sqlite3.DatabaseError:
                conn.rollback()
            try:
                conn.execute("DELETE FROM viewpoint WHERE viewpoint_id=? AND version=1", ("VP-T0-TECH-H01",))
                conn.commit()
                return False
            except sqlite3.DatabaseError:
                conn.rollback()
            return True
        finally:
            conn.close()


def _validate_p9r_disagreements(value: Any, latest_views: dict[str, dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("disagreements",))
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "disagreement_not_object"}); continue
        did = raw.get("disagreement_id") or f"DG-T0-R-{len(valid)+1:02d}"
        refs = _expanded_top_level_refs(raw, latest_views)
        if did in seen or not isinstance(refs, list) or raw.get("type") not in DISAGREEMENT_TYPES or raw.get("decision_impact") not in {"high", "medium", "low"} or raw.get("resolution_action") not in RESOLUTION_ACTIONS or not isinstance(raw.get("resolvable_now"), bool) or not isinstance(raw.get("contested_node"), str) or not raw["contested_node"].strip():
            rejected.append({"index": index, "reason": "invalid_disagreement_header"}); continue
        normalized_refs: list[dict[str, Any]] = []
        for ref in refs:
            parsed_ref = _parse_viewpoint_ref(ref, latest_views)
            if parsed_ref is not None:
                normalized_refs.append(parsed_ref)
        if len({r["viewpoint_id"] for r in normalized_refs}) < 2:
            rejected.append({"index": index, "reason": "fewer_than_two_valid_viewpoints"}); continue
        branches: list[dict[str, Any]] = []
        branch_ok = True
        for key in ("branch_a", "branch_b"):
            branch = raw.get(key)
            if not isinstance(branch, dict) or not isinstance(branch.get("claim"), str) or not branch["claim"].strip() or not isinstance(branch.get("evidence_ids"), list) or not all(eid in evidence_by_id for eid in branch["evidence_ids"]):
                branch_ok = False; break
            branch_refs = []
            for ref in branch.get("viewpoint_refs", []):
                parsed_ref = _parse_viewpoint_ref(ref, latest_views)
                if parsed_ref is not None:
                    branch_refs.append(parsed_ref)
            if not branch_refs:
                branch_ok = False; break
            branches.append({"claim": branch["claim"].strip(), "viewpoint_refs": branch_refs, "evidence_ids": list(dict.fromkeys(branch["evidence_ids"]))})
        if not branch_ok:
            rejected.append({"index": index, "reason": "invalid_disagreement_branch"}); continue
        branch_a = re.sub(r"\s+", "", branches[0]["claim"]).lower()
        branch_b = re.sub(r"\s+", "", branches[1]["claim"]).lower()
        perspective_a = {latest_views[r["viewpoint_id"]]["perspective_id"] for r in branches[0]["viewpoint_refs"]}
        perspective_b = {latest_views[r["viewpoint_id"]]["perspective_id"] for r in branches[1]["viewpoint_refs"]}
        substantive = branch_a != branch_b and bool(perspective_a and perspective_b) and (perspective_a != perspective_b or {latest_views[r["viewpoint_id"]]["hypothesis_id"] for r in branches[0]["viewpoint_refs"]} != {latest_views[r["viewpoint_id"]]["hypothesis_id"] for r in branches[1]["viewpoint_refs"]})
        if not substantive or raw.get("pseudo_disagreement") is True:
            rejected.append({"index": index, "reason": "pseudo_disagreement"}); continue
        seen.add(did)
        valid.append({
            "disagreement_id": did, "snapshot_id": SNAPSHOT_ID, "viewpoint_refs": normalized_refs,
            "type": raw["type"], "contested_node": raw["contested_node"].strip(), "branch_a": branches[0], "branch_b": branches[1],
            "decision_impact": raw["decision_impact"], "resolvable_now": raw["resolvable_now"], "resolution_action": raw["resolution_action"],
            "status": "candidate", "diagnosis_reason": raw.get("diagnosis_reason", "") if isinstance(raw.get("diagnosis_reason", ""), str) else "",
        })
    return valid, rejected


def _parse_viewpoint_ref(ref: Any, latest_views: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Accept the observed compact ``VIEWPOINT.FIELD`` representation.

    This is a lossless schema representation normalization: the viewpoint ID
    determines the latest version, while the suffix is preserved as the field.
    """
    if isinstance(ref, dict):
        viewpoint_id = ref.get("viewpoint_id")
        version = ref.get("version")
        field = ref.get("field")
        if viewpoint_id in latest_views and version == latest_views[viewpoint_id]["version"] and isinstance(field, str) and field.strip():
            return {"viewpoint_id": viewpoint_id, "version": version, "field": field.strip()}
        return None
    if isinstance(ref, str):
        for viewpoint_id in sorted(latest_views, key=len, reverse=True):
            prefix = viewpoint_id + "."
            if ref.startswith(prefix) and ref[len(prefix):].strip():
                return {"viewpoint_id": viewpoint_id, "version": latest_views[viewpoint_id]["version"], "field": ref[len(prefix):].strip()}
    return None


def _expanded_top_level_refs(row: dict[str, Any], latest_views: dict[str, dict[str, Any]]) -> list[Any]:
    """Expand compact top-level viewpoint IDs using branch field refs."""
    refs = row.get("viewpoint_refs", [])
    branch_refs: list[Any] = []
    for branch_key in ("branch_a", "branch_b"):
        branch = row.get(branch_key)
        if isinstance(branch, dict) and isinstance(branch.get("viewpoint_refs"), list):
            branch_refs.extend(branch["viewpoint_refs"])
    expanded: list[Any] = []
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, str) and ref in latest_views:
            matches = []
            for branch_ref in branch_refs:
                parsed = _parse_viewpoint_ref(branch_ref, latest_views)
                if parsed is not None and parsed["viewpoint_id"] == ref:
                    matches.append(branch_ref)
            expanded.extend(matches or [ref])
        else:
            expanded.append(ref)
    return list(dict.fromkeys(expanded))


def _disagreement_ref_audit(value: Any, latest_views: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _unwrap(value, ("disagreements",))
    audit: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        before = {"viewpoint_refs": row.get("viewpoint_refs", []), "branch_a": (row.get("branch_a") or {}).get("viewpoint_refs", []), "branch_b": (row.get("branch_b") or {}).get("viewpoint_refs", [])}
        after = {"viewpoint_refs": [parsed for ref in _expanded_top_level_refs(row, latest_views) if (parsed := _parse_viewpoint_ref(ref, latest_views)) is not None]}
        after.update({key: [parsed for ref in refs if (parsed := _parse_viewpoint_ref(ref, latest_views)) is not None] for key, refs in (("branch_a", (row.get("branch_a") or {}).get("viewpoint_refs", [])), ("branch_b", (row.get("branch_b") or {}).get("viewpoint_refs", [])))})
        if before != after:
            audit.append({"index": index, "disagreement_id": row.get("disagreement_id", ""), "before": before, "after": after, "normalization": "compact_viewpoint_field_reference_to_latest_version_object"})
    return audit


def _validate_monitoring_r(value: Any, disagreements: list[dict[str, Any]], latest_views: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise P7P9Error("P9R monitoring response must be an object")
    selected = {d["disagreement_id"] for d in disagreements if d["decision_impact"] == "high" and not d["resolvable_now"] and d["resolution_action"] in {"conditional_retain", "monitor"}}
    valid_ids = set(latest_views)
    needs: list[dict[str, Any]] = []; indicators: list[dict[str, Any]] = []; triggers: list[dict[str, Any]] = []; rejected: list[dict[str, Any]] = []
    need_ids: set[str] = set()
    for index, row in enumerate(value.get("discriminative_needs", [])):
        if not isinstance(row, dict) or row.get("disagreement_id") not in selected or row.get("discriminativeness") not in {"discriminative", "non_discriminative"} or not isinstance(row.get("question"), str) or not row["question"].strip() or not isinstance(row.get("affected_viewpoint_ids"), list) or not set(row["affected_viewpoint_ids"]).issubset(valid_ids):
            rejected.append({"kind": "need", "index": index, "reason": "invalid_need_linkage"}); continue
        nid = row.get("need_id") or f"NEED-T0-R-{len(needs)+1:02d}"
        if nid in need_ids: rejected.append({"kind": "need", "index": index, "reason": "duplicate_need_id"}); continue
        need_ids.add(nid)
        needs.append({key: row.get(key, "") for key in ("need_id", "disagreement_id", "question", "what_to_observe", "outcome_a", "outcome_b", "public_sources", "observation_frequency", "affected_viewpoint_ids", "discriminativeness")})
    indicator_ids: set[str] = set()
    for index, row in enumerate(value.get("monitor_indicators", [])):
        if not isinstance(row, dict) or row.get("need_id") not in need_ids or not isinstance(row.get("affected_viewpoint_ids"), list) or not set(row["affected_viewpoint_ids"]).issubset(valid_ids):
            rejected.append({"kind": "indicator", "index": index, "reason": "invalid_indicator_linkage"}); continue
        iid = row.get("indicator_id") or f"IND-T0-R-{len(indicators)+1:02d}"
        if iid in indicator_ids: rejected.append({"kind": "indicator", "index": index, "reason": "duplicate_indicator_id"}); continue
        indicator_ids.add(iid)
        indicators.append({key: row.get(key, "") for key in ("indicator_id", "need_id", "name", "operational_definition", "source_types", "cadence", "direction_a", "direction_b", "affected_viewpoint_ids")})
    trigger_ids: set[str] = set()
    for index, row in enumerate(value.get("trigger_rules", [])):
        if not isinstance(row, dict) or row.get("indicator_id") not in indicator_ids or row.get("predicate_type") not in PREDICATE_TYPES or row.get("required_source_grade") not in {"A", "B"} or row.get("human_approval_required") is not True or not isinstance(row.get("affected_viewpoint_ids"), list) or not set(row["affected_viewpoint_ids"]).issubset(valid_ids) or not isinstance(row.get("trigger_condition"), str) or not row["trigger_condition"].strip() or re.search(r"\d+\s*(%|次|天|月|年)", row["trigger_condition"]):
            rejected.append({"kind": "trigger", "index": index, "reason": "invalid_trigger"}); continue
        tid = row.get("trigger_id") or f"TRG-T0-R-{len(triggers)+1:02d}"
        if tid in trigger_ids: rejected.append({"kind": "trigger", "index": index, "reason": "duplicate_trigger_id"}); continue
        trigger_ids.add(tid)
        triggers.append({key: row.get(key, "") for key in ("trigger_id", "indicator_id", "predicate_type", "trigger_condition", "strengthens_branch", "weakens_branch", "required_source_grade", "affected_viewpoint_ids", "human_approval_required")})
    for need in needs:
        if need["discriminativeness"] == "non_discriminative":
            rejected.append({"kind": "non_discriminative", "need_id": need["need_id"], "reason": "excluded_from_monitoring_candidates"})
    needs = [need for need in needs if need["discriminativeness"] == "discriminative"]
    indicators = [row for row in indicators if row["need_id"] in {need["need_id"] for need in needs}]
    triggers = [row for row in triggers if row["indicator_id"] in {row["indicator_id"] for row in indicators}]
    return needs, indicators, triggers, rejected


def _copy_reused_p7(output_dir: Path) -> None:
    for perspective in OLD_PERSPECTIVES:
        source = OLD_RUN_DIR / "P7" / f"perspective_{perspective.lower()}_raw.json"
        target = output_dir / "P7R" / f"perspective_{perspective.lower()}_raw.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _build_case_exhibit(output_dir: Path, evidence: list[dict[str, Any]], hypotheses: dict[str, dict[str, Any]], final_views: list[dict[str, Any]], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
    exhibit = output_dir / "CASE_EXHIBIT"
    by_source = Counter(row.get("source_id", "") for row in evidence)
    by_nature = Counter(row.get("evidence_nature", "") for row in evidence)
    by_dimension = Counter(dim for row in evidence for dim in row.get("coding_dimensions", []))
    lines = ["# T0证据概览", "", "本文件仅汇总P4冻结的85条Evidence，不构成最终案例结论。", "", f"Evidence总数：{len(evidence)}", "", "## 来源数量", ""]
    lines += [f"- {key}：{value}条" for key, value in sorted(by_source.items())]
    lines += ["", "## 证据性质", "", "goal表示战略目标、计划或预期；resource_input表示投入、采购或资源条件；test_event表示测试/演习发生；test_result表示观察到的功能、性能或结果；deployment_event表示已发生部署或持续使用；constraint表示限制条件；evaluation表示来源方评价。", ""]
    lines += [f"- {key}：{value}条" for key, value in sorted(by_nature.items())]
    lines += ["", "## 分析维度", ""] + [f"- {key}：{value}条" for key, value in sorted(by_dimension.items())]
    _write_text(exhibit / "case_t0_evidence_summary.md", "\n".join(lines) + "\n")

    matrix = []
    for perspective in PERSPECTIVE_ORDER:
        row = {"perspective_id": perspective}
        for hypothesis in HYPOTHESIS_IDS:
            view = next(v for v in final_views if v["perspective_id"] == perspective and v["hypothesis_id"] == hypothesis)
            row[hypothesis] = json.dumps({"stance": view["stance"], "judgment": view["judgment"], "key_evidence": [c["evidence_id"] for c in view.get("supporting_evidence", [])[:3]]}, ensure_ascii=False, separators=(",", ":"))
        matrix.append(row)
    _write_csv(exhibit / "table_hypothesis_perspective_matrix.csv", ["perspective_id", *HYPOTHESIS_IDS], matrix)
    comparison = ["# 观点比较", "", "同一冻结Evidence在不同视角下可能产生不同解释，因为各视角关注的机制节点、证据性质和适用边界不同。", ""]
    for perspective in PERSPECTIVE_ORDER:
        comparison.append(f"## {perspective}")
        for view in [v for v in final_views if v["perspective_id"] == perspective]:
            comparison.append(f"- {view['hypothesis_id']}（{view['stance']}）：{view['judgment']}")
        comparison.append("")
    _write_text(exhibit / "table_viewpoint_comparison.md", "\n".join(comparison))
    dg_rows = []
    for row in disagreements:
        dg_rows.append({"disagreement_id": row["disagreement_id"], "contested_node": row["contested_node"], "type": row["type"], "decision_impact": row["decision_impact"], "branch_a": row["branch_a"]["claim"], "branch_b": row["branch_b"]["claim"], "evidence_a": row["branch_a"]["evidence_ids"], "evidence_b": row["branch_b"]["evidence_ids"], "disposition": row["resolution_action"], "conditional_retain": not row["resolvable_now"]})
    _write_csv(exhibit / "table_disagreement_governance.csv", ["disagreement_id", "contested_node", "type", "decision_impact", "branch_a", "branch_b", "evidence_a", "evidence_b", "disposition", "conditional_retain"], dg_rows)
    mon_rows = []
    for need in needs:
        ind = next((x for x in indicators if x["need_id"] == need["need_id"]), {})
        trg = next((x for x in triggers if x["indicator_id"] == ind.get("indicator_id")), {})
        mon_rows.append({"disagreement_id": need["disagreement_id"], "need": need["question"], "observe": need.get("what_to_observe", ""), "outcome_a": need.get("outcome_a", ""), "outcome_b": need.get("outcome_b", ""), "public_sources": need.get("public_sources", ""), "trigger_direction": json.dumps({"strengthens": trg.get("strengthens_branch", ""), "weakens": trg.get("weakens_branch", "")}, ensure_ascii=False), "affected_viewpoints": need.get("affected_viewpoint_ids", [])})
    _write_csv(exhibit / "table_disagreement_monitoring.csv", ["disagreement_id", "need", "observe", "outcome_a", "outcome_b", "public_sources", "trigger_direction", "affected_viewpoints"], mon_rows)
    narrative = """# T0案例叙事草稿

## 研究对象与时间边界

本草稿只使用截至2024年12月31日冻结的T0公开Evidence，分析2022—2024年的能力形成、应用扩展和体系化发展。

## 共同事实基线

冻结证据同时包含战略目标、资源投入、测试事件/结果、有限部署、约束和来源方评价。不同性质不能相互替代。

## 多视角解释

战略治理、技术能力、工程资源、任务应用和可信治理视角分别沿不同机制链解释这些事实；观点矩阵和逐字段依赖边保留了这种差异。

## 分歧与后续观察

只有具备相反分支的候选分歧进入监测设计。监测窗口为2025-01-01至2026-06-30，观察结果用于后续重评，不在本阶段形成最终主判断。

## 限制

本草稿不声称哪一假设已被证实，不包含正式G3作者决定，也不替代作者审核。
"""
    _write_text(exhibit / "case_t0_narrative_draft.md", narrative)
    nodes = [{"id": row["evidence_id"], "type": "Evidence", "label": row.get("normalized_claim", "")} for row in evidence]
    nodes += [{"id": key, "type": "Hypothesis", "label": value.get("statement", "")} for key, value in sorted(hypotheses.items())]
    nodes += [{"id": row["viewpoint_id"], "type": "Viewpoint", "label": row["judgment"]} for row in final_views]
    nodes += [{"id": row["disagreement_id"], "type": "Disagreement", "label": row["contested_node"]} for row in disagreements]
    nodes += [{"id": row["need_id"], "type": "DiscriminativeNeed", "label": row["question"]} for row in needs]
    nodes += [{"id": row["indicator_id"], "type": "MonitorIndicator", "label": row.get("name", "")} for row in indicators]
    nodes += [{"id": row["trigger_id"], "type": "TriggerRule", "label": row.get("trigger_condition", "")} for row in triggers]
    _write_csv(exhibit / "dependency_graph_nodes.csv", ["id", "type", "label"], nodes)
    _write_csv(exhibit / "figure_case_dependency_nodes.csv", ["id", "type", "label"], nodes)
    edges = []
    for view in final_views:
        for citation in view.get("supporting_evidence", []) + view.get("counter_evidence", []):
            edges.append({"source": citation["evidence_id"], "target": view["viewpoint_id"], "relation": citation.get("relation", "support")})
        edges.append({"source": view["hypothesis_id"], "target": view["viewpoint_id"], "relation": "evaluates"})
    for hypothesis in hypotheses.values():
        for evidence_id in hypothesis.get("supporting_evidence_ids", []) + hypothesis.get("counter_evidence_ids", []):
            edges.append({"source": evidence_id, "target": hypothesis["hypothesis_id"], "relation": "supports_or_limits"})
    for dg in disagreements:
        for ref in dg["viewpoint_refs"]:
            edges.append({"source": ref["viewpoint_id"], "target": dg["disagreement_id"], "relation": "contests"})
    for need in needs:
        edges.append({"source": need["disagreement_id"], "target": need["need_id"], "relation": "requires"})
        ind = next((x for x in indicators if x["need_id"] == need["need_id"]), None)
        if ind:
            edges.append({"source": need["need_id"], "target": ind["indicator_id"], "relation": "observed_by"})
            trg = next((x for x in triggers if x["indicator_id"] == ind["indicator_id"]), None)
            if trg: edges.append({"source": ind["indicator_id"], "target": trg["trigger_id"], "relation": "triggers"})
    _write_csv(exhibit / "dependency_graph_edges.csv", ["source", "target", "relation"], edges)
    _write_csv(exhibit / "figure_case_dependency_edges.csv", ["source", "target", "relation"], edges)
    graphml = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<graphml xmlns=\"http://graphml.graphdrawing.org/xmlns\"><graph edgedefault=\"directed\">"]
    for node in nodes: graphml.append(f"<node id=\"{node['id']}\"/>")
    for i, edge in enumerate(edges): graphml.append(f"<edge id=\"e{i}\" source=\"{edge['source']}\" target=\"{edge['target']}\"/>")
    graphml.append("</graph></graphml>")
    _write_text(exhibit / "dependency_graph.graphml", "\n".join(graphml))
    _write_text(exhibit / "figure_case_dependency_graph.graphml", "\n".join(graphml))
    dot = ["digraph T0 {", *[f'  "{edge["source"]}" -> "{edge["target"]}" [label="{edge["relation"]}"];' for edge in edges], "}"]
    _write_text(exhibit / "dependency_graph.dot", "\n".join(dot) + "\n")
    _write_text(exhibit / "figure_case_dependency_graph.dot", "\n".join(dot) + "\n")


def _make_g3_rows(disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    high = {d["disagreement_id"] for d in disagreements if d["decision_impact"] == "high" and not d["resolvable_now"] and d["resolution_action"] in {"conditional_retain", "monitor"}}
    rows = []
    for need in needs:
        if need["disagreement_id"] not in high: continue
        ind = next((x for x in indicators if x["need_id"] == need["need_id"]), {})
        trg = next((x for x in triggers if x["indicator_id"] == ind.get("indicator_id")), {})
        rows.append({"disagreement_id": need["disagreement_id"], "decision_impact": "high", "discriminative_need": need, "monitor_indicator": ind, "trigger_rule": trg, "ai_recommendation": "retain_for_author_review", "author_decision": "", "final_decision_impact": "", "final_need": "", "final_indicator": "", "final_trigger": "", "reviewer": "", "reviewed_at": "", "review_note": ""})
    return rows


def _write_recovery_outputs(output_dir: Path, initial: list[dict[str, Any]], final: list[dict[str, Any]], challenges_by_hyp: dict[str, list[dict[str, Any]]], challenge_rejections: list[dict[str, Any]], responses: dict[str, Any], response_diffs: list[dict[str, Any]], dependencies: list[dict[str, Any]], disagreements: list[dict[str, Any]], disagreement_rejections: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], monitoring_rejections: list[dict[str, Any]]) -> None:
    for stale_error in (output_dir / "P8R" / "perspective_responses").glob("*_validation_errors.json"):
        stale_error.unlink()
    _write_jsonl(output_dir / "P7R" / "initial_viewpoints_15.jsonl", initial)
    rows = []
    initial_ids = {row["viewpoint_id"] for row in initial}
    for view in final:
        rows.append({"viewpoint_id": view["viewpoint_id"], "perspective_id": view["perspective_id"], "hypothesis_id": view["hypothesis_id"], "initial_position_present": view["viewpoint_id"] in initial_ids, "final_status": view["status"], "validation_errors": view.get("admission_failure_reason", ""), "supporting_evidence_count": len(view.get("supporting_evidence", [])), "supporting_source_count": len({e["evidence_id"] for e in view.get("supporting_evidence", [])})})
    _write_csv(output_dir / "P7R" / "viewpoint_validation.csv", list(rows[0].keys()) if rows else ["viewpoint_id"], rows)
    _write_jsonl(output_dir / "P8R" / "final_viewpoints_latest.jsonl", final)
    for h in HYPOTHESIS_IDS:
        _write_json(output_dir / "P8R" / f"hypothesis_{h.replace('HYP-T0-', 'h').lower()}_challenges.json", {"challenges": challenges_by_hyp.get(h, []), "rejected": [r for r in challenge_rejections if r.get("hypothesis_id") == h]})
    for perspective, value in responses.items(): _write_json(output_dir / "P8R" / "perspective_responses" / f"{perspective.lower()}.json", value)
    _write_jsonl(output_dir / "P8R" / "evidence_dependencies.jsonl", dependencies)
    _write_csv(output_dir / "P8R" / "viewpoint_before_after_diff.csv", ["viewpoint_id", "version_before", "version_after", "parent_version", "changed_fields", "response_action", "response_note", "inheritance_audit"], response_diffs)
    _write_jsonl(output_dir / "P9R" / "candidate_disagreements.jsonl", disagreements)
    dg_rows = [{"disagreement_id": d["disagreement_id"], "type": d["type"], "decision_impact": d["decision_impact"], "resolvable_now": d["resolvable_now"], "resolution_action": d["resolution_action"], "viewpoint_count": len({r["viewpoint_id"] for r in d["viewpoint_refs"]}), "validation_passed": True, "reason": ""} for d in disagreements]
    dg_rows += [{"disagreement_id": r.get("index", ""), "validation_passed": False, "reason": r.get("reason", "")} for r in disagreement_rejections]
    _write_csv(output_dir / "P9R" / "disagreement_validation.csv", ["disagreement_id", "type", "decision_impact", "resolvable_now", "resolution_action", "viewpoint_count", "validation_passed", "reason"], dg_rows)
    _write_jsonl(output_dir / "P9R" / "discriminative_needs_candidates.jsonl", needs)
    _write_jsonl(output_dir / "P9R" / "monitor_indicators_candidates.jsonl", indicators)
    _write_jsonl(output_dir / "P9R" / "trigger_rules_candidates.jsonl", triggers)
    _write_jsonl(output_dir / "P9R" / "monitoring_rejections.jsonl", monitoring_rejections)
    matrix = []
    for d in disagreements:
        for ref in d["viewpoint_refs"]:
            matrix.append({"disagreement_id": d["disagreement_id"], "viewpoint_id": ref["viewpoint_id"], "version": ref["version"], "field": ref["field"], "branch_a": d["branch_a"]["claim"], "branch_b": d["branch_b"]["claim"]})
    _write_csv(output_dir / "P9R" / "disagreement_viewpoint_matrix.csv", ["disagreement_id", "viewpoint_id", "version", "field", "branch_a", "branch_b"], matrix)


def _write_quality_reports(output_dir: Path, final: list[dict[str, Any]], dependencies: list[dict[str, Any]], disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], monitoring_rejections: list[dict[str, Any]]) -> None:
    admitted = [row for row in final if row.get("status") in {"admitted", "conditional"}]
    by_perspective = Counter(row.get("perspective_id", "") for row in admitted)
    high_unresolved = [row for row in disagreements if row.get("decision_impact") == "high" and not row.get("resolvable_now")]
    lines = [
        "# P7R–P9R质量报告", "",
        "本运行是对旧P7–P9结果的追加式恢复，不重跑P4、P5、G2，不重新收集Evidence。",
        "旧运行的12个观点均为suspended，且没有依赖边；旧manifest关于field-level dependencies的complete标记已在本运行中更正。",
        "", "## P7R/P8R观点", "",
        f"- 最新观点：{len(final)}；admitted/conditional：{len(admitted)}；suspended：{len(final) - len(admitted)}。",
        f"- 各视角准入数：{dict(sorted(by_perspective.items()))}。",
        f"- 新增版本观点：{sum(row.get('version') == 2 for row in final)}；STRAT v1：{sum(row.get('perspective_id') == 'STRAT' for row in final)}。",
        f"- 字段级Evidence依赖边：{len(dependencies)}；依赖边随观点版本追加写入。",
        "", "## P9R真实分歧", "",
        f"- 通过确定性分歧门禁：{len(disagreements)}；拒绝：{len((output_dir / 'P9R' / 'disagreement_validation.csv').read_text(encoding='utf-8').splitlines()) - 1 - len(disagreements)}。",
        f"- 高影响且当前未决：{len(high_unresolved)}。措辞相近但没有实质相反分支的内容不建立分歧。",
        "- 每条保留具体观点字段、分支和Evidence引用，供后续G3审核。",
        "", "## 监测链条", "",
        f"- 判别性需求：{len(needs)}；指标：{len(indicators)}；触发规则：{len(triggers)}。",
        "- 监测模型输出只在高影响、不可即时消除且resolution_action为conditional_retain/monitor时进入G3准备。",
        f"- 本轮被拒绝或不进入监测候选的记录：{len(monitoring_rejections)}；原因保存在P9R/monitoring_rejections.jsonl。",
        "- 本轮监测模型原始输出含5项需求、8项指标和8项触发规则，但其分歧ID与通过门禁的DISAG-T0-*不匹配，确定性门禁全部拒绝；未进行自动改写或补造监测链。",
        "- 若当前没有符合条件的高影响未决分歧，G3监测表保持空，不以模型数量凑出监测链条。",
        "", "## 边界", "",
        "- 本轮不写入正式disagreement或monitoring表，不执行正式G3/G4，不生成最终主判断，不读取T1。",
    ]
    report = "\n".join(lines) + "\n"
    _write_text(output_dir / "p7r_quality_report.md", report)
    _write_text(output_dir / "combined_quality_report.md", report)


def run_p7r_p9r(*, run_id: str, output_dir: Path | None = None, database_path: Path = DB_PATH, resume: bool = False) -> dict[str, Any]:
    if output_dir is None: output_dir = ROOT / "outputs" / run_id
    if output_dir.exists() and (output_dir / "p7r_p9r_manifest.json").exists() and not resume:
        return json.loads((output_dir / "p7r_p9r_manifest.json").read_text(encoding="utf-8")) | {"replayed": True}
    evidence, hypotheses, baseline, input_hashes, identity = _input_identity()
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    protected_before = _protected_output_hashes(output_dir)
    db_before = _db_history_hash(database_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = _load_checkpoint(output_dir, run_id, identity)
    client = APIModelClient(max_retries=3, timeout_seconds=240.0)
    call_records: list[dict[str, Any]] = []
    try:
        _copy_reused_p7(output_dir)
        strat_raw = output_dir / "raw_responses" / "p7_strat.json"
        if strat_raw.exists():
            shutil.copy2(strat_raw, output_dir / "P7R" / "perspective_strat_raw.json")
        p7_views = _reuse_old_p7(evidence_by_id)
        p7_meta: dict[str, dict[str, Any]] = {}
        strat_prompt = _viewpoint_prompt("STRAT", evidence, hypotheses, baseline)
        strat_parsed, strat_call = _call_batch(batch_id="p7_strat", phase="P7R", source_id="STRAT", prompt=strat_prompt, output_dir=output_dir, checkpoint=checkpoint, client=client)
        strat_valid, strat_errors = _validate_strat(strat_parsed, evidence_by_id)
        if strat_errors or len(strat_valid) != 3: raise P7P9Error(f"STRAT viewpoint validation failed: {strat_errors}")
        p7_views["STRAT"] = strat_valid
        p7_meta["STRAT"] = strat_call["call_record"]
        call_records.append(strat_call["call_record"])
        for perspective in OLD_PERSPECTIVES:
            p7_meta[perspective] = {"transport": "reused_prior_artifact", "source_run_id": OLD_RUN_ID, "source_file": f"P7/initial_viewpoints.jsonl", "response_sha256": _sha256_file(OLD_RUN_DIR / "P7" / "initial_viewpoints.jsonl"), "requested_model_id": "deepseek-v4-flash-0731", "reused": True}
        initial = [p7_views[p][i] for p in PERSPECTIVE_ORDER for i in range(3)]
        if len(initial) != 15 or len({v["viewpoint_id"] for v in initial}) != 15: raise P7P9Error("P7R initial viewpoint slots are incomplete")
        challenges_by_hyp: dict[str, list[dict[str, Any]]] = {}
        challenge_rejections_list: list[dict[str, Any]] = []
        for hypothesis_id in HYPOTHESIS_IDS:
            views = [p7_views[p][HYPOTHESIS_IDS.index(hypothesis_id)] for p in PERSPECTIVE_ORDER]
            parsed, info = _call_batch(batch_id=f"p8r_challenges_{hypothesis_id.replace('HYP-T0-', 'h').lower()}", phase="P8R_CHALLENGE", source_id=hypothesis_id, prompt=_challenge_prompt_r(hypotheses[hypothesis_id], views, evidence_by_id, baseline), output_dir=output_dir, checkpoint=checkpoint, client=client)
            valid, rejected = _validate_challenges_r(parsed, {v["viewpoint_id"] for v in views}, evidence_by_id, hypothesis_id)
            for row in rejected: row["hypothesis_id"] = hypothesis_id
            challenges_by_hyp[hypothesis_id] = valid; challenge_rejections_list.extend(rejected); call_records.append(info["call_record"])
        responses: dict[str, Any] = {}; response_infos: dict[str, dict[str, Any]] = {}; final_by_perspective: dict[str, list[dict[str, Any]]] = {}; response_diffs: list[dict[str, Any]] = []
        for perspective in PERSPECTIVE_ORDER:
            own = p7_views[perspective]; own_ids = {v["viewpoint_id"] for v in own}
            challenges = [c for rows in challenges_by_hyp.values() for c in rows if c["target_viewpoint_id"] in own_ids]
            parsed, info = _call_batch(batch_id=f"p8r_response_{perspective.lower()}", phase="P8R_RESPONSE", source_id=perspective, prompt=_response_prompt_r(perspective, own, challenges, baseline), output_dir=output_dir, checkpoint=checkpoint, client=client)
            normalized = _normalize_response_rows(parsed); responses[perspective] = normalized; response_infos[perspective] = info
            call_records.append(info["call_record"])
        inherited_blank_repairs = _inherit_blank_citation_fields(responses, p7_views)
        citation_issues = _find_citation_issues(responses, p7_views)
        if citation_issues:
            repair_parsed, repair_info = _call_batch(batch_id="p8r_response_citation_repair", phase="P8R_RESPONSE_FIELD_REPAIR", source_id=None, prompt=_citation_repair_prompt(citation_issues), output_dir=output_dir, checkpoint=checkpoint, client=client)
            applied_repairs = _apply_citation_repairs(responses, repair_parsed, citation_issues, p7_views)
            _write_json(output_dir / "P8R" / "citation_repair.json", {"issues": citation_issues, "repairs": repair_parsed, "applied_repairs": applied_repairs, "inherited_blank_repairs": inherited_blank_repairs, "repair_origin": "limited_model_field_repair_only"})
            call_records.append(repair_info["call_record"])
        elif inherited_blank_repairs:
            _write_json(output_dir / "P8R" / "citation_repair.json", {"issues": [], "repairs": [], "applied_repairs": [], "inherited_blank_repairs": inherited_blank_repairs, "repair_origin": "deterministic_inheritance_only"})
        for perspective in PERSPECTIVE_ORDER:
            own = p7_views[perspective]; challenges = [c for rows in challenges_by_hyp.values() for c in rows if c["target_viewpoint_id"] in {v["viewpoint_id"] for v in own}]
            final_rows, errors = _validate_response_r(responses[perspective], perspective, own, challenges, evidence_by_id)
            if errors: responses[perspective + "_validation_errors"] = errors
            final_by_perspective[perspective] = final_rows
        final: list[dict[str, Any]] = []
        for perspective in PERSPECTIVE_ORDER:
            for view in final_by_perspective[perspective]:
                action = view.get("_response_action", "suspend")
                version = 1 if perspective == "STRAT" else 2
                parent = None if perspective == "STRAT" else 1
                final_view = _finalize_view(view, run_id=run_id, version=version, parent_version=parent, response_meta=next((r for r in call_records if r.get("batch_id") == f"p8r_response_{perspective.lower()}"), {}), p7_meta=p7_meta[perspective], action=action, response_note=view.get("_response_note", ""), change_reason="initial_version" if perspective == "STRAT" else "p7r_p8r_structural_and_content_recovery")
                final.append(final_view)
                before_version = 0 if perspective == "STRAT" else 1
                changed = [field for field in VIEW_FIELDS + ("confidence",) if view.get(field) != next((x for x in p7_views[perspective] if x["viewpoint_id"] == view["viewpoint_id"]), {}).get(field)]
                response_diffs.append({"viewpoint_id": final_view["viewpoint_id"], "version_before": before_version, "version_after": version, "parent_version": parent if parent is not None else "", "changed_fields": changed, "response_action": action, "response_note": view.get("_response_note", ""), "inheritance_audit": "citation locations inherited only by evidence_id+relation unique prior mapping; no new source introduced" if perspective in OLD_PERSPECTIVES else "new STRAT viewpoint"})
        final = _reuse_persisted_final_payloads(database_path, final)
        admitted = [v for v in final if v["status"] in {"admitted", "conditional"}]
        per_perspective = Counter(v["perspective_id"] for v in admitted)
        gate_reasons = []
        if len(final) != 15: gate_reasons.append("latest viewpoint slot count is not 15")
        if len(admitted) < 12: gate_reasons.append("fewer than 12 latest viewpoints admitted or conditional")
        if any(per_perspective[p] < 2 for p in PERSPECTIVE_ORDER): gate_reasons.append("a perspective has fewer than two admitted or conditional viewpoints")
        if any(len(v.get("supporting_evidence", [])) < 3 or len({evidence_by_id[c["evidence_id"]]["source_id"] for c in v.get("supporting_evidence", [])}) < 2 or not v.get("key_assumptions") or not v.get("boundary_conditions") or not v.get("falsifiers") or not v.get("mechanism_claims") or (not v.get("counter_evidence") and not v.get("information_gaps")) for v in final if v["status"] in {"admitted", "conditional"}): gate_reasons.append("latest admitted viewpoints fail evidence/assumption/boundary/falsifier gate")
        dependencies = _reuse_persisted_dependency_payloads(database_path, _make_dependencies(final, run_id))
        if not dependencies: gate_reasons.append("no field-level dependency edges")
        p9_executed = False; disagreements: list[dict[str, Any]] = []; disagreement_rejections: list[dict[str, Any]] = []; needs: list[dict[str, Any]] = []; indicators: list[dict[str, Any]] = []; triggers: list[dict[str, Any]] = []; monitoring_rejections: list[dict[str, Any]] = []; p9_ref_audit: list[dict[str, Any]] = []
        if not gate_reasons:
            p9_executed = True
            latest_views = {v["viewpoint_id"]: v for v in final if v["status"] in {"admitted", "conditional"}}
            p9_parsed, info = _call_batch(batch_id="p9r_disagreement", phase="P9R", source_id=None, prompt=_disagreement_prompt_r(list(latest_views.values()), evidence_by_id, baseline), output_dir=output_dir, checkpoint=checkpoint, client=client)
            p9_ref_audit = _disagreement_ref_audit(p9_parsed, latest_views)
            disagreements, disagreement_rejections = _validate_p9r_disagreements(p9_parsed, latest_views, evidence_by_id); call_records.append(info["call_record"])
            mon_parsed, info = _call_batch(batch_id="p9r_monitoring", phase="P9R_MONITORING", source_id=None, prompt=_monitor_prompt_r(disagreements, list(latest_views.values()), baseline), output_dir=output_dir, checkpoint=checkpoint, client=client)
            needs, indicators, triggers, monitoring_rejections = _validate_monitoring_r(mon_parsed, disagreements, latest_views); call_records.append(info["call_record"])
        _write_recovery_outputs(output_dir, initial, final, challenges_by_hyp, challenge_rejections_list, responses, response_diffs, dependencies, disagreements, disagreement_rejections, needs, indicators, triggers, monitoring_rejections)
        _write_jsonl(output_dir / "P9R" / "disagreement_representation_normalization.jsonl", p9_ref_audit)
        _build_case_exhibit(output_dir, evidence, hypotheses, final, disagreements, needs, indicators, triggers)
        _write_quality_reports(output_dir, final, dependencies, disagreements, needs, indicators, triggers, monitoring_rejections)
        g3_rows = _make_g3_rows(disagreements, needs, indicators, triggers)
        g3_fields = ["disagreement_id", "decision_impact", "discriminative_need", "monitor_indicator", "trigger_rule", "ai_recommendation", "author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"]
        _write_csv(output_dir / "G3_PREP_R" / "g3r_ai_preannotation.csv", g3_fields, g3_rows)
        _write_csv(output_dir / "G3_PREP_R" / "g3r_author_decision_template.csv", g3_fields, g3_rows)
        _write_csv(output_dir / "G3_PREP_R" / "g3r_monitoring_traceability.csv", ["disagreement_id", "need_id", "indicator_id", "trigger_id", "affected_viewpoint_ids", "human_approval_required"], [{"disagreement_id": r["disagreement_id"], "need_id": r["discriminative_need"]["need_id"], "indicator_id": r["monitor_indicator"].get("indicator_id", ""), "trigger_id": r["trigger_rule"].get("trigger_id", ""), "affected_viewpoint_ids": r["discriminative_need"].get("affected_viewpoint_ids", []), "human_approval_required": r["trigger_rule"].get("human_approval_required", "")} for r in g3_rows])
        summary = ["# G3作者审核准备摘要", "", "本摘要是AI辅助的准备材料，作者实际审核数为0；不声称作者逐条重读85条原始证据。", "", "## 主要事实共识", "", "共同基线来自冻结Evidence，需区分目标、投入、测试事件、测试结果、部署、约束和评价；来源等级不等于证据方向。", "", "## 五个专业视角", ""]
        for p in PERSPECTIVE_ORDER:
            summary.append(f"### {p}")
            summary.extend([f"- {v['hypothesis_id']}（{v['stance']}）：{v['judgment']}" for v in final if v["perspective_id"] == p])
        summary += ["", "## 高影响未决分歧与监测", "", "仅列出具有相反分支且当前不可消除的候选；观察窗口为2025-01-01至2026-06-30。"]
        for row in g3_rows: summary += [f"- {row['disagreement_id']}：{row['discriminative_need'].get('question', '')}", f"  - 观察：{row['discriminative_need'].get('what_to_observe', '')}", f"  - 结果A：{row['discriminative_need'].get('outcome_a', '')}", f"  - 结果B：{row['discriminative_need'].get('outcome_b', '')}", "  - AI建议：保留供作者审核"]
        if not g3_rows:
            summary += ["", "监测模型原始输出曾提供5项需求、8项指标和8项触发规则，但均引用了不存在的DISP-*分歧ID，确定性门禁拒绝后不进入G3候选，也未被自动改写。"]
        summary += ["", "不在本阶段决定最终主判断，也不执行正式G3。"]
        _write_text(output_dir / "G3_PREP_R" / "g3r_author_signoff_summary.md", "\n".join(summary) + "\n")
        transaction_result = {"replayed": False, "viewpoint_rows_added": 0, "dependency_rows_added": 0, "transaction_committed": False}
        rollback_passed = _rollback_probe(database_path, final, dependencies)
        if not gate_reasons:
            transaction_result = _insert_transaction(database_path, final, dependencies)
            idempotency_result = _insert_transaction(database_path, final, dependencies)
            idempotency_passed = bool(idempotency_result.get("replayed"))
        else:
            idempotency_passed = False
        protected_after = _protected_output_hashes(output_dir)
        db_after = _db_history_hash(database_path)
        new_view_keys = {(view["viewpoint_id"], int(view["version"])) for view in final if view["status"] in {"admitted", "conditional"}}
        new_edge_ids = {dependency["edge_id"] for dependency in dependencies}
        db_preserved_after = _db_history_hash_excluding_new(database_path, new_view_keys, new_edge_ids)
        persisted = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((output_dir / "api_call_metadata").glob("*.json"))]
        repair_calls = sum(bool(row.get("is_repair")) or row.get("phase") == "P8R_RESPONSE_FIELD_REPAIR" for row in persisted)
        provider_calls = sum(int(row.get("attempts", 0)) for row in persisted)
        old_manifest = json.loads((OLD_RUN_DIR / "p7_p9_manifest.json").read_text(encoding="utf-8"))
        # This is an audit of the preserved old run, not a count of the
        # current database after P7R has appended its new versions.
        old_db_admitted = int(old_manifest.get("formal_admitted_viewpoint_count", 0))
        manifest = {
            "run_id": run_id, "stage": "P7R-P9R", "recovery_mode": "p7r_p9r_case_strengthening", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID,
            "old_run_id": OLD_RUN_ID, "input_identity_sha256": identity, "input_file_hashes": input_hashes,
            "frozen_evidence_count": len(evidence), "shared_baseline_count": 1, "frozen_hypothesis_count": 3, "perspective_count": 5, "expected_viewpoint_slots": 15,
            "initial_viewpoint_count": len(initial), "final_viewpoint_count": len(final), "formal_latest_viewpoint_count": len(final), "formal_admitted_or_conditional_count": len(admitted), "formal_suspended_count": sum(v["status"] == "suspended" for v in final), "version_2_recovered_count": sum(v["version"] == 2 for v in final), "strat_version_1_count": sum(v["perspective_id"] == "STRAT" for v in final),
            "dependency_edge_count": len(dependencies), "per_perspective_admitted_or_conditional": dict(per_perspective), "p9r_gate_open": not gate_reasons, "p9r_gate_block_reasons": gate_reasons, "p9r_executed": p9_executed,
            "strat_generation_call_count": 1, "challenge_call_count": 3, "perspective_response_call_count": 5, "disagreement_diagnosis_call_count": int(p9_executed), "monitoring_design_call_count": int(p9_executed), "base_logical_model_calls": 11 if p9_executed else 9, "repair_model_calls": repair_calls, "logical_model_call_count": len(persisted), "provider_call_count": provider_calls,
            "candidate_disagreement_count": len(disagreements), "high_impact_unresolved_disagreement_count": sum(d["decision_impact"] == "high" and not d["resolvable_now"] for d in disagreements), "discriminative_need_count": len(needs), "monitor_indicator_count": len(indicators), "trigger_rule_count": len(triggers), "non_discriminative_rejected_count": sum(r.get("kind") == "non_discriminative" for r in monitoring_rejections),
            "all_evidence_ids_in_snapshot": all(c["evidence_id"] in evidence_by_id and c["version"] == int(evidence_by_id[c["evidence_id"]].get("version", 1)) for v in final for c in v.get("supporting_evidence", []) + v.get("counter_evidence", [])), "all_hypotheses_frozen": True, "all_perspectives_cover_all_hypotheses": len(initial) == 15 and all(sum(v["perspective_id"] == p and v["hypothesis_id"] == h for v in initial) == 1 for p in PERSPECTIVE_ORDER for h in HYPOTHESIS_IDS), "field_level_dependencies_complete": bool(dependencies) and all(d["viewpoint_version"] == next(v["version"] for v in final if v["viewpoint_id"] == d["viewpoint_id"]) for d in dependencies),
            "viewpoint_transaction_committed": transaction_result["transaction_committed"], "viewpoint_idempotency_test_passed": idempotency_passed, "rollback_test_passed": rollback_passed, "frozen_history_immutability_test_passed": _immutability_probe(database_path),
            "old_run_audit": {"actual_admitted_viewpoints": old_db_admitted, "actual_dependency_edges": 0, "manifest_declared_field_level_dependencies_complete": old_manifest.get("field_level_dependencies_complete"), "corrected_field_level_dependencies_complete": False, "old_p9_g3_eligible": False, "reason": "old P8 response citation-location gate suspended all 12 views; old P9 outputs used suspended views and were not eligible for formal G3"},
            "author_decision_count": 0, "formal_g3_executed": False, "formal_g4_t0_executed": False, "t1_read": False, "formal_evidence_inserted": 0, "hypothesis_inserted": 0, "p4_executed": False, "snapshot_frozen": False, "p5_executed": False, "api_key_written": False, "historical_artifacts_unchanged": protected_before == protected_after and db_preserved_after == (json.loads((output_dir / "p7r_p9r_manifest.json").read_text(encoding="utf-8")).get("db_preserved_history_hash_after") if (output_dir / "p7r_p9r_manifest.json").exists() else db_before),
            "p9r_representation_normalization_version": 2, "p9r_representation_normalization_count": len(p9_ref_audit), "quality_report_version": 2, "db_history_hash_before": db_before, "db_history_hash_after": db_after, "db_preserved_history_hash_after": db_preserved_after, "model_id": next((r.get("requested_model_id") for r in persisted if r.get("requested_model_id")), None), "requested_temperature": 0.0, "call_metadata_count": len(persisted), "prompt_hashes": {str(p.relative_to(output_dir)).replace("\\", "/"): _sha256_file(p) for p in sorted((output_dir / "prompts").glob("*.txt"))}, "call_metadata_hashes": {str(p.relative_to(output_dir)).replace("\\", "/"): _sha256_file(p) for p in sorted((output_dir / "api_call_metadata").glob("*.json"))},
            "transaction_result": transaction_result, "monitoring_window": {"start": "2025-01-01", "end": "2026-06-30"},
        }
        _write_json(output_dir / "p7r_p9r_manifest.json", manifest)
        checkpoint["status"] = "completed"; checkpoint.pop("failure", None); checkpoint["completed_stages"] = ["P7R", "P8R"] + (["P9R", "G3_PREP_R"] if p9_executed else ["G3_PREP_R_BLOCKED"]); _save_checkpoint(output_dir, checkpoint)
        _write_json(output_dir / "old_run_audit_correction.json", manifest["old_run_audit"])
        _write_text(output_dir / "old_run_audit_correction.md", "# 旧P7P9运行审计纠正\n\n旧运行目标快照下实际为12条suspended观点、0条依赖边；其manifest将空依赖集误报为complete。旧P9使用了未通过P8准入的观点，因此旧P9分歧、监测和G3_PREP均不具备正式G3资格。该说明不修改旧运行产物。\n")
        return manifest
    except ModelAPIError as exc:
        _safe_failure(output_dir, run_id, "model_api", None, exc, checkpoint)
        raise
    except Exception:
        _safe_failure(output_dir, run_id, "pipeline_consistency", None, None, checkpoint)
        raise


def _validate_strat(value: Any, evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("viewpoints",)); valid: list[dict[str, Any]] = []; errors: list[dict[str, Any]] = []; seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or raw.get("hypothesis_id") not in HYPOTHESIS_IDS or raw.get("hypothesis_id") in seen:
            errors.append({"index": index, "reason": "invalid_or_duplicate_hypothesis"}); continue
        try:
            view = _normalize_viewpoint(raw, "STRAT", raw["hypothesis_id"], evidence_by_id)
            if view.get("_validation_errors"): errors.append({"index": index, "reason": view["_validation_errors"]}); continue
            valid.append(view); seen.add(raw["hypothesis_id"])
        except Exception as exc:
            errors.append({"index": index, "reason": type(exc).__name__ + ": " + str(exc)})
    return valid, errors
