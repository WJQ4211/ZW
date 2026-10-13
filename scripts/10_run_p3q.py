"""Run the offline P3Q quality repair and write its preflight artifacts.

This command reads only the immutable P3a-R pilot outputs and source material.
It deliberately has no model-client path and refuses to reuse a non-empty run
directory. It never writes formal Evidence or any database table.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3ar import ROOT, relative_path, sha256_file, write_json  # noqa: E402
from src.p3q import run_p3q  # noqa: E402


TEMPLATE_FILES = (
    ROOT / "prompts/evidence_extract_p3b_v2.yaml",
    ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml",
    ROOT / "config/p3b_preflight.yaml",
)


def _report(result: dict) -> tuple[str, str]:
    rates = {
        "final_candidate_acceptance_rate": result["final_candidate_acceptance_rate"],
        "capitalized_entity_warning_count": result["capitalized_entity_warning_count"],
    }
    change_log = f"""# P3Q validator change log

Run: `{result['run_id']}`  
Input: `{result['input_p3ar_run_id']}`

- Historical P3a remains `deterministic_hardcoded_fixture` and is not eligible for G1.
- P3a-R is recorded here as `real_api_technical_pilot_passed`, with
  `not_evidence_quality_validated=true`; the immutable P3a-R directory was not edited.
- Capitalized entities are warnings (`needs_semantic_review`), not deterministic hard rejections.
- Hard gates remain: strict schema, in-scope source, real locator, normalized exact quote,
  non-empty Chinese translation, no unsupported numeric addition, no pseudo-continuous locator,
  and no plan/intent coded as an observed result.
- Semantic screening is named `same_model_separate_call_semantic_screening`; it is a quality
  screen only and cannot replace G1.
- Coding dimensions are reviewed one by one with `dimension`, `supported`,
  `supporting_span`, and `reason`. Unsupported dimensions are removed in the preview and
  recorded; all-unsupported candidates are held for human review.
- PDF OCR/extraction status is page-level. Machine text is preserved; any future G1 correction
  must use `human_corrected_excerpt` without overwriting `machine_extracted_excerpt`.
- Metrics are separated into raw, schema, quote, deterministic, semantic-screen and final
  counts. A same-model screen pass is not reported as extraction accuracy.
- Requested temperature is recorded as `0.0`; provider echo is `null` because no provider call
  was made by P3Q.

P3Q did not call an API, read T1, write formal Evidence, generate hypotheses, execute P3b,
execute P4/G1, or freeze a snapshot.
"""
    pdf = result["pdf_status"]
    preflight = f"""# P3b preflight report

## Decision

P3b was **not executed**. This is an offline quality-repair preview over the saved P3a-R
technical pilot responses. P4/G1 remain closed.

## Immutable input

- P3a-R status: `{result['input_p3ar_status']}`
- `not_evidence_quality_validated`: `{str(result['p3ar_not_evidence_quality_validated']).lower()}`
- P3a-R v6 hashes unchanged: `{str(result['p3ar_v6_hashes_unchanged']).lower()}`
- API calls in P3Q: `{result['provider_call_count']}`
- requested temperature: `{result['requested_temperature']}`
- provider echoed temperature: `{result['provider_echoed_temperature']}`

## Rejudgment counts

| Metric | Count/value |
|---|---:|
| raw_candidate_count | {result['raw_candidate_count']} |
| schema_valid_count | {result['schema_valid_count']} |
| exact_quote_matched_count | {result['exact_quote_matched_count']} |
| deterministic_gate_passed_count | {result['deterministic_gate_passed_count']} |
| same_model_semantic_screen_pass_count | {result['same_model_semantic_screen_pass_count']} |
| final_candidate_count | {result['final_candidate_count']} |
| final_candidate_acceptance_rate | {rates['final_candidate_acceptance_rate']:.6f} |
| pending_g1_visual_review_count | {result['pending_g1_visual_review_count']} |
| pending_g1_translation_review_count | {result['pending_g1_translation_review_count']} |
| capitalized_entity_warning_count | {rates['capitalized_entity_warning_count']} |

The semantic-screen pass count is not an accuracy estimate; there was no new model call in P3Q.

## PDF status: T0-SP-007

- pdf_page_count: `{pdf['pdf_page_count']}`
- non_text_pages: `{pdf['non_text_pages']}`
- page_47_disposition: `{pdf['page_47_disposition']}`
- ocr_required_pages: `{pdf['ocr_required_pages']}`
- layout_visual_review_required_pages: `{pdf['layout_visual_review_required_pages']}`
- machine extraction: `{pdf['extraction_method']}`

## Boundaries

- formal Evidence rows added: `0`
- hypotheses added: `0`
- T1 read: `false`
- P3b/P4/G1 executed: `false`
- snapshot frozen: `false`

The candidate preview is not a formal Evidence set and remains subject to future P3b,
human visual/translation review, and G1.
"""
    return change_log, preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty P3Q run directory: {run_dir}")

    result = run_p3q(args.run_id, run_dir)
    preflight_dir = run_dir / "p3b_preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    template_hashes: dict[str, str] = {}
    for source in TEMPLATE_FILES:
        target = preflight_dir / source.name
        shutil.copy2(source, target)
        template_hashes[relative_path(source)] = sha256_file(source)

    change_log, preflight_report = _report(result)
    (run_dir / "validator_change_log.md").write_text(change_log, encoding="utf-8")
    (run_dir / "p3b_preflight_report.md").write_text(preflight_report, encoding="utf-8")

    output_files = [path for path in run_dir.rglob("*") if path.is_file() and path.name != "p3q_manifest.json"]
    result["p3b_preflight_templates"] = template_hashes
    result["output_file_sha256"] = {relative_path(path): sha256_file(path) for path in sorted(output_files)}
    result["manifest_sha256"] = None
    result["executed_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    write_json(run_dir / "p3q_manifest.json", result)
    print(json.dumps({
        "run_id": args.run_id,
        "raw_candidate_count": result["raw_candidate_count"],
        "final_candidate_count": result["final_candidate_count"],
        "p3ar_v6_hashes_unchanged": result["p3ar_v6_hashes_unchanged"],
        "provider_call_count": result["provider_call_count"],
        "formal_evidence_db_rows_added": 0,
        "t1_read": False,
        "p3b_executed": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
