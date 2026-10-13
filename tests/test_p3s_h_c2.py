"""Offline P3S-H-C2 regression tests.

These tests inspect the rendered prompt and the historical v4 audit fixture;
they never call a model API and never modify the historical run.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.p3s import (
    EXTRACTION_TEMPLATE,
    _build_extraction_prompt,
    _load_yaml,
)


ROOT = Path(__file__).resolve().parents[1]
V4_RUN = ROOT / "outputs" / "P3S-20260826T123637Z-supplement-realapi-v4"


def _document():
    from src.p3ar import SourceSegment
    from src.p3s import P3SDocument

    segment = SourceSegment(
        source_id="SYN-001",
        source_locator="web_paragraph_0001",
        text="[PLACEHOLDER_SOURCE_TEXT]",
    )
    mapping = {
        "source_id": "SYN-001",
        "source_locator": "web_paragraph_0001",
        "character_start": 0,
        "character_end": len(segment.text),
        "text": segment.text,
    }
    return P3SDocument(
        source={"source_id": "SYN-001"},
        segments=(segment,),
        paragraph_map=(mapping,),
        source_text_sha256="synthetic-source-sha",
    )


def test_rendered_prompt_embeds_v12_eight_field_contract_and_example():
    template = _load_yaml(EXTRACTION_TEMPLATE)
    prompt = _build_extraction_prompt(_document(), template, "SYN-001-extract-001")
    example = json.dumps(template["output_schema"]["example"], ensure_ascii=False, indent=2)

    assert template["version"] == "1.2"
    assert example in prompt
    assert '"coding_dimensions": [\n' in prompt
    assert '"coding_dimensions": {\n' not in prompt
    assert prompt.count("OUTPUT_ITEM_COUNT: return 0 to 6 objects only") == 2
    assert prompt.count("OUTPUT_FIELDS: exactly eight fields") == 2
    assert prompt.count("Do not enumerate one item per paragraph.") == 2
    assert "Return a JSON array containing 0-6 objects." in prompt
    assert "coding_dimensions is an array, never an object." in prompt
    assert "Do not output additional fields." in prompt
    assert "ten fields" not in prompt.lower()
    source_index = prompt.index("SOURCE_PARAGRAPHS:")
    end_source_index = prompt.index("END_SOURCE_PARAGRAPHS")
    assert prompt.index("OUTPUT_ITEM_COUNT:") < source_index
    assert prompt.rindex("OUTPUT_ITEM_COUNT:") > end_source_index


def test_historical_v4_fixture_expected_limit_accounting_is_read_only():
    manifest = json.loads((V4_RUN / "p3s_manifest.json").read_text(encoding="utf-8"))
    rejected = [
        json.loads(line)
        for line in (V4_RUN / "rejected_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    overflow_batches = [
        row
        for row in rejected
        if row.get("candidate_index") is None
        and row.get("rejection_reasons") == ["candidate_count_exceeds_limit"]
    ]
    assert manifest["raw_candidate_count"] == 60
    assert manifest["provider_call_count"] == 5
    assert len(overflow_batches) == 4
    assert sum(row["candidate_count"] for row in overflow_batches) == 57
    assert (V4_RUN / "p3s_manifest.json").is_file()
