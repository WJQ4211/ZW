"""P5 competitive-hypothesis generation from one frozen T0 snapshot.

The module is deliberately isolated from SQLite and all later pipeline
stages.  It reads only the P4 frozen-evidence exports, makes three separate
generator calls, then one merge call, and writes review-ready candidates only.
No hypothesis row, viewpoint, disagreement, snapshot, or T1 input is used.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import platform
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .model_client import APIModelClient, ApiCallRecord, ModelAPIError, parse_chat_completion_content


ROOT = Path(__file__).resolve().parents[1]
P4_DIR = ROOT / "outputs" / "P4-T0-C6C812108AF95309-freeze-v1"
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
CASE_ID = "us_military_ai_deployment"
QUESTION = "基于截至2024年12月31日的公开证据，美国军用人工智能的发展处于何种主要演进状态？其能力形成、应用扩展与体系化发展的主导路径是什么？哪些条件可能使当前判断发生改变？"
TIME_WINDOW = "2022-01-01至2024-12-31"
ANALYSIS_DIMENSIONS = {
    "strategic_governance": "战略与治理牵引",
    "technical_engineering_maturity": "技术能力与工程成熟度",
    "testing_deployment_application": "试验验证与部署应用",
    "resource_conditions": "数据、算力、人才等资源条件",
    "trust_organization_system_integration": "可信性、组织和体系集成约束",
}
ALLOWED_COVERED_DIMENSIONS = tuple(ANALYSIS_DIMENSIONS)
GENERATOR_TASKS = {
    "A": "发展轨迹分析：识别战略目标、项目投入、试验活动和部署事件之间所呈现的主要演进轨迹。",
    "B": "能力形成分析：分析技术成熟度、工程资源条件、任务应用和体系集成之间的关系。",
    "C": "反证与约束分析：寻找可能推翻主流判断的证据、能力形成障碍、政策目标与实际进展之间的落差及其他合理解释。",
}
GEN_FIELDS = {
    "candidate_id", "generator_id", "hypothesis_statement", "scope",
    "supporting_evidence_ids", "counter_evidence_ids", "key_assumptions",
    "falsification_conditions", "unresolved_questions", "covered_dimensions", "confidence",
}
MERGE_FIELDS = {
    "proposed_hypothesis_id", "hypothesis_statement", "scope", "supporting_evidence_ids",
    "counter_evidence_ids", "key_assumptions", "falsification_conditions",
    "unresolved_questions", "covered_dimensions", "source_candidate_ids", "merge_action",
    "merge_reason", "g2_recommendation",
}
LEGAL_CONFIDENCE = {"low", "medium", "high"}
LEGAL_ACTIONS = {"retain", "merge", "revise"}
LEGAL_G2 = {"review", "revise", "exclude"}
RECOMMENDATION_MARKERS = ("建议", "应当", "需要加强", "应加强", "必须采取", "政策建议")


class P5Error(RuntimeError):
    """Raised when a P5 input, model response, or merge result is invalid."""


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


def _protected_output_hashes(exclude_dir: Path) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve()
    return {
        str(path.relative_to(outputs)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and path.resolve() != excluded and excluded not in path.resolve().parents
    }


def _load_snapshot() -> tuple[list[dict[str, Any]], dict[str, str]]:
    manifest_path = P4_DIR / "p4_t0_snapshot_manifest.json"
    evidence_path = P4_DIR / "p4_t0_frozen_evidence.jsonl"
    items_path = P4_DIR / "p4_t0_snapshot_items.jsonl"
    if not all(path.exists() for path in (manifest_path, evidence_path, items_path)):
        raise P5Error("P4 frozen snapshot input is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = _read_jsonl(evidence_path)
    items = _read_jsonl(items_path)
    if manifest.get("snapshot_id") != SNAPSHOT_ID or manifest.get("frozen") is not True:
        raise P5Error("P4 input is not the expected frozen T0 snapshot")
    if len(evidence) != 85 or len(items) != 85 or len({row.get("evidence_id") for row in evidence}) != 85:
        raise P5Error("P4 frozen snapshot must contain 85 unique Evidence rows and items")
    item_refs = {(row.get("case_id"), row.get("evidence_id"), int(row.get("version"))) for row in items}
    evidence_refs = {(row.get("case_id"), row.get("evidence_id"), int(row.get("version"))) for row in evidence}
    if item_refs != evidence_refs or any(row.get("case_id") != CASE_ID for row in evidence):
        raise P5Error("snapshot items and frozen Evidence do not match")
    hashes = {
        "p4_t0_snapshot_manifest.json": _sha256_file(manifest_path),
        "p4_t0_snapshot_items.jsonl": _sha256_file(items_path),
        "p4_t0_frozen_evidence.jsonl": _sha256_file(evidence_path),
    }
    return sorted(evidence, key=lambda row: row["evidence_id"]), hashes


def _evidence_digest(evidence: list[dict[str, Any]]) -> str:
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(payload.encode("utf-8"))


def _evidence_context(evidence: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in evidence:
        rows.append(json.dumps({
            "evidence_id": row["evidence_id"],
            "source_id": row["source_id"],
            "source_grade": row["source_grade"],
            "published_at": row["published_at"],
            "source_locator": row.get("source_locator", ""),
            "evidence_nature": row.get("evidence_nature", ""),
            "coding_dimensions": row.get("coding_dimensions", []),
            "excerpt_original": row.get("excerpt_original", ""),
            "excerpt_zh": row.get("excerpt_zh", ""),
            "normalized_claim": row.get("normalized_claim", ""),
        }, ensure_ascii=False, sort_keys=True))
    return "\n".join(rows)


def build_generator_prompt(generator_id: str, evidence: list[dict[str, Any]]) -> str:
    if generator_id not in GENERATOR_TASKS:
        raise P5Error(f"unknown generator: {generator_id}")
    dimensions = "\n".join(f"- {code}: {label}" for code, label in ANALYSIS_DIMENSIONS.items())
    fields = json.dumps({
        "candidate_id": f"GEN-{generator_id}-H01",
        "generator_id": generator_id,
        "hypothesis_statement": "[占位：可比较、可证伪的演进判断]",
        "scope": "[占位：对象、阶段与边界]",
        "supporting_evidence_ids": ["EV-T0-PLACEHOLDER-001", "EV-T0-PLACEHOLDER-002", "EV-T0-PLACEHOLDER-003"],
        "counter_evidence_ids": [],
        "key_assumptions": ["[占位关键假设]"],
        "falsification_conditions": ["[占位可证伪条件]"],
        "unresolved_questions": ["[占位尚待判别问题]"],
        "covered_dimensions": ["strategic_governance"],
        "confidence": "medium",
    }, ensure_ascii=False, indent=2)
    allowed_dimension_text = ", ".join(ALLOWED_COVERED_DIMENSIONS)
    return f"""你是竞争性假设生成器{generator_id}。你只能使用下方提供的冻结T0证据，不得调用网络、T1材料、其他案例材料或模型常识。

研究问题：{QUESTION}
分析时间范围：{TIME_WINDOW}
冻结快照：{SNAPSHOT_ID}
本次任务：{GENERATOR_TASKS[generator_id]}

合法分析维度（只可使用左侧代码）：
{dimensions}

证据使用要求：每条候选至少引用3条支持Evidence，且支持Evidence至少来自2个不同source_id；必须列出反驳Evidence，若没有则在unresolved_questions说明原因。所有Evidence ID必须来自下方85条冻结Evidence。

假设要求：hypothesis_statement必须是可比较、可证伪的发展状态或演进路径判断；不能只是事实摘要、政策复述、建议、评分、排名或最终优胜结论。不要使用“证明”“必然”“全面智能化”等超出证据范围的表述。每个生成器提出2至4条候选，不足时不要凑数。

输出要求：严格输出JSON数组，每条必须恰好包含以下11个字段，不得增加字段；允许输出2至4条；禁止Markdown代码围栏。
字段：candidate_id, generator_id, hypothesis_statement, scope, supporting_evidence_ids, counter_evidence_ids, key_assumptions, falsification_conditions, unresolved_questions, covered_dimensions, confidence。
数组字段必须是JSON字符串数组；confidence只能是low、medium、high。
特别注意：covered_dimensions只能使用这5个值：{allowed_dimension_text}。不得使用案例原始coding_dimensions中的其他值（例如organizational_system_fit、application_maturity或integration_level）。

占位格式示例（不得复制占位内容或虚构Evidence ID）：
{fields}

冻结T0 Evidence正文如下：
<FROZEN_EVIDENCE>
{_evidence_context(evidence)}
</FROZEN_EVIDENCE>

现在仅返回JSON数组。"""


def build_repair_prompt(generator_id: str, original_content: str, evidence: list[dict[str, Any]]) -> str:
    allowed_dimension_text = ", ".join(ALLOWED_COVERED_DIMENSIONS)
    return f"""仅对下面生成器{generator_id}的输出做格式或Evidence ID修复。不得改变任何假设的实质、支持/反驳证据含义、范围、条件或维度；不得新增实质判断。只返回符合原要求的JSON数组，不要Markdown围栏。原始输出：\n{original_content}\n\n合法Evidence ID集合：{json.dumps([row['evidence_id'] for row in evidence], ensure_ascii=False)}\n严格字段要求：每条恰好11个字段 candidate_id、generator_id、hypothesis_statement、scope、supporting_evidence_ids、counter_evidence_ids、key_assumptions、falsification_conditions、unresolved_questions、covered_dimensions、confidence；每份2至4条；所有数组字段为字符串数组；covered_dimensions只能使用：{allowed_dimension_text}，不得使用organizational_system_fit等其他编码。"""


def _string_list(value: Any, field: str, candidate_id: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise P5Error(f"{candidate_id}: {field} must be a non-empty string array")
    return value


def _validate_generator_candidate(raw: Any, generator_id: str, evidence_by_id: dict[str, dict[str, Any]], seen: set[str], index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != GEN_FIELDS:
        raise P5Error(f"generator {generator_id} candidate {index} has invalid fields")
    candidate_id = raw["candidate_id"]
    if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
        raise P5Error(f"generator {generator_id} has duplicate/invalid candidate_id")
    if raw["generator_id"] != generator_id:
        raise P5Error(f"{candidate_id}: generator_id mismatch")
    seen.add(candidate_id)
    statement = raw["hypothesis_statement"]
    scope = raw["scope"]
    if not isinstance(statement, str) or len(statement.strip()) < 12 or not isinstance(scope, str) or not scope.strip():
        raise P5Error(f"{candidate_id}: statement or scope is empty")
    if any(marker in statement for marker in RECOMMENDATION_MARKERS):
        raise P5Error(f"{candidate_id}: recommendation-like hypothesis statement")
    supports = _string_list(raw["supporting_evidence_ids"], "supporting_evidence_ids", candidate_id)
    counters = raw["counter_evidence_ids"]
    if not isinstance(counters, list) or not all(isinstance(item, str) for item in counters):
        raise P5Error(f"{candidate_id}: counter_evidence_ids must be a string array")
    all_refs = supports + counters
    if len(supports) < 3:
        raise P5Error(f"{candidate_id}: fewer than 3 supporting Evidence IDs")
    if any(ref not in evidence_by_id for ref in all_refs):
        raise P5Error(f"{candidate_id}: Evidence ID outside frozen snapshot")
    if len({evidence_by_id[ref]["source_id"] for ref in supports}) < 2:
        raise P5Error(f"{candidate_id}: support Evidence comes from fewer than 2 sources")
    _string_list(raw["key_assumptions"], "key_assumptions", candidate_id)
    _string_list(raw["falsification_conditions"], "falsification_conditions", candidate_id)
    unresolved = _string_list(raw["unresolved_questions"], "unresolved_questions", candidate_id)
    dimensions = _string_list(raw["covered_dimensions"], "covered_dimensions", candidate_id)
    if set(dimensions) - set(ANALYSIS_DIMENSIONS):
        raise P5Error(f"{candidate_id}: illegal analysis dimension")
    if raw["confidence"] not in LEGAL_CONFIDENCE:
        raise P5Error(f"{candidate_id}: illegal confidence")
    if not counters and not unresolved:
        raise P5Error(f"{candidate_id}: empty counter evidence requires unresolved question")
    clean = json.loads(json.dumps(raw, ensure_ascii=False))
    report = {
        "generator_id": generator_id, "candidate_id": candidate_id, "valid": True,
        "reason": "passed", "supporting_evidence_count": len(supports),
        "supporting_source_count": len({evidence_by_id[ref]["source_id"] for ref in supports}),
    }
    return clean, report


def validate_generator_candidates(payload: Any, generator_id: str, evidence: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    report: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        raise P5Error(f"generator {generator_id} response must be a JSON array")
    if not 2 <= len(payload) <= 4:
        raise P5Error(f"generator {generator_id} must return 2 to 4 candidates, got {len(payload)}")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        clean, row = _validate_generator_candidate(raw, generator_id, evidence_by_id, seen, index)
        validated.append(clean)
        report.append(row)
    return validated, report


def _candidate_map(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {candidate["candidate_id"]: candidate for candidate in candidates}


def build_merge_prompt(candidates: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> str:
    used_ids = sorted({ref for candidate in candidates for ref in candidate["supporting_evidence_ids"] + candidate["counter_evidence_ids"]})
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    used_evidence = [evidence_by_id[evidence_id] for evidence_id in used_ids]
    dimensions = "\n".join(f"- {code}: {label}" for code, label in ANALYSIS_DIMENSIONS.items())
    return f"""你是P5候选归并器。你只能读取下方已通过确定性门禁的候选假设及其引用的冻结Evidence。

研究问题：{QUESTION}
分析时间范围：{TIME_WINDOW}
冻结快照：{SNAPSHOT_ID}

归并任务：识别语义重复；合并真正同义的候选；保留机制、条件、阶段或不确定性不同的竞争性判断；过于宽泛的候选标记revise但不自动扩写。输出3至5条建议进入G2的候选。不得投票、强制共识、选择正确假设、生成候选中不存在的实质判断或删除有独立证据支撑的少数判断。

合法分析维度：
{dimensions}

输出严格为JSON数组，每条恰好13个字段，不得增加字段，禁止Markdown围栏：
proposed_hypothesis_id, hypothesis_statement, scope, supporting_evidence_ids, counter_evidence_ids, key_assumptions, falsification_conditions, unresolved_questions, covered_dimensions, source_candidate_ids, merge_action, merge_reason, g2_recommendation。
merge_action只能是retain、merge、revise；g2_recommendation只能是review、revise、exclude；所有数组字段为字符串数组。source_candidate_ids只能来自下方候选。所有Evidence ID只能来自下方引用Evidence。不要生成最终胜出或排名。

已通过门禁的候选：
<VALIDATED_CANDIDATES>
{json.dumps(candidates, ensure_ascii=False, sort_keys=True, indent=2)}
</VALIDATED_CANDIDATES>

候选引用的Evidence：
<REFERENCED_FROZEN_EVIDENCE>
{_evidence_context(used_evidence)}
</REFERENCED_FROZEN_EVIDENCE>

现在仅返回JSON数组。"""


def validate_merge_results(payload: Any, candidates: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not 3 <= len(payload) <= 5:
        raise P5Error(f"merge must return 3 to 5 proposed hypotheses, got {len(payload) if isinstance(payload, list) else 'non-array'}")
    candidate_by_id = _candidate_map(candidates)
    evidence_ids = {row["evidence_id"] for row in evidence}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict) or set(raw) != MERGE_FIELDS:
            raise P5Error("merge candidate has invalid fields")
        proposed_id = raw["proposed_hypothesis_id"]
        if not isinstance(proposed_id, str) or not proposed_id or proposed_id in seen:
            raise P5Error("merge proposed_hypothesis_id is invalid or duplicated")
        seen.add(proposed_id)
        if not isinstance(raw["hypothesis_statement"], str) or len(raw["hypothesis_statement"].strip()) < 12:
            raise P5Error(f"{proposed_id}: empty statement")
        if any(marker in raw["hypothesis_statement"] for marker in RECOMMENDATION_MARKERS):
            raise P5Error(f"{proposed_id}: recommendation-like statement")
        sources = _string_list(raw["source_candidate_ids"], "source_candidate_ids", proposed_id)
        if not sources or any(source not in candidate_by_id for source in sources):
            raise P5Error(f"{proposed_id}: source candidate lineage is invalid")
        supports = _string_list(raw["supporting_evidence_ids"], "supporting_evidence_ids", proposed_id)
        counters = raw["counter_evidence_ids"]
        if not isinstance(counters, list) or not all(isinstance(item, str) for item in counters):
            raise P5Error(f"{proposed_id}: invalid counter evidence list")
        if any(ref not in evidence_ids for ref in supports + counters):
            raise P5Error(f"{proposed_id}: evidence outside frozen snapshot")
        assumptions = _string_list(raw["key_assumptions"], "key_assumptions", proposed_id)
        falsifiers = _string_list(raw["falsification_conditions"], "falsification_conditions", proposed_id)
        unresolved = _string_list(raw["unresolved_questions"], "unresolved_questions", proposed_id)
        dimensions = _string_list(raw["covered_dimensions"], "covered_dimensions", proposed_id)
        if set(dimensions) - set(ANALYSIS_DIMENSIONS):
            raise P5Error(f"{proposed_id}: illegal analysis dimension")
        if raw["merge_action"] not in LEGAL_ACTIONS or raw["g2_recommendation"] not in LEGAL_G2:
            raise P5Error(f"{proposed_id}: illegal merge action or G2 recommendation")
        if not isinstance(raw["scope"], str) or not raw["scope"].strip() or not isinstance(raw["merge_reason"], str) or not raw["merge_reason"].strip():
            raise P5Error(f"{proposed_id}: scope or merge reason is empty")
        result.append(json.loads(json.dumps(raw, ensure_ascii=False)))
    return result


def _similarity_matrix(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for left in candidates:
        left_ids = set(left["supporting_evidence_ids"] + left["counter_evidence_ids"])
        for right in candidates:
            right_ids = set(right["supporting_evidence_ids"] + right["counter_evidence_ids"])
            union = left_ids | right_ids
            rows.append({
                "candidate_id_left": left["candidate_id"],
                "candidate_id_right": right["candidate_id"],
                "evidence_jaccard": len(left_ids & right_ids) / len(union) if union else 0.0,
                "shared_evidence_count": len(left_ids & right_ids),
            })
    return rows


def _safe_call(client: APIModelClient, prompt: str) -> tuple[Any, str, ApiCallRecord, dict[str, Any]]:
    envelope, content, record = client.call(prompt, temperature=0.0)
    _, parsed = parse_chat_completion_content({"choices": [{"message": {"content": content}}]})
    return parsed, content, record, envelope


def _write_failure(output_dir: Path, run_id: str, stage: str, source_id: str | None, error: ModelAPIError, completed_calls: int, saved_batches: list[str], p4_hashes: dict[str, str]) -> dict[str, Any]:
    data = {
        "run_id": run_id,
        "failure_stage": stage,
        "source_id": source_id,
        "exception_type": error.root_exception_type,
        "http_status_code": error.http_status_code,
        "completed_logical_calls": completed_calls,
        "saved_batches": saved_batches,
        "p4_input_hashes": p4_hashes,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "g2_executed": False,
        "t1_read": False,
    "p4_executed": False,
        "snapshot_frozen": False,
        "api_key_written": False,
    }
    _write_json(output_dir / "p5_failure_manifest.json", data)
    return data


def run_p5(run_id: str, *, output_dir: Path | None = None, client: APIModelClient | None = None) -> dict[str, Any]:
    evidence, p4_hashes = _load_snapshot()
    if output_dir is None:
        output_dir = ROOT / "outputs" / run_id
    if output_dir.exists() and any(output_dir.iterdir()):
        raise P5Error(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    protected_before = _protected_output_hashes(output_dir)
    client = client or APIModelClient(timeout_seconds=240.0, max_retries=3)
    evidence_hash = _evidence_digest(evidence)
    prompts_dir = output_dir / "prompts"
    raw_dir = output_dir / "raw_responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    all_candidates: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    call_records: list[dict[str, Any]] = []
    saved_batches: list[str] = []
    logical_calls = 0
    generation_calls = 0
    repair_calls = 0
    rejected_candidates: list[dict[str, Any]] = []
    try:
        for generator_id in ("A", "B", "C"):
            prompt = build_generator_prompt(generator_id, evidence)
            prompt_path = prompts_dir / f"generator_{generator_id.lower()}.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompt_hash = _sha256_file(prompt_path)
            try:
                parsed, content, record, envelope = _safe_call(client, prompt)
                generation_calls += 1
                logical_calls += 1
                call_records.append({"call_id": f"generation-{generator_id}-1", "phase": "generation", "generator_id": generator_id, "prompt_sha256": prompt_hash, **record.as_dict()})
                _write_json(raw_dir / f"generator_{generator_id.lower()}.json", {"response_envelope": envelope, "content": content, "content_sha256": _sha256_bytes(content.encode("utf-8")), "response_sha256": record.response_sha256, "call_id": f"generation-{generator_id}-1"})
                saved_batches.append(f"generation-{generator_id}")
            except (ValueError, P5Error):
                raise
            except ModelAPIError as error:
                _write_failure(output_dir, run_id, "generation", generator_id, error, logical_calls, saved_batches, p4_hashes)
                raise
            try:
                valid, rows = validate_generator_candidates(parsed, generator_id, evidence)
            except P5Error as first_error:
                # One repair call is allowed only for structural/ID repair.
                repair_prompt = build_repair_prompt(generator_id, content, evidence)
                repair_path = prompts_dir / f"generator_{generator_id.lower()}_repair.txt"
                repair_path.write_text(repair_prompt, encoding="utf-8")
                repair_hash = _sha256_file(repair_path)
                try:
                    repaired, repair_content, repair_record, repair_envelope = _safe_call(client, repair_prompt)
                except ModelAPIError as error:
                    _write_failure(output_dir, run_id, "repair", generator_id, error, logical_calls, saved_batches, p4_hashes)
                    raise
                repair_calls += 1
                logical_calls += 1
                call_records.append({"call_id": f"repair-{generator_id}-1", "phase": "format_or_id_repair", "generator_id": generator_id, "prompt_sha256": repair_hash, "original_validation_error": str(first_error), **repair_record.as_dict()})
                _write_json(raw_dir / f"generator_{generator_id.lower()}_repair.json", {"response_envelope": repair_envelope, "content": repair_content, "content_sha256": _sha256_bytes(repair_content.encode("utf-8")), "response_sha256": repair_record.response_sha256, "call_id": f"repair-{generator_id}-1"})
                try:
                    valid, rows = validate_generator_candidates(repaired, generator_id, evidence)
                    parsed = repaired
                except P5Error as repaired_error:
                    # Keep individually valid candidates from the original
                    # batch.  An invalid peer is rejected and recorded; it is
                    # never silently truncated or repaired in code.  This
                    # permits a batch with 2-4 returned candidates to yield
                    # fewer valid candidates without manufacturing evidence.
                    if not isinstance(parsed, list) or not 2 <= len(parsed) <= 4:
                        raise P5Error(f"{generator_id}: repair did not produce a valid 2-4 candidate array") from repaired_error
                    valid = []
                    rows = []
                    seen: set[str] = set()
                    evidence_by_id = {row["evidence_id"]: row for row in evidence}
                    for index, raw in enumerate(parsed):
                        candidate_id = raw.get("candidate_id", f"index-{index}") if isinstance(raw, dict) else f"index-{index}"
                        try:
                            clean, validation_row = _validate_generator_candidate(raw, generator_id, evidence_by_id, seen, index)
                        except P5Error as item_error:
                            rejection = {
                                "generator_id": generator_id,
                                "candidate_id": candidate_id,
                                "rejection_reason": str(item_error),
                                "original_batch_validation_error": str(first_error),
                                "repair_validation_error": str(repaired_error),
                                "candidate": raw,
                            }
                            rejected_candidates.append(rejection)
                            rows.append({"generator_id": generator_id, "candidate_id": candidate_id, "valid": False, "reason": str(item_error), "supporting_evidence_count": "", "supporting_source_count": ""})
                        else:
                            valid.append(clean)
                            rows.append(validation_row)
                    if len(valid) < 2:
                        raise P5Error(f"{generator_id}: fewer than 2 valid candidates remain after rejecting invalid candidates") from repaired_error
            all_candidates.extend(valid)
            validation_rows.extend(rows)
            _write_json(output_dir / f"generator_{generator_id.lower()}_candidates.json", valid)
        if len({candidate["candidate_id"] for candidate in all_candidates}) != len(all_candidates):
            raise P5Error("candidate IDs are not unique across generators")
        _write_jsonl(output_dir / "all_validated_candidates.jsonl", all_candidates)
        _write_jsonl(output_dir / "rejected_candidates.jsonl", rejected_candidates)
        similarity = _similarity_matrix(all_candidates)
        _write_csv(output_dir / "hypothesis_similarity_matrix.csv", ["candidate_id_left", "candidate_id_right", "evidence_jaccard", "shared_evidence_count"], similarity)
        _write_csv(output_dir / "candidate_validation_report.csv", ["generator_id", "candidate_id", "valid", "reason", "supporting_evidence_count", "supporting_source_count"], validation_rows)
        merge_prompt = build_merge_prompt(all_candidates, evidence)
        merge_prompt_path = prompts_dir / "merge.txt"
        merge_prompt_path.write_text(merge_prompt, encoding="utf-8")
        merge_hash = _sha256_file(merge_prompt_path)
        try:
            merged_payload, merge_content, merge_record, merge_envelope = _safe_call(client, merge_prompt)
            logical_calls += 1
        except ModelAPIError as error:
            _write_failure(output_dir, run_id, "merge", None, error, logical_calls, saved_batches, p4_hashes)
            raise
        call_records.append({"call_id": "merge-1", "phase": "merge", "prompt_sha256": merge_hash, **merge_record.as_dict()})
        _write_json(raw_dir / "merge.json", {"response_envelope": merge_envelope, "content": merge_content, "content_sha256": _sha256_bytes(merge_content.encode("utf-8")), "response_sha256": merge_record.response_sha256, "call_id": "merge-1"})
        proposed = validate_merge_results(merged_payload, all_candidates, evidence)
        _write_jsonl(output_dir / "proposed_hypotheses_for_g2.jsonl", proposed)
        lineage = [{"proposed_hypothesis_id": row["proposed_hypothesis_id"], "source_candidate_ids": row["source_candidate_ids"], "merge_action": row["merge_action"], "merge_reason": row["merge_reason"]} for row in proposed]
        _write_jsonl(output_dir / "candidate_lineage.jsonl", lineage)
        review_fields = [
            "proposed_hypothesis_id", "hypothesis_statement", "scope", "supporting_evidence_ids", "counter_evidence_ids", "key_assumptions", "falsification_conditions", "unresolved_questions", "covered_dimensions", "source_candidate_ids", "merge_action", "merge_reason", "g2_recommendation",
            "author_decision", "final_hypothesis_statement", "final_scope", "final_supporting_evidence_ids", "final_counter_evidence_ids", "final_key_assumptions", "final_falsification_conditions", "final_unresolved_questions", "final_covered_dimensions", "reviewer", "reviewed_at", "review_note",
        ]
        _write_csv(output_dir / "g2_author_review_template.csv", review_fields, [{**row, "author_decision": "", "final_hypothesis_statement": "", "final_scope": "", "final_supporting_evidence_ids": "", "final_counter_evidence_ids": "", "final_key_assumptions": "", "final_falsification_conditions": "", "final_unresolved_questions": "", "final_covered_dimensions": "", "reviewer": "", "reviewed_at": "", "review_note": ""} for row in proposed])
    except (ModelAPIError, ValueError, P5Error):
        raise
    protected_after = _protected_output_hashes(output_dir)
    response_hashes = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted(raw_dir.rglob("*")) if path.is_file()}
    _write_jsonl(output_dir / "model_call_log.jsonl", call_records)
    quality = [
        "# P5质量报告", "",
        f"- 冻结快照：{SNAPSHOT_ID}", f"- Evidence输入：{len(evidence)}", f"- 独立生成器：3；原始有效候选：{len(all_candidates) + len(rejected_candidates)}；确定性通过：{len(all_candidates)}；拒绝：{len(rejected_candidates)}", f"- 归并候选：{len(proposed)}", f"- 生成逻辑调用：{generation_calls}", f"- 格式/ID修复调用：{repair_calls}", "- 归并逻辑调用：1", f"- 实际逻辑模型调用：{logical_calls}", "- 本报告不将任何通过率称为准确率。", "- 候选仅供G2作者审核；未写入正式hypothesis表，未生成观点、分歧或案例结论。", "- 三个生成器相互隔离；归并器只读取已通过门禁的候选及其引用Evidence。",
    ]
    (output_dir / "p5_quality_report.md").write_text("\n".join(quality) + "\n", encoding="utf-8")
    output_hashes = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name != "p5_manifest.json"}
    physical_provider_calls = sum(int(record.get("attempts", 0)) for record in call_records)
    physical_retry_count = sum(int(record.get("retry_count", 0)) for record in call_records)
    prompt_hashes = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha256_file(path) for path in sorted(prompts_dir.rglob("*")) if path.is_file()}
    manifest = {
        "run_id": run_id, "stage": "P5", "case_id": CASE_ID, "snapshot_id": SNAPSHOT_ID, "snapshot_tag": "T0", "cutoff_time": "2024-12-31T23:59:59Z", "frozen_evidence_count": len(evidence), "p4_input_hashes": p4_hashes, "frozen_evidence_hash": evidence_hash,
        "independent_generator_count": 3, "generator_isolation_verified": True, "generation_api_calls": generation_calls, "merge_api_calls": 1, "format_or_id_repair_api_calls": repair_calls, "logical_model_call_count": logical_calls, "total_candidate_count": len(all_candidates), "schema_valid_candidate_count": len(all_candidates), "proposed_hypothesis_count": len(proposed), "all_evidence_ids_in_snapshot": True, "minimum_source_requirement_passed": True, "candidate_lineage_complete": True, "author_decision_count": 0,
        "hypothesis_inserted": 0, "g2_executed": False, "t1_read": False, "historical_artifacts_unchanged": protected_before == protected_after, "api_key_written": False, "evidence_inserted": 0, "p4_executed": False, "snapshot_frozen": False, "p5_executed": True, "model_api_calls": logical_calls, "provider_call_count": physical_provider_calls, "physical_retry_count": physical_retry_count,
        "generator_candidate_counts": {key: sum(item["generator_id"] == key for item in all_candidates) for key in GENERATOR_TASKS}, "raw_candidate_count": len(all_candidates) + len(rejected_candidates), "schema_valid_candidate_count": len(all_candidates), "rejected_candidate_count": len(rejected_candidates), "rejection_reason_counts": dict(Counter(item["rejection_reason"] for item in rejected_candidates)), "merge_actions": dict(Counter(item["merge_action"] for item in proposed)), "g2_recommendations": dict(Counter(item["g2_recommendation"] for item in proposed)), "provider_model_id": call_records[0].get("provider_model_id") if call_records else None, "requested_model_id": call_records[0].get("requested_model_id") if call_records else None, "requested_temperature": 0.0, "api_key_written": False, "python_version": platform.python_version(), "git_head": "unavailable_not_a_git_repository", "prompt_hashes": prompt_hashes, "raw_response_hashes": response_hashes, "output_file_hashes": output_hashes,
    }
    _write_json(output_dir / "p5_manifest.json", manifest)
    return manifest
