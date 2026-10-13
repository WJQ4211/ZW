"""P3b-R2a content-completeness repair.

The input is the immutable P3BR2 author-preparation output.  This step only
repairs field projection, creates non-formal split proposals, and prepares a
sign-off document.  It does not call a model, read source documents or T1,
write formal Evidence, freeze a snapshot, or execute G1/P4.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INPUT_RUN_ID = "P3BR2-20260826T050236Z-gate-correction-v1"
INPUT_DIR = ROOT / "outputs" / INPUT_RUN_ID
SOURCE_RUN_ID = "P3BR-20260826T064000Z-recovery-v3"
SOURCE_DIR = ROOT / "outputs" / SOURCE_RUN_ID
OUTPUT_PREFIX = "P3BR2A-"

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

ALLOWED_DIMENSIONS = {
    "application_maturity",
    "human_machine_authority",
    "integration_level",
    "engineering_resource_conditions",
    "trust_governance_constraints",
    "organizational_system_fit",
}

# These are author-directed split proposals, not model output and not formal
# Evidence.  The English excerpts themselves are always taken from the
# immutable P3b-R source record at runtime.
SPLIT_CHILD_SPECS: dict[str, list[dict[str, Any]]] = {
    "EV-T0-DB1424F52E33432F": [
        {
            "focus": "历史维护数据用于模型训练",
            "excerpt_zh": "联合后勤任务倡议团队使用历史维护数据训练AI模型，以预测超出正常时长的维护周期。",
            "claim": "历史维护数据被用于训练AI模型，以预测超出正常时长的维护周期。",
            "nature": "resource_input",
            "dimensions": ["engineering_resource_conditions"],
        },
        {
            "focus": "初始模型演示",
            "excerpt_zh": "初始后勤模型于2021年2月11日进行了演示。",
            "claim": "初始后勤模型于2021年2月11日进行了演示。",
            "nature": "test_event",
            "dimensions": ["application_maturity"],
        },
    ],
    "EV-T0-F25379FE058D2680": [
        {
            "focus": "2021年初首批协议已经授予",
            "excerpt_zh": "Tradewind项下的首批协议已于2021年初授予。",
            "claim": "Tradewind项下的首批协议已于2021年初授予。",
            "nature": "resource_input",
            "dimensions": ["engineering_resource_conditions"],
        },
        {
            "focus": "预计2023财年授予数十项协议",
            "excerpt_zh": "国防部预计在2023财年通过Tradewind授予数十份协议。",
            "claim": "国防部预计在2023财年通过Tradewind授予数十份协议。",
            "nature": "goal",
            "dimensions": ["engineering_resource_conditions"],
        },
    ],
    "EV-T0-794C24A620FE0968": [
        {
            "focus": "首个开发周期于2023年12月启动",
            "excerpt_zh": "首个开发周期（spiral challenge）于2023年12月启动。",
            "claim": "首个开发周期于2023年12月启动。",
            "nature": "test_event",
            "dimensions": ["application_maturity"],
        },
        {
            "focus": "预计2024年3月展示完整工作解决方案",
            "excerpt_zh": "执行方预计在2024年3月展示首个挑战的完整工作解决方案。",
            "claim": "执行方预计在2024年3月展示首个挑战的完整工作解决方案。",
            "nature": "goal",
            "dimensions": ["application_maturity"],
        },
    ],
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in fieldnames})


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


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {
        _relative(path): _sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _protected_hashes(exclude_dir: Path | None = None) -> dict[str, str]:
    outputs = ROOT / "outputs"
    excluded = exclude_dir.resolve() if exclude_dir else None
    return {
        _relative(path): _sha256_file(path)
        for path in sorted(outputs.rglob("*"))
        if path.is_file() and not (excluded and (path.resolve() == excluded or excluded in path.resolve().parents))
    }


def _digest(values: dict[str, str]) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable_not_a_git_repository"


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


def _blank_final_fields(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in FINAL_FIELDS:
        result[field] = ""
    result["human_reviewed"] = False
    return result


def _source_records() -> dict[str, dict[str, Any]]:
    # P3BR2 intentionally contains only preannotation fields.  The original
    # excerpt and translation are therefore joined from the immutable P3BR v3
    # candidate/visual records; no source document is opened here.
    combined = {row["evidence_id"]: row for row in _read_jsonl(SOURCE_DIR / "evidence_candidates_p3br_combined.jsonl")}
    recovered = {row["evidence_id"]: row for row in _read_jsonl(SOURCE_DIR / "recovered_candidates.jsonl")}
    visual = {row["evidence_id"]: row for row in _read_csv(SOURCE_DIR / "g1_visual_rescue_queue.csv")}
    records = dict(combined)
    records.update(recovered)
    records.update(visual)
    return records


def _visual_records() -> dict[str, dict[str, Any]]:
    return {row["evidence_id"]: row for row in _read_jsonl(INPUT_DIR / "ai_visual_pre_review_log.jsonl")}


def _complete_author_row(row: dict[str, str], sources: dict[str, dict[str, Any]], visual: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = _blank_final_fields(dict(row))
    evidence_id = row["evidence_id"]
    source = sources.get(evidence_id)
    if source is None:
        raise ValueError(f"no immutable source record for {evidence_id}")
    if evidence_id in visual:
        visual_row = visual[evidence_id]
        result.update(
            {
                "source_locator": visual_row["source_locator"],
                "proposed_source_locator": visual_row.get("proposed_source_locator") or visual_row["source_locator"],
                "proposed_nature": visual_row.get("proposed_nature") or visual_row.get("current_nature", ""),
                "proposed_dimensions": visual_row.get("proposed_dimensions") or visual_row.get("current_dimensions", []),
                "proposed_normalized_claim": source.get("normalized_claim", ""),
                "proposed_excerpt_zh": source.get("excerpt_zh", ""),
                "excerpt_original": source.get("excerpt_original", ""),
                "model_generated": False,
                "ai_assisted": True,
                "human_reviewed": False,
            }
        )
    else:
        result.update(
            {
                "proposed_source_locator": row.get("proposed_source_locator") or row.get("source_locator", ""),
                "proposed_nature": row.get("proposed_nature") or source.get("evidence_nature", ""),
                "proposed_dimensions": _as_list(row.get("proposed_dimensions")) or source.get("coding_dimensions", []),
                "proposed_normalized_claim": row.get("proposed_normalized_claim") or source.get("normalized_claim", ""),
                "proposed_excerpt_zh": row.get("proposed_excerpt_zh") or source.get("excerpt_zh", ""),
                "excerpt_original": source.get("excerpt_original", ""),
                "model_generated": row.get("model_generated", "true"),
                "ai_assisted": False,
                "human_reviewed": False,
            }
        )
    result["proposed_dimensions"] = _as_list(result.get("proposed_dimensions"))
    result["final_decision"] = ""
    result["final_nature"] = ""
    result["final_dimensions"] = ""
    result["final_normalized_claim"] = ""
    result["human_corrected_excerpt"] = ""
    result["final_excerpt_zh"] = ""
    result["reviewer"] = ""
    result["reviewed_at"] = ""
    result["review_note"] = ""
    return result


def _split_original_excerpt(text: str) -> list[str]:
    # Split only at sentence/conjunction boundaries; never manufacture text.
    candidates = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    if len(candidates) == 2:
        return candidates
    candidates = [part.strip() for part in re.split(r",\s+and\s+", text.strip(), maxsplit=1) if part.strip()]
    if len(candidates) == 2:
        return candidates
    return [text.strip(), text.strip()]


def _child_id(parent_id: str, part_index: int, source_id: str, locator: str) -> str:
    value = f"{parent_id}|{part_index}|{source_id}|{locator}".encode("utf-8")
    return "PCH-T0-" + hashlib.sha256(value).hexdigest()[:16].upper()


def _split_children(parent: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    parent_id = parent["evidence_id"]
    specs = SPLIT_CHILD_SPECS[parent_id]
    excerpts = _split_original_excerpt(source.get("excerpt_original", ""))
    children: list[dict[str, Any]] = []
    for index, spec in enumerate(specs, start=1):
        locator = parent.get("source_locator", "")
        child = {
            "proposal_child_id": _child_id(parent_id, index, parent["source_id"], locator),
            "parent_evidence_id": parent_id,
            "part_index": index,
            "source_id": parent["source_id"],
            "source_locator": locator,
            "excerpt_original": excerpts[index - 1],
            "proposed_excerpt_zh": spec["excerpt_zh"],
            "proposed_normalized_claim": spec["claim"],
            "proposed_nature": spec["nature"],
            "proposed_dimensions": spec["dimensions"],
            "proposed_decision": "accept",
            "human_reviewed": False,
            "final_decision": "",
            "reviewer": "",
            "reviewed_at": "",
            "review_note": "",
            "lineage_status": "proposal_only_parent_split_pending_g1",
        }
        children.append(child)
    return children


def _signoff_summary(rows: list[dict[str, Any]], children_by_parent: dict[str, list[dict[str, Any]]]) -> str:
    groups = ("accept", "revise", "split", "exclude")
    lines = [
        "# G1-T0作者签核摘要（P3b-R2a）",
        "",
        "本文件仅为作者审核准备材料，不是正式G1结果。所有final_*、reviewer、reviewed_at为空，human_reviewed=false。",
        "",
    ]
    for decision in groups:
        selected = [row for row in rows if row.get("proposed_decision") == decision]
        lines.extend([f"## {decision}（{len(selected)}项）", ""])
        for number, row in enumerate(selected, start=1):
            lines.extend(
                [
                    f"### {number}. {row['evidence_id']}",
                    f"- 来源定位：{row.get('source_id', '')} / {row.get('source_locator', '')}",
                    f"- 原文摘录：{row.get('excerpt_original', '')}",
                    f"- 规范化中文声明：{row.get('proposed_normalized_claim', '')}",
                    f"- 建议性质：{row.get('proposed_nature', '')}",
                    f"- 建议维度：{json.dumps(row.get('proposed_dimensions', []), ensure_ascii=False)}",
                    f"- 建议决定：{row.get('proposed_decision', '')}",
                    f"- 简短理由：{row.get('proposed_review_note', '') or row.get('risk_reason', '')}",
                    "",
                ]
            )
            if decision == "split":
                for child in children_by_parent.get(row["evidence_id"], []):
                    lines.extend(
                        [
                            f"  - 子建议 {child['proposal_child_id']}（part_{child['part_index']}）",
                            f"    - 原文摘录：{child['excerpt_original']}",
                            f"    - 中文摘录：{child['proposed_excerpt_zh']}",
                            f"    - 规范化中文声明：{child['proposed_normalized_claim']}",
                            f"    - 性质：{child['proposed_nature']}",
                            f"    - 维度：{json.dumps(child['proposed_dimensions'], ensure_ascii=False)}",
                            "",
                        ]
                    )
    return "\n".join(lines) + "\n"


def build_run(output_dir: Path) -> dict[str, Any]:
    executed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    protected_before = _protected_hashes(output_dir)
    input_hashes = _tree_hashes(INPUT_DIR)
    author_rows_in = _read_csv(INPUT_DIR / "g1_author_decision_sheet_p3br2.csv")
    sources = _source_records()
    visual = _visual_records()
    author_rows = [_complete_author_row(row, sources, visual) for row in author_rows_in]
    by_id = {row["evidence_id"]: row for row in author_rows}
    split_children: list[dict[str, Any]] = []
    for parent_id in SPLIT_CHILD_SPECS:
        if parent_id not in by_id:
            raise ValueError(f"split parent absent from author sheet: {parent_id}")
        if by_id[parent_id].get("proposed_decision") != "split":
            raise ValueError(f"split parent decision changed: {parent_id}")
        split_children.extend(_split_children(by_id[parent_id], sources[parent_id]))
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for child in split_children:
        children_by_parent.setdefault(child["parent_evidence_id"], []).append(child)

    dimensions_invalid = [
        (row["evidence_id"], dimension)
        for row in author_rows
        for dimension in row.get("proposed_dimensions", [])
        if dimension not in ALLOWED_DIMENSIONS
    ]
    completeness = {
        field: sum(bool(row.get(field)) for row in author_rows)
        for field in ("proposed_nature", "proposed_dimensions", "proposed_normalized_claim", "proposed_excerpt_zh", "excerpt_original")
    }
    decision_counts = Counter(row.get("proposed_decision", "") for row in author_rows)
    if len(author_rows) != 55 or len({row["evidence_id"] for row in author_rows}) != 55:
        raise ValueError("author decision sheet must contain 55 unique rows")
    if dict(decision_counts) != {"accept": 15, "revise": 34, "split": 3, "exclude": 3}:
        raise ValueError(f"unexpected author decision counts: {decision_counts}")
    if any(value != 55 for value in completeness.values()):
        raise ValueError(f"incomplete author fields: {completeness}")
    if dimensions_invalid:
        raise ValueError(f"invalid dimensions: {dimensions_invalid}")
    if len(split_children) != 6 or len({child["proposal_child_id"] for child in split_children}) != 6:
        raise ValueError("split child proposals must contain six unique IDs")

    decision_fields = [
        "evidence_id",
        "source_id",
        "source_locator",
        "proposed_source_locator",
        "excerpt_original",
        "proposed_decision",
        "proposed_nature",
        "proposed_dimensions",
        "proposed_normalized_claim",
        "proposed_excerpt_zh",
        "risk_level",
        "risk_reason",
        "proposed_review_note",
        "model_generated",
        "ai_assisted",
        "human_reviewed",
        "record_origin",
        *FINAL_FIELDS,
    ]
    _write_csv(output_dir / "g1_author_decision_sheet_p3br2a.csv", author_rows, decision_fields)
    _write_jsonl(output_dir / "split_child_proposals_p3br2a.jsonl", split_children)
    _write_jsonl(output_dir / "ai_visual_pre_review_log_p3br2a.jsonl", list(visual.values()))
    (output_dir / "g1_author_signoff_summary.md").parent.mkdir(parents=True, exist_ok=True)
    (output_dir / "g1_author_signoff_summary.md").write_text(_signoff_summary(author_rows, children_by_parent), encoding="utf-8")

    report = [
        "# P3b-R2a字段完整性报告",
        "",
        f"- 作者决策行数：{len(author_rows)}；唯一evidence_id：{len({row['evidence_id'] for row in author_rows})}",
        f"- 决定分布：accept={decision_counts['accept']}，revise={decision_counts['revise']}，split={decision_counts['split']}，exclude={decision_counts['exclude']}",
        f"- proposed_nature非空：{completeness['proposed_nature']}/{len(author_rows)}",
        f"- proposed_dimensions非空且合法：{completeness['proposed_dimensions']}/{len(author_rows)}",
        f"- proposed_normalized_claim非空：{completeness['proposed_normalized_claim']}/{len(author_rows)}",
        f"- proposed_excerpt_zh非空：{completeness['proposed_excerpt_zh']}/{len(author_rows)}",
        f"- excerpt_original非空：{completeness['excerpt_original']}/{len(author_rows)}",
        f"- 视觉字段映射项：{len(visual)}/7",
        f"- split父项：3；完整子建议：{len(split_children)}/6；子ID唯一：是",
        "- 所有final_*、reviewer、reviewed_at为空；human_reviewed=false",
        "- 本步骤未调用模型、未读取T1、未执行正式G1/P4、未写入Evidence、未冻结快照",
    ]
    (output_dir / "p3br2a_quality_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    protected_after = _protected_hashes(output_dir)
    output_hashes = {
        path.relative_to(output_dir).as_posix(): _sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    manifest: dict[str, Any] = {
        "run_id": output_dir.name,
        "case_id": "us_military_ai_deployment",
        "stage": "P3b-R2a",
        "executed_at_utc": executed_at,
        "input_run_id": INPUT_RUN_ID,
        "git_head": _git_head(),
        "python_version": platform.python_version(),
        "implementation_file_hashes": {
            "src/p3br2a.py": _sha256_file(ROOT / "src/p3br2a.py"),
            "scripts/15_run_p3br2a.py": _sha256_file(ROOT / "scripts/15_run_p3br2a.py"),
            "tests/test_p3br2a.py": _sha256_file(ROOT / "tests/test_p3br2a.py"),
        },
        "input_p3br2_hashes": input_hashes,
        "protected_prior_outputs_unchanged": protected_before == protected_after,
        "protected_outputs_before_digest": _digest(protected_before),
        "protected_outputs_after_digest": _digest(protected_after),
        "author_decision_row_count": len(author_rows),
        "author_unique_evidence_id_count": len({row["evidence_id"] for row in author_rows}),
        "decision_counts": dict(decision_counts),
        "field_completeness_counts": completeness,
        "visual_items_mapped": len(visual),
        "split_parent_count": 3,
        "split_child_count": len(split_children),
        "split_child_ids_unique": len({child["proposal_child_id"] for child in split_children}) == len(split_children),
        "all_final_fields_blank": all(all(str(row.get(field, "")) == "" for field in FINAL_FIELDS) for row in author_rows),
        "human_reviewed_true_count": sum(bool(row.get("human_reviewed")) for row in author_rows),
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "g1_executed": False,
        "p4_executed": False,
        "snapshot_frozen": False,
        "t1_read": False,
        "model_client_called": False,
        "provider_call_count": 0,
        "api_key_written": False,
        "stop_after_p3br2a": True,
        "output_file_hashes": output_hashes,
    }
    _write_json(output_dir / "p3br2a_manifest.json", manifest)
    manifest["manifest_self_hash_excluded_from_output_file_hashes"] = True
    _write_json(output_dir / "p3br2a_manifest.json", manifest)
    return manifest


def new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = ROOT / "outputs" / f"{OUTPUT_PREFIX}{stamp}-content-completeness-v1"
    suffix = 2
    while candidate.exists():
        candidate = ROOT / "outputs" / f"{OUTPUT_PREFIX}{stamp}-content-completeness-v{suffix}"
        suffix += 1
    return candidate


if __name__ == "__main__":
    directory = new_run_dir()
    result = build_run(directory)
    print(json.dumps({"run_id": result["run_id"], "author_decision_row_count": result["author_decision_row_count"]}, ensure_ascii=False))
