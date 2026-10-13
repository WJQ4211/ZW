"""P7-P9 multi-perspective T0 viewpoint and disagreement preparation.

This module consumes only the frozen T0 export and the formal G2 hypothesis
export.  Model responses are kept as audit artifacts; deterministic checks
inject runtime metadata and decide whether a viewpoint can be admitted.  P9
disagreements and monitoring objects remain candidates for G3 and are not
written to their formal tables in this stage.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .model_client import APIModelClient, ModelAPIError, parse_chat_completion_content
from .schemas import (
    CODING_DIMENSION_VALUES,
    DependencyRelation,
    EvidenceDependency,
    Importance,
    Viewpoint,
    ViewpointStatus,
    model_validate,
)


ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
P4_DIR = ROOT / "outputs" / "P4-T0-C6C812108AF95309-freeze-v1"
G2_DIR = ROOT / "outputs" / "G2-T0-1B8D6A450720F041-formal-v1"
DB_PATH = ROOT / "data" / "research.sqlite3"
QUESTION = "基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？其能力形成、应用扩展与体系化发展的主导路径是什么？哪些条件可能使当前判断发生改变？"
TIME_WINDOW = "2022-01-01至2024-12-31"
PROMPT_TEMPLATE_VERSION = "p7-p9-v1"

PERSPECTIVES: dict[str, str] = {
    "TECH": "技术能力视角：算法与模型能力、数据、算力、技术成熟度、试验结果和技术性能边界。",
    "ENG": "工程与资源视角：工程化、基础设施、预算采办、接口、保障、资源投入及规模化部署条件。",
    "OPS": "任务应用与体系视角：任务适配、实际部署、跨军种复用、跨平台协同、组织运用和体系集成。",
    "TRUST": "可信治理视角：可靠性、安全、测试评估、负责任AI、授权责任、治理机制和制度落地。",
}
HYPOTHESIS_IDS = ("HYP-T0-01", "HYP-T0-02", "HYP-T0-03")
STANCE_VALUES = {"support", "challenge", "conditional", "insufficient"}
ACTION_VALUES = {"maintain", "revise", "conditional_retain", "suspend", "exit"}
DISAGREEMENT_TYPES = {"fact", "scope", "mechanism", "assumption", "risk_weight"}
CHALLENGE_TYPES = {
    "evidence_mismatch", "goal_result_confusion", "mechanism_gap", "assumption_risk",
    "scope_overreach", "counter_evidence_omitted", "uncertainty_understated",
}
RESOLUTION_ACTIONS = {"evidence_check", "scope_split", "conditional_retain", "monitor"}
PREDICATE_TYPES = {"event_match", "status_change", "expert_confirmed"}
REQUIRED_VIEW_FIELDS = (
    "stance", "judgment", "key_assumptions", "mechanism_claims", "boundary_conditions",
    "falsifiers", "uncertainties", "information_gaps",
)


class P7P9Error(RuntimeError):
    """Raised for a non-recoverable input, checkpoint, or consistency error."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
            values: dict[str, Any] = {}
            for field in fields:
                value = row.get(field, "")
                values[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (list, dict)) else value
            writer.writerow(values)


def _protected_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents
    }


def _parse_json_compat(content: str) -> Any:
    """Parse strict JSON, a surrounding Markdown JSON fence, or raw JSON."""
    cleaned = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return json.loads(cleaned)


def _unwrap(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    raise P7P9Error(f"response does not contain an accepted array wrapper: {keys}")


def _extract_wrapped(value: Any, key: str) -> list[Any]:
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return value[key]
    return []


def _load_inputs() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any], dict[str, str], str]:
    p4_manifest_path = P4_DIR / "p4_t0_snapshot_manifest.json"
    p4_evidence_path = P4_DIR / "p4_t0_frozen_evidence.jsonl"
    g2_manifest_path = G2_DIR / "g2_manifest.json"
    g2_hypothesis_path = G2_DIR / "g2_final_hypotheses.jsonl"
    g2_baseline_path = G2_DIR / "g2_shared_baseline.jsonl"
    paths = (p4_manifest_path, p4_evidence_path, g2_manifest_path, g2_hypothesis_path, g2_baseline_path)
    if not all(path.exists() for path in paths):
        raise P7P9Error("P4/G2 authoritative input is incomplete")
    p4_manifest = json.loads(p4_manifest_path.read_text(encoding="utf-8"))
    g2_manifest = json.loads(g2_manifest_path.read_text(encoding="utf-8"))
    evidence = _read_jsonl(p4_evidence_path)
    hypotheses = _read_jsonl(g2_hypothesis_path)
    baselines = _read_jsonl(g2_baseline_path)
    if p4_manifest.get("snapshot_id") != SNAPSHOT_ID or p4_manifest.get("frozen") is not True:
        raise P7P9Error("P4 input is not the expected frozen T0 snapshot")
    if g2_manifest.get("snapshot_id") != SNAPSHOT_ID or g2_manifest.get("formal_hypothesis_count") != 3:
        raise P7P9Error("G2 input is not the expected formal hypothesis set")
    if len(evidence) != 85 or len({row.get("evidence_id") for row in evidence}) != 85:
        raise P7P9Error("P4 frozen Evidence count or uniqueness check failed")
    if len(hypotheses) != 3 or {row.get("hypothesis_id") for row in hypotheses} != set(HYPOTHESIS_IDS):
        raise P7P9Error("G2 formal hypothesis IDs are incomplete")
    if len(baselines) != 1 or baselines[0].get("baseline_id") != "BASE-T0-01":
        raise P7P9Error("shared baseline is missing")
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    for hypothesis in hypotheses:
        if hypothesis.get("snapshot_id") != SNAPSHOT_ID or hypothesis.get("frozen") is not True:
            raise P7P9Error(f"hypothesis is not frozen in the target snapshot: {hypothesis.get('hypothesis_id')}")
        refs = hypothesis.get("supporting_evidence_ids", []) + hypothesis.get("counter_evidence_ids", [])
        if any(ref not in evidence_by_id for ref in refs):
            raise P7P9Error(f"hypothesis references Evidence outside the snapshot: {hypothesis.get('hypothesis_id')}")
    hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): _sha256_file(path) for path in paths}
    identity_value = {
        "snapshot_id": SNAPSHOT_ID,
        "evidence": evidence,
        "hypotheses": hypotheses,
        "baseline": baselines[0],
        "p4_manifest_sha256": hashes["outputs/P4-T0-C6C812108AF95309-freeze-v1/p4_t0_snapshot_manifest.json"],
        "g2_manifest_sha256": hashes["outputs/G2-T0-1B8D6A450720F041-formal-v1/g2_manifest.json"],
    }
    identity = _sha256_bytes(_json(identity_value).encode("utf-8"))
    return evidence, {row["hypothesis_id"]: row for row in hypotheses}, baselines[0], hashes, identity


def _evidence_context(evidence: list[dict[str, Any]]) -> str:
    fields = (
        "evidence_id", "source_id", "source_grade", "published_at", "source_locator",
        "title", "publisher", "evidence_nature", "coding_dimensions", "normalized_claim",
        "excerpt_original", "excerpt_zh",
    )
    return "\n".join(_json({field: row.get(field, "") for field in fields}) for row in evidence)


def _hypothesis_context(hypotheses: dict[str, dict[str, Any]]) -> str:
    return "\n".join(_json({
        "hypothesis_id": key,
        "statement": value.get("statement", ""),
        "scope": value.get("scope", ""),
        "supporting_evidence_ids": value.get("supporting_evidence_ids", []),
        "counter_evidence_ids": value.get("counter_evidence_ids", []),
        "key_assumptions": value.get("key_assumptions", []),
        "conversion_criteria": value.get("conversion_criteria", []),
        "covered_dimensions": value.get("covered_dimensions", []),
    }) for key, value in sorted(hypotheses.items()))


def _viewpoint_prompt(perspective_id: str, evidence: list[dict[str, Any]], hypotheses: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    example = {
        "viewpoint_id": f"VP-T0-{perspective_id}-H01",
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
    return f"""你是{perspective_id}专业视角分析器。只使用下方同一冻结T0快照、共享事实基线和三项正式假设，不得联网、读取T1、使用其他视角输出或调用模型常识。

研究问题：{QUESTION}
时间范围：{TIME_WINDOW}
冻结快照：{SNAPSHOT_ID}
你的视角：{PERSPECTIVES[perspective_id]}

任务：分别评价全部三项假设，不投票、不排名、不选择优胜假设。每个观点必须是对假设的简明判断，不能只复述假设。excerpt_original是事实核查首要字段；战略目标、计划和预期能力不得写成已实现能力，来源等级不等于证据方向。不要输出隐藏思维链，只输出简明论证。

输出必须是JSON数组，或单一对象 {{"viewpoints":[...]}}。必须正好覆盖HYP-T0-01、HYP-T0-02、HYP-T0-03各一次。每个对象至少包含：viewpoint_id、perspective_id、hypothesis_id、snapshot_id、stance、judgment、supporting_evidence、counter_evidence、key_assumptions、mechanism_claims、boundary_conditions、falsifiers、uncertainties、information_gaps、confidence、status。stance只能是support、challenge、conditional、insufficient；status固定为candidate。每个观点至少3条支持证据且来自至少2个source_id，并提供至少一条反证、限制性证据或明确的信息缺口。所有Evidence引用必须是下方冻结Evidence中的evidence_id和精确version。supporting_evidence/counter_evidence的每项必须包含evidence_id、version、target_field、relation、importance；relation使用support、challenge、limit或background，importance使用high、medium或low。mechanism_claims每项包含claim、basis（evidence或model_inference）和evidence_ids。不要输出额外解释或Markdown围栏之外的文字。

占位格式示例（不得复制占位内容）：
{json.dumps(example, ensure_ascii=False, indent=2)}

共享事实基线：
{_json(baseline)}

正式假设：
{_hypothesis_context(hypotheses)}

冻结Evidence（只能从这些内容引用）：
<FROZEN_EVIDENCE>
{_evidence_context(evidence)}
</FROZEN_EVIDENCE>

现在只返回观点JSON。"""


def _challenge_prompt(hypothesis: dict[str, Any], viewpoints: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    refs = {citation["evidence_id"] for view in viewpoints for citation in view.get("supporting_evidence", []) + view.get("counter_evidence", [])}
    evidence = [evidence_by_id[ref] for ref in sorted(refs) if ref in evidence_by_id]
    return f"""你是证据指向式批判审查器。只审查下方同一假设、四个专业视角已经形成的候选观点、这些观点实际引用的Evidence原文和共享基线。不得读取其他未提供的材料，不得联网，不得要求形成共识，不得输出隐藏思维链。

研究问题：{QUESTION}
冻结快照：{SNAPSHOT_ID}
目标假设：{_json(hypothesis)}
共享基线：{_json(baseline)}
候选观点：\n{_json(viewpoints)}
被引用Evidence：\n{_evidence_context(evidence)}

返回JSON数组或{{\"challenges\":[...]}}。每个观点最多提出2项高影响质疑，也可以没有质疑。每项必须包含challenge_id、target_viewpoint_id、target_field、challenge_type、challenge_statement、evidence_ids、impact、response_required。challenge_type只能是evidence_mismatch、goal_result_confusion、mechanism_gap、assumption_risk、scope_overreach、counter_evidence_omitted、uncertainty_understated；target_viewpoint_id必须来自给定观点；target_field必须指向具体字段、关键假设、机制、边界、反证或不确定性；evidence_ids只能来自给定Evidence；impact只能是high、medium、low；response_required为布尔值。禁止只写空泛的“证据不足”或“需要进一步研究”。"""


def _response_prompt(perspective_id: str, viewpoints: list[dict[str, Any]], challenges: list[dict[str, Any]], baseline: dict[str, Any]) -> str:
    return f"""你是{perspective_id}专业视角的质疑回应与观点修订器。只处理下方自己视角的三个观点及其收到的具体质疑。不得读取其他视角的回应，不得投票、排名或自动趋同，不得联网或读取T1，不得输出隐藏思维链。

研究问题：{QUESTION}
冻结快照：{SNAPSHOT_ID}
共享基线：{_json(baseline)}
本视角候选观点：{_json(viewpoints)}
收到的质疑：{_json(challenges)}

返回JSON数组或{{\"responses\":[...]}}，每个viewpoint_id恰好一次。每项包含viewpoint_id、response_action、response_note、updated_viewpoint。response_action只能是maintain、revise、conditional_retain、suspend、exit。updated_viewpoint应包含需要保留或修订的stance、judgment、supporting_evidence、counter_evidence、key_assumptions、mechanism_claims、boundary_conditions、falsifiers、uncertainties、information_gaps、confidence。只能修订已有观点字段，不能修改原文摘录或引入冻结快照外Evidence。Evidence引用必须保留精确evidence_id和version。"""


def _disagreement_prompt(final_viewpoints: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]], baseline: dict[str, Any]) -> str:
    refs = {citation["evidence_id"] for view in final_viewpoints for citation in view.get("supporting_evidence", []) + view.get("counter_evidence", [])}
    evidence = [evidence_by_id[ref] for ref in sorted(refs) if ref in evidence_by_id]
    return f"""你是分歧诊断器。只使用下方12个最终T0观点、其引用的Evidence、共享事实基线和研究问题。措辞不同但实质相同不建立分歧；不要为了数量制造分歧；不得投票、排名、选择主判断、联网或读取T1。

研究问题：{QUESTION}
冻结快照：{SNAPSHOT_ID}
共享基线：{_json(baseline)}
最终观点：{_json(final_viewpoints)}
引用Evidence：{_evidence_context(evidence)}

返回JSON数组或{{\"disagreements\":[...]}}，可返回空数组。每项包含disagreement_id、viewpoint_refs、type、contested_node、branch_a、branch_b、decision_impact、resolvable_now、resolution_action、status、diagnosis_reason。type只能是fact、scope、mechanism、assumption、risk_weight；viewpoint_refs必须指向至少两个不同观点和具体字段，格式为{{\"viewpoint_id\":\"...\",\"version\":1,\"field\":\"...\"}}；branch_a和branch_b均需有claim、viewpoint_refs、evidence_ids；decision_impact只能是high、medium、low；resolution_action只能是evidence_check、scope_split、conditional_retain或monitor；status固定为candidate；说明分歧是否影响总体研判以及当前是否可核查。"""


def _monitor_prompt(disagreements: list[dict[str, Any]], final_viewpoints: list[dict[str, Any]], baseline: dict[str, Any]) -> str:
    selected = [row for row in disagreements if row.get("decision_impact") == "high" and row.get("resolvable_now") is False and row.get("resolution_action") in {"conditional_retain", "monitor"}]
    return f"""你是判别性证据与公开监测方案设计器。只处理下方高影响、当前不可消除且处置为conditional_retain或monitor的候选分歧。不得联网、读取T1、增加模型常识或生成结论；若没有合格分歧，返回三个空数组。

研究问题：{QUESTION}
冻结快照：{SNAPSHOT_ID}
共享基线：{_json(baseline)}
待处理分歧：{_json(selected)}
相关观点：{_json(final_viewpoints)}

返回单一JSON对象，包含discriminative_needs、monitor_indicators、trigger_rules三个数组。每项discriminative_need包含need_id、disagreement_id、question、what_to_observe、outcome_a、outcome_b、public_sources、observation_frequency、affected_viewpoint_ids、discriminativeness；每项monitor_indicator包含indicator_id、need_id、name、operational_definition、source_types、cadence、direction_a、direction_b、affected_viewpoint_ids；每项trigger_rule包含trigger_id、indicator_id、predicate_type、trigger_condition、strengthens_branch、weakens_branch、required_source_grade、affected_viewpoint_ids、human_approval_required。discriminativeness只能是discriminative或non_discriminative；predicate_type只能是event_match、status_change或expert_confirmed；required_source_grade只能是A或B；human_approval_required必须为true。必须说明结果A和结果B如何改变分支相对支持方向，不得自行创造数值阈值。"""


def _generic_repair_prompt(content: str, expected: str) -> str:
    return f"""只做JSON格式修复，不改变下方模型输出的任何实质性内容、观点判断、Evidence引用或范围。删除Markdown围栏并返回严格JSON。预期顶层形式：{expected}。不得新增实质判断、Evidence、字段或解释。原始模型输出如下：\n{content}"""


def _checkpoint_path(output_dir: Path) -> Path:
    return output_dir / "stage_checkpoint.json"


def _load_or_create_checkpoint(output_dir: Path, run_id: str, identity: str) -> dict[str, Any]:
    path = _checkpoint_path(output_dir)
    if path.exists():
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        if checkpoint.get("run_id") != run_id or checkpoint.get("input_identity_sha256") != identity or checkpoint.get("prompt_template_version") != PROMPT_TEMPLATE_VERSION:
            raise P7P9Error("checkpoint identity or prompt template mismatch")
        return checkpoint
    checkpoint = {
        "run_id": run_id,
        "input_identity_sha256": identity,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "status": "running",
        "completed_batches": {},
        "completed_stages": [],
        "call_metadata_paths": [],
    }
    _write_json(path, checkpoint)
    return checkpoint


def _save_checkpoint(output_dir: Path, checkpoint: dict[str, Any]) -> None:
    _write_json(_checkpoint_path(output_dir), checkpoint)


def _safe_failure_manifest(output_dir: Path, run_id: str, stage: str, source_id: str | None, error: ModelAPIError | None, checkpoint: dict[str, Any]) -> None:
    completed = list(checkpoint.get("completed_batches", {}).keys())
    failure = {
        "run_id": run_id,
        "failure_stage": stage,
        "source_id": source_id,
        "exception_type": error.root_exception_type if error else "P7P9Error",
        "http_status_code": error.http_status_code if error else None,
        "completed_logical_calls": len(checkpoint.get("call_metadata_paths", [])),
        "saved_batches": completed,
        "evidence_inserted": 0,
        "formal_g3_executed": False,
        "formal_g4_t0_executed": False,
        "t1_read": False,
    }
    checkpoint["status"] = "failed"
    checkpoint["failure"] = {key: failure[key] for key in ("failure_stage", "source_id", "exception_type", "http_status_code")}
    _save_checkpoint(output_dir, checkpoint)
    _write_json(output_dir / "p7_p9_failure_manifest.json", failure)


def _call_batch(
    *,
    batch_id: str,
    phase: str,
    source_id: str | None,
    prompt: str,
    expected_wrapper: str,
    output_dir: Path,
    checkpoint: dict[str, Any],
    client: APIModelClient,
) -> tuple[Any, dict[str, Any]]:
    prompt_path = output_dir / "prompts" / f"{batch_id}.txt"
    raw_path = output_dir / "raw_responses" / f"{batch_id}.json"
    normalized_path = output_dir / "normalized_responses" / f"{batch_id}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    prompt_sha = _sha256_file(prompt_path)
    existing = checkpoint.get("completed_batches", {}).get(batch_id)
    if existing:
        if existing.get("prompt_sha256") != prompt_sha or existing.get("input_identity_sha256") != checkpoint.get("input_identity_sha256") or existing.get("prompt_template_version") != PROMPT_TEMPLATE_VERSION:
            raise P7P9Error(f"checkpoint batch identity mismatch: {batch_id}")
        existing_raw = output_dir / existing.get("raw_response_path", "")
        existing_normalized = output_dir / existing.get("normalized_response_path", "")
        record_path = output_dir / existing.get("call_metadata_path", "")
        if not existing_raw.exists() or not existing_normalized.exists() or not record_path.exists():
            raise P7P9Error(f"completed checkpoint batch artifacts are missing: {batch_id}")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if _sha256_file(existing_raw) != record.get("response_sha256"):
            raise P7P9Error(f"completed checkpoint raw response hash mismatch: {batch_id}")
        return json.loads(existing_normalized.read_text(encoding="utf-8")), {"reused": True, "call_record": record}
    try:
        payload, content, call_record = client.call(prompt, temperature=0.0, max_tokens=16000)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(raw_path, payload)
        primary_record_path = output_dir / "api_call_metadata" / f"{batch_id}.json"
        primary_record = call_record.as_dict() | {"batch_id": batch_id, "call_id": batch_id, "phase": phase, "source_id": source_id, "prompt_sha256": prompt_sha, "response_sha256": _sha256_file(raw_path), "is_repair": False}
        _write_json(primary_record_path, primary_record)
        parsed: Any
        try:
            parsed = _parse_json_compat(content)
        except Exception:
            repair_prompt = _generic_repair_prompt(content, f"JSON数组或{{\"{expected_wrapper}\": [...]}}")
            repair_id = f"{batch_id}__format_repair"
            repair_prompt_path = output_dir / "prompts" / f"{repair_id}.txt"
            repair_prompt_path.write_text(repair_prompt, encoding="utf-8")
            repair_payload, repair_content, repair_record = client.call(repair_prompt, temperature=0.0, max_tokens=16000)
            repair_raw_path = output_dir / "raw_responses" / f"{repair_id}.json"
            repair_raw_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(repair_raw_path, repair_payload)
            try:
                parsed = _parse_json_compat(repair_content)
            except Exception as exc:
                raise P7P9Error(f"JSON parse failed after one format repair: {batch_id}") from exc
            repair_record_path = output_dir / "api_call_metadata" / f"{repair_id}.json"
            repair_meta = repair_record.as_dict() | {"batch_id": batch_id, "call_id": repair_id, "phase": phase, "source_id": source_id, "prompt_sha256": _sha256_file(repair_prompt_path), "response_sha256": _sha256_file(repair_raw_path), "is_repair": True}
            _write_json(repair_record_path, repair_meta)
            raw_path = repair_raw_path
            call_record = repair_record
            call_record_meta = repair_meta
        else:
            call_record_meta = primary_record
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(normalized_path, parsed)
        record_path = output_dir / "api_call_metadata" / f"{call_record_meta['call_id']}.json"
        checkpoint.setdefault("completed_batches", {})[batch_id] = {
            "status": "completed",
            "input_identity_sha256": checkpoint["input_identity_sha256"],
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_sha256": prompt_sha,
            "raw_response_path": str(raw_path.relative_to(output_dir)).replace("\\", "/"),
            "normalized_response_path": str(normalized_path.relative_to(output_dir)).replace("\\", "/"),
            "call_metadata_path": str(record_path.relative_to(output_dir)).replace("\\", "/"),
            "response_sha256": call_record_meta["response_sha256"],
            "phase": phase,
            "source_id": source_id,
        }
        checkpoint.setdefault("call_metadata_paths", []).append(str(record_path.relative_to(output_dir)).replace("\\", "/"))
        _save_checkpoint(output_dir, checkpoint)
        return parsed, {"reused": False, "call_record": call_record_meta}
    except ModelAPIError:
        raise


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a non-empty string array")
    return [item.strip() for item in value]


def _citation(value: Any, evidence_by_id: dict[str, dict[str, Any]], default_relation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("citation is not an object")
    evidence_id = value.get("evidence_id")
    version = value.get("version")
    if not isinstance(evidence_id, str) or evidence_id not in evidence_by_id or not isinstance(version, int):
        raise ValueError("citation Evidence ID or version is invalid")
    expected_version = int(evidence_by_id[evidence_id].get("version", 1))
    if version != expected_version:
        raise ValueError("citation version does not match frozen snapshot")
    target_field = value.get("target_field")
    if not isinstance(target_field, str) or not target_field.strip():
        raise ValueError("citation target_field is empty")
    relation = value.get("relation", default_relation)
    if relation not in {"support", "challenge", "limit", "background"}:
        raise ValueError("citation relation is invalid")
    importance = value.get("importance", "medium")
    if importance not in {"high", "medium", "low"}:
        raise ValueError("citation importance is invalid")
    formal = {"evidence_id": evidence_id, "version": version, "relation": relation, "note": ""}
    details = {"evidence_id": evidence_id, "version": version, "target_field": target_field, "relation": relation, "importance": importance}
    return formal, details


def _normalize_mechanisms(value: Any, evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise ValueError("mechanism_claims is empty or not an array")
    claims: list[str] = []
    details: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            claim = item.strip()
            if not claim:
                raise ValueError("mechanism claim is empty")
            claims.append(claim)
            details.append({"claim": claim, "basis": "model_inference", "evidence_ids": []})
            continue
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip() or item.get("basis") not in {"evidence", "model_inference"}:
            raise ValueError("mechanism claim structure is invalid")
        refs = item.get("evidence_ids", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) and ref in evidence_by_id for ref in refs):
            raise ValueError("mechanism claim Evidence IDs are invalid")
        claim = item["claim"].strip()
        claims.append(claim)
        details.append({"claim": claim, "basis": item["basis"], "evidence_ids": list(dict.fromkeys(refs))})
    return claims, details


def _normalize_viewpoint(raw: Any, perspective_id: str, hypothesis_id: str, evidence_by_id: dict[str, dict[str, Any]], *, fallback_errors: list[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("viewpoint is not an object")
    errors = list(fallback_errors or [])
    if raw.get("perspective_id") not in {None, perspective_id}:
        errors.append("perspective_id mismatch")
    if raw.get("hypothesis_id") not in {None, hypothesis_id}:
        errors.append("hypothesis_id mismatch")
    stance = raw.get("stance")
    if stance not in STANCE_VALUES:
        raise ValueError("invalid stance")
    judgment = raw.get("judgment")
    if not isinstance(judgment, str) or not judgment.strip():
        raise ValueError("judgment is empty")
    supports_raw = raw.get("supporting_evidence")
    counters_raw = raw.get("counter_evidence", [])
    if not isinstance(supports_raw, list) or not isinstance(counters_raw, list):
        raise ValueError("evidence citation lists are invalid")
    supports: list[dict[str, Any]] = []
    support_details: list[dict[str, Any]] = []
    counters: list[dict[str, Any]] = []
    counter_details: list[dict[str, Any]] = []
    for item in supports_raw:
        formal, details = _citation(item, evidence_by_id, "support")
        supports.append(formal)
        support_details.append(details)
    for item in counters_raw:
        formal, details = _citation(item, evidence_by_id, "challenge")
        counters.append(formal)
        counter_details.append(details)
    assumptions = _string_list(raw.get("key_assumptions"), "key_assumptions")
    boundaries = _string_list(raw.get("boundary_conditions"), "boundary_conditions")
    falsifiers = _string_list(raw.get("falsifiers"), "falsifiers")
    uncertainties = _string_list(raw.get("uncertainties"), "uncertainties")
    gaps = _string_list(raw.get("information_gaps"), "information_gaps")
    mechanism_claims, mechanism_details = _normalize_mechanisms(raw.get("mechanism_claims"), evidence_by_id)
    if len(supports) < 3:
        errors.append("fewer than three supporting Evidence citations")
    source_count = len({evidence_by_id[c["evidence_id"]]["source_id"] for c in supports})
    if source_count < 2:
        errors.append("supporting Evidence comes from fewer than two sources")
    if not counters and not gaps:
        errors.append("no counter/limitation citation or information gap")
    confidence = raw.get("confidence", "low")
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
        errors.append("invalid confidence was normalized to low")
    expected_id = f"VP-T0-{perspective_id}-{hypothesis_id.replace('HYP-T0-', 'H')}"
    return {
        "viewpoint_id": expected_id,
        "perspective_id": perspective_id,
        "hypothesis_id": hypothesis_id,
        "snapshot_id": SNAPSHOT_ID,
        "stance": stance,
        "judgment": judgment.strip(),
        "supporting_evidence": supports,
        "counter_evidence": counters,
        "key_assumptions": assumptions,
        "mechanism_claims": mechanism_claims,
        "boundary_conditions": boundaries,
        "falsifiers": falsifiers,
        "uncertainties": uncertainties,
        "information_gaps": gaps,
        "confidence": confidence,
        "status": "candidate",
        "_citation_details": support_details + counter_details,
        "_mechanism_details": mechanism_details,
        "_validation_errors": errors,
        "_support_source_count": source_count,
    }


def _restore_response_citation_locations(update: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Carry forward P7 citation locations when P8 changes no citation set.

    P8 may return an otherwise valid updated viewpoint with the same Evidence
    IDs but omit the audit-only target_field/importance fields.  Reusing the
    already validated P7 locations is deterministic and does not invent a new
    source, excerpt, or claim.  A newly introduced citation without a prior
    location remains invalid and is handled by the normal gate.
    """
    restored = dict(update)
    prior = {
        (detail.get("evidence_id"), detail.get("relation", "")): detail
        for detail in base.get("_citation_details", [])
    }
    for field in ("supporting_evidence", "counter_evidence"):
        citations = restored.get(field)
        if not isinstance(citations, list):
            continue
        enriched: list[Any] = []
        for item in citations:
            if not isinstance(item, dict):
                enriched.append(item)
                continue
            copied = dict(item)
            relation = copied.get("relation", "support" if field == "supporting_evidence" else "challenge")
            prior_detail = prior.get((copied.get("evidence_id"), relation))
            if prior_detail:
                copied.setdefault("target_field", prior_detail.get("target_field", ""))
                copied.setdefault("importance", prior_detail.get("importance", "medium"))
            enriched.append(copied)
        restored[field] = enriched
    return restored


def _suspended_viewpoint(perspective_id: str, hypothesis_id: str, errors: list[str]) -> dict[str, Any]:
    return {
        "viewpoint_id": f"VP-T0-{perspective_id}-{hypothesis_id.replace('HYP-T0-', 'H')}",
        "perspective_id": perspective_id,
        "hypothesis_id": hypothesis_id,
        "snapshot_id": SNAPSHOT_ID,
        "stance": "insufficient",
        "judgment": "该视角×假设位置未形成满足确定性准入条件的候选观点。",
        "supporting_evidence": [], "counter_evidence": [],
        "key_assumptions": ["待形成可核查观点后再评估"],
        "mechanism_claims": ["尚未形成可核查机制主张"],
        "boundary_conditions": ["仅保留该位置，暂不作实质性判断"],
        "falsifiers": ["获得满足准入条件的观点与证据引用"],
        "uncertainties": ["模型候选未通过确定性校验"],
        "information_gaps": ["；".join(errors) or "未返回可校验观点"],
        "confidence": "low", "status": "candidate",
        "_citation_details": [], "_mechanism_details": [],
        "_validation_errors": errors or ["no valid model viewpoint"],
        "_support_source_count": 0,
    }


def _validate_p7(value: Any, perspective_id: str, evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("viewpoints",))
    by_hyp: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append({"index": index, "reason": "viewpoint is not an object"})
            continue
        hypothesis_id = raw.get("hypothesis_id")
        if hypothesis_id not in HYPOTHESIS_IDS or hypothesis_id in by_hyp:
            errors.append({"index": index, "reason": "invalid or duplicate hypothesis_id"})
            continue
        try:
            by_hyp[hypothesis_id] = _normalize_viewpoint(raw, perspective_id, hypothesis_id, evidence_by_id)
        except Exception as exc:
            errors.append({"index": index, "hypothesis_id": hypothesis_id, "reason": type(exc).__name__ + ": " + str(exc)})
    output: list[dict[str, Any]] = []
    for hypothesis_id in HYPOTHESIS_IDS:
        if hypothesis_id not in by_hyp:
            output.append(_suspended_viewpoint(perspective_id, hypothesis_id, ["missing viewpoint or invalid model object"]))
        else:
            output.append(by_hyp[hypothesis_id])
    return output, errors


def _challenge_ref(value: Any, valid_view_ids: set[str]) -> tuple[str, str] | None:
    if not isinstance(value, dict) or value.get("target_viewpoint_id") not in valid_view_ids or not isinstance(value.get("target_field"), str) or not value["target_field"].strip():
        return None
    return value["target_viewpoint_id"], value["target_field"]


def _validate_challenges(value: Any, valid_view_ids: set[str], evidence_by_id: dict[str, dict[str, Any]], hypothesis_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("challenges",))
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    per_view: Counter[str] = Counter()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "challenge is not an object"})
            continue
        ref = _challenge_ref(raw, valid_view_ids)
        evidence_ids = raw.get("evidence_ids", [])
        if ref is None or raw.get("challenge_type") not in CHALLENGE_TYPES or raw.get("impact") not in {"high", "medium", "low"} or not isinstance(raw.get("challenge_statement"), str) or not raw["challenge_statement"].strip() or not isinstance(evidence_ids, list) or not all(ref_id in evidence_by_id for ref_id in evidence_ids):
            rejected.append({"index": index, "reason": "invalid challenge fields or Evidence references"})
            continue
        if per_view[ref[0]] >= 2:
            rejected.append({"index": index, "reason": "more than two challenges for one viewpoint"})
            continue
        per_view[ref[0]] += 1
        valid.append({
            "challenge_id": raw.get("challenge_id") or f"CH-{hypothesis_id}-{len(valid)+1:02d}",
            "target_viewpoint_id": ref[0], "target_field": ref[1],
            "challenge_type": raw["challenge_type"], "challenge_statement": raw["challenge_statement"].strip(),
            "evidence_ids": list(dict.fromkeys(evidence_ids)), "impact": raw["impact"],
            "response_required": bool(raw.get("response_required", True)),
        })
    return valid, rejected


def _validate_response(value: Any, perspective_id: str, initial: list[dict[str, Any]], challenges: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("responses", "viewpoints"))
    initial_by_id = {row["viewpoint_id"]: row for row in initial}
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or raw.get("viewpoint_id") not in initial_by_id or raw.get("viewpoint_id") in by_id:
            errors.append({"index": index, "reason": "response viewpoint_id is invalid or duplicated"})
            continue
        action = raw.get("response_action")
        if action not in ACTION_VALUES:
            errors.append({"viewpoint_id": raw.get("viewpoint_id"), "reason": "invalid response_action"})
            continue
        base = initial_by_id[raw["viewpoint_id"]]
        update = raw.get("updated_viewpoint") if isinstance(raw.get("updated_viewpoint"), dict) else raw
        update = _restore_response_citation_locations(update, initial_by_id[raw["viewpoint_id"]])
        merged = dict(base)
        for field in REQUIRED_VIEW_FIELDS + ("confidence",):
            if field in update:
                merged[field] = update[field]
        hyp = base["hypothesis_id"]
        try:
            normalized = _normalize_viewpoint(merged, perspective_id, hyp, evidence_by_id)
        except Exception as exc:
            errors.append({"viewpoint_id": raw["viewpoint_id"], "reason": type(exc).__name__ + ": " + str(exc)})
            normalized = dict(base)
            normalized["_validation_errors"] = list(base.get("_validation_errors", [])) + ["response update failed deterministic validation"]
            normalized["_citation_details"] = list(base.get("_citation_details", []))
            normalized["_mechanism_details"] = list(base.get("_mechanism_details", []))
        normalized["_response_action"] = action
        normalized["_response_note"] = raw.get("response_note", "") if isinstance(raw.get("response_note", ""), str) else ""
        normalized["_challenge_ids"] = [c["challenge_id"] for c in challenges if c["target_viewpoint_id"] == raw["viewpoint_id"]]
        by_id[raw["viewpoint_id"]] = normalized
    output: list[dict[str, Any]] = []
    for row in initial:
        if row["viewpoint_id"] not in by_id:
            missing = dict(row)
            missing["_response_action"] = "suspend"
            missing["_response_note"] = "未收到可校验的质疑回应，保留位置并暂停准入"
            missing["_validation_errors"] = list(missing.get("_validation_errors", [])) + ["missing response"]
            output.append(missing)
        else:
            output.append(by_id[row["viewpoint_id"]])
    return output, errors


def _finalize_viewpoint(view: dict[str, Any], run_id: str, perspective_call: dict[str, Any], response_call: dict[str, Any]) -> dict[str, Any]:
    action = view.get("_response_action", "suspend")
    deterministic_errors = list(view.get("_validation_errors", []))
    if action in {"suspend", "exit"} or deterministic_errors:
        status = "suspended"
        failure_reason = "; ".join(deterministic_errors) or ("response action requested suspension" if action == "suspend" else "response action requested exit")
    elif action == "conditional_retain":
        status = "conditional"
        failure_reason = ""
    else:
        status = "admitted"
        failure_reason = ""
    details = {
        "p7_call": perspective_call,
        "p8_response_call": response_call,
        "perspective_id": view["perspective_id"],
        "model_id": perspective_call.get("call_record", {}).get("requested_model_id"),
        "prompt_sha256": perspective_call.get("call_record", {}).get("prompt_sha256"),
        "response_sha256": perspective_call.get("call_record", {}).get("response_sha256"),
        "run_id": run_id,
        "response_action": action,
        "response_note": view.get("_response_note", ""),
        "citation_details": view.get("_citation_details", []),
        "mechanism_claim_details": view.get("_mechanism_details", []),
    }
    return {
        "schema_version": "phase-a.1",
        "created_at": _now_utc(),
        "run_id": run_id,
        "case_id": CASE_ID,
        "viewpoint_id": view["viewpoint_id"],
        "version": 1,
        "parent_version": None,
        "snapshot_id": SNAPSHOT_ID,
        "perspective_id": view["perspective_id"],
        "hypothesis_id": view["hypothesis_id"],
        "stance": view["stance"],
        "judgment": view["judgment"],
        "supporting_evidence": view.get("supporting_evidence", []),
        "counter_evidence": view.get("counter_evidence", []),
        "key_assumptions": view.get("key_assumptions", []),
        "mechanism_claims": view.get("mechanism_claims", []),
        "boundary_conditions": view.get("boundary_conditions", []),
        "falsifiers": view.get("falsifiers", []),
        "uncertainties": view.get("uncertainties", []),
        "information_gaps": view.get("information_gaps", []),
        "confidence": view.get("confidence", "low"),
        "status": status,
        "change_reason": "initial_version",
        "generator": details,
        "human_verified": False,
        "admission_failure_reason": failure_reason,
    }


def _make_dependencies(final_views: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str, str, str]] = set()
    for view in final_views:
        if view["status"] == "suspended":
            continue
        for detail in view.get("generator", {}).get("citation_details", []):
            relation = detail.get("relation", "support")
            if relation not in {"support", "challenge", "limit", "background"}:
                continue
            key = (view["viewpoint_id"], detail["evidence_id"], int(detail["version"]), detail["target_field"], relation, detail.get("importance", "medium"))
            if key in seen:
                continue
            seen.add(key)
            edge_seed = _json({"viewpoint_id": view["viewpoint_id"], "evidence_id": detail["evidence_id"], "version": detail["version"], "target_field": detail["target_field"], "relation": relation})
            output.append({
                "schema_version": "phase-a.1", "created_at": _now_utc(), "run_id": run_id,
                "case_id": CASE_ID,
                "edge_id": "EDGE-P7P9-" + _sha256_bytes(edge_seed.encode("utf-8"))[:20],
                "evidence_id": detail["evidence_id"], "evidence_version": int(detail["version"]),
                "viewpoint_id": view["viewpoint_id"], "viewpoint_version": 1,
                "target_field": detail["target_field"], "relation": relation,
                "importance": detail.get("importance", "medium"), "created_by": "P7P9_MODEL_PIPELINE",
                "human_verified": False, "changed_fields": [],
            })
    return output


def _insert_transaction(database_path: Path, final_views: list[dict[str, Any]], dependencies: list[dict[str, Any]], *, inject_failure_after: int | None = None) -> dict[str, Any]:
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        snapshot = conn.execute("SELECT case_id, phase, frozen FROM evidence_snapshot WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()
        if not snapshot or snapshot[0] != CASE_ID or snapshot[1] != "T0" or snapshot[2] != 1:
            raise P7P9Error("target T0 snapshot is missing or not frozen")
        expected_ids = {view["viewpoint_id"] for view in final_views}
        rows = conn.execute("SELECT viewpoint_id, version, payload_json FROM viewpoint WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchall()
        if rows:
            existing = {row[0]: json.loads(row[2]) for row in rows}
            if set(existing) != expected_ids or any(existing[key] != view for key, view in ((view["viewpoint_id"], view) for view in final_views)):
                raise P7P9Error("target snapshot already contains different viewpoint content")
            expected_dep_ids = {dependency["edge_id"] for dependency in dependencies}
            dep_rows = conn.execute("SELECT edge_id, payload_json FROM evidence_dependency WHERE viewpoint_id IN ({})".format(",".join("?" for _ in expected_ids)), tuple(expected_ids)).fetchall() if expected_ids else []
            existing_deps = {row[0]: json.loads(row[1]) for row in dep_rows}
            if set(existing_deps) != expected_dep_ids or any(existing_deps[key] != dependency for key, dependency in ((dependency["edge_id"], dependency) for dependency in dependencies)):
                raise P7P9Error("target snapshot already contains different dependency content")
            conn.rollback()
            return {"replayed": True, "viewpoint_rows_added": 0, "dependency_rows_added": 0, "transaction_committed": False}
        for index, view in enumerate(final_views):
            model_validate(Viewpoint, view)
            conn.execute("INSERT INTO viewpoint(viewpoint_id,version,snapshot_id,status,payload_json) VALUES (?,?,?,?,?)", (view["viewpoint_id"], 1, SNAPSHOT_ID, view["status"], _json(view)))
            if inject_failure_after is not None and index + 1 >= inject_failure_after:
                raise P7P9Error("injected viewpoint transaction failure")
        for dependency in dependencies:
            model_validate(EvidenceDependency, dependency)
            conn.execute("INSERT INTO evidence_dependency(edge_id,case_id,evidence_id,evidence_version,viewpoint_id,viewpoint_version,relation,importance,payload_json) VALUES (?,?,?,?,?,?,?,?,?)", (dependency["edge_id"], CASE_ID, dependency["evidence_id"], dependency["evidence_version"], dependency["viewpoint_id"], dependency["viewpoint_version"], dependency["relation"], dependency["importance"], _json(dependency)))
        conn.commit()
        return {"replayed": False, "viewpoint_rows_added": len(final_views), "dependency_rows_added": len(dependencies), "transaction_committed": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rollback_probe(database_path: Path, final_views: list[dict[str, Any]], dependencies: list[dict[str, Any]]) -> bool:
    with tempfile.TemporaryDirectory(prefix=".p7p9-rollback-") as temp:
        copied = Path(temp) / "research.sqlite3"
        shutil.copy2(database_path, copied)
        try:
            _insert_transaction(copied, final_views, dependencies, inject_failure_after=1)
        except P7P9Error as exc:
            if "injected viewpoint transaction failure" not in str(exc):
                return False
        else:
            return False
        conn = sqlite3.connect(str(copied))
        try:
            count = conn.execute("SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0]
            dep_count = conn.execute("SELECT COUNT(*) FROM evidence_dependency WHERE viewpoint_id LIKE 'VP-T0-%'").fetchone()[0]
            return count == 0 and dep_count == 0
        finally:
            conn.close()


def _db_counts(database_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(database_path))
    try:
        return {
            "viewpoint_count": conn.execute("SELECT COUNT(*) FROM viewpoint WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "dependency_count": conn.execute("SELECT COUNT(*) FROM evidence_dependency d JOIN viewpoint v ON d.viewpoint_id=v.viewpoint_id AND d.viewpoint_version=v.version WHERE v.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "disagreement_count": conn.execute("SELECT COUNT(*) FROM disagreement WHERE snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
            "need_count": conn.execute("SELECT COUNT(*) FROM discriminative_need n JOIN disagreement d ON n.disagreement_id=d.disagreement_id WHERE d.snapshot_id=?", (SNAPSHOT_ID,)).fetchone()[0],
        }
    finally:
        conn.close()


def _validate_disagreements(value: Any, valid_view_ids: set[str], evidence_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _unwrap(value, ("disagreements",))
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "disagreement is not an object"})
            continue
        did = raw.get("disagreement_id") or f"DG-T0-{len(valid)+1:02d}"
        refs = raw.get("viewpoint_refs")
        if not isinstance(refs, list):
            rejected.append({"index": index, "reason": "viewpoint_refs is not an array"})
            continue
        normalized_refs: list[dict[str, Any]] = []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("viewpoint_id") in valid_view_ids and ref.get("version") == 1 and isinstance(ref.get("field"), str) and ref["field"].strip():
                normalized_refs.append({"viewpoint_id": ref["viewpoint_id"], "version": 1, "field": ref["field"]})
        if did in seen or len({ref["viewpoint_id"] for ref in normalized_refs}) < 2 or raw.get("type") not in DISAGREEMENT_TYPES or raw.get("decision_impact") not in {"high", "medium", "low"} or not isinstance(raw.get("contested_node"), str) or not raw["contested_node"].strip() or not isinstance(raw.get("branch_a"), dict) or not isinstance(raw.get("branch_b"), dict) or raw.get("resolution_action") not in RESOLUTION_ACTIONS or not isinstance(raw.get("resolvable_now"), bool):
            rejected.append({"index": index, "reason": "invalid disagreement structure or fewer than two viewpoint branches"})
            continue
        branches: list[dict[str, Any]] = []
        branch_ok = True
        for key in ("branch_a", "branch_b"):
            branch = raw[key]
            branch_refs = []
            for ref in branch.get("viewpoint_refs", []) if isinstance(branch.get("viewpoint_refs", []), list) else []:
                if isinstance(ref, dict) and ref.get("viewpoint_id") in valid_view_ids and ref.get("version") == 1 and isinstance(ref.get("field"), str):
                    branch_refs.append({"viewpoint_id": ref["viewpoint_id"], "version": 1, "field": ref["field"]})
            ids = branch.get("evidence_ids", [])
            if not isinstance(branch.get("claim"), str) or not branch["claim"].strip() or not isinstance(ids, list) or not all(item in evidence_by_id for item in ids):
                branch_ok = False
            branches.append({"claim": branch.get("claim", ""), "viewpoint_refs": branch_refs, "evidence_ids": list(dict.fromkeys(ids)) if isinstance(ids, list) else []})
        if not branch_ok:
            rejected.append({"index": index, "reason": "invalid disagreement branch"})
            continue
        seen.add(did)
        valid.append({
            "disagreement_id": did, "snapshot_id": SNAPSHOT_ID, "viewpoint_refs": normalized_refs,
            "type": raw["type"], "contested_node": raw["contested_node"].strip(), "branch_a": branches[0], "branch_b": branches[1],
            "decision_impact": raw["decision_impact"], "resolvable_now": raw["resolvable_now"], "resolution_action": raw["resolution_action"],
            "status": "candidate", "diagnosis_reason": raw.get("diagnosis_reason", "") if isinstance(raw.get("diagnosis_reason", ""), str) else "",
        })
    return valid, rejected


def _validate_monitoring(value: Any, disagreements: list[dict[str, Any]], valid_view_ids: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise P7P9Error("monitoring response must be an object")
    selected_ids = {row["disagreement_id"] for row in disagreements if row["decision_impact"] == "high" and row["resolvable_now"] is False and row["resolution_action"] in {"conditional_retain", "monitor"}}
    needs: list[dict[str, Any]] = []
    indicators: list[dict[str, Any]] = []
    triggers: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    need_ids: set[str] = set()
    for index, raw in enumerate(_extract_wrapped(value, "discriminative_needs")):
        if not isinstance(raw, dict) or raw.get("disagreement_id") not in selected_ids or raw.get("discriminativeness") not in {"discriminative", "non_discriminative"} or not isinstance(raw.get("question"), str) or not raw["question"].strip() or not isinstance(raw.get("affected_viewpoint_ids"), list) or not set(raw["affected_viewpoint_ids"]).issubset(valid_view_ids):
            rejected.append({"kind": "need", "index": index, "reason": "invalid or out-of-scope discriminative need"})
            continue
        need_id = raw.get("need_id") or f"NEED-T0-{len(needs)+1:02d}"
        if need_id in need_ids:
            rejected.append({"kind": "need", "index": index, "reason": "duplicate need_id"})
            continue
        need_ids.add(need_id)
        needs.append({key: raw.get(key, "") for key in ("need_id", "disagreement_id", "question", "what_to_observe", "outcome_a", "outcome_b", "public_sources", "observation_frequency", "affected_viewpoint_ids", "discriminativeness")})
    indicator_ids: set[str] = set()
    for index, raw in enumerate(_extract_wrapped(value, "monitor_indicators")):
        if not isinstance(raw, dict) or raw.get("need_id") not in need_ids or not isinstance(raw.get("affected_viewpoint_ids"), list) or not set(raw["affected_viewpoint_ids"]).issubset(valid_view_ids):
            rejected.append({"kind": "indicator", "index": index, "reason": "invalid indicator linkage"})
            continue
        indicator_id = raw.get("indicator_id") or f"IND-T0-{len(indicators)+1:02d}"
        if indicator_id in indicator_ids:
            rejected.append({"kind": "indicator", "index": index, "reason": "duplicate indicator_id"})
            continue
        indicator_ids.add(indicator_id)
        indicators.append({key: raw.get(key, "") for key in ("indicator_id", "need_id", "name", "operational_definition", "source_types", "cadence", "direction_a", "direction_b", "affected_viewpoint_ids")})
    for index, raw in enumerate(_extract_wrapped(value, "trigger_rules")):
        condition = raw.get("trigger_condition") if isinstance(raw, dict) else ""
        required_grade = raw.get("required_source_grade") if isinstance(raw, dict) else None
        required_grade_ok = required_grade in {"A", "B"} or (isinstance(required_grade, list) and len(required_grade) == 1 and required_grade[0] in {"A", "B"})
        if not isinstance(raw, dict) or raw.get("indicator_id") not in indicator_ids or raw.get("predicate_type") not in PREDICATE_TYPES or not isinstance(raw.get("affected_viewpoint_ids"), list) or not set(raw["affected_viewpoint_ids"]).issubset(valid_view_ids) or not required_grade_ok or raw.get("human_approval_required") is not True or not isinstance(condition, str) or not condition.strip() or re.search(r"\d+\s*(%|次|天|月|年)", condition):
            rejected.append({"kind": "trigger", "index": index, "reason": "invalid trigger linkage, source grade, approval, or numeric threshold"})
            continue
        trigger_id = raw.get("trigger_id") or f"TRG-T0-{len(triggers)+1:02d}"
        if isinstance(required_grade, list):
            required_grade = required_grade[0]
        triggers.append({key: raw.get(key, "") for key in ("trigger_id", "indicator_id", "predicate_type", "trigger_condition", "strengthens_branch", "weakens_branch", "affected_viewpoint_ids", "human_approval_required")} | {"required_source_grade": required_grade})
    non_discriminative = [need for need in needs if need["discriminativeness"] == "non_discriminative"]
    return needs, indicators, triggers, rejected + [{"kind": "non_discriminative", "need_id": need["need_id"], "reason": "non_discriminative need excluded from G3 monitoring candidates"} for need in non_discriminative]


def _quality_csv_rows(initial: list[dict[str, Any]], final: list[dict[str, Any]]) -> list[dict[str, Any]]:
    final_by_id = {row["viewpoint_id"]: row for row in final}
    rows: list[dict[str, Any]] = []
    for row in initial:
        end = final_by_id[row["viewpoint_id"]]
        rows.append({
            "viewpoint_id": row["viewpoint_id"], "perspective_id": row["perspective_id"], "hypothesis_id": row["hypothesis_id"],
            "initial_validation_passed": not row.get("_validation_errors"), "initial_validation_errors": row.get("_validation_errors", []),
            "final_status": end["status"], "final_validation_errors": end.get("admission_failure_reason", ""),
            "supporting_evidence_count": len(end.get("supporting_evidence", [])), "supporting_source_count": end.get("generator", {}).get("support_source_count", ""),
        })
    return rows


def _make_g3_rows(disagreements: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    high = {row["disagreement_id"]: row for row in disagreements if row["decision_impact"] == "high" and row["resolvable_now"] is False and row["resolution_action"] in {"conditional_retain", "monitor"}}
    indicator_by_need = {row["need_id"]: row for row in indicators}
    trigger_by_indicator = {row["indicator_id"]: row for row in triggers}
    rows: list[dict[str, Any]] = []
    for need in needs:
        if need["discriminativeness"] != "discriminative" or need["disagreement_id"] not in high:
            continue
        indicator = indicator_by_need.get(need["need_id"], {})
        trigger = trigger_by_indicator.get(indicator.get("indicator_id"), {})
        rows.append({
            "disagreement_id": need["disagreement_id"], "decision_impact": "high", "discriminative_need": need,
            "monitor_indicator": indicator, "trigger_rule": trigger, "ai_recommendation": "retain_for_author_review",
            "author_decision": "", "final_decision_impact": "", "final_need": "", "final_indicator": "", "final_trigger": "", "reviewer": "", "reviewed_at": "", "review_note": "",
        })
    return rows


def _write_outputs(output_dir: Path, run_id: str, input_hashes: dict[str, str], identity: str, hypotheses: dict[str, dict[str, Any]], baseline: dict[str, Any], initial: list[dict[str, Any]], final: list[dict[str, Any]], dependencies: list[dict[str, Any]], challenges_by_hyp: dict[str, list[dict[str, Any]]], challenge_rejections: list[dict[str, Any]], disagreements: list[dict[str, Any]], disagreement_rejections: list[dict[str, Any]], needs: list[dict[str, Any]], indicators: list[dict[str, Any]], triggers: list[dict[str, Any]], monitoring_rejections: list[dict[str, Any]], call_meta: list[dict[str, Any]], protected_before: dict[str, str], protected_after: dict[str, str], transaction_result: dict[str, Any], rollback_passed: bool, idempotency_passed: bool) -> dict[str, Any]:
    _write_jsonl(output_dir / "P7" / "initial_viewpoints.jsonl", initial)
    _write_csv(output_dir / "P7" / "viewpoint_validation.csv", ["viewpoint_id", "perspective_id", "hypothesis_id", "initial_validation_passed", "initial_validation_errors", "final_status", "final_validation_errors", "supporting_evidence_count", "supporting_source_count"], _quality_csv_rows(initial, final))
    for hypothesis_id in HYPOTHESIS_IDS:
        _write_json(output_dir / "P8" / f"hypothesis_{hypothesis_id.replace('HYP-T0-', 'h').lower()}_challenges.json", {"challenges": challenges_by_hyp.get(hypothesis_id, []), "rejected": [r for r in challenge_rejections if r.get("hypothesis_id") == hypothesis_id]})
    _write_jsonl(output_dir / "P8" / "final_viewpoints.jsonl", final)
    admission_rows = [{"viewpoint_id": row["viewpoint_id"], "perspective_id": row["perspective_id"], "hypothesis_id": row["hypothesis_id"], "status": row["status"], "admission_failure_reason": row.get("admission_failure_reason", ""), "human_verified": False} for row in final]
    _write_csv(output_dir / "P8" / "viewpoint_admission_report.csv", ["viewpoint_id", "perspective_id", "hypothesis_id", "status", "admission_failure_reason", "human_verified"], admission_rows)
    _write_jsonl(output_dir / "P8" / "evidence_dependencies.jsonl", dependencies)
    diff_rows = []
    initial_by_id = {row["viewpoint_id"]: row for row in initial}
    for row in final:
        before = initial_by_id[row["viewpoint_id"]]
        changed = [field for field in REQUIRED_VIEW_FIELDS + ("confidence",) if before.get(field) != row.get(field)]
        diff_rows.append({"viewpoint_id": row["viewpoint_id"], "version_before": 0, "version_after": 1, "changed_fields": changed, "response_action": row.get("generator", {}).get("response_action", ""), "response_note": row.get("generator", {}).get("response_note", "")})
    _write_csv(output_dir / "P8" / "viewpoint_before_after_diff.csv", ["viewpoint_id", "version_before", "version_after", "changed_fields", "response_action", "response_note"], diff_rows)
    _write_jsonl(output_dir / "P9" / "candidate_disagreements.jsonl", disagreements)
    disagreement_rows = [{"disagreement_id": row["disagreement_id"], "type": row["type"], "decision_impact": row["decision_impact"], "resolvable_now": row["resolvable_now"], "resolution_action": row["resolution_action"], "viewpoint_count": len({ref["viewpoint_id"] for ref in row["viewpoint_refs"]}), "validation_passed": True} for row in disagreements]
    disagreement_rows += [{"disagreement_id": r.get("index", ""), "validation_passed": False, "reason": r.get("reason", "")} for r in disagreement_rejections]
    _write_csv(output_dir / "P9" / "disagreement_validation.csv", ["disagreement_id", "type", "decision_impact", "resolvable_now", "resolution_action", "viewpoint_count", "validation_passed", "reason"], disagreement_rows)
    _write_jsonl(output_dir / "P9" / "discriminative_needs_candidates.jsonl", needs)
    _write_jsonl(output_dir / "P9" / "monitor_indicators_candidates.jsonl", indicators)
    _write_jsonl(output_dir / "P9" / "trigger_rules_candidates.jsonl", triggers)
    matrix_rows = []
    for disagreement in disagreements:
        for ref in disagreement["viewpoint_refs"]:
            matrix_rows.append({"disagreement_id": disagreement["disagreement_id"], "viewpoint_id": ref["viewpoint_id"], "version": ref["version"], "field": ref["field"], "branch_a": disagreement["branch_a"]["claim"], "branch_b": disagreement["branch_b"]["claim"]})
    _write_csv(output_dir / "P9" / "disagreement_viewpoint_matrix.csv", ["disagreement_id", "viewpoint_id", "version", "field", "branch_a", "branch_b"], matrix_rows)
    g3_rows = _make_g3_rows(disagreements, needs, indicators, triggers)
    _write_csv(output_dir / "G3_PREP" / "g3_ai_preannotation.csv", list(g3_rows[0].keys()) if g3_rows else ["disagreement_id", "decision_impact", "discriminative_need", "monitor_indicator", "trigger_rule", "ai_recommendation", "author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"], g3_rows)
    _write_csv(output_dir / "G3_PREP" / "g3_author_decision_template.csv", ["disagreement_id", "decision_impact", "discriminative_need", "monitor_indicator", "trigger_rule", "ai_recommendation", "author_decision", "final_decision_impact", "final_need", "final_indicator", "final_trigger", "reviewer", "reviewed_at", "review_note"], g3_rows)
    trace_rows = [{"disagreement_id": row["disagreement_id"], "need_id": row["discriminative_need"]["need_id"], "indicator_id": row.get("monitor_indicator", {}).get("indicator_id", ""), "trigger_id": row.get("trigger_rule", {}).get("trigger_id", ""), "affected_viewpoint_ids": row["discriminative_need"].get("affected_viewpoint_ids", []), "human_approval_required": row.get("trigger_rule", {}).get("human_approval_required", "")} for row in g3_rows]
    _write_csv(output_dir / "G3_PREP" / "g3_monitoring_traceability.csv", ["disagreement_id", "need_id", "indicator_id", "trigger_id", "affected_viewpoint_ids", "human_approval_required"], trace_rows)
    summary_lines = [
        "# G3作者签核摘要", "", "本摘要仅为AI生成的审核准备材料，作者实际审核数为0。",
        "", "## 主要事实共识", "", "共享事实基线沿用P4冻结证据和G2授权结果；本阶段不把任何观点标记为最终主判断。",
        "", "## 各专业视角核心判断", "",
    ]
    for perspective_id in PERSPECTIVES:
        summary_lines.append(f"- {perspective_id}：" + "；".join(row["judgment"] for row in final if row["perspective_id"] == perspective_id))
    summary_lines += ["", "## 高影响未决分歧", ""]
    if not g3_rows:
        summary_lines.append("当前未形成符合筛选条件的判别性监测项。")
    for row in g3_rows:
        summary_lines += [f"- {row['disagreement_id']}：{row['discriminative_need'].get('question', '')}", f"  - 观察：{row['discriminative_need'].get('what_to_observe', '')}", f"  - 结果A：{row['discriminative_need'].get('outcome_a', '')}", f"  - 结果B：{row['discriminative_need'].get('outcome_b', '')}", "  - AI建议：保留供作者审核"]
    (output_dir / "G3_PREP" / "g3_author_signoff_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    persisted_call_meta = []
    for meta_path in sorted((output_dir / "api_call_metadata").glob("*.json")):
        persisted_call_meta.append(json.loads(meta_path.read_text(encoding="utf-8")))
    call_meta_sorted = sorted(persisted_call_meta or call_meta, key=lambda row: row.get("call_id", ""))
    logical_calls = len(call_meta_sorted)
    repair_calls = sum(bool(row.get("is_repair")) for row in call_meta_sorted)
    provider_calls = sum(int(row.get("attempts", 0)) for row in call_meta_sorted)
    base_calls = 13
    manifest = {
        "run_id": run_id, "stage": "P7-P9", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID,
        "hypothesis_set_hash": input_hashes.get("outputs/G2-T0-1B8D6A450720F041-formal-v1/g2_final_hypotheses.jsonl"),
        "frozen_evidence_count": 85, "shared_baseline_count": 1, "frozen_hypothesis_count": 3,
        "perspective_count": 4, "expected_viewpoint_slots": 12, "initial_viewpoint_count": len(initial), "final_viewpoint_count": len(final), "formal_viewpoint_count": len(final), "dependency_edge_count": len(dependencies),
        "challenge_call_count": 3, "perspective_response_call_count": 4, "disagreement_diagnosis_call_count": 1, "monitoring_design_call_count": 1,
        "base_logical_model_calls": base_calls, "repair_model_calls": repair_calls, "logical_model_call_count": logical_calls, "provider_call_count": provider_calls,
        "all_evidence_ids_in_snapshot": all(ref["evidence_id"] in {row["evidence_id"] for row in _read_jsonl(P4_DIR / "p4_t0_frozen_evidence.jsonl")} for view in final for ref in view.get("supporting_evidence", []) + view.get("counter_evidence", [])),
        "all_hypotheses_frozen": all(row.get("frozen") is True for row in hypotheses.values()),
        "all_perspectives_cover_all_hypotheses": len(initial) == 12 and {row["perspective_id"] for row in initial} == set(PERSPECTIVES) and {row["hypothesis_id"] for row in initial} == set(HYPOTHESIS_IDS) and all(sum(row["perspective_id"] == p and row["hypothesis_id"] == h for row in initial) == 1 for p in PERSPECTIVES for h in HYPOTHESIS_IDS),
        "field_level_dependencies_complete": all(dep["viewpoint_id"] in {row["viewpoint_id"] for row in final} and dep["viewpoint_version"] == 1 for dep in dependencies),
        "viewpoint_transaction_committed": transaction_result.get("transaction_committed", False), "viewpoint_idempotency_test_passed": idempotency_passed, "rollback_test_passed": rollback_passed,
        "candidate_disagreement_count": len(disagreements), "high_impact_unresolved_disagreement_count": sum(row["decision_impact"] == "high" and row["resolvable_now"] is False for row in disagreements), "discriminative_need_count": sum(row.get("discriminativeness") == "discriminative" for row in needs), "monitor_indicator_count": len(indicators), "trigger_rule_count": len(triggers), "non_discriminative_rejected_count": sum(row.get("kind") == "non_discriminative" for row in monitoring_rejections),
        "author_decision_count": 0, "formal_g3_executed": False, "formal_g4_t0_executed": False, "t1_read": False, "historical_artifacts_unchanged": protected_before == protected_after, "api_key_written": False, "formal_evidence_inserted": 0, "hypothesis_inserted": 0, "p4_executed": False, "snapshot_frozen": False, "p5_executed": False,
        "input_identity_sha256": identity, "input_file_hashes": input_hashes, "model_id": next((row.get("requested_model_id") for row in call_meta_sorted if row.get("requested_model_id")), None), "requested_temperature": 0.0, "prompt_hashes": {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted((output_dir / "prompts").glob("*.txt"))}, "call_metadata_count": len(call_meta_sorted), "call_metadata_hashes": {str((output_dir / "api_call_metadata" / f"{row['call_id']}.json").relative_to(output_dir)).replace("\\", "/"): _sha256_file(output_dir / "api_call_metadata" / f"{row['call_id']}.json") for row in call_meta_sorted if (output_dir / "api_call_metadata" / f"{row['call_id']}.json").exists()},
        "transaction_result": transaction_result,
    }
    _write_json(output_dir / "p7_p9_manifest.json", manifest)
    checkpoint = json.loads(_checkpoint_path(output_dir).read_text(encoding="utf-8"))
    checkpoint["status"] = "completed"
    checkpoint.pop("failure", None)
    checkpoint["completed_stages"] = ["P7", "P8", "P9", "G3_PREP"]
    _save_checkpoint(output_dir, checkpoint)
    return manifest


def run_p7_p9(*, run_id: str, output_dir: Path | None = None, database_path: Path = DB_PATH, resume: bool = False, inject_failure_after: int | None = None) -> dict[str, Any]:
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    if output_dir.exists() and (output_dir / "p7_p9_manifest.json").exists() and not resume:
        return json.loads((output_dir / "p7_p9_manifest.json").read_text(encoding="utf-8")) | {"replayed": True}
    evidence, hypotheses, baseline, input_hashes, identity = _load_inputs()
    protected_before = _protected_hashes(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = _load_or_create_checkpoint(output_dir, run_id, identity)
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    call_meta: list[dict[str, Any]] = []
    client: APIModelClient | None = None

    def get_client() -> APIModelClient:
        nonlocal client
        if client is None:
            client = APIModelClient(max_retries=3, timeout_seconds=180.0)
        return client

    p7_views: dict[str, list[dict[str, Any]]] = {}
    p7_raw: dict[str, Any] = {}
    p7_call_info: dict[str, dict[str, Any]] = {}
    try:
        for perspective_id in PERSPECTIVES:
            batch_id = f"p7_{perspective_id.lower()}"
            prompt = _viewpoint_prompt(perspective_id, evidence, hypotheses, baseline)
            parsed, call_info = _call_batch(batch_id=batch_id, phase="P7", source_id=perspective_id, prompt=prompt, expected_wrapper="viewpoints", output_dir=output_dir, checkpoint=checkpoint, client=get_client())
            views, errors = _validate_p7(parsed, perspective_id, evidence_by_id)
            p7_views[perspective_id] = views
            p7_raw[perspective_id] = parsed
            p7_call_info[perspective_id] = call_info
            if not call_info.get("reused"):
                call_meta.append(call_info["call_record"])
            else:
                call_meta.append(call_info["call_record"])
            raw_relative = checkpoint["completed_batches"][batch_id]["raw_response_path"]
            raw_target = output_dir / "P7" / f"perspective_{perspective_id.lower()}_raw.json"
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_dir / raw_relative, raw_target)
        initial = [p7_views[perspective][index] for perspective in PERSPECTIVES for index in range(3)]
        _write_jsonl(output_dir / "P7" / "initial_viewpoints.jsonl", initial)
        _write_csv(output_dir / "P7" / "viewpoint_validation.csv", ["viewpoint_id", "perspective_id", "hypothesis_id", "initial_validation_passed", "initial_validation_errors", "final_status", "final_validation_errors", "supporting_evidence_count", "supporting_source_count"], [{**row, "final_status": "pending", "final_validation_errors": ""} for row in _quality_csv_rows(initial, initial)])

        challenges_by_hyp: dict[str, list[dict[str, Any]]] = {}
        challenge_rejections: list[dict[str, Any]] = []
        for hypothesis_id in HYPOTHESIS_IDS:
            viewpoints = [p7_views[p][HYPOTHESIS_IDS.index(hypothesis_id)] for p in PERSPECTIVES]
            prompt = _challenge_prompt(hypotheses[hypothesis_id], viewpoints, evidence_by_id, baseline)
            batch_id = f"p8_challenges_{hypothesis_id.replace('HYP-T0-', 'h').lower()}"
            parsed, call_info = _call_batch(batch_id=batch_id, phase="P8_CHALLENGE", source_id=hypothesis_id, prompt=prompt, expected_wrapper="challenges", output_dir=output_dir, checkpoint=checkpoint, client=get_client())
            valid_ids = {row["viewpoint_id"] for row in viewpoints}
            valid, rejected = _validate_challenges(parsed, valid_ids, evidence_by_id, hypothesis_id)
            for row in rejected:
                row["hypothesis_id"] = hypothesis_id
            challenges_by_hyp[hypothesis_id] = valid
            challenge_rejections.extend(rejected)
            call_meta.append(call_info["call_record"])
            _write_json(output_dir / "P8" / f"hypothesis_{hypothesis_id.replace('HYP-T0-', 'h').lower()}_challenges.json", {"challenges": valid, "rejected": rejected})

        final_views_by_perspective: dict[str, list[dict[str, Any]]] = {}
        for perspective_id in PERSPECTIVES:
            own_views = p7_views[perspective_id]
            own_ids = {row["viewpoint_id"] for row in own_views}
            own_challenges = [challenge for rows in challenges_by_hyp.values() for challenge in rows if challenge["target_viewpoint_id"] in own_ids]
            prompt = _response_prompt(perspective_id, own_views, own_challenges, baseline)
            batch_id = f"p8_response_{perspective_id.lower()}"
            parsed, call_info = _call_batch(batch_id=batch_id, phase="P8_RESPONSE", source_id=perspective_id, prompt=prompt, expected_wrapper="responses", output_dir=output_dir, checkpoint=checkpoint, client=get_client())
            final_views_by_perspective[perspective_id], _ = _validate_response(parsed, perspective_id, own_views, own_challenges, evidence_by_id)
            call_meta.append(call_info["call_record"])
            _write_json(output_dir / "P8" / "perspective_response" / f"{perspective_id.lower()}.json", parsed)
        final_candidates = [final_views_by_perspective[p][index] for p in PERSPECTIVES for index in range(3)]
        final_views = [_finalize_viewpoint(view, run_id, p7_call_info[view["perspective_id"]], next((meta for meta in call_meta if meta.get("batch_id") == f"p8_response_{view['perspective_id'].lower()}"), {})) for view in final_candidates]
        dependencies = _make_dependencies(final_views, run_id)

        valid_view_ids = {row["viewpoint_id"] for row in final_views}
        disagreement_prompt = _disagreement_prompt(final_views, evidence_by_id, baseline)
        parsed_disagreements, call_info = _call_batch(batch_id="p9_disagreement_diagnosis", phase="P9", source_id=None, prompt=disagreement_prompt, expected_wrapper="disagreements", output_dir=output_dir, checkpoint=checkpoint, client=get_client())
        disagreements, disagreement_rejections = _validate_disagreements(parsed_disagreements, valid_view_ids, evidence_by_id)
        call_meta.append(call_info["call_record"])
        monitoring_prompt = _monitor_prompt(disagreements, final_views, baseline)
        parsed_monitoring, call_info = _call_batch(batch_id="p9_monitoring_design", phase="P9_MONITORING", source_id=None, prompt=monitoring_prompt, expected_wrapper="discriminative_needs", output_dir=output_dir, checkpoint=checkpoint, client=get_client())
        needs, indicators, triggers, monitoring_rejections = _validate_monitoring(parsed_monitoring, disagreements, valid_view_ids)
        call_meta.append(call_info["call_record"])

        rollback_passed = _rollback_probe(database_path, final_views, dependencies) if inject_failure_after is None else False
        transaction_result = _insert_transaction(database_path, final_views, dependencies, inject_failure_after=inject_failure_after)
        idempotency_result = _insert_transaction(database_path, final_views, dependencies) if inject_failure_after is None else {"replayed": False}
        idempotency_passed = bool(idempotency_result.get("replayed"))
        protected_after = _protected_hashes(output_dir)
        # Replace the temporary raw placeholders in _write_outputs with the actual raw files already written above.
        manifest = _write_outputs(output_dir, run_id, input_hashes, identity, hypotheses, baseline, initial, final_views, dependencies, challenges_by_hyp, challenge_rejections, disagreements, disagreement_rejections, needs, indicators, triggers, monitoring_rejections, call_meta, protected_before, protected_after, transaction_result, rollback_passed, idempotency_passed)
        return manifest
    except ModelAPIError as exc:
        _safe_failure_manifest(output_dir, run_id, "model_api", None, exc, checkpoint)
        raise
    except Exception:
        _safe_failure_manifest(output_dir, run_id, "pipeline_consistency", None, None, checkpoint)
        raise
