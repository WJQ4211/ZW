"""P11R + P12R + G1Q-T1 incremental recovery.

The previous P11/P12 run is read-only input.  This module does not import any
T0 hypothesis, viewpoint, disagreement, or monitoring artifact.  It applies
only the user-provided offline corrections to the prior T1 candidate rows and
uses the model API for the three new full-text sources.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from .model_client import APIModelClient, ApiCallRecord, ModelAPIError
from .schemas import CODING_DIMENSION_VALUES, EVIDENCE_NATURE_VALUES
from .t1_workflow import (
    CASE_ID,
    CASE_QUESTION,
    DIMENSION_DEFINITIONS,
    G4_DIR,
    NATURE_DEFINITIONS,
    ROOT,
    _candidate_validation,
    _clean_html,
    _fetch,
    _normalize_dimension_reviews,
    _parse_json_response,
    _paragraph_rows,
    _read_only_db_counts,
    _semantic_decision,
    file_hashes,
    now_utc,
    sha256_bytes,
    sha256_text,
    write_csv,
    write_json,
    write_jsonl,
)


OLD_RUN_ID = "G4-P11-P12-20260827T051332Z-v1"
OLD_RUN = ROOT / "outputs" / OLD_RUN_ID
T1_START = date(2025, 1, 1)
T1_END = date(2026, 6, 30)

SUPPLEMENTAL_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "T1-DARPA-001",
        "title": "Sharpening AI warfighting advantage on the battlefield",
        "publisher": "DARPA",
        "source_grade": "A",
        "category": "strategy_policy_governance;test_evaluation_constraint",
        "document_date": "2025-03-17",
        "canonical_url": "https://www.darpa.mil/news/2025/saber-warfighter-ai",
        "capture_url": "https://www.darpa.mil/news/2025/saber-warfighter-ai",
        "capture_type": "official_html",
        "reason": "补足官方作战化AI红队能力、生态和战场应用约束覆盖。",
    },
    {
        "source_id": "T1-DARPA-003",
        "title": "DART and MSAI triumph at DARPA Triage Challenge",
        "publisher": "DARPA",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date": "2025-11-14",
        "canonical_url": "https://www.darpa.mil/news/2025/dart-msai-triumph-darpa-triage-challenge",
        "capture_url": "https://www.darpa.mil/news/2025/dart-msai-triumph-darpa-triage-challenge",
        "capture_type": "official_html",
        "reason": "补足竞赛测试事件、测试结果与官方评价的区分。",
    },
    {
        "source_id": "T1-IR-001",
        "title": "Opportunities for Artificial Intelligence in Operational Medicine: Lessons from the United States Military",
        "publisher": "Bioengineering (MDPI), peer-reviewed research",
        "source_grade": "B",
        "category": "independent_review_research",
        "document_date": "2025-05-14",
        "canonical_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12108871/",
        "capture_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12108871/",
        "capture_type": "pmc_html",
        "doi": "10.3390/bioengineering12050519",
        "pmcid": "PMC12108871",
        "reason": "补足公开同行评议材料对AI应用条件、实施障碍、可信性或人机协同的独立观察。",
    },
]

DASH_IDS = {
    "CE-T1-7AB164824EAAD82A",
    "CE-T1-11BE4D003ECA817F",
    "CE-T1-06554E1EE0D2B6C0",
}
MASH_IDS = {
    "CE-T1-6B0D6E320A21509E",
    "CE-T1-8FF394626242D949",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _save_supplemental_source(base: Path, item: dict[str, Any]) -> dict[str, Any]:
    raw, resolved_url, content_type, status = _fetch(item["capture_url"])
    if not raw:
        raise RuntimeError(f"empty source response for {item['source_id']}")
    text, parsed = _clean_html(raw)
    if not text.strip():
        raise RuntimeError(f"empty cleaned source text for {item['source_id']}")
    published = date.fromisoformat(item["document_date"])
    if not T1_START <= published <= T1_END:
        raise RuntimeError(f"source date outside T1 window for {item['source_id']}")
    raw_path = base / "P11R_T1_COLLECTION" / "source_raw" / f"{item['source_id']}.html"
    text_path = base / "P11R_T1_COLLECTION" / "source_text" / f"{item['source_id']}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    # Write bytes explicitly so the recorded text hash covers the exact
    # archived bytes on Windows as well as on POSIX (text-mode newline
    # translation would otherwise make the recorded hash differ from the
    # file hash).
    text_path.write_bytes(text.encode("utf-8"))
    return {
        **item,
        "case_id": CASE_ID,
        "resolved_url": resolved_url,
        "http_status": status,
        "content_type": content_type,
        "verified_title": parsed.get("title") or item["title"],
        "public_release_verified": True,
        "publication_date_verification": "user-supplied-date-recorded-against-original-archive",
        "raw_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
        "text_path": str(text_path.relative_to(ROOT)).replace("\\", "/"),
        "raw_sha256": sha256_bytes(raw),
        "text_sha256": sha256_bytes(text_path.read_bytes()),
        "file_size": len(raw),
        "full_text_status": "verified_full_text",
        "selection_status": "supplemental_candidate_for_g1",
        "temporal_status": "within_t1_window",
        "duplicate_status": "not_duplicate_of_existing_selected_source",
        "duplicate_of": "",
        "access_error": "",
        "parser": parsed.get("parser", "html_clean_paragraphs"),
    }


def _build_supplemental_extraction_prompt(row: dict[str, Any], paragraphs: list[dict[str, Any]]) -> str:
    dims = "\n".join(f"- {name}: {value}" for name, value in DIMENSION_DEFINITIONS.items())
    natures = "\n".join(f"- {name}: {value}" for name, value in NATURE_DEFINITIONS.items())
    source_specific = (
        "对SABER材料，若原文直接陈述当前缺少成熟的作战化AI红队能力及更广泛生态，应作为constraint或信息缺口候选，不作为项目成效。\n"
        "对DARPA挑战赛，分别区分竞赛发生(test_event)、明确公布的名次或结果(test_result)和官方评价(evaluation)。\n"
        "对同行评议论文，只提取能够直接支持AI应用条件、实施障碍、可信性或人机协同的陈述；不能支撑案例问题的医学细节不要纳入。\n"
    )
    body = "\n".join(f"[{p['source_locator']}] {p['text']}" for p in paragraphs)
    return (
        "T1增量证据候选抽取。只使用提供的原文，不使用外部知识或其他运行输出。\n"
        f"研究问题：{CASE_QUESTION}\n"
        f"来源ID：{row['source_id']}\n标题：{row['verified_title']}\n发布日期：{row['document_date']}\n"
        "返回JSON数组，每条恰好包含8个字段：source_id、source_locator、excerpt_original、excerpt_zh、normalized_claim、evidence_nature、coding_dimensions、topics。不得输出额外字段，允许空数组，每份来源最多6条。\n"
        "source_id必须等于给定来源；source_locator必须是给定段落定位；excerpt_original必须逐字连续来自指定段落；excerpt_zh和normalized_claim必须使用中文；normalized_claim只能表达一个原子事实或主张，不增加主体、行动、数字、范围或结果。\n"
        "evidence_nature必须为单一枚举字符串；coding_dimensions和topics必须为字符串数组；不要把目标、计划或负责人判断写成已经实现的结果，不把演示、竞赛或试验写成部署，不把预算或合同写成规模化能力。\n"
        "性质定义：\n" + natures + "\n编码维度定义：\n" + dims + "\n" + source_specific + "原文段落：\n" + body
    )


def _build_supplemental_semantic_prompt(row: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    dims = "\n".join(f"- {name}: {value}" for name, value in DIMENSION_DEFINITIONS.items())
    natures = "\n".join(f"- {name}: {value}" for name, value in NATURE_DEFINITIONS.items())
    payload = json.dumps(candidates, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "T1增量候选证据语义筛选。只检查候选字段是否被所给原文直接支持，不读取其他阶段分析。\n"
        f"研究问题：{CASE_QUESTION}\n来源：{row['source_id']}\n"
        "为每条返回candidate_evidence_id、status、reason、claim_supported、nature_supported、dimension_reviews、revised_fields。status只能为accept、revise或reject；dimension_reviews必须是数组，逐维度包含dimension、supported、supporting_span、reason。只处理原候选声明的维度，禁止新增维度；revise不得修改excerpt_original和source_locator。\n"
        "性质定义：\n" + natures + "\n编码维度定义：\n" + dims + "\n候选：\n" + payload
    )


def _write_call_log(path: Path, phase: str, source_id: str, record: ApiCallRecord, prompt: str, response_content: str) -> None:
    value = record.as_dict()
    value.update({
        "phase": phase,
        "source_id": source_id,
        "prompt_sha256": sha256_text(prompt),
        "response_sha256": sha256_text(response_content),
        "api_key_written": False,
    })
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _apply_old_corrections(old_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corrected = []
    log = []
    for original in old_rows:
        before = dict(original)
        after = dict(original)
        candidate_id = original["candidate_evidence_id"]
        actions: list[str] = []
        changed_fields: list[str] = []
        combined_status = "retained_prior_candidate"
        t1_change_signal_eligible = True
        if candidate_id in {"CE-T1-6B7DB29A3B242353", "CE-T1-1797A97EF532F332"}:
            combined_status = "proposed_exclude"
            actions.append("offline_exclusion_recommendation")
        if candidate_id == "CE-T1-7DE738404CD33807":
            combined_status = "historical_context_not_t1_delta"
            t1_change_signal_eligible = False
            actions.append("historical_context_flag")
        if candidate_id in DASH_IDS or candidate_id in MASH_IDS:
            claim = str(after.get("normalized_claim", ""))
            if not claim.startswith("据官方披露的初步结果") and not claim.startswith("据参与者报告"):
                prefix = "据官方披露的初步结果，" if candidate_id in DASH_IDS else "据参与者报告，"
                after["normalized_claim"] = prefix + claim
                changed_fields.append("normalized_claim")
                actions.append("add_source_qualification")
        if candidate_id == "CE-T1-AA937CC0AF5E2372":
            after["normalized_claim"] = "卢克空军基地部分人员在邮件起草、信息摘要和数据分析等日常任务中使用AI。"
            changed_fields.append("normalized_claim")
            actions.append("limit_local_deployment_scope")
        if candidate_id == "CE-T1-D0E7B9B8CC1298AB":
            dimensions = list(after.get("coding_dimensions", []))
            if "application_maturity" in dimensions:
                after["coding_dimensions"] = [d for d in dimensions if d != "application_maturity"]
                changed_fields.append("coding_dimensions")
            actions.append("remove_unsupported_application_maturity")
        after["correction_status"] = combined_status
        after["t1_change_signal_eligible"] = t1_change_signal_eligible
        after["lineage"] = "prior_p12_v1"
        reason = "；".join(actions) if actions else "offline_type_scope_check_no_change"
        log.append({
            "candidate_evidence_id": candidate_id,
            "source_id": original.get("source_id", ""),
            "correction_action": ";".join(actions) if actions else "checked_no_change",
            "changed_fields": json.dumps(changed_fields, ensure_ascii=False),
            "before": json.dumps(before, ensure_ascii=False, sort_keys=True),
            "after": json.dumps(after, ensure_ascii=False, sort_keys=True),
            "reason": reason,
            "combined_status": combined_status,
            "t1_change_signal_eligible": t1_change_signal_eligible,
            "model_called": False,
            "human_reviewed": False,
        })
        corrected.append(after)
    return corrected, log


def _safe_api_failure(base: Path, stage: str, source_id: str, exc: ModelAPIError, completed_calls: int, saved_batches: list[str]) -> None:
    write_json(base / "p12r_failure_manifest.json", {
        "run_id": base.name,
        "failure_stage": stage,
        "source_id": source_id,
        "exception_type": exc.root_exception_type,
        "http_status_code": exc.http_status_code,
        "completed_logical_calls": completed_calls,
        "saved_batches": saved_batches,
        "evidence_inserted": 0,
        "formal_g1_t1_executed": False,
        "t1_snapshot_frozen": False,
        "impact_localization_executed": False,
        "viewpoint_update_executed": False,
        "t1_read": True,
        "api_key_written": False,
    })


def _supplemental_p12(base: Path, sources: list[dict[str, Any]], old_rows: list[dict[str, Any]]) -> dict[str, Any]:
    p12 = base / "P12R_T1_EXTRACTION"
    prompts = base / "prompts" / "P12R"
    raw = base / "raw_responses" / "P12R"
    checkpoint = base / "checkpoints" / "p12r_checkpoint.json"
    for path in (p12, prompts, raw / "extraction", raw / "semantic", checkpoint.parent):
        path.mkdir(parents=True, exist_ok=True)
    call_log = base / "raw_responses" / "model_call_log.jsonl"
    client = APIModelClient(timeout_seconds=180.0, max_retries=3)
    old_hashes = {row.get("content_hash", "") for row in old_rows}
    raw_candidates: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    final: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    semantic_rows: list[dict[str, Any]] = []
    anchor_rows: list[dict[str, Any]] = []
    extraction_calls = 0
    semantic_calls = 0
    retry_count = 0
    validation_rows: list[dict[str, Any]] = []
    for source in sources:
        text = (ROOT / source["text_path"]).read_text(encoding="utf-8")
        paragraphs = _paragraph_rows(source["source_id"], text, False)
        anchor_rows.extend(paragraphs)
        prompt = _build_supplemental_extraction_prompt(source, paragraphs)
        prompt_path = prompts / f"extract_{source['source_id']}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        try:
            payload, content, record = client.call(prompt, temperature=0.0, max_tokens=6000)
        except ModelAPIError as exc:
            _safe_api_failure(base, "extraction", source["source_id"], exc, extraction_calls + semantic_calls, [s["source_id"] for s in sources if s["source_id"] != source["source_id"]])
            raise
        extraction_calls += 1
        retry_count += record.retry_count
        write_json(raw / "extraction" / f"{source['source_id']}.json", payload)
        _write_call_log(call_log, "extraction", source["source_id"], record, prompt, content)
        parsed, error = _parse_json_response(content, ("candidates", "evidence_candidates"))
        if error:
            rejected.append({"source_id": source["source_id"], "stage": "extraction_parse", "reason": error})
            continue
        if len(parsed) > 6:
            rejected.append({"source_id": source["source_id"], "stage": "extraction_batch", "reason": "candidate_count_exceeds_limit", "raw_candidate_count": len(parsed)})
            continue
        valid_for_semantic: list[dict[str, Any]] = []
        for index, item in enumerate(parsed):
            raw_candidates.append({"source_id": source["source_id"], "raw_candidate_index": index, "candidate": item})
            candidate, validation_error = _candidate_validation(item, source, paragraphs)
            row = {
                "source_id": source["source_id"],
                "raw_candidate_index": index,
                "candidate_evidence_id": candidate.get("candidate_evidence_id", "") if candidate else "",
                "schema_valid": bool(candidate),
                "exact_quote_matched": bool(candidate),
                "deterministic_gate_passed": bool(candidate),
                "reason": validation_error,
            }
            validation_rows.append(row)
            if not candidate:
                rejected.append({"source_id": source["source_id"], "stage": "deterministic", "reason": validation_error, "raw_candidate_index": index, "candidate": item})
                continue
            candidate["extraction_run_id"] = base.name
            if candidate["content_hash"] in old_hashes:
                rejected.append({"source_id": source["source_id"], "stage": "duplicate_existing", "reason": "content_hash_matches_existing_t1_candidate", "candidate_evidence_id": candidate["candidate_evidence_id"]})
                continue
            if any(candidate["content_hash"] == item["content_hash"] for item in validated):
                rejected.append({"source_id": source["source_id"], "stage": "duplicate_internal", "reason": "duplicate_supplemental_content_hash", "candidate_evidence_id": candidate["candidate_evidence_id"]})
                continue
            validated.append(candidate)
            valid_for_semantic.append(candidate)
        if not valid_for_semantic:
            continue
        semantic_prompt = _build_supplemental_semantic_prompt(source, valid_for_semantic)
        (prompts / f"semantic_{source['source_id']}.txt").write_text(semantic_prompt, encoding="utf-8")
        try:
            semantic_payload, semantic_content, semantic_record = client.call(semantic_prompt, temperature=0.0, max_tokens=6000)
        except ModelAPIError as exc:
            _safe_api_failure(base, "semantic_screening", source["source_id"], exc, extraction_calls + semantic_calls, [s["source_id"] for s in sources if s["source_id"] != source["source_id"]])
            raise
        semantic_calls += 1
        retry_count += semantic_record.retry_count
        write_json(raw / "semantic" / f"{source['source_id']}.json", semantic_payload)
        _write_call_log(call_log, "semantic_screening", source["source_id"], semantic_record, semantic_prompt, semantic_content)
        reviews, semantic_error = _parse_json_response(semantic_content, ("reviews", "semantic_reviews"))
        if semantic_error:
            for candidate in valid_for_semantic:
                rejected.append({"source_id": source["source_id"], "stage": "semantic", "reason": semantic_error, "candidate_evidence_id": candidate["candidate_evidence_id"]})
            continue
        reviews_by_id = {str(review.get("candidate_evidence_id")): review for review in reviews if isinstance(review, dict)}
        for candidate in valid_for_semantic:
            review = reviews_by_id.get(candidate["candidate_evidence_id"], {})
            decision, decision_error, dimensions = _semantic_decision(candidate, review)
            semantic_rows.append({
                "candidate_evidence_id": candidate["candidate_evidence_id"],
                "source_id": source["source_id"],
                "status": review.get("status", ""),
                "reason": review.get("reason", decision_error),
                "claim_supported": review.get("claim_supported"),
                "nature_supported": review.get("nature_supported"),
                "dimension_reviews": dimensions,
                "normalization_origin": "model_response_as_received",
            })
            if decision:
                decision["recovery_origin"] = "p12r_supplemental_real_api"
                decision["extraction_run_id"] = base.name
                decision["semantic_screen_status"] = review.get("status", "")
                decision["semantic_screen_reason"] = review.get("reason", "")
                final.append(decision)
            else:
                rejected.append({"source_id": source["source_id"], "stage": "semantic", "reason": decision_error, "candidate_evidence_id": candidate["candidate_evidence_id"]})
        write_json(checkpoint, {
            "run_id": base.name,
            "status": "in_progress",
            "completed_sources": [source["source_id"]],
            "extraction_logical_call_count": extraction_calls,
            "semantic_logical_call_count": semantic_calls,
        })
    write_jsonl(p12 / "paragraph_anchor_map.jsonl", anchor_rows)
    write_jsonl(p12 / "supplemental_evidence_candidates_raw.jsonl", raw_candidates)
    write_jsonl(p12 / "supplemental_evidence_candidates_validated.jsonl", validated)
    write_jsonl(p12 / "supplemental_semantic_screen_results.jsonl", semantic_rows)
    write_jsonl(p12 / "supplemental_rejected_candidates.jsonl", rejected)
    write_csv(p12 / "supplemental_candidate_validation.csv", ["source_id", "raw_candidate_index", "candidate_evidence_id", "schema_valid", "exact_quote_matched", "deterministic_gate_passed", "reason"], validation_rows)
    write_jsonl(base / "supplemental_evidence_candidates.jsonl", final)
    write_jsonl(base / "supplemental_rejected_candidates.jsonl", rejected)
    write_json(checkpoint, {
        "run_id": base.name,
        "status": "completed",
        "completed_sources": [source["source_id"] for source in sources],
        "extraction_logical_call_count": extraction_calls,
        "semantic_logical_call_count": semantic_calls,
    })
    return {
        "raw_candidates": raw_candidates,
        "validated": validated,
        "final": final,
        "rejected": rejected,
        "semantic_rows": semantic_rows,
        "validation_rows": validation_rows,
        "extraction_calls": extraction_calls,
        "semantic_calls": semantic_calls,
        "retry_count": retry_count,
    }


def _preannotation(candidate: dict[str, Any], lineage: str) -> dict[str, Any]:
    cid = candidate["candidate_evidence_id"]
    correction_status = candidate.get("correction_status", "")
    if correction_status == "proposed_exclude":
        decision = "exclude"
        risk = "high"
        reason = "离线审核建议排除：场景背景或资源缺口未明确归因于AI。"
    elif correction_status == "historical_context_not_t1_delta":
        decision = "revise"
        risk = "medium"
        reason = "历史背景材料，不作为T1变化触发证据。"
    elif candidate.get("normalized_claim", "").startswith("据官方披露的初步结果") or candidate.get("normalized_claim", "").startswith("据参与者报告"):
        decision = "revise"
        risk = "high"
        reason = "性能主张保留来源方或参与者披露限定。"
    elif cid == "CE-T1-AA937CC0AF5E2372":
        decision = "revise"
        risk = "high"
        reason = "局部基地和部分人员的日常使用，不能推广为普遍部署。"
    elif cid == "CE-T1-D0E7B9B8CC1298AB":
        decision = "revise"
        risk = "high"
        reason = "仅支持test_event和organizational_system_fit。"
    else:
        decision = "accept"
        risk = "high" if candidate.get("evidence_nature") in {"test_result", "deployment_event", "constraint", "evaluation", "governance_event"} else "medium"
        reason = "需作者核验原文、性质、维度和范围。"
    return {
        "candidate_evidence_id": cid,
        "lineage": lineage,
        "source_id": candidate.get("source_id", ""),
        "source_locator": candidate.get("source_locator", ""),
        "proposed_decision": decision,
        "proposed_nature": candidate.get("evidence_nature", ""),
        "proposed_dimensions": json.dumps(candidate.get("coding_dimensions", []), ensure_ascii=False),
        "proposed_normalized_claim": candidate.get("normalized_claim", ""),
        "proposed_excerpt_zh": candidate.get("excerpt_zh", ""),
        "risk_level": risk,
        "risk_reason": reason,
        "proposed_review_note": candidate.get("semantic_screen_reason", "") or reason,
        "ai_preannotation": True,
        "human_reviewed": False,
        "t1_change_signal_eligible": candidate.get("t1_change_signal_eligible", True),
    }


def _write_g1_outputs(base: Path, old_corrected: list[dict[str, Any]], supplemental: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> dict[str, Any]:
    g1 = base / "G1Q_T1_PREP"
    g1.mkdir(parents=True, exist_ok=True)
    combined = []
    for candidate in old_corrected:
        combined.append((candidate, "prior_p12_v1"))
    for candidate in supplemental:
        combined.append((candidate, "p12r_supplemental_real_api"))
    preannotations = [_preannotation(candidate, lineage) for candidate, lineage in combined]
    pre_fields = list(preannotations[0]) if preannotations else ["candidate_evidence_id"]
    write_csv(base / "combined_g1_t1_ai_preannotation.csv", pre_fields, preannotations)
    template = []
    for candidate, lineage in combined:
        annotation = next(item for item in preannotations if item["candidate_evidence_id"] == candidate["candidate_evidence_id"])
        template.append({
            **annotation,
            "excerpt_original": candidate.get("excerpt_original", ""),
            "excerpt_zh": candidate.get("excerpt_zh", ""),
            "normalized_claim": candidate.get("normalized_claim", ""),
            "evidence_nature": candidate.get("evidence_nature", ""),
            "coding_dimensions": json.dumps(candidate.get("coding_dimensions", []), ensure_ascii=False),
            "author_decision": "",
            "final_decision": "",
            "final_nature": "",
            "final_dimensions": "",
            "final_normalized_claim": "",
            "human_corrected_excerpt": "",
            "final_excerpt_zh": "",
            "reviewer": "",
            "reviewed_at": "",
            "review_note": "",
        })
    template_fields = [
        "candidate_evidence_id", "lineage", "source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim",
        "evidence_nature", "coding_dimensions", "proposed_decision", "proposed_nature", "proposed_dimensions", "proposed_normalized_claim",
        "proposed_excerpt_zh", "risk_level", "risk_reason", "proposed_review_note", "ai_preannotation", "human_reviewed",
        "t1_change_signal_eligible", "author_decision", "final_decision", "final_nature", "final_dimensions", "final_normalized_claim",
        "human_corrected_excerpt", "final_excerpt_zh", "reviewer", "reviewed_at", "review_note",
    ]
    write_csv(base / "combined_g1_t1_author_decision_template.csv", template_fields, template)
    write_csv(base / "before_after_correction_log.csv", list(corrections[0]) if corrections else ["candidate_evidence_id"], corrections)
    high = [row for row in preannotations if row["risk_level"] == "high"]
    write_csv(base / "g1_t1_high_priority.csv", pre_fields, high)
    summary = (
        "# G1-T1作者终审包准备摘要\n\n"
        f"本轮保留旧P12的30条候选并完成离线口径检查，新增P12R最终候选{len(supplemental)}条，合并审核集合{len(combined)}条。\n\n"
        f"AI建议分布：{dict(Counter(row['proposed_decision'] for row in preannotations))}；高风险{len(high)}条。作者实际审核数为0；所有author_decision、final_*、reviewer、reviewed_at和review_note均为空，human_reviewed均为false。\n\n"
        "本包不写入正式Evidence，不执行正式G1-T1，不冻结T1快照，不进行影响定位或观点更新。"
    )
    (base / "g1_t1_author_signoff_summary.md").write_text(summary, encoding="utf-8")
    return {"combined": combined, "preannotations": preannotations, "high_count": len(high)}


def _write_balance_and_signal(base: Path, combined: list[tuple[dict[str, Any], str]], sources: list[dict[str, Any]]) -> None:
    institution = Counter()
    grade = Counter()
    source_type = Counter()
    dimension = Counter()
    for candidate, _ in combined:
        publisher = candidate.get("publisher", "")
        if "DARPA" in publisher:
            institution["DARPA"] += 1
        elif "Space Force" in publisher:
            institution["U.S. Space Force"] += 1
        elif "Air Force" in publisher or "Air Force" in candidate.get("title", ""):
            institution["U.S. Air Force/DAF"] += 1
        elif "Bioengineering" in publisher or "peer-reviewed" in publisher:
            institution["peer-reviewed research"] += 1
        else:
            institution[publisher or "unknown"] += 1
        grade[candidate.get("source_grade", "")] += 1
        source_type["independent_review_research" if "peer-reviewed" in publisher else "official"] += 1
        for item in candidate.get("coding_dimensions", []):
            dimension[item] += 1
    balance = [
        "# T1来源平衡报告",
        "",
        f"合并候选共{len(combined)}条；补充来源3份，其中可核验全文3份。",
        "",
        f"来源机构分布：{dict(institution)}。",
        f"来源等级分布：{dict(grade)}。",
        f"官方来源与独立评议来源：{dict(source_type)}。",
        f"编码维度覆盖：{dict(dimension)}。",
        "",
        "新增来源覆盖：DARPA（战略/作战化AI约束与竞赛测试）、PMC同行评议研究（独立应用条件）；既有全文主要来自空军、空军部、太空军和DARPA。",
        "",
        "范围限制：材料以官方公开叙述为主，不能代表所有军种、所有任务或未公开项目；官方披露的试验、部署和性能不等同于独立验证或全军普遍成熟度；同行评议论文聚焦作战医疗，不能直接推广到美国军用AI整体。",
    ]
    (base / "source_balance_report.md").write_text("\n".join(balance) + "\n", encoding="utf-8")
    signals = [
        "# T1变化信号预览（证据层）",
        "",
        "本文件只按T1候选证据层整理，不比较T0假设，不更新观点，也不执行影响定位。",
        "",
        "## 可能支持组织化、平台化或跨组织整合的信号",
    ]
    for candidate, lineage in combined:
        text = f"{candidate.get('normalized_claim','')} {candidate.get('excerpt_original','')}".lower()
        if any(key in text for key in ("platform", "integration", "integrat", "enterprise", "cross", "architecture", "ecosystem", "协同", "整合")):
            signals.append(f"- {candidate.get('candidate_evidence_id')}: {candidate.get('normalized_claim','')}（{candidate.get('source_locator','')}）")
    signals.extend(["", "## 仍处于试验、局部应用或官方自报阶段的限制信号"])
    for candidate, lineage in combined:
        if candidate.get("evidence_nature") in {"goal", "test_event", "test_result", "deployment_event", "evaluation", "constraint"}:
            signals.append(f"- {candidate.get('candidate_evidence_id')}: nature={candidate.get('evidence_nature')}；{candidate.get('normalized_claim','')}（{candidate.get('source_locator','')}）")
    signals.extend(["", "## 可能触发后续局部重评的证据ID", "- 仅列入T1候选ID供后续人工审核；本阶段不作影响定位："])
    for candidate, _ in combined:
        if candidate.get("lineage") == "p12r_supplemental_real_api" or candidate.get("evidence_nature") in {"deployment_event", "test_result", "constraint", "governance_event"}:
            signals.append(f"- {candidate.get('candidate_evidence_id')}")
    signals.extend(["", "## 不应直接触发观点更新的材料", "- 标记为historical_context_not_t1_delta的CE-T1-7DE738404CD33807。", "- 仅为一般背景、场景参数或范围不足的候选，须经G1判断后方可使用。"])
    (base / "t1_change_signal_preview.md").write_text("\n".join(signals) + "\n", encoding="utf-8")


def run_recovery(run_id: str) -> dict[str, Any]:
    base = ROOT / "outputs" / run_id
    if base.exists():
        raise RuntimeError(f"run directory already exists: {run_id}")
    base.mkdir(parents=True)
    before_hashes = file_hashes(OLD_RUN)
    db_before = _read_only_db_counts()
    old_manifest = json.loads((OLD_RUN / "p11_p12_manifest.json").read_text(encoding="utf-8"))
    old_candidates = _load_jsonl(OLD_RUN / "P12_T1_EXTRACTION" / "t1_evidence_candidates.jsonl")
    old_corrected, correction_log = _apply_old_corrections(old_candidates)
    p11_dir = base / "P11R_T1_COLLECTION"
    p11_dir.mkdir(parents=True, exist_ok=True)
    source_rows = []
    for item in SUPPLEMENTAL_SOURCES:
        source_rows.append(_save_supplemental_source(base, item))
    write_csv(base / "supplemental_source_inventory.csv", [
        "case_id", "source_id", "verified_title", "publisher", "source_grade", "category", "document_date", "doi", "pmcid",
        "canonical_url", "capture_url", "resolved_url", "capture_type", "raw_path", "text_path", "raw_sha256", "text_sha256",
        "file_size", "full_text_status", "selection_status", "temporal_status", "public_release_verified", "duplicate_status", "reason",
    ], source_rows)
    write_csv(p11_dir / "supplemental_source_inventory.csv", [
        "case_id", "source_id", "verified_title", "publisher", "source_grade", "category", "document_date", "doi", "pmcid",
        "canonical_url", "capture_url", "resolved_url", "capture_type", "raw_path", "text_path", "raw_sha256", "text_sha256",
        "file_size", "full_text_status", "selection_status", "temporal_status", "public_release_verified", "duplicate_status", "reason",
    ], source_rows)
    write_csv(base / "supplemental_capture_log.csv", ["source_id", "canonical_url", "capture_url", "capture_type", "document_date", "raw_sha256", "text_sha256", "status", "notes"], [
        {"source_id": row["source_id"], "canonical_url": row["canonical_url"], "capture_url": row["capture_url"], "capture_type": row["capture_type"], "document_date": row["document_date"], "raw_sha256": row["raw_sha256"], "text_sha256": row["text_sha256"], "status": row["full_text_status"], "notes": row["publication_date_verification"]} for row in source_rows
    ])
    supplemental = _supplemental_p12(base, source_rows, old_candidates)
    g1 = _write_g1_outputs(base, old_corrected, supplemental["final"], correction_log)
    _write_balance_and_signal(base, g1["combined"], source_rows)
    write_jsonl(base / "supplemental_evidence_candidates.jsonl", supplemental["final"])
    write_jsonl(base / "supplemental_rejected_candidates.jsonl", supplemental["rejected"])
    after_hashes = file_hashes(OLD_RUN)
    db_after = _read_only_db_counts()
    if before_hashes != after_hashes:
        raise RuntimeError("historical P11/P12 run changed")
    if db_before != db_after:
        raise RuntimeError("database changed during P11R/P12R/G1Q")
    nature_counts = Counter(c.get("evidence_nature", "") for c in supplemental["final"])
    dimension_counts = Counter(d for c in supplemental["final"] for d in c.get("coding_dimensions", []))
    no_key = all("sk-sp-" not in path.read_text(encoding="utf-8", errors="ignore") for path in base.rglob("*") if path.is_file())
    manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "P11R_P12R_G1Q_T1",
        "source_run_id": OLD_RUN_ID,
        "old_run_manifest_sha256": sha256_bytes((OLD_RUN / "p11_p12_manifest.json").read_bytes()),
        "old_candidate_count": len(old_candidates),
        "old_candidates_reprocessed": False,
        "old_candidates_offline_correction_count": len(correction_log),
        "supplemental_source_count": len(source_rows),
        "supplemental_full_text_source_count": len([row for row in source_rows if row["full_text_status"] == "verified_full_text"]),
        "p12r_extraction_logical_call_count": supplemental["extraction_calls"],
        "p12r_semantic_logical_call_count": supplemental["semantic_calls"],
        "p12r_model_call_count": supplemental["extraction_calls"] + supplemental["semantic_calls"],
        "physical_retry_count": supplemental["retry_count"],
        "supplemental_raw_candidate_count": len(supplemental["raw_candidates"]),
        "supplemental_deterministic_gate_pass_count": len(supplemental["validated"]),
        "supplemental_final_candidate_count": len(supplemental["final"]),
        "supplemental_rejected_count": len(supplemental["rejected"]),
        "rejected_reason_counts": dict(Counter(item.get("reason", "") for item in supplemental["rejected"])),
        "supplemental_candidates_by_nature": dict(nature_counts),
        "supplemental_candidates_by_dimension": dict(dimension_counts),
        "combined_g1_candidate_count": len(g1["combined"]),
        "g1_high_priority_count": g1["high_count"],
        "author_actual_review_count": 0,
        "formal_g1_t1_executed": False,
        "evidence_inserted": 0,
        "t1_snapshot_frozen": False,
        "impact_localization_executed": False,
        "viewpoint_update_executed": False,
        "t1_read": True,
        "api_key_written": not no_key,
        "api_key_written_verified": no_key,
        "historical_input_unchanged": True,
        "database_counts_before": db_before,
        "database_counts_after": db_after,
        "input_hashes": {
            "old_run_manifest": sha256_bytes((OLD_RUN / "p11_p12_manifest.json").read_bytes()),
            "old_candidates": sha256_bytes((OLD_RUN / "P12_T1_EXTRACTION" / "t1_evidence_candidates.jsonl").read_bytes()),
            "old_author_template": sha256_bytes((OLD_RUN / "G1_T1_PREP" / "g1_t1_author_decision_template.csv").read_bytes()),
            "supplemental_sources": {row["source_id"]: row["raw_sha256"] for row in source_rows},
        },
        "code_hashes": {
            "src/t1_recovery.py": sha256_bytes((ROOT / "src" / "t1_recovery.py").read_bytes()),
        },
        "output_file_hashes": file_hashes(base),
    }
    write_json(base / "p11r_p12r_manifest.json", manifest)
    return {"run_id": run_id, "manifest": manifest}
