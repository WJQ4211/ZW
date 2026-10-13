"""Append-only audit correction for the P3S compatibility experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model_client import chat_completion_content
from src.p3ar import sha256_file
from src.p3s import (
    P3S_MODEL_FIELDS,
    _deterministic_validation,
    _field_audit,
    _load_canonical,
    _load_p2s_sources,
    _parse_json,
    _parse_p3s_candidate,
    _prepare_document,
)


COMPAT_RUN = ROOT / "outputs" / "P3SCOMPAT-20260826T125652Z-schema-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    out_dir = ROOT / "outputs" / args.run_id
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("audit correction output directory must be new and empty")
    out_dir.mkdir(parents=True, exist_ok=False)

    original_manifest = COMPAT_RUN / "p3s_compat_manifest.json"
    original_manifest_sha_before = sha256_file(original_manifest)
    manifest = json.loads(original_manifest.read_text(encoding="utf-8"))
    raw_envelope_path = COMPAT_RUN / "raw_model_output.json"
    envelope = json.loads(raw_envelope_path.read_text(encoding="utf-8"))
    content = chat_completion_content(envelope)
    fenced_response = content.strip().startswith("```")
    values = _parse_json(content)
    sources = _load_p2s_sources()
    document = _prepare_document(sources["T0-SP-001"])
    canonical, _ = _load_canonical()
    segment_map = {segment.source_locator: segment for segment in document.segments}
    validation = []
    for index, value in enumerate(values):
        actual, missing, extra = _field_audit(value)
        candidate, corroboration = _parse_p3s_candidate(value, "T0-SP-001")
        deterministic_reasons = _deterministic_validation(
            candidate,
            corroboration,
            document,
            segment_map,
            canonical,
            [],
        )
        validation.append(
            {
                "candidate_index": index,
                "actual_fields": actual,
                "missing_fields": missing,
                "extra_fields": extra,
                "schema_valid": set(actual) == P3S_MODEL_FIELDS and not missing and not extra,
                "exact_quote_match": candidate.source_locator in segment_map
                and candidate.excerpt_original in segment_map[candidate.source_locator].text,
                "deterministic_reasons": deterministic_reasons,
            }
        )
    original_manifest_sha_after = sha256_file(original_manifest)
    audit = {
        "run_id": args.run_id,
        "stage": "P3S-COMPAT-AUDIT-CORRECTION",
        "original_compatibility_run_id": manifest["run_id"],
        "original_manifest_sha256": original_manifest_sha_before,
        "original_manifest_sha256_after": original_manifest_sha_after,
        "original_manifest_unchanged": original_manifest_sha_before == original_manifest_sha_after,
        "original_response_http_status": manifest.get("response_status_code"),
        "response_wrapped_in_markdown_json_fence": fenced_response,
        "response_parse_behavior": "compatibility script used bare json.loads; formal P3S _parse_json strips the JSON fence",
        "formal_parser_replay": {
            "parsed_count": len(values),
            "count_within_limit": 0 <= len(values) <= 6,
            "schema_valid": sum(bool(row["schema_valid"]) for row in validation),
            "exact_quote_match": sum(bool(row["exact_quote_match"]) for row in validation),
            "deterministic_pass": sum(not row["deterministic_reasons"] for row in validation),
        },
        "operational_compatibility_pass": bool(
            manifest.get("response_status_code") == 200
            and len(values) <= 6
            and all(row["schema_valid"] and row["exact_quote_match"] and not row["deterministic_reasons"] for row in validation)
        ),
        "new_model_api_calls": 0,
        "evidence_inserted": 0,
        "g1_executed": False,
        "p4_executed": False,
        "t1_read": False,
        "source_id": "T0-SP-001",
        "validation_rows": validation,
    }
    (out_dir / "p3s_compat_audit_correction.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "p3s_compat_audit_correction.md").write_text(
        "# P3S兼容性审计纠正\n\n"
        "原兼容性脚本使用裸 `json.loads`，将Markdown `json`围栏误判为非严格JSON；正式P3S解析器会剥离该围栏。\n\n"
        f"- 原响应HTTP：{audit['original_response_http_status']}\n"
        f"- parsed_count：{audit['formal_parser_replay']['parsed_count']}\n"
        f"- count_within_limit：{str(audit['formal_parser_replay']['count_within_limit']).lower()}\n"
        f"- schema_valid：{audit['formal_parser_replay']['schema_valid']}\n"
        f"- exact_quote_match：{audit['formal_parser_replay']['exact_quote_match']}\n"
        f"- deterministic_pass：{audit['formal_parser_replay']['deterministic_pass']}\n"
        f"- operational_compatibility_pass：{str(audit['operational_compatibility_pass']).lower()}\n"
        "- 本纠正只修订审计结论，不把兼容性候选写入Evidence。\n",
        encoding="utf-8",
    )
    print(json.dumps({k: audit[k] for k in ("run_id", "original_response_http_status", "operational_compatibility_pass", "new_model_api_calls", "original_manifest_unchanged")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

