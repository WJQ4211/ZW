"""Offline P3b-preflight quality repair for the historical P3a-R pilot.

P3Q reads the immutable P3AR-v6 prompt packets and provider responses. It
never calls a model, writes the Evidence table, reads T1, or changes v6.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .p3ar import (
    CASE_ID,
    CODING_DIMENSION_DEFINITIONS,
    ROOT,
    SourceSegment,
    extract_pdf_segments,
    hash_paths,
    inject_metadata,
    lexical_auto_rejection,
    parse_candidate,
    relative_path,
    resolve_locator,
    sha256_file,
    write_csv,
    write_json,
    write_jsonl,
)


V6_RUN_ID = "P3AR-20260826T010914-real-api-v6"
V6_DIR = ROOT / "outputs" / V6_RUN_ID
P3A_AUDIT = ROOT / "outputs/P3A-20260825T161738Z-pilot/p3a_r_audit_note.json"
SOURCE_IDS = ("T0-SP-007", "T0-TD-008")
ALLOWED_NATURES = {"goal", "resource_input", "test_event", "test_result", "deployment_event", "governance_event", "constraint", "evaluation"}


def _decode_model_json(content: str) -> Any:
    value = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        value = fenced.group(1).strip()
    return json.loads(value)


def _load_response_candidates(path: Path) -> tuple[list[Any], str | None]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        content = envelope["choices"][0]["message"]["content"]
        value = _decode_model_json(content)
        if not isinstance(value, list):
            return [], "model_content_not_array"
        return value, None
    except (OSError, UnicodeDecodeError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return [], "model_response_not_parseable"


def _load_semantic_reviews(path: Path) -> dict[int, dict[str, Any]]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        value = _decode_model_json(envelope["choices"][0]["message"]["content"])
    except (OSError, UnicodeDecodeError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, list):
        return {}
    return {
        item["candidate_index"]: item
        for item in value
        if isinstance(item, dict) and isinstance(item.get("candidate_index"), int)
    }


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _capitalized_entity_warnings(candidate: Any, excerpt: str) -> list[str]:
    claim = candidate.normalized_claim
    entities = sorted(set(re.findall(r"\b[A-Z][A-Za-z0-9-]{2,}\b", claim)))
    missing = [entity for entity in entities if entity.casefold() not in excerpt.casefold()]
    return ["capitalized_entity_needs_semantic_review:" + entity for entity in missing]


def _plan_as_result(candidate: Any, excerpt: str) -> bool:
    if candidate.evidence_nature not in {"test_result", "deployment_event"}:
        return False
    return bool(re.search(r"\b(plan|planned|goal|aim|intend|will|should|expected)\b", excerpt, flags=re.IGNORECASE))


def _dimension_support(dimension: str, excerpt: str) -> tuple[bool, str, str]:
    """Conservative, offline dimension screen used for the preview only."""
    text = excerpt.casefold()
    patterns: dict[str, tuple[str, ...]] = {
        "integration_level": ("integrat", "in conjunction", "cross-platform", "cross-component", "system", "platform", "workflow"),
        "application_maturity": ("exercise", "test", "pilot", "deploy", "operational", "field", "research", "procurement", "use"),
        "human_machine_authority": ("watchstander", "watch officer", "operator", "decision", "in the loop", "supervised", "authority", "human"),
        "engineering_resource_conditions": ("data", "model", "compute", "interface", "budget", "training", "documentation", "resource", "tool"),
        "trust_governance_constraints": ("safety", "security", "risk", "reliab", "test and evaluation", "ethic", "governance", "standard"),
        "organizational_system_fit": ("command center", "coordination", "workflow", "organization", "component", "watch officer", "task force", "destroyer"),
    }
    matches = [token for token in patterns.get(dimension, ()) if token in text]
    if dimension == "human_machine_authority" and not matches:
        return False, "", "System coordination or information transmission alone does not establish an authority relationship."
    if matches:
        return True, excerpt, "The excerpt contains a direct span relevant to the declared dimension: " + ", ".join(matches)
    return False, "", "No direct supporting span for this declared dimension was found in the excerpt."


def _dimension_reviews(candidate: Any) -> tuple[list[dict[str, Any]], list[str]]:
    reviews: list[dict[str, Any]] = []
    removed: list[str] = []
    for dimension in candidate.coding_dimensions:
        supported, span, reason = _dimension_support(dimension, candidate.excerpt_original)
        reviews.append(
            {
                "dimension": dimension,
                "supported": supported,
                "supporting_span": span,
                "reason": reason,
                "review_origin": "offline_keyword_heuristic",
                "model_generated": False,
                "human_reviewed": False,
            }
        )
        if not supported:
            removed.append(dimension)
    return reviews, removed


def _semantic_screen(review: dict[str, Any] | None, candidate: Any) -> tuple[str, str, list[dict[str, Any]], list[str]]:
    if not review:
        return "pending_human_review", "same_model_separate_call_semantic_screening response missing", [], list(candidate.coding_dimensions)
    status = str(review.get("status", "reject"))
    if status != "pass" or review.get("claim_supported") is not True or review.get("nature_supported") is not True:
        return "reject", str(review.get("reason", "same-model semantic screen did not pass")), [], list(candidate.coding_dimensions)
    dimension_reviews, removed = _dimension_reviews(candidate)
    if not dimension_reviews:
        return "pending_human_review", "No declared dimensions were available for dimension-level screening.", dimension_reviews, removed
    if len(removed) == len(dimension_reviews):
        return "pending_human_review", "All declared dimensions lacked a directly supporting span.", dimension_reviews, removed
    return "pass", "Same-model separate-call screen passed; unsupported dimensions were removed in the P3Q preview.", dimension_reviews, removed


def deterministic_gate_reasons(candidate: Any, segment: SourceSegment | None, seen_excerpts: set[str] | None = None) -> list[str]:
    """Return language-neutral hard-gate failures for a single candidate."""
    reasons: list[str] = []
    if candidate is None:
        return ["schema_invalid"]
    if candidate.source_id not in SOURCE_IDS:
        reasons.append("source_id_out_of_scope")
    if segment is None:
        reasons.append("locator_not_found")
    else:
        if _normalized(candidate.excerpt_original) not in _normalized(segment.text):
            reasons.append("excerpt_not_found_at_specified_locator")
    if not candidate.excerpt_zh.strip():
        reasons.append("excerpt_zh_empty")
    if candidate.evidence_nature not in ALLOWED_NATURES:
        reasons.append("evidence_nature_invalid")
    if _plan_as_result(candidate, candidate.excerpt_original):
        reasons.append("plan_or_intent_coded_as_result")
    lexical_reason = lexical_auto_rejection(candidate, segment) if segment is not None else None
    if lexical_reason:
        reasons.append(lexical_reason)
    if seen_excerpts is not None:
        normalized_excerpt = _normalized(candidate.excerpt_original).casefold()
        if normalized_excerpt in seen_excerpts:
            reasons.append("same_source_duplicate")
    return reasons


def immutable_v6_hashes() -> dict[str, str]:
    if not V6_DIR.exists():
        raise FileNotFoundError(f"Immutable P3AR input not found: {V6_DIR}")
    return {relative_path(path): sha256_file(path) for path in sorted(V6_DIR.rglob("*")) if path.is_file()}


def load_v6_context() -> tuple[dict[str, Any], dict[str, dict[str, SourceSegment]], dict[str, dict[str, str]]]:
    manifest = json.loads((V6_DIR / "p3ar_manifest.json").read_text(encoding="utf-8"))
    packets: dict[str, dict[str, SourceSegment]] = {}
    for packet_path in sorted((V6_DIR / "prompt_packets/extraction").glob("*.json")):
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packets[packet["batch_id"]] = {
            item["source_locator"]: SourceSegment(**item)
            for item in packet["segments"]
        }
    source_rows = {}
    with (ROOT / "data/curated/source_manifest_t0_p2.csv").open(encoding="utf-8-sig", newline="") as handle:
        import csv

        for row in csv.DictReader(handle):
            if row["source_id"] in SOURCE_IDS:
                source_rows[row["source_id"]] = row
    return manifest, packets, source_rows


def run_p3q(run_id: str, run_dir: Path) -> dict[str, Any]:
    before_hashes = immutable_v6_hashes()
    manifest, packets, source_rows = load_v6_context()
    pdf_segments, pdf_info = extract_pdf_segments(ROOT / "data/staging/t0_candidates/manual/2024-06-RAI-STRATEGY-IMPLEMENTATION-PATHWAY.pdf", "T0-SP-007")
    pdf_segment_map = {segment.source_locator: segment for segment in pdf_segments}
    pdf_info["machine_extracted_excerpt_policy"] = "preserve_raw; human_corrected_excerpt_only_after_G1"
    p3a_audit = json.loads(P3A_AUDIT.read_text(encoding="utf-8"))
    all_previews: list[dict[str, Any]] = []
    final_previews: list[dict[str, Any]] = []
    hard_rejection_counts: Counter[str] = Counter()
    warnings_count = 0
    raw_count = 0
    schema_valid_count = 0
    exact_count = 0
    gate_count = 0
    inherited_claim_nature_pass_count = 0
    offline_dimension_screen_pass_count = 0
    seen_by_source: dict[str, set[str]] = {source_id: set() for source_id in SOURCE_IDS}
    review_paths = {path.stem: path for path in (V6_DIR / "raw_semantic_reviews").glob("*.json")}

    for raw_path in sorted((V6_DIR / "raw_model_outputs").glob("*.json")):
        batch_id = raw_path.stem
        source_id = "T0-SP-007" if batch_id.startswith("T0-SP-007") else "T0-TD-008"
        segments = packets[batch_id]
        values, response_error = _load_response_candidates(raw_path)
        review_map = _load_semantic_reviews(review_paths.get(batch_id, Path(""))) if batch_id in review_paths else {}
        raw_count += len(values)
        for index, value in enumerate(values):
            reasons: list[str] = []
            warnings: list[str] = []
            candidate = None
            segment = None
            visual_segment = None
            if response_error:
                reasons.append(response_error)
            try:
                candidate = parse_candidate(value)
                schema_valid_count += 1
            except Exception:
                reasons.append("schema_invalid")
            if candidate is not None:
                if candidate.source_id not in SOURCE_IDS:
                    reasons.append("source_id_out_of_scope")
                if candidate.source_id != source_id:
                    reasons.append("source_id_does_not_match_batch")
                segment = resolve_locator(candidate.source_locator, segments)
                # Keep historical packet text for rejudging the historical API
                # response. Use fresh pdfplumber segments only for page-level
                # visual-review status and future-preflight metadata.
                visual_segment = (
                    pdf_segment_map.get(candidate.source_locator, segment)
                    if candidate.source_id == "T0-SP-007" and segment is not None
                    else segment
                )
                if segment is None:
                    reasons.append("locator_not_found")
                else:
                    if _normalized(candidate.excerpt_original) in _normalized(segment.text):
                        exact_count += 1
                    else:
                        reasons.append("excerpt_not_found_at_specified_locator")
                normalized_excerpt = _normalized(candidate.excerpt_original).casefold()
                if normalized_excerpt in seen_by_source.get(candidate.source_id, set()):
                    reasons.append("same_source_duplicate")
                else:
                    seen_by_source.setdefault(candidate.source_id, set()).add(normalized_excerpt)
                warnings = _capitalized_entity_warnings(candidate, candidate.excerpt_original)
                warnings_count += len(warnings)
                # Keep all hard gates deterministic and independent of claim
                # language; semantic screening handles unsupported subjects,
                # actions, results, and dimensions.
                reasons.extend(
                    reason for reason in deterministic_gate_reasons(candidate, segment)
                    if reason not in reasons
                )
            deterministic_pass = not reasons
            if deterministic_pass:
                gate_count += 1
            else:
                for reason in set(reasons):
                    hard_rejection_counts[reason] += 1
            review = review_map.get(index)
            inherited_claim_nature_pass = bool(
                review
                and review.get("status") == "pass"
                and review.get("claim_supported") is True
                and review.get("nature_supported") is True
            )
            inherited_claim_nature_pass_count += int(inherited_claim_nature_pass)
            semantic_status, semantic_reason, dimension_reviews, removed_dimensions = _semantic_screen(review, candidate) if candidate else ("reject", "schema invalid", [], [])
            offline_dimension_screen_pass_count += int(any(item["supported"] for item in dimension_reviews))
            final_candidate = bool(deterministic_pass and semantic_status == "pass")
            preview: dict[str, Any] = {
                "batch_id": batch_id,
                "candidate_index": index,
                "candidate": value,
                "schema_valid": candidate is not None,
                "exact_quote_matched": "excerpt_not_found_at_specified_locator" not in reasons,
                "deterministic_gate_passed": deterministic_pass,
                "hard_rejection_reasons": reasons,
                "warnings": warnings,
                "semantic_screening_mode": "same_model_separate_call_semantic_screening",
                "semantic_screen_status": semantic_status,
                "semantic_screen_reason": semantic_reason,
                "dimension_reviews": dimension_reviews,
                "removed_unsupported_dimensions": removed_dimensions,
                "final_candidate": final_candidate,
                "pending_g1_visual_review": bool(final_candidate and visual_segment and visual_segment.visual_review_required),
                "pending_g1_translation_review": bool(final_candidate and candidate and candidate.excerpt_zh.strip()),
            }
            all_previews.append(preview)
            if final_candidate and candidate and segment:
                adjusted = candidate.copy(deep=True) if hasattr(candidate, "copy") else candidate
                adjusted.coding_dimensions = [item["dimension"] for item in dimension_reviews if item["supported"]]
                record = inject_metadata(adjusted, source_rows[candidate.source_id], run_id, visual_segment or segment)
                record.update({
                    "p3q_preview_only": True,
                    "semantic_screening_mode": "same_model_separate_call_semantic_screening",
                    "dimension_reviews": dimension_reviews,
                    "removed_unsupported_dimensions": removed_dimensions,
                    "pending_g1_visual_review": bool((visual_segment or segment).visual_review_required),
                    "pending_g1_translation_review": True,
                    "machine_extracted_excerpt": candidate.excerpt_original,
                    "human_corrected_excerpt": None,
                })
                final_previews.append(record)

    after_hashes = immutable_v6_hashes()
    if before_hashes != after_hashes:
        raise RuntimeError("P3AR-v6 changed during P3Q; refusing to finalize")
    pending_visual = sum(bool(item["pending_g1_visual_review"]) for item in all_previews if item["final_candidate"])
    pending_translation = sum(bool(item["pending_g1_translation_review"]) for item in all_previews if item["final_candidate"])
    output_file_hashes = {}
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(run_dir / "candidate_rejudgment_preview.jsonl", all_previews)
    write_jsonl(run_dir / "final_candidate_preview.jsonl", final_previews)
    write_jsonl(
        run_dir / "rejected_candidates.jsonl",
        [row for row in all_previews if not row["final_candidate"]],
    )
    write_jsonl(
        run_dir / "semantic_screen_results.jsonl",
        [
            {
                "batch_id": row["batch_id"],
                "candidate_index": row["candidate_index"],
                "semantic_screening_mode": row["semantic_screening_mode"],
                "semantic_screen_status": row["semantic_screen_status"],
                "semantic_screen_reason": row["semantic_screen_reason"],
                "dimension_reviews": row["dimension_reviews"],
                "removed_unsupported_dimensions": row["removed_unsupported_dimensions"],
            }
            for row in all_previews
        ],
    )
    # Preserve a separately addressable copy of the historical real prompts
    # and API responses for audit. The source v6 tree remains read-only.
    input_artifacts = run_dir / "input_artifacts"
    for source_dir, target_dir in (
        (V6_DIR / "prompt_packets", input_artifacts / "prompt_packets"),
        (V6_DIR / "raw_model_outputs", input_artifacts / "raw_model_outputs"),
        (V6_DIR / "raw_semantic_reviews", input_artifacts / "raw_semantic_reviews"),
    ):
        target_dir.mkdir(parents=True, exist_ok=True)
        for source_path in sorted(source_dir.rglob("*")):
            if source_path.is_file():
                target_path = target_dir / source_path.relative_to(source_dir)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
    write_json(run_dir / "pdf_status_t0_sp_007.json", pdf_info)
    write_json(run_dir / "v6_input_hashes.json", before_hashes)
    output_file_hashes = {relative_path(path): sha256_file(path) for path in sorted(run_dir.rglob("*")) if path.is_file()}
    result = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "P3Q",
        "executed_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_p3ar_run_id": V6_RUN_ID,
        "p3ar_status": "real_api_technical_pilot_passed",
        "input_p3ar_status": "real_api_technical_pilot_passed",
        "p3ar_not_evidence_quality_validated": True,
        "normalized_claim_language_policy": "future_p3b_must_use_zh_CN; historical_p3a_r_preview_is_not_rewritten",
        "old_p3a_generation_mode": p3a_audit["generation_mode"],
        "model_api_called": False,
        "file_bridge_called": False,
        "provider_call_count": 0,
        "logical_extraction_batch_count": 0,
        "requested_temperature": 0.0,
        "provider_echoed_temperature": None,
        "provider_metrics_status": "not_observable_because_no_provider_call_was_made_in_P3Q",
        "raw_candidate_count": raw_count,
        "schema_valid_count": schema_valid_count,
        "exact_quote_matched_count": exact_count,
        "deterministic_gate_passed_count": gate_count,
        "inherited_v6_same_model_claim_nature_pass_count": inherited_claim_nature_pass_count,
        "offline_keyword_dimension_screen_pass_count": offline_dimension_screen_pass_count,
        "hybrid_preview_pass_count": len(final_previews),
        "final_candidate_count": len(final_previews),
        "final_candidate_acceptance_rate": len(final_previews) / raw_count if raw_count else 0.0,
        "pending_g1_visual_review_count": pending_visual,
        "pending_g1_translation_review_count": pending_translation,
        "capitalized_entity_warning_count": warnings_count,
        "hard_rejection_reason_counts": dict(hard_rejection_counts),
        "semantic_screening_label": "offline_keyword_dimension_heuristic_plus_inherited_v6_review",
        "dimension_support_is_not_accuracy": True,
        "pdf_status": pdf_info,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "p3b_executed": False,
        "p4_executed": False,
        "g1_executed": False,
        "snapshot_frozen": False,
        "p3ar_v6_hashes_unchanged": before_hashes == after_hashes,
        "p3ar_v6_input_hashes": before_hashes,
        "historical_api_artifacts_copied": True,
        "historical_api_artifacts_source": relative_path(V6_DIR),
        "output_file_sha256": output_file_hashes,
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
    }
    write_json(run_dir / "p3q_manifest.json", result)
    return result
