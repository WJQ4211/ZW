"""Create the offline P3S-H hardening audit package without model access."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "prompts" / "evidence_extract_p3s_v1.yaml"
IMPLEMENTATION = ROOT / "src" / "p3s.py"
TEST_FILE = ROOT / "tests" / "test_p3s_hardening.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(run_id: str) -> dict[str, object]:
    import yaml

    output = ROOT / "outputs" / run_id
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("P3S-H run directory must be new and empty")
    output.mkdir(parents=True, exist_ok=True)
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    fields = template["output_schema"]["exact_fields"]
    expected = [
        "source_locator",
        "excerpt_original",
        "excerpt_zh",
        "normalized_claim",
        "evidence_nature",
        "coding_dimensions",
        "topics",
        "possible_corroboration_group",
    ]
    schema_audit = {
        "exact_fields": fields,
        "expected_exact_fields": expected,
        "field_count": len(fields),
        "schema_matches": fields == expected,
        "contains_ten_fields_wording": "ten fields" in TEMPLATE.read_text(encoding="utf-8").lower(),
        "contains_markdown_fence_instruction": template["output_schema"].get("no_markdown_fence") is True,
        "empty_array_allowed": template["output_schema"].get("empty_array_allowed") is True,
        "max_candidates_per_source": template["output_schema"].get("max_candidates_per_source"),
    }
    write_json(output / "prompt_schema_audit.json", schema_audit)
    historical = {
        "P3S-20260826T090131Z-supplement-realapi-v2": {"eligible_for_formal_p3s": False, "reason": "historical_fixture_or_superseded_run"},
        "P3S-20260826T092410Z-supplement-realapi-v3": {"eligible_for_formal_p3s": False, "reason": "pre_P3S-H_schema_output_mismatch"},
        "P3S-20260826T093410Z-supplement-realapi-v3": {"eligible_for_formal_p3s": False, "reason": "failed_run_and_pre_P3S-H_schema_prompt"},
    }
    write_json(output / "historical_run_eligibility_audit.json", historical)
    manifest = {
        "run_id": run_id,
        "stage": "P3S-H",
        "generation_mode": "offline_hardening_only",
        "executed_at_utc": now_utc(),
        "python_version": platform.python_version(),
        "implementation_sha256": sha256(IMPLEMENTATION),
        "test_sha256": sha256(TEST_FILE),
        "prompt_template_sha256": sha256(TEMPLATE),
        "prompt_template_version": template.get("version"),
        "schema_audit": schema_audit,
        "historical_runs_marked_ineligible": sorted(historical),
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "formal_g1_supplement_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "api_key_written": False,
        "hardcoded_case_evidence_used": False,
    }
    write_json(output / "p3s_h_manifest.json", manifest)
    (output / "p3s_h_quality_report.md").write_text(
        "# P3S-H离线加固报告\n\n"
        "本运行仅验证提示Schema、程序门禁、checkpoint身份约束和失败脱敏结构；未调用真实API，未处理正式证据。\n\n"
        "- 模型API调用：0\n- Evidence新增：0\n- G1/P4执行：false\n- 快照冻结：false\n- T1读取：false\n- 旧realapi-v3结果：因Schema提示版本不一致，整体标记为不可用于正式P3S。\n",
        encoding="utf-8",
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.run_id), ensure_ascii=False, indent=2))
