"""P3a-R validate step: validate only saved API responses and source locators."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.model_client import chat_completion_content  # noqa: E402
from src.p3ar import (  # noqa: E402
    CASE_ID,
    P1B_DIR,
    P1C_DIR,
    P2_INVENTORY,
    P2_MANIFEST,
    P2_SOURCE,
    ROOT,
    SOURCE_IDS,
    SP_PDF,
    TD_TEXT,
    hash_paths,
    inject_metadata,
    lexical_auto_rejection,
    load_source_metadata,
    parse_candidate,
    reason_counts,
    relative_path,
    resolve_locator,
    sha256_file,
    write_csv,
    write_json,
    write_jsonl,
)


def current_git_head() -> dict[str, str]:
    for candidate in (ROOT / ".github_upload_staging", ROOT):
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        value = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return {"commit": value, "short_commit": value[:12], "git_root": str(candidate)}
    raise RuntimeError("A real Git HEAD is required for P3a-R; refusing unverified_local_copy.")


def parse_content_from_response(path: Path) -> tuple[Any, str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None, "api_response_envelope_not_object"
    try:
        content = chat_completion_content(payload)
    except ValueError as exc:
        return None, str(exc)
    cleaned = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        return None, "model_content_not_strict_json"


def semantic_rows_for_response(path: Path) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    decoded, error = parse_content_from_response(path)
    if error or not isinstance(decoded, list):
        return {}, [{"semantic_response_path": relative_path(path), "error": error or "semantic_response_not_array"}]
    mapping: dict[int, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    allowed = {"candidate_index", "status", "reason", "claim_supported", "nature_supported", "dimensions_supported"}
    for item in decoded:
        if not isinstance(item, dict) or set(item) != allowed:
            errors.append({"semantic_response_path": relative_path(path), "error": "semantic_review_schema_invalid", "item": item})
            continue
        if not isinstance(item["candidate_index"], int) or item["status"] not in {"pass", "revise", "reject"}:
            errors.append({"semantic_response_path": relative_path(path), "error": "semantic_review_value_invalid", "item": item})
            continue
        mapping[item["candidate_index"]] = item
    return mapping, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    prepare_manifest_path = run_dir / "prepare_manifest.json"
    execute_manifest_path = run_dir / "execute_manifest.json"
    if not prepare_manifest_path.exists() or not execute_manifest_path.exists():
        raise RuntimeError("prepare and execute-model must complete before validate")
    if not (run_dir / "raw_model_outputs").exists():
        raise RuntimeError("raw_model_outputs is required")

    prepare_manifest = json.loads(prepare_manifest_path.read_text(encoding="utf-8"))
    execute_manifest = json.loads(execute_manifest_path.read_text(encoding="utf-8"))
    if execute_manifest.get("transport") != "openai_compatible_api":
        raise RuntimeError("P3a-R validation requires real API responses; FileBridge cannot be mixed in")
    source_metadata = load_source_metadata()
    protected_before = hash_paths(
        [
            P2_MANIFEST,
            P2_SOURCE,
            P2_INVENTORY,
            *[path for directory in (P1B_DIR.parent / "P1-20260825T081142Z-7f4c9b", P1B_DIR, P1C_DIR, ROOT / "outputs" / prepare_manifest.get("p2_run_id", "")) if directory.exists() for path in directory.rglob("*") if path.is_file()],
        ]
    )

    packet_dir = run_dir / "prompt_packets" / "extraction"
    packet_by_batch = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in packet_dir.glob("*.json")}
    raw_paths = sorted((run_dir / "raw_model_outputs").glob("*.json"))
    semantic_paths = sorted((run_dir / "raw_semantic_reviews").glob("*.json"))
    semantic_by_batch = {path.stem: path for path in semantic_paths}
    rejected: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    semantic_review_output: list[dict[str, Any]] = []
    prior_by_source: dict[str, list[str]] = {source_id: [] for source_id in SOURCE_IDS}
    raw_candidate_count = 0
    quote_presence_matches = 0
    structural_count = 0
    semantic_considered = 0
    semantic_pass_count = 0
    nature_supported_count = 0
    dimension_supported_count = 0
    translation_nonempty_count = 0
    nature_counts: Counter[str] = Counter()
    dimension_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()

    for raw_path in raw_paths:
        batch_id = raw_path.stem
        packet = packet_by_batch.get(batch_id)
        if packet is None:
            continue
        segment_map = {
            item["source_locator"]: type("Segment", (), item)()  # simple attribute adapter for shared resolver
            for item in packet["segments"]
        }
        # The resolver uses only the attributes present in SourceSegment; the
        # adapter avoids reading any source outside the prepared packet.
        decoded, response_error = parse_content_from_response(raw_path)
        semantic_mapping, semantic_errors = semantic_rows_for_response(semantic_by_batch[batch_id]) if batch_id in semantic_by_batch else ({}, [{"error": "semantic_response_missing"}])
        semantic_review_output.extend(semantic_errors)
        items = decoded if isinstance(decoded, list) else []
        raw_candidate_count += len(items)
        for index, item in enumerate(items):
            base_row = {"batch_id": batch_id, "candidate_index": index, "source_id": item.get("source_id") if isinstance(item, dict) else ""}
            reasons: list[str] = []
            candidate = None
            segment = None
            if response_error:
                reasons.append(response_error)
            if not isinstance(item, dict):
                reasons.append("candidate_not_object")
            else:
                try:
                    candidate = parse_candidate(item)
                except Exception as exc:  # schema errors are recorded, not repaired
                    reasons.append("candidate_schema_invalid:" + type(exc).__name__)
            if candidate is not None:
                source_counts[candidate.source_id] += 1
                if candidate.source_id not in SOURCE_IDS:
                    reasons.append("source_id_out_of_scope")
                if candidate.source_id != packet["source_id"]:
                    reasons.append("source_id_does_not_match_prompt_batch")
                segment = resolve_locator(candidate.source_locator, segment_map)
                if segment is None:
                    reasons.append("source_locator_not_found_or_noncontiguous")
                else:
                    normalized_excerpt = re.sub(r"\s+", " ", candidate.excerpt_original).strip()
                    normalized_segment = re.sub(r"\s+", " ", segment.text).strip()
                    exact = bool(normalized_excerpt) and normalized_excerpt in normalized_segment
                    if exact:
                        quote_presence_matches += 1
                    else:
                        reasons.append("excerpt_not_found_at_specified_locator")
                    if candidate.source_locator.endswith("--08") and not re.search(r"paragraph-06--08$", candidate.source_locator):
                        reasons.append("pseudo_continuous_locator")
                if not candidate.excerpt_zh.strip():
                    reasons.append("excerpt_zh_empty")
                else:
                    translation_nonempty_count += 1
                auto_reason = lexical_auto_rejection(candidate, segment) if segment is not None else None
                if auto_reason:
                    reasons.append(auto_reason)
                structural_count += int(not reasons)
                if candidate.source_id in prior_by_source:
                    normalized = re.sub(r"\s+", " ", candidate.excerpt_original).strip().casefold()
                    if normalized in prior_by_source[candidate.source_id]:
                        reasons.append("same_source_duplicate")
                    else:
                        prior_by_source[candidate.source_id].append(normalized)
            review = semantic_mapping.get(index)
            if candidate is not None:
                semantic_review_output.append({"batch_id": batch_id, "candidate_index": index, "candidate": item, "review": review})
            if not reasons and review is None:
                reasons.append("semantic_review_missing")
            if review is not None:
                semantic_considered += 1
                claim_supported = review.get("claim_supported") is True
                nature_supported = review.get("nature_supported") is True
                dimensions_supported = review.get("dimensions_supported") is True
                nature_supported_count += int(nature_supported)
                dimension_supported_count += int(dimensions_supported)
                semantic_pass_count += int(review.get("status") == "pass" and claim_supported and nature_supported and dimensions_supported)
                if review.get("status") != "pass":
                    reasons.append("semantic_review_" + str(review.get("status")))
                if not claim_supported:
                    reasons.append("claim_not_directly_supported_by_excerpt")
                if not nature_supported:
                    reasons.append("evidence_nature_not_supported_by_excerpt")
                if not dimensions_supported:
                    reasons.append("coding_dimensions_not_supported_by_excerpt")
            row = {
                **base_row,
                "source_locator": candidate.source_locator if candidate is not None else (item.get("source_locator") if isinstance(item, dict) else ""),
                "quote_presence_match": not any(reason.startswith("excerpt_not_found") for reason in reasons),
                "semantic_review_status": review.get("status") if review else "missing",
                "semantic_claim_supported": review.get("claim_supported") if review else False,
                "semantic_nature_supported": review.get("nature_supported") if review else False,
                "semantic_dimensions_supported": review.get("dimensions_supported") if review else False,
                "accepted": False,
                "failure_reasons": "; ".join(reasons),
            }
            if not reasons and candidate is not None and segment is not None:
                record = inject_metadata(candidate, source_metadata[candidate.source_id], args.run_id, segment)
                accepted.append(record)
                nature_counts[candidate.evidence_nature] += 1
                dimension_counts.update(candidate.coding_dimensions)
                row["accepted"] = True
            else:
                rejected.append({"batch_id": batch_id, "candidate_index": index, "candidate": item, "rejection_reasons": reasons})
            validation_rows.append(row)

    protected_after = hash_paths(
        [
            P2_MANIFEST,
            P2_SOURCE,
            P2_INVENTORY,
            *[path for directory in (P1B_DIR.parent / "P1-20260825T081142Z-7f4c9b", P1B_DIR, P1C_DIR, ROOT / "outputs" / prepare_manifest.get("p2_run_id", "")) if directory.exists() for path in directory.rglob("*") if path.is_file()],
        ]
    )
    if protected_before != protected_after:
        raise RuntimeError("Protected P1/P1b/P1c/P2 artifact changed during validation")

    write_jsonl(run_dir / "evidence_candidates_p3ar.jsonl", accepted)
    write_jsonl(run_dir / "rejected_candidates.jsonl", rejected)
    write_jsonl(run_dir / "semantic_review_results.jsonl", semantic_review_output)
    write_csv(
        run_dir / "quote_validation.csv",
        [
            "batch_id", "candidate_index", "source_id", "source_locator", "quote_presence_match",
            "semantic_review_status", "semantic_claim_supported", "semantic_nature_supported",
            "semantic_dimensions_supported", "accepted", "failure_reasons",
        ],
        validation_rows,
    )
    summary_rows = []
    for source_id in SOURCE_IDS:
        source_rows = [row for row in validation_rows if row["source_id"] == source_id]
        summary_rows.append(
            {
                "source_id": source_id,
                "raw_candidate_count": len(source_rows),
                "accepted_candidate_count": sum(row["accepted"] for row in source_rows),
                "rejected_candidate_count": sum(not row["accepted"] for row in source_rows),
                "quote_presence_match_rate": (sum(row["quote_presence_match"] for row in source_rows) / len(source_rows)) if source_rows else None,
            }
        )
    write_csv(run_dir / "extraction_summary_by_source.csv", list(summary_rows[0]), summary_rows)

    output_files = [path for path in run_dir.rglob("*") if path.is_file() and path.name != "p3ar_manifest.json"]
    output_hashes = {relative_path(path): sha256_file(path) for path in sorted(output_files)}
    quote_rate = quote_presence_matches / raw_candidate_count if raw_candidate_count else 0.0
    alignment_rate = semantic_pass_count / semantic_considered if semantic_considered else 0.0
    nature_rate = nature_supported_count / semantic_considered if semantic_considered else 0.0
    dimension_rate = dimension_supported_count / semantic_considered if semantic_considered else 0.0
    translation_rate = translation_nonempty_count / raw_candidate_count if raw_candidate_count else 0.0
    manifest = {
        "run_id": args.run_id,
        "case_id": CASE_ID,
        "stage": "P3a-R",
        "executed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "git_head": current_git_head(),
        "p2_run_id": prepare_manifest.get("p2_run_id"),
        "source_ids": list(SOURCE_IDS),
        "generation_mode": "real_model_api_extraction",
        "real_model_extraction_executed": True,
        "validation_scope": "structural_quote_presence_and_claim_evidence_semantic_review",
        "result_not_eligible_for_g1": True,
        "transport": execute_manifest.get("transport"),
        "requested_model_id": execute_manifest.get("requested_model_id"),
        "provider_model_ids_observed": sorted({call.get("provider_model_id") for call in execute_manifest.get("api_call_records", []) if call.get("provider_model_id")}),
        "provider_call_count": execute_manifest.get("provider_call_count"),
        "provider_call_count_status": execute_manifest.get("provider_call_count_status", "observed_network_attempts"),
        "provider_metrics": {
            "temperature": execute_manifest.get("provider_temperature"),
            "cost": execute_manifest.get("provider_cost"),
            "token_usage_recorded_per_call": True,
            "status": "provider_temperature_and_cost_not_observable" if execute_manifest.get("provider_temperature") is None and execute_manifest.get("provider_cost") is None else "partial",
        },
        "logical_extraction_batch_count": execute_manifest.get("logical_extraction_batch_count"),
        "logical_semantic_review_batch_count": execute_manifest.get("logical_semantic_review_batch_count"),
        "api_key_written": False,
        "file_bridge_calls_mixed": False,
        "input_files": prepare_manifest.get("input_files"),
        "prompt_template": prepare_manifest.get("prompt_template"),
        "raw_candidate_count": raw_candidate_count,
        "structurally_valid_candidate_count": structural_count,
        "candidate_evidence_count": len(accepted),
        "rejected_candidate_count": len(rejected),
        "rejected_reason_counts": reason_counts(rejected),
        "quote_presence_match_rate": quote_rate,
        "claim_quote_alignment_pass_rate": alignment_rate,
        "nature_supported_rate": nature_rate,
        "dimension_supported_rate": dimension_rate,
        "translation_nonempty_rate": translation_rate,
        "metric_definitions": {
            "quote_presence_match_rate": "exact normalized excerpt match at the specified source locator divided by raw model candidates",
            "claim_quote_alignment_pass_rate": "second-pass semantic status pass with claim_supported true divided by semantic reviews",
            "nature_supported_rate": "second-pass nature_supported true divided by semantic reviews",
            "dimension_supported_rate": "second-pass dimensions_supported true divided by semantic reviews",
            "translation_nonempty_rate": "non-empty excerpt_zh divided by raw model candidates",
        },
        "evidence_nature_counts": dict(nature_counts),
        "coding_dimension_counts": dict(dimension_counts),
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "p4_executed": False,
        "g1_executed": False,
        "snapshot_frozen": False,
        "protected_p1_p1b_p1c_p2_unchanged": protected_before == protected_after,
        "output_file_sha256": output_hashes,
        "boundary_declarations": {
            "only_requested_sources_processed": True,
            "old_p3a_candidates_used_as_model_input": False,
            "formal_evidence_written": False,
            "competitive_hypotheses_generated": False,
            "case_conclusion_formed": False,
            "t1_material_read": False,
            "p3b_executed": False,
            "p4_or_g1_executed": False,
        },
        "stop_condition": "P3a-R complete for T0-SP-007 and T0-TD-008; stop before P3b, P4, G1, snapshot freeze, hypotheses, and T1.",
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
    }
    write_json(run_dir / "p3ar_manifest.json", manifest)
    print(json.dumps({"run_id": args.run_id, "raw_candidate_count": raw_candidate_count, "candidate_evidence_count": len(accepted), "rejected_candidate_count": len(rejected), "quote_presence_match_rate": quote_rate, "claim_quote_alignment_pass_rate": alignment_rate, "formal_evidence_db_rows_added": 0, "t1_read": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
