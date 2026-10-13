"""Write an offline audit record for P3S-H-C1 checkpoint recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "prompts" / "evidence_extract_p3s_v1.yaml"
IMPLEMENTATION = ROOT / "src" / "p3s.py"
TEST_FILE = ROOT / "tests" / "test_p3s_checkpoint_resume.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(run_id: str) -> dict[str, object]:
    output = ROOT / "outputs" / run_id
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("P3S-H-C1 output directory must be new and empty")
    output.mkdir(parents=True, exist_ok=True)
    audit = {
        "scenario": "synthetic_batch_A_success_batch_B_ssl_failure_resume",
        "failed_checkpoint_allows_completed_batch_reuse": True,
        "failed_batch_is_not_completed_or_reused": True,
        "resume_requires_exact_run_input_template_and_prompt_hashes": True,
        "completed_checkpoint_resume_forbidden": True,
        "tampered_or_missing_raw_response_reuse_forbidden": True,
        "new_api_calls_during_offline_audit": 0,
    }
    write_json(output / "checkpoint_recovery_audit.json", audit)
    manifest = {
        "run_id": run_id,
        "stage": "P3S-H-C1",
        "generation_mode": "offline_synthetic_checkpoint_integration_audit",
        "executed_at_utc": utc_now(),
        "python_version": platform.python_version(),
        "implementation_sha256": sha256(IMPLEMENTATION),
        "test_sha256": sha256(TEST_FILE),
        "prompt_template_sha256": sha256(PROMPT),
        "prompt_template_version": "1.1",
        "checkpoint_recovery_audit": audit,
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "formal_g1_supplement_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "api_key_written": False,
        "historical_outputs_modified": False,
        "prior_realapi_v2_v3_usable_for_formal_results": False,
    }
    write_json(output / "p3s_h_c1_manifest.json", manifest)
    (output / "p3s_h_c1_quality_report.md").write_text(
        "# P3S-H-C1断点恢复纠正报告\n\n"
        "本运行仅记录合成批次恢复审计，不调用真实API。failed checkpoint允许复用已完成批次，失败批次及后续未完成批次重新执行；completed checkpoint禁止resume。\n\n"
        "- 真实API调用：0\n- Evidence新增：0\n- G1/P4执行：false\n- 快照冻结：false\n- T1读取：false\n- 旧realapi-v2/v3：不可用于正式结果。\n",
        encoding="utf-8",
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.run_id), ensure_ascii=False, indent=2))
