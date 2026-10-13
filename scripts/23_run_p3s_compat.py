"""Run one isolated P3S prompt compatibility call for T0-SP-001."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model_client import APIModelClient, ModelAPIError
from src.p3ar import sha256_bytes, sha256_file
from src.p3s import (
    EXTRACTION_TEMPLATE,
    P3S_MODEL_FIELDS,
    _build_extraction_prompt,
    _field_audit,
    _load_yaml,
    _parse_json,
    _parse_p3s_candidate,
    _prepare_document,
    _load_p2s_sources,
)


SOURCE_ID = "T0-SP-001"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "candidate_index",
        "actual_fields",
        "missing_fields",
        "extra_fields",
        "schema_valid",
        "source_locator_exists",
        "exact_quote_match",
        "coding_dimensions_is_array",
        "topics_is_array",
        "evidence_nature_is_single_legal_enum",
        "possible_corroboration_group_is_string",
        "compatibility_pass",
        "reasons",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _output_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "p3s_compat_manifest.json"
    }


def run_compatibility(run_id: str) -> dict[str, Any]:
    out_dir = ROOT / "outputs" / run_id
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError("compatibility output directory must be new and empty")
    out_dir.mkdir(parents=True, exist_ok=False)

    template = _load_yaml(EXTRACTION_TEMPLATE)
    sources = _load_p2s_sources()
    source = sources[SOURCE_ID]
    document = _prepare_document(source)
    prompt = _build_extraction_prompt(document, template, f"{SOURCE_ID}-compat-001")
    prompt_path = out_dir / "generated_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    prompt_sha = sha256_bytes(prompt.encode("utf-8"))

    base = {
        "run_id": run_id,
        "stage": "P3S-COMPAT",
        "source_id": SOURCE_ID,
        "model_api_calls": 0,
        "semantic_model_calls": 0,
        "evidence_inserted": 0,
        "g1_executed": False,
        "p4_executed": False,
        "t1_read": False,
        "api_key_written": False,
        "compatibility_pass": False,
        "prompt_template_version": template["version"],
        "prompt_template_sha256": sha256_file(EXTRACTION_TEMPLATE),
        "prompt_sha256": prompt_sha,
        "input_text_sha256": document.source_text_sha256,
        "input_text_path": source["text_path"],
        "transport": "openai_compatible_api",
    }

    try:
        # Compatibility is a single-call experiment; do not use the normal
        # P3S retry policy here.
        client = APIModelClient(max_retries=0)
        payload, content, record = client.call(prompt, temperature=0.0, max_tokens=6000)
    except ModelAPIError as exc:
        base.update(
            {
                "failure_stage": "compatibility_extraction",
                "exception_type": exc.root_exception_type,
                "http_status_code": exc.http_status_code,
                "model_api_calls": 1,
                "transport": "openai_compatible_api",
            }
        )
        _write_json(out_dir / "p3s_compat_manifest.json", base)
        (out_dir / "p3s_compat_report.md").write_text(
            "# P3S兼容性试验报告\n\nAPI调用失败；未重试、未切换模型或服务商。仅保留脱敏失败阶段和异常类型。\n",
            encoding="utf-8",
        )
        return base
    except Exception as exc:  # Do not serialize exception text: it may echo provider data.
        base.update(
            {
                "failure_stage": "compatibility_extraction",
                "exception_type": type(exc).__name__,
                "http_status_code": None,
                "model_api_calls": 1,
            }
        )
        _write_json(out_dir / "p3s_compat_manifest.json", base)
        (out_dir / "p3s_compat_report.md").write_text(
            "# P3S兼容性试验报告\n\nAPI响应处理失败；未重试或切换模型。\n",
            encoding="utf-8",
        )
        return base

    _write_json(out_dir / "raw_model_output.json", payload)
    base.update(
        {
            "model_api_calls": 1,
            "model_id": record.provider_model_id or record.requested_model_id,
            "provider_call_count": record.attempts,
            "physical_retry_count": record.retry_count,
            "response_sha256": record.response_sha256,
            "response_status_code": record.response_status_code,
            "requested_temperature": record.request_parameters.get("temperature"),
            "provider_temperature": record.provider_temperature,
            "provider_token_usage": record.provider_token_usage,
            "provider_cost": record.provider_cost,
            "latency_ms": record.latency_ms,
        }
    )

    rows: list[dict[str, Any]] = []
    reasons_by_candidate: dict[int, list[str]] = {}
    values: Any = None
    try:
        if content.strip().startswith("```") or not content.strip().startswith("["):
            raise ValueError("response_not_plain_json_array")
        values = _parse_json(content)
    except Exception as exc:
        reasons_by_candidate[-1] = [str(exc) if str(exc) == "response_not_plain_json" else "response_not_strict_json"]
        values = []
    if not isinstance(values, list):
        reasons_by_candidate[-1] = ["response_not_json_array"]
        values = []
    segment_map = {segment.source_locator: segment for segment in document.segments}
    count_limit_exceeded = len(values) > 6
    if count_limit_exceeded:
        reasons_by_candidate[-1] = ["candidate_count_exceeds_limit"]
    for index, value in enumerate(values):
        actual, missing, extra = _field_audit(value)
        reasons: list[str] = []
        schema_valid = False
        source_locator_exists = False
        exact_quote_match = False
        dimensions_is_array = isinstance(value, dict) and isinstance(value.get("coding_dimensions"), list)
        topics_is_array = isinstance(value, dict) and isinstance(value.get("topics"), list)
        nature_is_legal = False
        corroboration_is_string = isinstance(value, dict) and isinstance(value.get("possible_corroboration_group"), str)
        try:
            candidate, _ = _parse_p3s_candidate(value, SOURCE_ID)
            schema_valid = True
            source_locator_exists = candidate.source_locator in segment_map
            exact_quote_match = source_locator_exists and candidate.excerpt_original in segment_map[candidate.source_locator].text
            nature_is_legal = candidate.evidence_nature in {"goal", "resource_input", "test_event", "test_result", "deployment_event", "governance_event", "constraint", "evaluation"}
            if not source_locator_exists:
                reasons.append("source_locator_not_found")
            if not exact_quote_match:
                reasons.append("excerpt_not_continuous_exact_match")
            if count_limit_exceeded:
                reasons.append("candidate_count_exceeds_limit")
        except Exception:
            reasons.append("schema_invalid")
        if not dimensions_is_array:
            reasons.append("coding_dimensions_not_array")
        if not topics_is_array:
            reasons.append("topics_not_array")
        if not nature_is_legal:
            reasons.append("evidence_nature_invalid")
        if not corroboration_is_string:
            reasons.append("possible_corroboration_group_not_string")
        reasons = sorted(set(reasons))
        rows.append(
            {
                "candidate_index": index,
                "actual_fields": json.dumps(actual, ensure_ascii=False),
                "missing_fields": json.dumps(missing, ensure_ascii=False),
                "extra_fields": json.dumps(extra, ensure_ascii=False),
                "schema_valid": str(schema_valid and not count_limit_exceeded).lower(),
                "source_locator_exists": str(source_locator_exists).lower(),
                "exact_quote_match": str(exact_quote_match).lower(),
                "coding_dimensions_is_array": str(dimensions_is_array).lower(),
                "topics_is_array": str(topics_is_array).lower(),
                "evidence_nature_is_single_legal_enum": str(nature_is_legal).lower(),
                "possible_corroboration_group_is_string": str(corroboration_is_string).lower(),
                "compatibility_pass": str(not reasons).lower(),
                "reasons": json.dumps(reasons, ensure_ascii=False),
            }
        )
    if -1 in reasons_by_candidate:
        rows.append(
            {
                "candidate_index": "",
                "actual_fields": "[]",
                "missing_fields": "[]",
                "extra_fields": "[]",
                "schema_valid": "false",
                "source_locator_exists": "false",
                "exact_quote_match": "false",
                "coding_dimensions_is_array": "false",
                "topics_is_array": "false",
                "evidence_nature_is_single_legal_enum": "false",
                "possible_corroboration_group_is_string": "false",
                "compatibility_pass": "false",
                "reasons": json.dumps(reasons_by_candidate[-1], ensure_ascii=False),
            }
        )
    _write_csv(out_dir / "schema_validation.csv", rows)
    passed = isinstance(values, list) and 0 <= len(values) <= 6 and bool(values or not rows or rows[-1].get("candidate_index") == "")
    if values:
        passed = passed and all(row["compatibility_pass"] == "true" for row in rows if row["candidate_index"] != "")
    elif rows and rows[-1].get("candidate_index") == "":
        passed = False
    base["compatibility_pass"] = passed
    base["raw_candidate_count"] = len(values)
    base["schema_valid_count"] = sum(row["schema_valid"] == "true" for row in rows)
    base["exact_quote_matched_count"] = sum(row["exact_quote_match"] == "true" for row in rows)
    base["output_file_hashes"] = _output_hashes(out_dir)
    _write_json(out_dir / "p3s_compat_manifest.json", base)
    (out_dir / "p3s_compat_report.md").write_text(
        "# P3S兼容性试验报告\n\n"
        f"- 返回候选数：{len(values)}\n"
        f"- Schema通过：{base['schema_valid_count']}\n"
        f"- 精确定位匹配：{base['exact_quote_matched_count']}\n"
        f"- compatibility_pass：{str(passed).lower()}\n"
        "- 仅执行一次抽取调用；未调用语义筛选、未写入Evidence、未生成G1模板。\n",
        encoding="utf-8",
    )
    return base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run_compatibility(args.run_id)
    print(json.dumps({key: result.get(key) for key in ("run_id", "model_api_calls", "semantic_model_calls", "compatibility_pass", "api_key_written")}, ensure_ascii=False))
    return 0 if result.get("compatibility_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
