"""P3b-R2 audit-gate correction and G1-T0 preparation.

This module only reads the immutable P3b-R v3 artifacts.  It does not call a
model, read source documents or T1, and it never writes formal Evidence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INPUT_RUN_ID = "P3BR-20260826T064000Z-recovery-v3"
INPUT_DIR = ROOT / "outputs" / INPUT_RUN_ID
OUTPUT_PREFIX = "P3BR2-"

FINAL_FIELDS = (
    "final_decision",
    "final_nature",
    "final_dimensions",
    "final_normalized_claim",
    "human_corrected_excerpt",
    "final_excerpt_zh",
    "reviewer",
    "reviewed_at",
    "review_note",
)

VISUAL_SUGGESTIONS: dict[str, dict[str, Any]] = {
    "EV-T0-63E815AC3300FE8A": {
        "proposed_decision": "accept",
        "proposed_source_locator": "pdf_pages_52_53",
        "reason": "建议接受；将定位修正为pdf_pages_52_53，等待作者视觉核验。",
    },
    "EV-T0-51D65DEC82584BCD": {
        "proposed_decision": "accept",
        "proposed_nature": "constraint",
        "reason": "建议接受，性质修订为constraint，仍等待作者视觉核验。",
    },
    "EV-T0-246C331615E11FB3": {
        "proposed_decision": "exclude",
        "reason": "建议排除；与EV-T0-8E35B508DF5C2E65同段事实重合。",
    },
    "EV-T0-6FE734DE05953A3C": {
        "proposed_decision": "accept",
        "proposed_nature": "constraint",
        "proposed_dimensions": [
            "trust_governance_constraints",
            "engineering_resource_conditions",
        ],
        "reason": "建议接受；性质修订为constraint，维度按协议要求保留治理约束和工程资源条件。",
    },
    "EV-T0-04572D780936A8D6": {
        "proposed_decision": "accept",
        "proposed_nature": "goal",
        "reason": "建议接受，性质保持goal，仍等待作者视觉核验。",
    },
    "EV-T0-CA999183D4D473C4": {
        "proposed_decision": "exclude",
        "reason": "继续建议排除：与既有同段事实重复。",
    },
    "EV-T0-1C84CB9C7F271D13": {
        "proposed_decision": "exclude",
        "reason": "继续建议排除：与既有同一事实重复。",
    },
}

RECOVERY_CORRECTIONS: dict[str, dict[str, Any]] = {
    "EV-T0-2D9AD11D628EFF58": {
        "proposed_decision": "revise",
        "proposed_nature": "constraint",
        "proposed_review_note": "性质修订为constraint；保留原摘录和事实范围，等待作者审核。",
    },
    "EV-T0-A515E3B9391C51B4": {
        "proposed_decision": "revise",
        "proposed_nature": "constraint",
        "proposed_review_note": "normalized_claim聚焦路线图延误；不扩展主体、时间、数字或结果。",
    },
    "EV-T0-794C24A620FE0968": {
        "proposed_decision": "split",
        "proposed_nature": "test_event + goal",
        "proposed_dimensions": ["application_maturity"],
        "proposed_review_note": "拆分为开发周期已开始的test_event和预计展示的goal；两个拆分建议均另列非空维度。",
    },
    "EV-T0-E4EADC8D35D78E4A": {
        "proposed_decision": "revise",
        "proposed_nature": "evaluation",
        "proposed_review_note": "性质修订为evaluation，并标注为来源方自我评价；不推断已部署能力。",
    },
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        _relative(path): _sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _protected_output_hashes(exclude_dir: Path | None = None) -> dict[str, str]:
    outputs = ROOT / "outputs"
    exclude = exclude_dir.resolve() if exclude_dir else None
    values: dict[str, str] = {}
    for path in sorted(outputs.rglob("*")):
        if not path.is_file():
            continue
        if exclude and (path.resolve() == exclude or exclude in path.resolve().parents):
            continue
        values[_relative(path)] = _sha256_file(path)
    return values


def _digest_map(values: dict[str, str]) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable_not_a_git_repository"


def _blank_final_fields(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in FINAL_FIELDS:
        result[field] = ""
    result["human_reviewed"] = False
    return result


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _base_preannotation(row: dict[str, Any]) -> dict[str, Any]:
    result = _blank_final_fields(dict(row))
    result["model_generated"] = True
    result["human_reviewed"] = False
    result["proposed_dimensions"] = _as_list(row.get("proposed_dimensions"))
    return result


def _apply_visual_suggestion(row: dict[str, Any], suggestion: dict[str, Any]) -> dict[str, Any]:
    result = _base_preannotation(row)
    result["record_origin"] = "p3br_visual_queue_ai_assisted_recommendation"
    result["risk_level"] = "high"
    result["proposed_decision"] = suggestion["proposed_decision"]
    if suggestion.get("proposed_source_locator"):
        result["proposed_source_locator"] = suggestion["proposed_source_locator"]
    if suggestion.get("proposed_nature"):
        result["proposed_nature"] = suggestion["proposed_nature"]
    if suggestion.get("proposed_dimensions"):
        result["proposed_dimensions"] = suggestion["proposed_dimensions"]
    result["risk_reason"] = "PDF定位存在视觉复核风险；AI辅助预复核建议不能代替作者审核。"
    result["proposed_review_note"] = suggestion["reason"]
    result["human_reviewed"] = False
    return result


def _recovery_preannotation(record: dict[str, Any]) -> dict[str, Any]:
    result = {
        "evidence_id": record.get("evidence_id", ""),
        "source_id": record.get("source_id", ""),
        "source_locator": record.get("source_locator") or record.get("page_or_section", ""),
        "proposed_decision": "accept",
        "proposed_nature": record.get("evidence_nature", ""),
        "proposed_dimensions": record.get("coding_dimensions", []),
        "proposed_normalized_claim": record.get("normalized_claim", ""),
        "proposed_excerpt_zh": record.get("excerpt_zh", ""),
        "risk_level": "high",
        "risk_reason": "恢复候选需重新确认性质、维度、定位或拆分边界。",
        "proposed_review_note": "恢复候选；确定性修复不能代替作者审核。",
        "model_generated": True,
        "human_reviewed": False,
        "record_origin": "p3br_recovered_candidate",
        "proposed_source_locator": record.get("source_locator") or record.get("page_or_section", ""),
        "recovery_origin": record.get("recovery_origin", ""),
        "original_error": record.get("original_error", []),
        "repair_action": record.get("repair_action", []),
        "model_generated_fields": record.get("model_generated_fields", []),
        "semantic_review_result": record.get("semantic_review_result", {}),
    }
    correction = RECOVERY_CORRECTIONS.get(result["evidence_id"])
    if correction:
        result.update(correction)
    if result["evidence_id"] == "EV-T0-794C24A620FE0968":
        result["proposed_split_parts"] = [
            {
                "part_index": 1,
                "split_focus": "开发周期已开始",
                "proposed_nature": "test_event",
                "proposed_dimensions": ["application_maturity"],
            },
            {
                "part_index": 2,
                "split_focus": "预计展示",
                "proposed_nature": "goal",
                "proposed_dimensions": ["application_maturity"],
            },
        ]
    return _blank_final_fields(result)


def _visual_log_row(queue_row: dict[str, str]) -> dict[str, Any]:
    evidence_id = queue_row["evidence_id"]
    suggestion = VISUAL_SUGGESTIONS[evidence_id]
    return {
        "evidence_id": evidence_id,
        "source_id": queue_row.get("source_id", ""),
        "source_locator": queue_row.get("source_locator", ""),
        "proposed_source_locator": suggestion.get("proposed_source_locator", queue_row.get("source_locator", "")),
        "current_nature": queue_row.get("evidence_nature", ""),
        "proposed_nature": suggestion.get("proposed_nature", queue_row.get("evidence_nature", "")),
        "current_dimensions": _as_list(queue_row.get("coding_dimensions")),
        "proposed_dimensions": suggestion.get("proposed_dimensions", _as_list(queue_row.get("coding_dimensions"))),
        "proposed_decision": suggestion["proposed_decision"],
        "reason": suggestion["reason"],
        "review_origin": "ai_assisted_visual_pre_review_reused_without_new_model_call",
        "model_generated": False,
        "ai_assisted": True,
        "human_reviewed": False,
        "final_decision": "",
        "human_corrected_excerpt": "",
        "reviewer": "",
        "reviewed_at": "",
        "review_note": "",
    }


def _author_row(row: dict[str, Any]) -> dict[str, Any]:
    result = _blank_final_fields(dict(row))
    result["human_reviewed"] = False
    return result


def _decision_fieldnames() -> list[str]:
    return [
        "evidence_id",
        "source_id",
        "source_locator",
        "proposed_source_locator",
        "proposed_decision",
        "proposed_nature",
        "proposed_dimensions",
        "proposed_normalized_claim",
        "proposed_excerpt_zh",
        "risk_level",
        "risk_reason",
        "proposed_review_note",
        "model_generated",
        "human_reviewed",
        "record_origin",
        "final_decision",
        "final_nature",
        "final_dimensions",
        "final_normalized_claim",
        "human_corrected_excerpt",
        "final_excerpt_zh",
        "reviewer",
        "reviewed_at",
        "review_note",
    ]


def _diff_row(evidence_id: str, field: str, before: Any, after: Any, reason: str, origin: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "field": field,
        "before": before,
        "after": after,
        "reason": reason,
        "review_origin": origin,
        "human_reviewed": False,
    }


def build_run(output_dir: Path) -> dict[str, Any]:
    executed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    protected_before = _protected_output_hashes(output_dir)
    input_tree_before = _tree_hashes(INPUT_DIR)

    original_rows = _read_csv(INPUT_DIR / "g1_ai_preannotation.csv")
    visual_queue = _read_csv(INPUT_DIR / "g1_visual_rescue_queue.csv")
    recovered = _read_jsonl(INPUT_DIR / "recovered_candidates.jsonl")
    recovered_by_id = {row.get("evidence_id", ""): row for row in recovered}
    original_by_id = {row.get("evidence_id", ""): row for row in original_rows}

    revised_rows: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    for original in original_rows:
        row = _base_preannotation(original)
        revised_rows.append(row)

    recovered_rows = [_recovery_preannotation(row) for row in recovered]
    revised_rows.extend(recovered_rows)

    visual_rows = [_visual_log_row(row) for row in visual_queue]
    visual_by_id = {row["evidence_id"]: row for row in visual_rows}

    for evidence_id, suggestion in VISUAL_SUGGESTIONS.items():
        if evidence_id not in visual_by_id:
            raise ValueError(f"visual suggestion missing from immutable queue: {evidence_id}")

    for row in revised_rows:
        evidence_id = row.get("evidence_id", "")
        if evidence_id in RECOVERY_CORRECTIONS:
            before = recovered_by_id[evidence_id]
            correction = RECOVERY_CORRECTIONS[evidence_id]
            if "proposed_nature" in correction:
                diffs.append(_diff_row(evidence_id, "proposed_nature", before.get("evidence_nature", ""), correction["proposed_nature"], "按P3b-R2性质纠偏要求修订。", "protocol_directed_ai_preannotation_revision"))
            if evidence_id == "EV-T0-A515E3B9391C51B4":
                diffs.append(_diff_row(evidence_id, "proposed_normalized_claim", before.get("normalized_claim", ""), row.get("proposed_normalized_claim", ""), "确认规范化声明聚焦路线图延误，且不扩展原事实范围。", "protocol_directed_ai_preannotation_revision"))
            if evidence_id == "EV-T0-794C24A620FE0968":
                diffs.append(_diff_row(evidence_id, "split_proposals", "单条混合命题", row.get("proposed_split_parts", []), "拆分为开发周期已开始的test_event和预计展示的goal，两个部分均有非空维度。", "protocol_directed_ai_preannotation_revision"))
            if evidence_id == "EV-T0-E4EADC8D35D78E4A":
                diffs.append(_diff_row(evidence_id, "proposed_review_note", before.get("evidence_nature", ""), correction["proposed_review_note"], "标明来源方自我评价属性。", "protocol_directed_ai_preannotation_revision"))

    for evidence_id, suggestion in VISUAL_SUGGESTIONS.items():
        original = next(row for row in visual_queue if row["evidence_id"] == evidence_id)
        if "proposed_source_locator" in suggestion:
            diffs.append(_diff_row(evidence_id, "proposed_source_locator", original.get("source_locator", ""), suggestion["proposed_source_locator"], suggestion["reason"], "ai_assisted_visual_pre_review"))
        if "proposed_nature" in suggestion:
            diffs.append(_diff_row(evidence_id, "proposed_nature", original.get("evidence_nature", ""), suggestion["proposed_nature"], suggestion["reason"], "ai_assisted_visual_pre_review"))
        if "proposed_dimensions" in suggestion:
            diffs.append(_diff_row(evidence_id, "proposed_dimensions", _as_list(original.get("coding_dimensions")), suggestion["proposed_dimensions"], suggestion["reason"], "ai_assisted_visual_pre_review"))
        diffs.append(_diff_row(evidence_id, "proposed_decision", original.get("recommended_action", ""), suggestion["proposed_decision"], suggestion["reason"], "ai_assisted_visual_pre_review"))

    original_high = [row for row in original_rows if row.get("risk_level") == "high"]
    original_medium = [row for row in original_rows if row.get("risk_level") == "medium"]
    original_low = [row for row in original_rows if row.get("risk_level") == "low"]
    recovered_high = recovered_rows
    visual_high = [_apply_visual_suggestion(next(row for row in visual_queue if row["evidence_id"] == item["evidence_id"]), VISUAL_SUGGESTIONS[item["evidence_id"]]) for item in visual_rows]

    high_rows = [_author_row(row) for row in original_high + recovered_high + visual_high]
    medium_rows = [_author_row(_base_preannotation(row)) for row in original_medium]
    sample_size = math.ceil(len(original_low) * 0.20)
    low_sorted = sorted(original_low, key=lambda row: hashlib.sha256(row["evidence_id"].encode("utf-8")).hexdigest())
    low_sample = [_author_row(_base_preannotation(row)) for row in low_sorted[:sample_size]]
    author_rows = high_rows + medium_rows + low_sample
    if len({row.get("evidence_id") for row in author_rows}) != len(author_rows):
        raise ValueError("duplicate evidence_id in author decision sheet")
    diffs.append(_diff_row("__AUTHOR_SAMPLING__", "row_count", 51, len(author_rows), "作者表补入全部medium，并按ceil(20%)抽取low。", "p3br2_sampling_correction"))

    split_rows: list[dict[str, Any]] = []
    parent = next(row for row in recovered_rows if row["evidence_id"] == "EV-T0-794C24A620FE0968")
    for part in parent["proposed_split_parts"]:
        split_rows.append({
            "parent_evidence_id": parent["evidence_id"],
            "source_id": parent["source_id"],
            "source_locator": parent["source_locator"],
            "part_index": part["part_index"],
            "split_focus": part["split_focus"],
            "proposed_nature": part["proposed_nature"],
            "proposed_dimensions": part["proposed_dimensions"],
            "proposed_excerpt_zh": "",
            "human_reviewed": False,
            "final_decision": "",
            "reviewer": "",
            "reviewed_at": "",
            "review_note": "",
        })

    all_author_ids = {row["evidence_id"] for row in author_rows}
    expected_high = len(original_high) + len(recovered_high) + len(visual_high)
    if (expected_high, len(medium_rows), len(low_sample), len(author_rows)) != (49, 4, 2, 55):
        raise ValueError(f"unexpected risk coverage: {expected_high}, {len(medium_rows)}, {len(low_sample)}, {len(author_rows)}")

    preannotation_fieldnames = _decision_fieldnames() + ["proposed_split_parts", "recovery_origin", "original_error", "repair_action", "model_generated_fields", "semantic_review_result"]
    _write_csv(output_dir / "g1_ai_preannotation_p3br2.csv", revised_rows, preannotation_fieldnames)
    _write_csv(output_dir / "g1_author_high_priority_p3br2.csv", high_rows, _decision_fieldnames())
    _write_csv(output_dir / "g1_author_low_risk_sample_p3br2.csv", low_sample, _decision_fieldnames())
    _write_csv(output_dir / "g1_author_decision_sheet_p3br2.csv", author_rows, _decision_fieldnames())
    _write_jsonl(output_dir / "ai_visual_pre_review_log.jsonl", visual_rows)
    _write_jsonl(output_dir / "recovery_split_proposals.jsonl", split_rows)
    _write_jsonl(output_dir / "evidence_candidates_p3br2_combined.jsonl", revised_rows)
    _write_csv(output_dir / "p3br2_before_after_diff.csv", diffs, ["evidence_id", "field", "before", "after", "reason", "review_origin", "human_reviewed"])

    all_preannotation_decisions = Counter(row.get("proposed_decision", "") for row in revised_rows + visual_high)
    risk_report = [
        "# P3b-R2风险覆盖核验报告",
        "",
        "本报告是审核门禁准备材料，不是正式G1审核结果。所有作者字段保持空白，human_reviewed=false。",
        "",
        f"- P3b原43条：{len(original_rows)}",
        f"- Schema恢复条数：{len(recovered_rows)}",
        f"- 视觉待恢复队列：{len(visual_rows)}（其中视觉核验5条、重复排除2条）",
        f"- AI建议接受/修改/拆分/排除（预标注+视觉队列）：{all_preannotation_decisions.get('accept', 0)}/{all_preannotation_decisions.get('revise', 0)}/{all_preannotation_decisions.get('split', 0)}/{all_preannotation_decisions.get('exclude', 0)}",
        "",
        f"- high总量与作者覆盖：{expected_high}/{expected_high}",
        f"- medium总量与作者覆盖：{len(medium_rows)}/{len(medium_rows)}",
        f"- low总量：{len(original_low)}；ceil(20%)样本量：{sample_size}；实际抽样：{len(low_sample)}",
        f"- 低风险确定性样本：{', '.join(row['evidence_id'] for row in low_sample)}",
        "- medium漏失：无",
        f"- 作者实际审核数：0；human_reviewed=true记录数：{sum(str(row.get('human_reviewed')).lower() == 'true' for row in author_rows)}",
        "",
        "## 重点纠偏",
        "",
        "- EV-T0-794C24A620FE0968保留一个父级拆分建议，并另存两个具有非空维度的拆分部分；未创建正式Evidence子记录。",
        "- 视觉预复核建议只作为AI辅助建议记录，不等同于人工视觉核验。",
        "- P3BR v3及此前运行目录未被改写。",
    ]
    (output_dir / "p3br2_risk_coverage_report.md").write_text("\n".join(risk_report) + "\n", encoding="utf-8")

    output_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    protected_after = _protected_output_hashes(output_dir)
    manifest: dict[str, Any] = {
        "run_id": output_dir.name,
        "case_id": "us_military_ai_deployment",
        "stage": "P3b-R2",
        "executed_at_utc": executed_at,
        "git_head": _git_head(),
        "execution_mode": "offline_artifact_recomposition_no_model_call",
        "input_run_id": INPUT_RUN_ID,
        "protected_prior_outputs_unchanged": protected_before == protected_after,
        "protected_outputs_before_digest": _digest_map(protected_before),
        "protected_outputs_after_digest": _digest_map(protected_after),
        "input_v3_tree_digest_before": _digest_map(input_tree_before),
        "input_v3_tree_digest_after": _digest_map(_tree_hashes(INPUT_DIR)),
        "input_v3_hashes": input_tree_before,
        "p3b_original_candidate_count": len(original_rows),
        "schema_recovered_candidate_count": len(recovered_rows),
        "visual_queue_count": len(visual_rows),
        "visual_review_count": sum(row["proposed_decision"] != "exclude" for row in visual_rows),
        "visual_duplicate_exclusion_count": sum(row["proposed_decision"] == "exclude" for row in visual_rows),
        "high_total": expected_high,
        "high_author_coverage": len(high_rows),
        "medium_total": len(medium_rows),
        "medium_author_coverage": len(medium_rows),
        "medium_missing_from_author_decision_sheet": sorted(set(row["evidence_id"] for row in original_medium) - all_author_ids),
        "low_total": len(original_low),
        "low_sample_size": sample_size,
        "low_sample_count": len(low_sample),
        "author_decision_row_count": len(author_rows),
        "author_actual_review_count": 0,
        "all_author_final_fields_blank": all(all(str(row.get(field, "")) == "" for field in FINAL_FIELDS) for row in author_rows),
        "human_reviewed_true_count": sum(bool(row.get("human_reviewed")) for row in author_rows),
        "ai_preannotation_row_count": len(revised_rows),
        "ai_recommendation_counts": dict(all_preannotation_decisions),
        "revision_diff_count": len(diffs),
        "model_client_called": False,
        "provider_call_count": 0,
        "api_key_written": False,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "g1_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "stop_after_p3br2": True,
        "output_file_hashes": output_hashes,
    }
    _write_json(output_dir / "p3br2_manifest.json", manifest)
    # The manifest is intentionally excluded from its own output hash map.
    manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    _write_json(output_dir / "p3br2_manifest.json", manifest)
    return manifest


def new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = ROOT / "outputs" / f"{OUTPUT_PREFIX}{stamp}-gate-correction-v1"
    suffix = 2
    while candidate.exists():
        candidate = ROOT / "outputs" / f"{OUTPUT_PREFIX}{stamp}-gate-correction-v{suffix}"
        suffix += 1
    return candidate


if __name__ == "__main__":
    directory = new_run_dir()
    manifest = build_run(directory)
    print(json.dumps({"run_id": manifest["run_id"], "author_decision_row_count": manifest["author_decision_row_count"]}, ensure_ascii=False))
