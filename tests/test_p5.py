"""Offline P5 validation tests; no real API call is made here."""

from __future__ import annotations

import json

import pytest

from src.p5 import (
    GEN_FIELDS,
    MERGE_FIELDS,
    SNAPSHOT_ID,
    P5Error,
    build_generator_prompt,
    build_merge_prompt,
    validate_generator_candidates,
    validate_merge_results,
)


def _evidence():
    return [
        {"evidence_id": f"EV-T0-{i:032X}", "source_id": f"T0-SRC-{i % 3}", "source_grade": "A", "published_at": "2024-01-01", "source_locator": f"p{i}", "evidence_nature": "goal", "coding_dimensions": ["application_maturity"], "excerpt_original": f"Source excerpt {i}", "excerpt_zh": f"中文摘录{i}", "normalized_claim": f"命题{i}"}
        for i in range(1, 8)
    ]


def _candidate(generator="A", suffix="01"):
    ids = [row["evidence_id"] for row in _evidence()]
    return {
        "candidate_id": f"GEN-{generator}-H{suffix}",
        "generator_id": generator,
        "hypothesis_statement": "不同条件下能力形成可能呈现阶段性扩展路径。",
        "scope": "2022至2024年公开T0证据所覆盖的美国军用人工智能发展状态。",
        "supporting_evidence_ids": ids[:3],
        "counter_evidence_ids": [ids[3]],
        "key_assumptions": ["证据覆盖范围足以支持比较。"],
        "falsification_conditions": ["后续材料显示该路径不再成立。"],
        "unresolved_questions": ["不同任务的持续使用程度仍待判别。"],
        "covered_dimensions": ["strategic_governance", "technical_engineering_maturity"],
        "confidence": "medium",
    }


def test_generator_schema_has_exactly_eleven_fields_and_evidence_requirements():
    assert set(_candidate()) == GEN_FIELDS
    validated, report = validate_generator_candidates([_candidate(), _candidate(suffix="02")], "A", _evidence())
    assert len(validated) == 2
    assert all(row["valid"] for row in report)


def test_generator_rejects_insufficient_cross_source_support():
    candidate = _candidate()
    candidate["supporting_evidence_ids"] = [row["evidence_id"] for row in _evidence()[:3]]
    evidence = _evidence()
    for row in evidence:
        row["source_id"] = "T0-SAME"
    with pytest.raises(P5Error, match="fewer than 2 sources"):
        validate_generator_candidates([candidate, _candidate(suffix="02")], "A", evidence)


def test_generator_rejects_outside_snapshot_id():
    candidate_1 = _candidate()
    candidate_2 = _candidate(suffix="02")
    candidate_2["supporting_evidence_ids"][0] = "EV-T1-NOT-ALLOWED"
    with pytest.raises(P5Error, match="outside frozen snapshot"):
        validate_generator_candidates([candidate_1, candidate_2], "A", _evidence())


def test_merge_schema_and_lineage_are_restricted_to_validated_candidates():
    evidence = _evidence()
    candidates = [_candidate("A", "01"), _candidate("B", "01"), _candidate("C", "01")]
    merge = {
        "proposed_hypothesis_id": "PH-T0-01",
        "hypothesis_statement": "美国军用人工智能可能沿着受治理和工程条件约束的渐进扩展路径发展。",
        "scope": "T0公开证据覆盖的2022至2024年发展状态。",
        "supporting_evidence_ids": [evidence[0]["evidence_id"], evidence[1]["evidence_id"], evidence[2]["evidence_id"]],
        "counter_evidence_ids": [evidence[3]["evidence_id"]],
        "key_assumptions": ["不同来源证据可进行机制比较。"],
        "falsification_conditions": ["后续观察显示持续使用已普遍发生。"],
        "unresolved_questions": ["跨任务集成程度仍待判别。"],
        "covered_dimensions": ["strategic_governance"],
        "source_candidate_ids": [candidate["candidate_id"] for candidate in candidates],
        "merge_action": "retain",
        "merge_reason": "保留机制差异，未将候选强行合并。",
        "g2_recommendation": "review",
    }
    assert set(merge) == MERGE_FIELDS
    out = validate_merge_results([merge, {**merge, "proposed_hypothesis_id": "PH-T0-02", "merge_action": "revise"}, {**merge, "proposed_hypothesis_id": "PH-T0-03", "merge_action": "merge"}], candidates, evidence)
    assert len(out) == 3
    assert all(set(row["source_candidate_ids"]) <= {candidate["candidate_id"] for candidate in candidates} for row in out)


def test_prompts_contain_frozen_snapshot_and_not_other_generator_outputs():
    evidence = _evidence()
    prompt_a = build_generator_prompt("A", evidence)
    prompt_b = build_generator_prompt("B", evidence)
    assert SNAPSHOT_ID in prompt_a and "FROZEN_EVIDENCE" in prompt_a
    assert "GEN-B-H" not in prompt_a and "GEN-A-H" not in prompt_b
    assert "candidate_id" in build_merge_prompt([_candidate("A"), _candidate("B"), _candidate("C")], evidence)

