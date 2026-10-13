"""Create the offline P3S-H-C2 audit without model or database access."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V4_RUN = ROOT / "outputs" / "P3S-20260826T123637Z-supplement-realapi-v4"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def audit_v4_fixture() -> dict[str, Any]:
    """Recompute the C2 counters from v4 artifacts without changing them."""
    manifest = json.loads((V4_RUN / "p3s_manifest.json").read_text(encoding="utf-8"))
    rejected = _read_jsonl(V4_RUN / "rejected_candidates.jsonl")
    calls = _read_jsonl(V4_RUN / "model_call_log.jsonl")
    with (V4_RUN / "semantic_screening.csv").open(encoding="utf-8-sig", newline="") as handle:
        semantic_rows = list(csv.DictReader(handle))

    logical_calls = [row for row in calls if row.get("logical_call_executed", True) is not False]
    extraction_calls = [row for row in logical_calls if row.get("phase") == "extraction"]
    semantic_calls = [
        row for row in logical_calls if row.get("phase") == "same_model_separate_call_semantic_screening"
    ]
    overflow_batches = [
        row
        for row in rejected
        if row.get("candidate_index") is None
        and row.get("rejection_reasons") == ["candidate_count_exceeds_limit"]
    ]

    reason_counts: Counter[str] = Counter()
    for row in rejected:
        # The four batch marker rows are not candidate rejections. Detailed
        # validation text is retained on rows but is not an aggregate reason.
        if row.get("candidate_index") is None:
            continue
        for reason in row.get("rejection_reasons", []):
            if reason == "candidate_count_exceeds_limit" or reason.startswith("1 validation error"):
                continue
            reason_counts[reason] += 1

    actual_semantic_candidates = sum(1 for row in semantic_rows if row.get("status") != "skipped")
    return {
        "fixture_run_id": manifest["run_id"],
        "fixture_sha256_before": {str(path.relative_to(V4_RUN)): _sha256(path) for path in V4_RUN.rglob("*") if path.is_file()},
        "raw_candidate_count": manifest["raw_candidate_count"],
        "logical_model_call_count": len(logical_calls),
        "model_api_calls": len(logical_calls),
        "logical_extraction_batch_count": len(extraction_calls),
        "logical_semantic_screen_batch_count": len(semantic_calls),
        "semantic_screened_candidate_count": actual_semantic_candidates,
        "semantic_skipped_no_valid_candidates_count": sum(1 for row in semantic_rows if row.get("status") == "skipped"),
        "candidate_count_limit_exceeded_batch_count": len(overflow_batches),
        "candidates_rejected_due_to_batch_limit": sum(row.get("candidate_count", 0) for row in overflow_batches),
        "rejected_count": len(rejected),
        "rejection_reason_counts": dict(reason_counts),
        "historical_manifest_not_modified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    out_dir = ROOT / "outputs" / args.run_id
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("P3S-H-C2 output directory must be new and empty")
    out_dir.mkdir(parents=True, exist_ok=False)
    audit = audit_v4_fixture()
    fixture_metric_keys = (
        "logical_model_call_count",
        "model_api_calls",
        "logical_extraction_batch_count",
        "logical_semantic_screen_batch_count",
        "semantic_screened_candidate_count",
        "semantic_skipped_no_valid_candidates_count",
    )
    for key in fixture_metric_keys:
        audit[f"fixture_{key}"] = audit[key]
    audit["run_id"] = args.run_id
    audit["stage"] = "P3S-H-C2"
    audit["logical_model_call_count"] = 0
    audit["model_api_calls"] = 0
    audit["logical_extraction_batch_count"] = 0
    audit["logical_semantic_screen_batch_count"] = 0
    audit["semantic_screened_candidate_count"] = 0
    audit["evidence_inserted"] = 0
    audit["formal_g1_executed"] = False
    audit["p4_executed"] = False
    audit["snapshot_frozen"] = False
    audit["t1_read"] = False
    audit["compatibility_api_executed"] = False
    (out_dir / "p3s_h_c2_manifest.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "p3s_h_c2_quality_report.md").write_text(
        "# P3S-H-C2离线审计\n\n"
        f"- v4原始候选：{audit['raw_candidate_count']}\n"
        f"- v4实际网络逻辑调用：{audit['fixture_model_api_calls']}\n"
        f"- v4实际语义调用：{audit['fixture_logical_semantic_screen_batch_count']}\n"
        f"- v4实际提交语义模型的候选：{audit['fixture_semantic_screened_candidate_count']}\n"
        f"- 跳过语义筛选来源批次：{audit['semantic_skipped_no_valid_candidates_count']}\n"
        f"- 超限批次：{audit['candidate_count_limit_exceeded_batch_count']}\n"
        f"- 因超限拒绝候选：{audit['candidates_rejected_due_to_batch_limit']}\n"
        f"- 候选级拒绝原因：{json.dumps(audit['rejection_reason_counts'], ensure_ascii=False, sort_keys=True)}\n"
        "\n本审计只读取历史v4文件，不修改v4产物，不调用模型，不写入Evidence。\n",
        encoding="utf-8",
    )
    print(json.dumps({k: audit[k] for k in ("run_id", "stage", "raw_candidate_count", "model_api_calls", "candidate_count_limit_exceeded_batch_count", "candidates_rejected_due_to_batch_limit", "semantic_screened_candidate_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
