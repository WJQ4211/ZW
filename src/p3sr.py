"""Offline recovery of P3S-v5 semantic responses for G1 preparation.

This module never calls a model, writes SQLite, reads T1, or mutates the
source run.  It only adapts the representation of ``dimension_reviews`` and
replays the existing semantic gate over the v5 candidates.
"""

from __future__ import annotations

import csv
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .p3ar import sha256_file
from .p3s import (
    CASE_ID,
    _apply_revision,
    _deterministic_validation,
    _load_canonical,
    _load_p2s_sources,
    _parse_json,
    _parse_semantic_response,
    _prepare_document,
)
from .schemas import model_dump


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN_ID = "P3S-20260826T131735Z-supplement-realapi-v5"
SOURCE_RUN_DIR = ROOT / "outputs" / SOURCE_RUN_ID
SOURCE_IDS = ("T0-SP-001", "T0-TD-001", "T0-TD-002", "T0-TD-003", "T0-TD-005")
NEGATIVE_DIMENSION_VALUES = {
    "",
    "unsupported",
    "not supported",
    "no direct support",
    "不支持",
    "无直接支持",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values), encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _dimension_supported(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return bool(normalized) and normalized not in NEGATIVE_DIMENSION_VALUES and not normalized.startswith("not supported") and not normalized.startswith("no direct support")


def normalize_dimension_reviews(
    semantic_item: dict[str, Any],
    declared_dimensions: list[str],
    excerpt_original: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Convert only the dimension-review representation, retaining before/after."""
    before = copy.deepcopy(semantic_item)
    raw_reviews = semantic_item.get("dimension_reviews")
    if not isinstance(raw_reviews, dict):
        raise ValueError("dimension_reviews_must_be_object_for_v5_recovery")
    after = copy.deepcopy(semantic_item)
    converted: list[dict[str, Any]] = []
    missing: list[str] = []
    ignored_extra = sorted(set(raw_reviews) - set(declared_dimensions))
    for dimension in declared_dimensions:
        if dimension not in raw_reviews:
            missing.append(dimension)
            converted.append(
                {
                    "dimension": dimension,
                    "supported": False,
                    "supporting_span": excerpt_original,
                    "reason": "missing_declared_dimension",
                }
            )
            continue
        raw_value = raw_reviews[dimension]
        converted.append(
            {
                "dimension": dimension,
                "supported": _dimension_supported(raw_value),
                "supporting_span": excerpt_original,
                "reason": raw_value if isinstance(raw_value, str) else str(raw_value),
            }
        )
    after["dimension_reviews"] = converted
    reason = "converted_object_to_declared_dimension_array"
    if missing:
        reason += ";missing_declared_dimensions_marked_unsupported"
    if ignored_extra:
        reason += ";extra_dimensions_ignored"
    return before, after, reason


def _gate_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make null no-revision values consumable by the existing gate interface.

    The recorded before/after response is not changed.  ``null`` means that
    the model supplied no allowed revision, so the replay view treats it as
    an empty revision object solely for gate validation.
    """
    view = copy.deepcopy(items)
    for item in view:
        if item.get("revised_fields") is None:
            item["revised_fields"] = {}
    return view


def _risk_for(record: dict[str, Any]) -> tuple[str, str]:
    text = f"{record.get('excerpt_original', '')} {record.get('normalized_claim', '')}"
    reasons: list[str] = []
    if record.get("evidence_nature") == "test_event" and re.search(r"\b(aims?|seeks?|will|plan(?:ned)?|expected|intend|goal)\b|计划|预计|将", text, re.I):
        reasons.append("未来计划或意图被标为test_event")
    if re.search(r"\b(said|stated|according to|director|program manager|program lead|officials?)\b|负责人|项目主管|据.*表示", text, re.I) and record.get("evidence_nature") in {"test_result", "deployment_event", "evaluation"}:
        reasons.append("项目负责人或来源方自述结果，需核验独立支撑")
    if len(record.get("excerpt_original", "").split()) >= 65:
        reasons.append("长摘录可能含多个事实，需检查原子性")
    if reasons:
        return "high", "；".join(reasons)
    if record.get("evidence_nature") in {"test_result", "deployment_event", "evaluation", "constraint"}:
        return "medium", "性质或语义边界需要作者重点确认"
    return "low", "未命中自动高风险规则，仍需作者G1审核"


def _author_rows(final_candidates: list[dict[str, Any]], sources: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in final_candidates:
        source = sources[record["source_id"]]
        risk_level, risk_reason = _risk_for(record)
        rows.append(
            {
                "evidence_id": record["evidence_id"],
                "source_id": record["source_id"],
                "title": source.get("title", ""),
                "publisher": source.get("publisher", ""),
                "source_grade": source.get("source_grade", ""),
                "published_at": source.get("published_at", ""),
                "canonical_url": source.get("canonical_url", ""),
                "source_locator": record["source_locator"],
                "excerpt_original": record["excerpt_original"],
                "excerpt_zh": record["excerpt_zh"],
                "normalized_claim": record["normalized_claim"],
                "evidence_nature": record["evidence_nature"],
                "coding_dimensions": json.dumps(record["coding_dimensions"], ensure_ascii=False),
                "possible_corroboration_group": record.get("possible_corroboration_group", ""),
                "semantic_status": record.get("semantic_screen_status", ""),
                "semantic_reason": record.get("semantic_screen_reason", ""),
                "dimension_reviews": json.dumps(record.get("dimension_reviews", []), ensure_ascii=False),
                "ai_recommendation": "accept",
                "risk_level": risk_level,
                "risk_reason": risk_reason,
                "author_decision": "",
                "human_reviewed": "false",
                "final_decision": "",
                "final_nature": "",
                "final_dimensions": "",
                "human_corrected_excerpt": "",
                "reviewer": "",
                "reviewed_at": "",
                "review_note": "",
            }
        )
    return rows


def recover(run_id: str, out_dir: Path) -> dict[str, Any]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError("P3S-v5-R output directory must be new and empty")
    out_dir.mkdir(parents=True, exist_ok=False)
    source_hashes_before = _tree_hashes(SOURCE_RUN_DIR)
    validated = _read_jsonl(SOURCE_RUN_DIR / "evidence_candidates_p3s_validated.jsonl")
    if len(validated) != 28:
        raise RuntimeError(f"deterministic_input_candidates_expected_28_got_{len(validated)}")
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in validated:
        by_source[record["source_id"]].append(record)
    sources = _load_p2s_sources()
    documents = {source_id: _prepare_document(sources[source_id]) for source_id in SOURCE_IDS}
    canonical, _ = _load_canonical()
    semantic_normalizations: list[dict[str, Any]] = []
    normalized_semantic_by_source: dict[str, list[dict[str, Any]]] = {}
    null_revision_count = 0
    for source_id in SOURCE_IDS:
        semantic_path = SOURCE_RUN_DIR / "raw_model_outputs" / "semantic" / f"{source_id}-semantic-001.json"
        envelope = json.loads(semantic_path.read_text(encoding="utf-8"))
        semantic_items = _parse_json(envelope["choices"][0]["message"]["content"])
        normalized_items: list[dict[str, Any]] = []
        candidates = by_source[source_id]
        if len(semantic_items) != len(candidates):
            raise RuntimeError(f"semantic_candidate_alignment_mismatch_{source_id}")
        for item in semantic_items:
            index = item.get("candidate_index")
            if not isinstance(index, int) or index < 0 or index >= len(candidates):
                raise RuntimeError(f"semantic_candidate_index_invalid_{source_id}")
            candidate = candidates[index]
            before, after, reason = normalize_dimension_reviews(item, candidate["coding_dimensions"], candidate["excerpt_original"])
            if item.get("revised_fields") is None:
                null_revision_count += 1
            normalized_items.append(after)
            semantic_normalizations.append(
                {
                    "source_id": source_id,
                    "candidate_index": index,
                    "evidence_id": candidate["evidence_id"],
                    "before": before,
                    "after": after,
                    "conversion_reason": reason,
                    "review_origin": "offline_schema_representation_normalization",
                    "model_generated": False,
                    "human_reviewed": False,
                }
            )
        normalized_semantic_by_source[source_id] = normalized_items

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    deterministic_seen: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unsupported_dimension_removal_count = 0
    semantic_status_counts: Counter[str] = Counter()
    rejection_reason_counts: Counter[str] = Counter()
    for source_id in SOURCE_IDS:
        document = documents[source_id]
        segment_map = {segment.source_locator: segment for segment in document.segments}
        gate_items = normalized_semantic_by_source[source_id]
        semantic_map, semantic_error = _parse_semantic_response(_gate_view(gate_items))
        if semantic_error:
            raise RuntimeError(f"normalized_semantic_response_invalid_{source_id}_{semantic_error}")
        for index, candidate in enumerate(by_source[source_id]):
            review = semantic_map.get(index)
            semantic_status = review.get("status") if review else "missing"
            semantic_status_counts[semantic_status] += 1
            normalization = next(item for item in semantic_normalizations if item["source_id"] == source_id and item["candidate_index"] == index)
            if review is None:
                rejected.append({"source_id": source_id, "candidate_index": index, "candidate": candidate, "rejection_reasons": ["semantic_review_missing"], "normalization": normalization})
                rejection_reason_counts["semantic_review_missing"] += 1
                continue
            if review.get("status") == "reject" or review.get("claim_supported") is not True or review.get("nature_supported") is not True:
                rejected.append({"source_id": source_id, "candidate_index": index, "candidate": candidate, "rejection_reasons": ["semantic_claim_nature_or_relevance_reject"], "semantic_review": review, "normalization": normalization})
                rejection_reason_counts["semantic_claim_nature_or_relevance_reject"] += 1
                continue
            try:
                revised_candidate, applied_revision_fields = _apply_revision(_candidate_from_record(candidate), review)
            except Exception as exc:
                rejected.append({"source_id": source_id, "candidate_index": index, "candidate": candidate, "rejection_reasons": ["semantic_revision_invalid", str(exc)], "semantic_review": review, "normalization": normalization})
                rejection_reason_counts["semantic_revision_invalid"] += 1
                continue
            candidate_for_gate = dict(candidate)
            revised_payload = model_dump(revised_candidate)
            for field in ("normalized_claim", "excerpt_zh", "evidence_nature", "coding_dimensions", "topics"):
                candidate_for_gate[field] = revised_payload[field]
            if applied_revision_fields:
                normalization.setdefault("gate_modifications", []).extend(
                    {"field": field, "reason": "existing_gate_revision_applied"}
                    for field in applied_revision_fields
                )
            declared = list(candidate_for_gate["coding_dimensions"])
            dimension_reviews = {item["dimension"]: item for item in review.get("dimension_reviews", [])}
            unsupported = [dimension for dimension in declared if dimension_reviews.get(dimension, {}).get("supported") is not True]
            if unsupported:
                unsupported_dimension_removal_count += len(unsupported)
                candidate_for_gate["coding_dimensions"] = [dimension for dimension in declared if dimension not in unsupported]
            if not candidate_for_gate["coding_dimensions"]:
                rejected.append({"source_id": source_id, "candidate_index": index, "candidate": candidate, "rejection_reasons": ["all_coding_dimensions_unsupported"], "semantic_review": review, "normalization": normalization})
                rejection_reason_counts["all_coding_dimensions_unsupported"] += 1
                continue
            if unsupported:
                normalization.setdefault("gate_modifications", []).append({"field": "coding_dimensions", "removed": unsupported, "reason": "unsupported_dimensions_removed_after_normalization"})
            reasons = _deterministic_validation(
                _candidate_from_record(candidate_for_gate),
                candidate_for_gate.get("possible_corroboration_group", ""),
                document,
                segment_map,
                canonical,
                deterministic_seen[source_id],
            )
            if reasons:
                rejected.append({"source_id": source_id, "candidate_index": index, "candidate": candidate_for_gate, "rejection_reasons": reasons, "semantic_review": review, "normalization": normalization})
                for reason in reasons:
                    rejection_reason_counts[reason] += 1
                continue
            final = dict(candidate_for_gate)
            final.update(
                {
                    "case_id": CASE_ID,
                    "semantic_screen_status": review.get("status", ""),
                    "semantic_screen_reason": review.get("reason", ""),
                    "dimension_reviews": review.get("dimension_reviews", []),
                    "semantic_screening_mode": "same_model_separate_call_semantic_screening",
                    "recovery_origin": "offline_schema_representation_normalization",
                    "recovery_run_id": run_id,
                    "human_reviewed": False,
                    "formal_evidence_db_rows_added": 0,
                }
            )
            accepted.append(final)
            deterministic_seen[source_id].append(final)
            lineage.append(
                {
                    "recovery_run_id": run_id,
                    "source_run_id": SOURCE_RUN_ID,
                    "evidence_id": final["evidence_id"],
                    "source_id": source_id,
                    "source_locator": final["source_locator"],
                    "semantic_status": review.get("status"),
                    "recovery_origin": "offline_schema_representation_normalization",
                    "parent_candidate_evidence_id": final["evidence_id"],
                }
            )

    accepted.sort(key=lambda value: value["evidence_id"])
    rejected.sort(key=lambda value: (value.get("source_id", ""), value.get("candidate_index", -1)))
    if semantic_status_counts != Counter({"accept": 27, "reject": 1}):
        report = {"semantic_status_counts": dict(semantic_status_counts), "expected": {"accept": 27, "reject": 1}}
        _write_json(out_dir / "p3sr_mismatch_report.json", report)
        raise RuntimeError(f"semantic_replay_count_mismatch:{report}")

    source_hashes_after = _tree_hashes(SOURCE_RUN_DIR)
    normalization_fieldnames = ["source_id", "candidate_index", "evidence_id", "before", "after", "conversion_reason", "review_origin", "model_generated", "human_reviewed", "gate_modifications"]
    _write_jsonl(out_dir / "semantic_response_normalization.jsonl", semantic_normalizations)
    _write_csv(
        out_dir / "semantic_before_after.csv",
        normalization_fieldnames,
        [
            {
                **{key: item.get(key) for key in normalization_fieldnames if key not in {"before", "after", "gate_modifications"}},
                "before": json.dumps(item["before"], ensure_ascii=False),
                "after": json.dumps(item["after"], ensure_ascii=False),
                "gate_modifications": json.dumps(item.get("gate_modifications", []), ensure_ascii=False),
            }
            for item in semantic_normalizations
        ],
    )
    _write_jsonl(out_dir / "evidence_candidates_p3sr_final.jsonl", accepted)
    _write_jsonl(out_dir / "rejected_candidates_p3sr.jsonl", rejected)
    _write_jsonl(out_dir / "recovery_lineage.jsonl", lineage)
    author_rows = _author_rows(accepted, sources)
    author_fields = list(author_rows[0]) if author_rows else []
    _write_csv(out_dir / "g1_supplement_ai_preannotation.csv", author_fields, author_rows)
    _write_csv(out_dir / "g1_supplement_author_decision_template.csv", author_fields, author_rows)

    quality = {
        "source_count": len(SOURCE_IDS),
        "deterministic_input_candidates": len(validated),
        "semantic_accept_count": semantic_status_counts["accept"],
        "semantic_reject_count": semantic_status_counts["reject"],
        "recovered_g1_candidate_count": len(accepted),
        "unsupported_dimension_removal_count": unsupported_dimension_removal_count,
        "revised_fields_null_treated_as_empty_noop_count": null_revision_count,
        "rejection_reason_counts": dict(rejection_reason_counts),
        "candidates_by_source": dict(Counter(item["source_id"] for item in accepted)),
        "candidates_by_evidence_nature": dict(Counter(item["evidence_nature"] for item in accepted)),
        "high_risk_count": sum(1 for row in author_rows if row["risk_level"] == "high"),
        "author_actual_review_count": 0,
    }
    _write_json(out_dir / "p3sr_quality_report.json", quality)
    (out_dir / "p3sr_quality_report.md").write_text(
        "# P3S-v5-R质量报告\n\n"
        f"- 确定性输入候选：{len(validated)}\n"
        f"- 语义accept：{semantic_status_counts['accept']}；reject：{semantic_status_counts['reject']}\n"
        f"- 进入G1准备集合：{len(accepted)}\n"
        f"- 规范化后不受支持维度移除：{unsupported_dimension_removal_count}\n"
        f"- revised_fields为空值按无修订处理：{null_revision_count}\n"
        f"- 高风险项目：{quality['high_risk_count']}\n"
        "\n本运行只做离线Schema表示规范化和既有语义门禁重放，不调用模型、不写入Evidence、不执行G1/P4、不读取T1。\n",
        encoding="utf-8",
    )
    manifest = {
        "run_id": run_id,
        "source_run_id": SOURCE_RUN_ID,
        "recovery_mode": "offline_schema_representation_normalization",
        "source_run_file_hashes_unchanged": source_hashes_before == source_hashes_after,
        "source_run_manifest_sha256": sha256_file(SOURCE_RUN_DIR / "p3s_manifest.json"),
        "new_model_api_calls": 0,
        "model_api_calls": 0,
        "deterministic_input_candidates": len(validated),
        "semantic_accept_count": semantic_status_counts["accept"],
        "semantic_reject_count": semantic_status_counts["reject"],
        "recovered_g1_candidate_count": len(accepted),
        "estimated_a_grade_ratio_if_all_accepted": (31 + len(accepted)) / (60 + len(accepted)),
        "minimum_g1_acceptances_needed": 13,
        "revised_fields_null_treated_as_empty_noop_count": null_revision_count,
        "evidence_inserted": 0,
        "formal_g1_supplement_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "p5_executed": False,
        "t1_read": False,
        "api_key_written": False,
        "author_actual_review_count": 0,
        "hypothesis_inserted": 0,
        "semantic_status_counts": dict(semantic_status_counts),
        "rejection_reason_counts": dict(rejection_reason_counts),
        "output_file_hashes": {},
    }
    manifest["output_file_hashes"] = {
        str(path.relative_to(out_dir)).replace("\\", "/"): sha256_file(path)
        for path in sorted(out_dir.rglob("*"))
        if path.is_file() and path.name != "p3sr_manifest.json"
    }
    _write_json(out_dir / "p3sr_manifest.json", manifest)
    return manifest


def _candidate_from_record(record: dict[str, Any]):
    from .schemas import EvidenceCandidate, model_validate

    return model_validate(EvidenceCandidate, {key: record[key] for key in ("source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "coding_dimensions", "evidence_nature", "topics")})
