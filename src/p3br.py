"""P3b-R recovery and G1-T0 preannotation preparation.

This module consumes only the immutable P3b candidate/rejection artifacts and
the local P2 source text needed to verify locators.  It does not re-extract
the nine sources, does not write formal Evidence, and does not read T1.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .model_client import APIModelClient
from .p3ar import (
    CASE_ID,
    CODING_DIMENSION_DEFINITIONS,
    ROOT,
    SourceSegment,
    inject_metadata,
    normalize_for_match,
    relative_path,
    resolve_locator,
    sha256_bytes,
    sha256_file,
    source_excerpt_hash,
    stable_evidence_id,
    write_csv,
    write_json,
    write_jsonl,
)
from .p3b import (
    _candidate_fields,
    _decode_json,
    _deterministic_reasons,
    _semantic_rows,
    build_semantic_prompt,
    load_include_documents,
)
from .schemas import EvidenceCandidate, model_validate


P3B_RUN_ID = "P3B-20260826T040000Z-full-t0-v3"
P3B_DIR = ROOT / "outputs" / P3B_RUN_ID
P3B_SOURCE = P3B_DIR / "evidence_candidates_p3b.jsonl"
P3B_REJECTED = P3B_DIR / "rejected_candidates_p3b.jsonl"
SEMANTIC_TEMPLATE = ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml"
ALLOWED_DIMENSIONS = set(CODING_DIMENSION_DEFINITIONS)
ALLOWED_NATURES = {
    "goal",
    "resource_input",
    "test_event",
    "test_result",
    "deployment_event",
    "governance_event",
    "constraint",
    "evaluation",
}
SCHEMA_REPAIRABLE_ERROR = "schema_invalid"
VISUAL_ERROR = "excerpt_not_found_at_specified_locator"

# These are stable identifiers/locators supplied by the protocol.  No source
# excerpt or real-world conclusion is embedded in production code.
PREANNOTATION_RULES: dict[str, dict[str, Any]] = {
    "EV-T0-DB1424F52E33432F": {
        "decision": "split",
        "nature": "resource_input + test_event",
        "note": "拆分历史数据训练与模型演示两个事实。",
    },
    "EV-T0-C86CE008843D093B": {
        "decision": "revise",
        "nature": "resource_input",
        "note": "项目周期和技术领域属于项目资源/实施输入，不是战略目标本身。",
    },
    "EV-T0-772E45D0B54BCC9C": {
        "decision": "revise",
        "nature": "resource_input",
        "note": "合同授予属于资源投入，不等于能力部署。",
    },
    "EV-T0-8E35B508DF5C2E65": {
        "decision": "revise",
        "nature": "constraint",
        "note": "缺少具体计划和时间表是实施约束。",
    },
    "EV-T0-357C993D92C95187": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "Tradewind市场监管属于治理机制，不能直接等同军事AI能力部署。",
    },
    "EV-T0-F25379FE058D2680": {
        "decision": "split",
        "nature": "resource_input + goal",
        "note": "已授予首批协议是资源投入；预计授予数十份协议是目标/预期。",
    },
    "EV-T0-00FCD33BA877C8BA": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "设立AI与数据加速计划属于已发生的治理/组织机制事件。",
    },
    "EV-T0-27EE6FA7DA2FADC0": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "CDAO职责确立属于机构职责和治理机制事件。",
    },
    "EV-T0-A42AF7A5AE2B6DAD": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "创建伦理AI工作表属于治理工具发布/建立。",
    },
    "EV-T0-2E42D28FCCBD88DD": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "发布AI采办指南属于治理指南采用/发布，不自动证明部署。",
    },
    "EV-T0-0A73030BBF2649F9": {
        "decision": "revise",
        "nature": "governance_event",
        "note": "CDAO承担协调和监督职责属于治理职责确立。",
    },
    "EV-T0-62B1ECDA7DF87F1A": {
        "decision": "revise",
        "nature": "constraint",
        "note": "数据质量困难是工程与资源约束，不是评价结论。",
    },
    "EV-T0-30E9B209F0439B21": {
        "decision": "revise",
        "nature": "evaluation",
        "note": "变革性进展是来源方评价，不应作为具体性能测试结果。",
    },
    "EV-T0-F67161443E222AE4": {
        "decision": "revise",
        "nature": "constraint",
        "note": "机载AI定义描述运行边界，建议标为约束。",
    },
    "EV-T0-850EF492D703EA37": {
        "decision": "revise",
        "nature": "constraint",
        "dimensions_remove": ["application_maturity"],
        "note": "删除application_maturity，仅保留constraint和engineering_resource_conditions。",
    },
    "EV-T0-A8459E6E8776A15D": {
        "decision": "revise",
        "nature": "deployment_event",
        "dimensions_remove": ["integration_level"],
        "note": "跨战区部署不自动证明跨平台或体系集成，删除integration_level。",
    },
}

VISUAL_DUPLICATE_KEYS = {
    ("T0-IR-006", "pdf_page_44"),
    ("T0-PB-007", "pdf_page_25"),
}
VISUAL_RESCUE_KEYS = {
    ("T0-IR-006", "pdf_page_52"),
    ("T0-IR-006", "pdf_page_82"),
    ("T0-PB-007", "pdf_page_2"),
    ("T0-SP-007", "pdf_page_16"),
    ("T0-SP-007", "pdf_page_25"),
}

G1_FINAL_FIELDS = (
    "final_decision",
    "final_nature",
    "final_dimensions",
    "final_normalized_claim",
    "human_corrected_excerpt",
    "final_excerpt_zh",
    "reviewer",
    "reviewed_at",
    "review_note",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _secret_safe_text(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


def _write_model_payload(path: Path, payload: Any, secret: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_secret_safe_text(text, secret), encoding="utf-8")


def _file_hashes(root: Path, *, exclude_root: Path | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    exclude = exclude_root.resolve() if exclude_root else None
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if exclude and (path.resolve() == exclude or exclude in path.resolve().parents):
            continue
        result[relative_path(path)] = sha256_file(path)
    return result


def _hash_map_digest(values: dict[str, str]) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(candidate.get("source_id", "")),
        str(candidate.get("source_locator", "")),
        str(candidate.get("excerpt_original", "")),
    )


def _repairable_schema_candidate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Apply only structural repairs explicitly authorized by P3b-R."""
    value = dict(raw)
    actions: list[str] = []
    notes: list[str] = []
    model_fields: list[str] = []
    dimensions = value.get("coding_dimensions")
    if isinstance(dimensions, list):
        legal = [item for item in dimensions if item in ALLOWED_DIMENSIONS]
        removed = [item for item in dimensions if item not in ALLOWED_DIMENSIONS]
        if removed:
            actions.append("remove_invalid_coding_dimensions")
            notes.append("removed_invalid_dimensions=" + ",".join(map(str, removed)))
        value["coding_dimensions"] = legal
    if "excerpt_zh_note" in value:
        actions.append("move_excerpt_zh_note_to_repair_note")
        notes.append(str(value.pop("excerpt_zh_note")))
    return value, actions, notes, model_fields


def _claim_repair_prompt(candidate: dict[str, Any], index: int) -> str:
    allowed = {
        "source_id": candidate.get("source_id"),
        "source_locator": candidate.get("source_locator"),
        "excerpt_original": candidate.get("excerpt_original"),
        "evidence_nature": candidate.get("evidence_nature"),
        "coding_dimensions": candidate.get("coding_dimensions"),
        "topics": candidate.get("topics"),
    }
    return "\n".join(
        [
            "PROMPT_ID: p3br_normalized_claim_repair_v1",
            "VERSION: 1.0",
            f"REPAIR_INDEX: {index}",
            "TASK: Generate only a Chinese normalized evidence claim for this one existing candidate.",
            "OUTPUT: strict JSON object with exactly one key: normalized_claim.",
            "Do not change or repeat the excerpt, locator, source, numbers, entities, actions, scope, or result.",
            "Do not add source-attribution prefixes such as 本报告认为 or 某机构指出.",
            "Use one checkable proposition only. If the excerpt is insufficient, return an empty normalized_claim.",
            json.dumps(allowed, ensure_ascii=False, indent=2),
        ]
    ) + "\n"


def _validate_repaired_claim(claim: Any, excerpt: str) -> list[str]:
    reasons: list[str] = []
    if not isinstance(claim, str) or not claim.strip():
        return ["normalized_claim_empty"]
    if not re.search(r"[\u3400-\u9fff]", claim):
        reasons.append("normalized_claim_not_chinese")
    if re.search(r"(?:本报告认为|本研究认为|某机构指出|来源指出|报告指出)", claim):
        reasons.append("source_attribution_prefix_forbidden")
    claim_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", claim))
    excerpt_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", excerpt))
    if not claim_numbers.issubset(excerpt_numbers):
        reasons.append("normalized_claim_adds_numeric_scope")
    return reasons


def _annotate_recovery(
    candidate: dict[str, Any],
    *,
    original_error: list[str],
    actions: list[str],
    notes: list[str],
    model_fields: list[str],
) -> dict[str, Any]:
    return {
        "recovery_origin": "p3b_schema_invalid_candidate_recovery",
        "original_error": list(original_error),
        "repair_action": list(actions),
        "repair_note": "; ".join(notes),
        "model_generated_fields": list(model_fields),
        "semantic_review_result": None,
        "candidate": dict(candidate),
    }


def _empty_final_fields() -> dict[str, str]:
    return {field: "" for field in G1_FINAL_FIELDS}


def _preannotation_row(record: dict[str, Any]) -> dict[str, Any]:
    evidence_id = record["evidence_id"]
    rule = PREANNOTATION_RULES.get(evidence_id, {})
    nature = rule.get("nature", record["evidence_nature"])
    dimensions = list(record.get("coding_dimensions", []))
    for item in rule.get("dimensions_remove", []):
        dimensions = [value for value in dimensions if value != item]
    if record.get("pending_g1_visual_review"):
        risk_level = "high"
        risk_reason = "PDF版式或机器抽取定位需要G1视觉核验。"
    elif rule or record.get("evidence_nature") in {
        "test_result",
        "deployment_event",
        "constraint",
        "evaluation",
        "governance_event",
    }:
        risk_level = "high"
        risk_reason = "性质、范围或维度需要作者在G1逐条确认。"
    elif record.get("evidence_nature") == "test_event":
        risk_level = "medium"
        risk_reason = "测试/演习发生与观察到的结果需要保持区分。"
    else:
        risk_level = "low"
        risk_reason = "未触发本轮强制高风险规则，仍需作者抽样审核。"
    decision = rule.get("decision", "revise" if risk_level == "high" else "accept")
    note = rule.get("note", "保留原P3b候选字段，等待作者G1决定。")
    return {
        "evidence_id": evidence_id,
        "source_id": record["source_id"],
        "source_locator": record["source_locator"],
        "proposed_decision": decision,
        "proposed_nature": nature,
        "proposed_dimensions": json.dumps(dimensions, ensure_ascii=False),
        "proposed_normalized_claim": record["normalized_claim"],
        "proposed_excerpt_zh": record["excerpt_zh"],
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "proposed_review_note": note,
        "model_generated": True,
        "human_reviewed": False,
        "record_origin": "p3b_original_43",
        **_empty_final_fields(),
    }


def _author_row_from_record(record: dict[str, Any], *, origin: str, decision: str, reason: str) -> dict[str, Any]:
    return {
        "evidence_id": record["evidence_id"],
        "source_id": record["source_id"],
        "source_locator": record["source_locator"],
        "proposed_decision": decision,
        "proposed_nature": record.get("evidence_nature", ""),
        "proposed_dimensions": json.dumps(record.get("coding_dimensions", []), ensure_ascii=False),
        "proposed_normalized_claim": record.get("normalized_claim", ""),
        "proposed_excerpt_zh": record.get("excerpt_zh", ""),
        "risk_level": "high",
        "risk_reason": reason,
        "proposed_review_note": "恢复候选或视觉待核验项；不得视为作者审核。",
        "model_generated": True,
        "human_reviewed": False,
        "record_origin": origin,
        **_empty_final_fields(),
    }


def _csv_fields(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["evidence_id"]
    ordered = list(rows[0].keys())
    for row in rows[1:]:
        for key in row:
            if key not in ordered:
                ordered.append(key)
    return ordered


def _semantic_candidate_payload(candidate: EvidenceCandidate, index: int) -> dict[str, Any]:
    return {"candidate_index": index, **{key: getattr(candidate, key) for key in (
        "source_id", "source_locator", "excerpt_original", "excerpt_zh",
        "normalized_claim", "coding_dimensions", "evidence_nature", "topics",
    )}}


def run_p3br(run_id: str, run_dir: Path) -> dict[str, Any]:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty P3b-R directory: {run_dir}")
    if not P3B_SOURCE.exists() or not P3B_REJECTED.exists():
        raise FileNotFoundError("P3b authoritative candidate/rejection artifacts are required")
    p3b_hashes_before = _file_hashes(P3B_DIR)
    protected_outputs_before = _file_hashes(ROOT / "outputs")
    documents = {doc.source["source_id"]: doc for doc in load_include_documents()}
    original_candidates = _jsonl(P3B_SOURCE)
    rejected_rows = _jsonl(P3B_REJECTED)
    semantic_template = yaml.safe_load(SEMANTIC_TEMPLATE.read_text(encoding="utf-8"))
    if not isinstance(semantic_template, dict):
        raise RuntimeError("Semantic screening template is invalid")
    run_dir.mkdir(parents=True, exist_ok=True)
    secret = __import__("os").environ.get("MODEL_API_KEY", "")
    client = APIModelClient()
    api_calls: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    unrecovered: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    recovery_by_row: set[tuple[str, int | None]] = set()
    current_seen: dict[str, set[str]] = {}
    for record in original_candidates:
        current_seen.setdefault(record["source_id"], set()).add(normalize_for_match(record["excerpt_original"]).casefold())

    schema_rows = [row for row in rejected_rows if SCHEMA_REPAIRABLE_ERROR in row.get("rejection_reasons", [])]
    schema_recovery_attempted = len(schema_rows)
    claim_repair_rows = [row for row in schema_rows if not row.get("candidate", {}).get("normalized_claim")]
    claim_repair_count = 0

    # Structural recovery consumes the old raw candidate only; it never reads a
    # prior final candidate as model input.
    for repair_index, row in enumerate(schema_rows, start=1):
        raw = row.get("candidate")
        original_error = list(row.get("rejection_reasons", []))
        if not isinstance(raw, dict):
            unrecovered.append({"recovery_status": "unrecovered", "original_error": original_error, "row": row})
            continue
        source_id = str(raw.get("source_id", ""))
        locator = str(raw.get("source_locator", ""))
        # The protocol explicitly allows this metadata candidate to remain out
        # of the recovered pool.
        if source_id == "T0-SP-007" and locator == "pdf_page_16" and "excerpt_zh_note" in raw:
            unrecovered.append({
                "recovery_origin": "p3b_schema_invalid_candidate_recovery",
                "recovery_status": "not_recovered_research_relevance",
                "original_error": original_error,
                "repair_action": [],
                "model_generated_fields": [],
                "semantic_review_result": None,
                "batch_id": row.get("batch_id"),
                "candidate_index": row.get("candidate_index"),
                "candidate": raw,
                "reason": "研究相关性不足的元描述，按协议不强行恢复。",
            })
            recovery_by_row.add((str(row.get("batch_id")), row.get("candidate_index")))
            continue
        cleaned, actions, notes, model_fields = _repairable_schema_candidate(raw)
        if not cleaned.get("normalized_claim"):
            prompt = _claim_repair_prompt(cleaned, repair_index)
            prompt_path = run_dir / "prompt_packets/claim_repair" / f"claim-repair-{repair_index:02d}.txt"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            payload, content, call_record = client.call(prompt, temperature=0.0, max_tokens=800)
            raw_path = run_dir / "raw_model_outputs/claim_repair" / f"claim-repair-{repair_index:02d}.json"
            _write_model_payload(raw_path, payload, secret)
            metadata = call_record.as_dict()
            metadata.update({
                "phase": "p3br_normalized_claim_repair",
                "logical_call_type": "normalized_claim_repair",
                "repair_index": repair_index,
                "prompt_path": relative_path(prompt_path),
                "prompt_sha256": sha256_file(prompt_path),
                "raw_response_path": relative_path(raw_path),
                "raw_response_sha256": sha256_file(raw_path),
                "response_content_sha256": sha256_bytes(content.encode("utf-8")),
                "requested_temperature": 0.0,
                "provider_echoed_temperature": call_record.provider_temperature,
            })
            meta_path = run_dir / "api_call_metadata" / f"claim-repair-{repair_index:02d}.json"
            write_json(meta_path, metadata)
            api_calls.append(metadata)
            claim_repair_count += 1
            try:
                value = _decode_json(content)
                if not isinstance(value, dict) or set(value) != {"normalized_claim"}:
                    raise ValueError("claim_repair_schema_invalid")
                claim = value.get("normalized_claim")
                claim_reasons = _validate_repaired_claim(claim, str(cleaned.get("excerpt_original", "")))
                if claim_reasons:
                    raise ValueError(";".join(claim_reasons))
                cleaned["normalized_claim"] = claim
                actions.append("model_generate_normalized_claim_only")
                model_fields.append("normalized_claim")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                unrecovered.append({
                    "recovery_status": "unrecovered_claim_repair",
                    "original_error": original_error,
                    "repair_action": actions,
                    "repair_note": "; ".join(notes),
                    "model_generated_fields": model_fields,
                    "candidate": cleaned,
                    "reason": str(exc),
                })
                recovery_by_row.add((str(row.get("batch_id")), row.get("candidate_index")))
                continue
        try:
            candidate = model_validate(EvidenceCandidate, cleaned)
        except Exception as exc:
            unrecovered.append({
                "recovery_status": "unrecovered_schema_after_repair",
                "original_error": original_error,
                "repair_action": actions,
                "repair_note": "; ".join(notes),
                "model_generated_fields": model_fields,
                "candidate": cleaned,
                "reason": str(exc),
            })
            recovery_by_row.add((str(row.get("batch_id")), row.get("candidate_index")))
            continue
        document = documents.get(candidate.source_id)
        segment = resolve_locator(candidate.source_locator, {s.source_locator: s for s in document.segments}) if document else None
        reasons = _deterministic_reasons(candidate, segment, current_seen.setdefault(candidate.source_id, set()))
        provenance = _annotate_recovery(
            cleaned,
            original_error=original_error,
            actions=actions,
            notes=notes,
            model_fields=model_fields,
        )
        provenance["batch_id"] = row.get("batch_id")
        provenance["candidate_index"] = row.get("candidate_index")
        if reasons or document is None or segment is None:
            provenance["recovery_status"] = "unrecovered_deterministic_gate"
            provenance["deterministic_reasons"] = reasons or ["source_or_locator_not_found"]
            unrecovered.append(provenance)
            recovery_by_row.add((str(row.get("batch_id")), row.get("candidate_index")))
            continue
        current_seen[candidate.source_id].add(normalize_for_match(candidate.excerpt_original).casefold())
        eligible.append({"candidate": candidate, "segment": segment, "provenance": provenance})
        recovery_by_row.add((str(row.get("batch_id")), row.get("candidate_index")))

    # Re-run one real same-model semantic screening call for only the repaired
    # candidates.  It is separate from the four claim-repair calls.
    semantic_results: list[dict[str, Any]] = []
    dimension_revisions: list[dict[str, Any]] = []
    unsupported_dimension_removals = 0
    if eligible:
        semantic_values = [_semantic_candidate_payload(item["candidate"], index) for index, item in enumerate(eligible)]
        semantic_prompt = build_semantic_prompt(
            [dict(item, candidate_index=item["candidate_index"]) for item in semantic_values],
            "P3BR-recovered-semantic-001",
            semantic_template,
        )
        semantic_prompt_path = run_dir / "prompt_packets/semantic_screening/recovered-batch-001.txt"
        semantic_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        semantic_prompt_path.write_text(semantic_prompt, encoding="utf-8")
        payload, content, call_record = client.call(semantic_prompt, temperature=0.0, max_tokens=5000)
        raw_path = run_dir / "raw_semantic_reviews/recovered-batch-001.json"
        _write_model_payload(raw_path, payload, secret)
        metadata = call_record.as_dict()
        metadata.update({
            "phase": "same_model_separate_call_semantic_screening",
            "logical_call_type": "recovered_candidate_semantic_screening",
            "prompt_path": relative_path(semantic_prompt_path),
            "prompt_sha256": sha256_file(semantic_prompt_path),
            "raw_response_path": relative_path(raw_path),
            "raw_response_sha256": sha256_file(raw_path),
            "response_content_sha256": sha256_bytes(content.encode("utf-8")),
            "requested_temperature": 0.0,
            "provider_echoed_temperature": call_record.provider_temperature,
        })
        write_json(run_dir / "api_call_metadata/recovered-batch-001-semantic.json", metadata)
        api_calls.append(metadata)
        try:
            semantic_map, semantic_error = _semantic_rows(_decode_json(content))
        except (ValueError, TypeError, json.JSONDecodeError):
            semantic_map, semantic_error = {}, "semantic_content_not_strict_json"
        if semantic_error:
            for item in eligible:
                item["provenance"]["recovery_status"] = "unrecovered_semantic_screen"
                item["provenance"]["semantic_review_result"] = {"error": semantic_error}
                unrecovered.append(item["provenance"])
        else:
            for local_index, item in enumerate(eligible):
                candidate = item["candidate"]
                segment = item["segment"]
                provenance = item["provenance"]
                review = semantic_map.get(local_index)
                semantic_row = {
                    "candidate_index": local_index,
                    "source_id": candidate.source_id,
                    "source_locator": candidate.source_locator,
                    "recovery_origin": provenance["recovery_origin"],
                    "review": review,
                }
                semantic_results.append(semantic_row)
                provenance["semantic_review_result"] = review
                if not isinstance(review, dict) or review.get("status") not in {"pass", "revise"} or review.get("claim_supported") is not True or review.get("nature_supported") is not True:
                    provenance["recovery_status"] = "unrecovered_semantic_screen"
                    provenance["reason"] = "semantic_claim_or_nature_not_supported"
                    unrecovered.append(provenance)
                    continue
                declared = list(candidate.coding_dimensions)
                reviews = {row["dimension"]: row for row in review.get("dimension_reviews", []) if isinstance(row, dict) and "dimension" in row}
                if not set(declared).issubset(reviews):
                    provenance["recovery_status"] = "unrecovered_semantic_dimension_schema"
                    provenance["reason"] = "semantic_dimension_review_incomplete"
                    unrecovered.append(provenance)
                    continue
                supported = [dimension for dimension in declared if reviews[dimension].get("supported") is True]
                removed = [dimension for dimension in declared if dimension not in supported]
                unsupported_dimension_removals += len(removed)
                dimension_revisions.append({
                    "source_id": candidate.source_id,
                    "source_locator": candidate.source_locator,
                    "original_dimensions": declared,
                    "final_dimensions": supported,
                    "removed_dimensions": removed,
                    "dimension_reviews": list(review.get("dimension_reviews", [])),
                    "review_origin": "same_model_separate_call_semantic_screening",
                    "model_generated": True,
                    "human_reviewed": False,
                    "recovery_origin": provenance["recovery_origin"],
                })
                candidate.coding_dimensions = supported
                record = inject_metadata(candidate, documents[candidate.source_id].source, run_id, segment)
                record.update({
                    "recovery_origin": provenance["recovery_origin"],
                    "original_error": provenance["original_error"],
                    "repair_action": provenance["repair_action"],
                    "repair_note": provenance["repair_note"],
                    "model_generated_fields": provenance["model_generated_fields"],
                    "semantic_review_result": review,
                    "semantic_screening_mode": "same_model_separate_call_semantic_screening",
                    "p3br_candidate_only": True,
                    "human_corrected_excerpt": None,
                    "possible_corroboration_group": "",
                })
                recovered.append(record)

    # The seven locator failures form a visual rescue queue; none are silently
    # accepted.  Two are retained as duplicate-exclusion suggestions.
    visual_queue: list[dict[str, Any]] = []
    for row in rejected_rows:
        if VISUAL_ERROR not in row.get("rejection_reasons", []):
            continue
        raw = row.get("candidate") or {}
        source_id = str(raw.get("source_id", ""))
        locator = str(raw.get("source_locator", ""))
        key = (source_id, locator)
        action = "exclude" if key in VISUAL_DUPLICATE_KEYS else "visual_review"
        reason = "与同一事实的另一定位重复，建议作者确认后排除。" if action == "exclude" else "机器抽取定位未精确匹配，必须进行PDF视觉核验，不得自动接受。"
        visual_queue.append({
            "evidence_id": stable_evidence_id(source_id, locator, str(raw.get("excerpt_original", ""))),
            "source_id": source_id,
            "source_locator": locator,
            "excerpt_original": raw.get("excerpt_original", ""),
            "excerpt_zh": raw.get("excerpt_zh", ""),
            "normalized_claim": raw.get("normalized_claim", ""),
            "evidence_nature": raw.get("evidence_nature", ""),
            "coding_dimensions": json.dumps(raw.get("coding_dimensions", []), ensure_ascii=False),
            "original_error": ";".join(row.get("rejection_reasons", [])),
            "recommended_action": action,
            "recommended_reason": reason,
            "pending_visual_review": True,
            "final_decision": "",
            "human_corrected_excerpt": "",
            "reviewer": "",
            "reviewed_at": "",
            "review_note": "",
        })

    # All non-schema rejections and skipped/failed schema repairs are retained
    # for audit.  Do not duplicate rows already classified above.
    for row in rejected_rows:
        key = (str(row.get("batch_id")), row.get("candidate_index"))
        if key in recovery_by_row:
            continue
        unrecovered.append({
            "recovery_status": "unrecovered_original_rejection",
            "original_error": row.get("rejection_reasons", []),
            "batch_id": row.get("batch_id"),
            "candidate_index": row.get("candidate_index"),
            "candidate": row.get("candidate"),
        })

    # Current 43-item AI preannotations are a preparation artifact only.
    preannotations = [_preannotation_row(record) for record in original_candidates]
    nature_revisions: list[dict[str, Any]] = []
    for row, record in zip(preannotations, original_candidates, strict=True):
        rule = PREANNOTATION_RULES.get(record["evidence_id"])
        if not rule:
            continue
        nature_revisions.append({
            "evidence_id": record["evidence_id"],
            "old_nature": record["evidence_nature"],
            "proposed_nature": row["proposed_nature"],
            "old_dimensions": record.get("coding_dimensions", []),
            "proposed_dimensions": json.loads(row["proposed_dimensions"]),
            "reason": rule["note"],
            "revision_origin": "author_requested_ai_preannotation_rule",
            "model_generated": True,
            "human_reviewed": False,
        })
    for item in dimension_revisions:
        nature_revisions.append({"revision_origin": "recovered_candidate_semantic_dimension_screening", **item})

    high_rows = [row for row in preannotations if row["risk_level"] == "high"]
    for record in recovered:
        high_rows.append(_author_row_from_record(record, origin="p3br_recovered_candidate", decision="revise", reason="恢复候选已通过确定性门禁和真实同模型语义筛选，仍需G1确认。"))
    for row in visual_queue:
        high_rows.append(_author_row_from_record({**row, "evidence_id": row["evidence_id"], "evidence_nature": row["evidence_nature"], "coding_dimensions": json.loads(row["coding_dimensions"]), "normalized_claim": row["normalized_claim"], "excerpt_zh": row["excerpt_zh"]}, origin="g1_visual_rescue_queue", decision="exclude" if row["recommended_action"] == "exclude" else "revise", reason=row["recommended_reason"]))
    high_by_id: dict[str, dict[str, Any]] = {}
    for row in high_rows:
        high_by_id.setdefault(row["evidence_id"], row)
    high_rows = list(high_by_id.values())

    low_rows = [row for row in preannotations if row["risk_level"] == "low"]
    low_rows_sorted = sorted(low_rows, key=lambda row: hashlib.sha256(row["evidence_id"].encode("utf-8")).hexdigest())
    sample_count = (len(low_rows_sorted) + 4) // 5 if low_rows_sorted else 0
    low_sample = low_rows_sorted[:sample_count]
    decision_rows = high_rows + low_sample

    combined = [dict(record, record_origin="p3b_original_43") for record in original_candidates] + recovered
    claim_groups: dict[str, set[str]] = {}
    for record in combined:
        claim_groups.setdefault(normalize_for_match(record.get("normalized_claim", "")).casefold(), set()).add(record["source_id"])
    for record in combined:
        claim_key = normalize_for_match(record.get("normalized_claim", "")).casefold()
        if len(claim_groups.get(claim_key, set())) > 1:
            record["possible_corroboration_group"] = "CORR-" + hashlib.sha256(claim_key.encode("utf-8")).hexdigest()[:12].upper()

    write_jsonl(run_dir / "recovered_candidates.jsonl", recovered)
    write_jsonl(run_dir / "unrecovered_candidates.jsonl", unrecovered)
    write_jsonl(run_dir / "evidence_candidates_p3br_combined.jsonl", combined)
    write_jsonl(run_dir / "evidence_nature_revision_log.jsonl", nature_revisions)
    write_jsonl(run_dir / "semantic_screen_results_recovered.jsonl", semantic_results)
    write_jsonl(run_dir / "dimension_revision_log_recovered.jsonl", dimension_revisions)
    write_csv(run_dir / "g1_visual_rescue_queue.csv", _csv_fields(visual_queue), visual_queue)
    write_csv(run_dir / "g1_ai_preannotation.csv", _csv_fields(preannotations), preannotations)
    write_csv(run_dir / "g1_author_high_priority.csv", _csv_fields(high_rows), high_rows)
    write_csv(run_dir / "g1_author_low_risk_sample.csv", _csv_fields(low_sample), low_sample)
    write_csv(run_dir / "g1_author_decision_sheet.csv", _csv_fields(decision_rows), decision_rows)

    decision_counts = Counter(row["proposed_decision"] for row in preannotations)
    quality = {
        "p3b_original_candidate_count": len(original_candidates),
        "schema_invalid_candidate_count": schema_recovery_attempted,
        "schema_recovery_claim_model_call_count": claim_repair_count,
        "schema_recovery_deterministic_eligible_count": len(eligible),
        "schema_recovered_final_count": len(recovered),
        "visual_rescue_queue_count": len(visual_queue),
        "visual_waiting_count": sum(row["recommended_action"] == "visual_review" for row in visual_queue),
        "visual_duplicate_exclusion_recommendation_count": sum(row["recommended_action"] == "exclude" for row in visual_queue),
        "ai_preannotation_decision_counts": dict(decision_counts),
        "author_reviewed_count": 0,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "g1_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "api_key_written": False,
        "unsupported_dimension_removal_count": unsupported_dimension_removals,
    }
    report = "\n".join(
        [
            "# P3b-R quality report",
            "",
            "本运行只执行P3b候选恢复和G1-T0预标注准备，未执行正式G1、P4、Evidence写入、快照冻结、假设生成或T1读取。",
            "",
            f"- P3b原43条: {len(original_candidates)}",
            f"- Schema恢复候选尝试: {schema_recovery_attempted}",
            f"- 缺失normalized_claim的真实模型修补调用: {claim_repair_count}",
            f"- 通过确定性门禁并进入恢复语义筛选: {len(eligible)}",
            f"- 最终恢复候选: {len(recovered)}",
            f"- 视觉待恢复队列: {len(visual_queue)}（其中等待视觉核验{quality['visual_waiting_count']}，重复事实排除建议{quality['visual_duplicate_exclusion_recommendation_count']}）",
            f"- AI预标注建议计数（仅原43条）: {json.dumps(dict(decision_counts), ensure_ascii=False)}",
            "- 作者实际审核数: 0",
            "",
            "恢复修补只改变结构字段或由真实模型补充缺失的中文normalized_claim；原摘录、定位、来源和事实范围未由程序改写。所有候选仍需作者G1决定。",
        ]
    ) + "\n"
    (run_dir / "p3br_quality_report.md").write_text(report, encoding="utf-8")

    p3b_hashes_after = _file_hashes(P3B_DIR)
    protected_outputs_after = _file_hashes(ROOT / "outputs", exclude_root=run_dir)
    if p3b_hashes_before != p3b_hashes_after:
        raise RuntimeError("Protected P3b artifact changed during P3b-R")
    if protected_outputs_before != protected_outputs_after:
        raise RuntimeError("A protected prior output changed during P3b-R")

    output_hashes = _file_hashes(run_dir)
    manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "P3b-R+G1-T0-preannotation",
        "executed_at_utc": _now_utc(),
        "p3b_input_run_id": P3B_RUN_ID,
        "p3b_input_hashes": p3b_hashes_before,
        "p3b_hashes_unchanged": p3b_hashes_before == p3b_hashes_after,
        "protected_outputs_before_file_count": len(protected_outputs_before),
        "protected_outputs_before_digest": _hash_map_digest(protected_outputs_before),
        "protected_prior_outputs_unchanged": protected_outputs_before == protected_outputs_after,
        "reprocessed_source_count": 0,
        "api_logical_call_count": len(api_calls),
        "provider_call_count": sum(int(call.get("attempts", 0)) for call in api_calls),
        "claim_repair_model_call_count": claim_repair_count,
        "semantic_screen_model_call_count": 1 if eligible else 0,
        "model_id": client.model,
        "requested_temperature": 0.0,
        "provider_echoed_temperature": None,
        "schema_invalid_candidate_count": schema_recovery_attempted,
        "schema_recovery_deterministic_eligible_count": len(eligible),
        "schema_recovered_final_count": len(recovered),
        "visual_rescue_queue_count": len(visual_queue),
        "visual_waiting_count": quality["visual_waiting_count"],
        "visual_duplicate_exclusion_recommendation_count": quality["visual_duplicate_exclusion_recommendation_count"],
        "p3b_original_candidate_count": len(original_candidates),
        "combined_candidate_count": len(combined),
        "ai_preannotation_counts": dict(decision_counts),
        "author_reviewed_count": 0,
        "unsupported_dimension_removal_count": unsupported_dimension_removals,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "g1_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "api_key_written": False,
        "candidate_only": True,
        "output_file_sha256": output_hashes,
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
    }
    write_json(run_dir / "p3br_manifest.json", manifest)
    # A final secret scan protects against a provider echoing credentials.
    for path in run_dir.rglob("*"):
        if path.is_file() and ("sk" + "-sp-") in path.read_text(encoding="utf-8", errors="ignore"):
            raise RuntimeError("API key material detected in P3b-R output")
    return manifest
