"""Shared deterministic preparation and validation helpers for P3a-R.

This module contains no case-specific evidence claims. It only extracts raw
source text, builds auditable locators, and validates model-returned fields
against the supplied source segments.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
import yaml

from .schemas import CODING_DIMENSION_VALUES, EVIDENCE_NATURE_VALUES, EvidenceCandidate, model_dump, model_validate


CASE_ID = "us_military_ai_deployment"
SOURCE_IDS = ("T0-SP-007", "T0-TD-008")
P2_RUN_ID = "P2-20260825T154432Z-author-pilot"
ROOT = Path(__file__).resolve().parents[1]
P2_MANIFEST = ROOT / "outputs" / P2_RUN_ID / "p2_manifest.json"
P2_SOURCE = ROOT / "data/curated/source_manifest_t0_p2.csv"
P2_INVENTORY = ROOT / "outputs" / P2_RUN_ID / "selected_source_inventory.csv"
P1_DIR = ROOT / "outputs/P1-20260825T081142Z-7f4c9b"
P1B_DIR = ROOT / "outputs/P1B-20260825T140911Z-manual"
P1C_DIR = ROOT / "outputs/P1C-20260825T152730Z-manual-web"
SP_PDF = ROOT / "data/staging/t0_candidates/manual/2024-06-RAI-STRATEGY-IMPLEMENTATION-PATHWAY.pdf"
TD_TEXT = P1C_DIR / "web_text/T0-TD-008.txt"
PROMPT_TEMPLATE = ROOT / "prompts/evidence_extract_v1.yaml"

CODING_DIMENSION_DEFINITIONS = {
    "integration_level": "能力集成层级：单工具、单任务、单平台、跨平台、跨任务或体系级集成。",
    "application_maturity": "应用成熟度：概念战略、研发、试验、试点、采购部署或持续运用。",
    "human_machine_authority": "人机权限关系：辅助决策、人在回路中、受监督自主或更高程度自主。仅有系统协同、信息传输或培训文档不足以支持该维度。",
    "engineering_resource_conditions": "工程与资源条件：数据、算力、模型、接口、供应链、预算、人才和保障。",
    "trust_governance_constraints": "可信与治理约束：安全、可靠性、测试评价、授权边界、责任与伦理。",
    "organizational_system_fit": "组织与体系适配：指挥流程、任务编组、跨平台协同、人员负荷和组织变革。",
}


@dataclass(frozen=True)
class SourceSegment:
    source_id: str
    source_locator: str
    text: str
    pdf_page_number: int | None = None
    printed_page_number: int | None = None
    section_title: str = ""
    machine_extracted_anchor: str = ""
    visual_review_required: bool = False
    extraction_status: str = "extracted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_locator": self.source_locator,
            "text": self.text,
            "pdf_page_number": self.pdf_page_number,
            "printed_page_number": self.printed_page_number,
            "section_title": self.section_title,
            "machine_extracted_anchor": self.machine_extracted_anchor,
            "visual_review_required": self.visual_review_required,
            "extraction_status": self.extraction_status,
        }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values), encoding="utf-8")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def relative_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")


def protected_paths() -> list[Path]:
    paths = [
        ROOT / "data/curated/source_manifest_t0.csv",
        ROOT / "data/curated/search_log_t0.csv",
        ROOT / "data/curated/source_manifest_t0_p1b.csv",
        ROOT / "data/curated/search_log_t0_p1b.csv",
        ROOT / "data/curated/source_manifest_t0_p1c.csv",
        ROOT / "data/curated/p2_review_template.csv",
        ROOT / "data/curated/p2_review_decisions_t0.csv",
        P2_SOURCE,
        P2_MANIFEST,
        P2_INVENTORY,
    ]
    for directory in (P1_DIR, P1B_DIR, P1C_DIR, ROOT / "outputs" / P2_RUN_ID):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted({path for path in paths if path.is_file()})


def hash_paths(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in paths if path.is_file()}


def load_source_metadata() -> dict[str, dict[str, str]]:
    rows = {row["source_id"]: row for row in read_csv(P2_SOURCE)}
    missing = set(SOURCE_IDS) - set(rows)
    if missing:
        raise RuntimeError("P2 source manifest is missing: " + ", ".join(sorted(missing)))
    return {source_id: {**rows[source_id], "case_id": CASE_ID} for source_id in SOURCE_IDS}


def _printed_page_number(lines: list[str]) -> int | None:
    for line in reversed(lines[-8:]):
        match = re.fullmatch(r"\s*(\d{1,3})\s*", line)
        if match:
            return int(match.group(1))
    return None


def _section_title(lines: list[str]) -> str:
    for line in lines[:24]:
        value = re.sub(r"\s+", " ", line).strip(" -:;.,")
        if not value or len(value) > 100 or len(value.split()) > 12:
            continue
        letters = [char for char in value if char.isalpha()]
        if letters and sum(char.isupper() for char in letters) / len(letters) >= 0.72:
            return value
    return ""


def _visual_review_required(text: str) -> bool:
    suspicious = (
        r"\bAl\b",
        r"\bAJ\b",
        r"\bRAJ\b",
        r"\bRAT\b",
        r"\bOoD\b",
        r"\bD0D\b",
        r"\bAI/ML\b",
    )
    return any(re.search(pattern, text) for pattern in suspicious)


def extract_pdf_segments(pdf_path: Path, source_id: str) -> tuple[list[SourceSegment], dict[str, Any]]:
    if source_id not in SOURCE_IDS:
        raise ValueError("PDF extraction is only configured for the selected source")
    segments: list[SourceSegment] = []
    non_text_pages: list[int] = []
    ocr_required_pages: list[int] = []
    page_dispositions: dict[str, str] = {}
    layout_review_pages = {25, 26, 27, 28, 29, 30, 37}
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3, layout=False) or ""
            page_status = "extracted"
            if not text.strip():
                non_text_pages.append(page_number)
                if page_number == len(pdf.pages) and page.images:
                    page_status = "decorative_back_cover_no_ocr_required"
                    page_dispositions[str(page_number)] = page_status
                else:
                    page_status = "ocr_pending"
                    ocr_required_pages.append(page_number)
            lines = text.splitlines()
            printed = _printed_page_number(lines)
            section = _section_title(lines)
            printed_token = str(printed) if printed is not None else "pending"
            section_token = re.sub(r"[^A-Za-z0-9]+", "_", section).strip("_") or "pending"
            page_visual_review = _visual_review_required(text) or page_number in layout_review_pages
            anchor = f"pdf_page_{page_number}|printed_page_{printed_token}|section_{section_token}"
            segments.append(
                SourceSegment(
                    source_id=source_id,
                    source_locator=f"pdf_page_{page_number}",
                    text=text,
                    pdf_page_number=page_number,
                    printed_page_number=printed,
                    section_title=section,
                    machine_extracted_anchor=anchor,
                    visual_review_required=page_visual_review,
                    extraction_status=page_status,
                )
            )
        info = {
            "file_name": pdf_path.name,
            "file_sha256": sha256_file(pdf_path),
            "file_size_bytes": pdf_path.stat().st_size,
            "pdf_page_count": len(pdf.pages),
            "extraction_status": "page_level",
            "non_text_pages": non_text_pages,
            "page_47_disposition": page_dispositions.get("47"),
            "ocr_required_pages": ocr_required_pages,
            "ocr_required": int(bool(ocr_required_pages)),
            "layout_visual_review_required_pages": sorted(layout_review_pages | {
                segment.pdf_page_number for segment in segments if segment.visual_review_required and segment.pdf_page_number is not None
            }),
            "extraction_method": "pdfplumber_page_extract_text_no_ocr",
        }
    return segments, info


def _clean_web_body(text: str) -> str:
    text = normalize_for_match(text)
    date_match = re.search(r"\b\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\b", text)
    if not date_match:
        raise RuntimeError("Could not find a dated article boundary in the saved web text")
    after_date = text[date_match.start():]
    author_match = re.search(r"\bFrom\s+.+?\b(?:Public Affairs|Affairs)\b", after_date)
    if not author_match:
        raise RuntimeError("Could not find the article author boundary in the saved web text")
    body = after_date[author_match.end():]
    footer_matches = list(re.finditer(r"\bShare\s+PRINT\s+RSS\b", body, flags=re.IGNORECASE))
    if footer_matches:
        body = body[:footer_matches[-1].start()]
    # The saved visible-text archive repeats a small action/tag toolbar inside
    # the article. Remove that toolbar by structure, not by source claims.
    toolbar = re.search(r"\bDownload\s+Download\b", body, flags=re.IGNORECASE)
    if toolbar:
        tail = body[toolbar.end():]
        article_start = re.search(r"\b[A-Z][A-Za-z0-9’'-]*(?:’s|'s)\s+\w+", tail)
        if article_start:
            body = body[:toolbar.start()] + tail[article_start.start():]
        else:
            body = body[:toolbar.start()]
    translation_match = re.search(r"\b(?:选择语言|Original|Please rate this translation)\b", body, flags=re.IGNORECASE)
    if translation_match:
        body = body[:translation_match.start()]
    return normalize_for_match(body)


def _stable_web_paragraphs(body: str) -> list[str]:
    """Split visible article text without splitting common abbreviations."""
    boundaries: list[int] = []
    for match in re.finditer(r"[.!?]\s+(?=[A-Z\"“])", body):
        prefix = body[max(0, match.start() - 12):match.start()]
        token_match = re.search(r"([A-Za-z.]+)$", prefix)
        token = token_match.group(1) if token_match else ""
        if "." in token or len(token) <= 4:
            continue
        boundaries.append(match.end() - 1)
    starts = [0] + boundaries
    ends = boundaries + [len(body)]
    return [body[start:end].strip() for start, end in zip(starts, ends) if body[start:end].strip()]


def extract_web_segments(text_path: Path, source_id: str) -> tuple[list[SourceSegment], dict[str, Any]]:
    if source_id != "T0-TD-008":
        raise ValueError("Web paragraph preparation is only configured for T0-TD-008")
    body = _clean_web_body(text_path.read_text(encoding="utf-8"))
    parts = _stable_web_paragraphs(body)
    segments: list[SourceSegment] = []
    for index, part in enumerate(parts, start=1):
        locator = f"article/paragraph-{index:02d}"
        segments.append(
            SourceSegment(
                source_id=source_id,
                source_locator=locator,
                text=part,
                section_title="article",
                machine_extracted_anchor=locator,
                visual_review_required=_visual_review_required(part),
            )
        )
    if not segments:
        raise RuntimeError("No web article paragraphs were extracted")
    return segments, {
        "text_path": relative_path(text_path),
        "text_sha256": sha256_file(text_path),
        "text_character_count": len(body),
        "paragraph_count": len(segments),
        "extraction_status": "visible_text_cleaned_no_ocr_no_summary",
    }


def load_template() -> dict[str, Any]:
    value = yaml.safe_load(PROMPT_TEMPLATE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Evidence prompt template must be a YAML object")
    return value


def build_extraction_prompt(segments: list[SourceSegment], template: dict[str, Any], batch_id: str) -> str:
    source_id = segments[0].source_id
    candidate_limit = 8 if source_id == "T0-SP-007" else 5
    lines = [
        f"PROMPT_ID: {template['prompt_id']}",
        f"VERSION: {template['version']}",
        f"BATCH_ID: {batch_id}",
        f"SOURCE_DOCUMENT_ID: {source_id}",
        "RESEARCH_QUESTION: Based only on public, non-classified material available through 2024-12-31, what dominant deployment form is more likely for U.S. military AI capability building in 2025-2027?",
        "TASK: Extract a small set of auditable evidence candidates from the supplied source segments.",
        "INPUT LIMIT: Use only the source segments below, the research question, and the field definitions in this prompt. Do not infer from prior runs.",
        "OUTPUT: A strict JSON array. Each object must contain exactly the eight fields in the output schema.",
        "Each candidate must express one checkable fact or one bounded explicit claim.",
        "normalized_claim must be written in Chinese only. It must state only the evidence proposition and must not begin with source-attribution prefixes such as ‘本报告认为’ or ‘DoD RAI Pathway指出’. Source title, publisher, date, and URL/path are deterministic metadata fields supplied by the program, not claim text.",
        f"CANDIDATE_LIMIT: Output no more than {candidate_limit} candidates for this source; output fewer when evidence is insufficient.",
        "source_id must equal SOURCE_DOCUMENT_ID exactly. source_locator must equal the exact source_locator shown for the segment; do not substitute the machine anchor or a paragraph label.",
        "coding_dimensions must use only these exact keys: integration_level, application_maturity, human_machine_authority, engineering_resource_conditions, trust_governance_constraints, organizational_system_fit.",
        "excerpt_original must be copied verbatim from one named segment; do not silently correct extraction errors.",
        "All English candidates must have a non-empty excerpt_zh; translation is provisional and requires pending_g1_translation_review.",
        "Use test_event only for the occurrence, participants, or configuration of a test or exercise. Use test_result only when the excerpt reports an observed function, performance, or result.",
        "An exercise or demonstration is not automatically proof of operational effectiveness.",
        "Do not generate hypotheses, viewpoints, conclusions, or a final 2025-2027 route judgment.",
        "Do not use pseudo-continuous locators. If a fact requires non-contiguous text, output separate candidates.",
        "SOURCE_SEGMENTS:",
    ]
    for segment in segments:
        lines.extend(
            [
                f"--- {segment.source_locator} ---",
                f"pdf_page_number: {segment.pdf_page_number}",
                f"printed_page_number: {segment.printed_page_number}",
                f"section_title: {segment.section_title}",
                f"machine_extracted_anchor: {segment.machine_extracted_anchor}",
                f"visual_review_required: {str(segment.visual_review_required).lower()}",
                segment.text,
            ]
        )
    lines.extend(
        [
            "FIELD_DEFINITIONS:",
            json.dumps(template.get("output_schema", {}), ensure_ascii=False, sort_keys=True),
            json.dumps(template.get("coding_dimensions", {}), ensure_ascii=False, sort_keys=True),
            json.dumps(template.get("evidence_nature", {}), ensure_ascii=False, sort_keys=True),
            "JSON_REPAIR: " + str(template.get("json_repair_prompt", "Return only valid JSON.")),
        ]
    )
    return "\n".join(lines) + "\n"


def build_semantic_review_prompt(candidates: list[dict[str, Any]], template: dict[str, Any], batch_id: str) -> str:
    review_items = []
    for index, candidate in enumerate(candidates):
        review_items.append(
            {
                "candidate_index": index,
                "source_locator": candidate.get("source_locator"),
                "excerpt_original": candidate.get("excerpt_original"),
                "normalized_claim": candidate.get("normalized_claim"),
                "coding_dimensions": candidate.get("coding_dimensions"),
                "evidence_nature": candidate.get("evidence_nature"),
                "topics": candidate.get("topics"),
            }
        )
    return "\n".join(
        [
            "PROMPT_ID: same_model_separate_call_semantic_screening",
            "VERSION: 1.0",
            f"BATCH_ID: {batch_id}",
            "TASK: Independently audit whether each candidate's claim, dimensions, and evidence nature are directly supported by its own excerpt.",
            "INPUT LIMIT: Use only the candidate fields below. Do not use source metadata, other candidates, prior runs, or outside knowledge.",
            "OUTPUT: strict JSON array with exactly candidate_index, status, reason, claim_supported, nature_supported, dimension_reviews.",
            "dimension_reviews must be an array with one object per declared dimension, each object containing exactly dimension, supported, supporting_span, reason.",
            "Use the complete six-dimension definitions below when assessing each declared dimension:",
            json.dumps(CODING_DIMENSION_DEFINITIONS, ensure_ascii=False, sort_keys=True),
            "status must be pass, revise, or reject. Do not rewrite candidates.",
            "Reject claims that add a subject, action, or result absent from the excerpt. Reject mere glossary or generic definition entries that do not directly serve the research question.",
            json.dumps(review_items, ensure_ascii=False, indent=2),
        ]
    ) + "\n"


def resolve_locator(locator: str, segments: dict[str, SourceSegment]) -> SourceSegment | None:
    exact = segments.get(locator)
    if exact is not None:
        return exact
    match = re.fullmatch(r"(.+/paragraph-)(\d{2})--(\d{2})", locator)
    if not match:
        return None
    start, end = int(match.group(2)), int(match.group(3))
    if end < start:
        return None
    keys = [f"{match.group(1)}{index:02d}" for index in range(start, end + 1)]
    if any(key not in segments for key in keys):
        return None
    first = segments[keys[0]]
    return SourceSegment(
        source_id=first.source_id,
        source_locator=locator,
        text=" ".join(segments[key].text for key in keys),
        section_title=first.section_title,
        machine_extracted_anchor=";".join(segments[key].machine_extracted_anchor for key in keys),
        visual_review_required=any(segments[key].visual_review_required for key in keys),
    )


def parse_candidate(value: Any) -> EvidenceCandidate:
    if not isinstance(value, dict) or set(value) != frozenset(
        {
            "source_id",
            "source_locator",
            "excerpt_original",
            "excerpt_zh",
            "normalized_claim",
            "coding_dimensions",
            "evidence_nature",
            "topics",
        }
    ):
        raise ValueError("candidate_fields_must_match_strict_schema")
    return model_validate(EvidenceCandidate, value)


def normalize_date(value: str) -> tuple[str, str]:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value, "day"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value + "-01", "month"
    if re.fullmatch(r"\d{4}", value):
        return value + "-01-01", "year"
    raise ValueError("Unsupported published_at value: " + value)


def stable_evidence_id(source_id: str, locator: str, excerpt: str) -> str:
    digest = sha256_bytes((source_id + "\n" + locator + "\n" + excerpt).encode("utf-8"))[:16]
    return "EV-T0-" + digest.upper()


def source_excerpt_hash(source_id: str, locator: str, excerpt: str) -> str:
    return sha256_bytes((source_id + "\n" + locator + "\n" + excerpt).encode("utf-8"))


def inject_metadata(candidate: EvidenceCandidate, source: dict[str, str], run_id: str, segment: SourceSegment) -> dict[str, Any]:
    published_at, precision = normalize_date(source["published_at"] or source["document_date"])
    payload = model_dump(candidate)
    payload.update(
        {
            "case_id": CASE_ID,
            "evidence_id": stable_evidence_id(candidate.source_id, candidate.source_locator, candidate.excerpt_original),
            "snapshot_tag": "T0",
            "title": source["title"],
            "publisher": source["publisher"],
            "source_grade": source["source_grade"],
            "published_at": published_at,
            "published_at_precision": precision,
            "source_type": source.get("material_type", ""),
            "url_or_path": source.get("canonical_url") or source.get("url_or_path") or source.get("local_path", ""),
            "page_or_section": candidate.source_locator,
            "content_hash": source_excerpt_hash(candidate.source_id, candidate.source_locator, candidate.excerpt_original),
            "excerpt": candidate.excerpt_original,
            "reviewed_by": "",
            "status": "candidate",
            "run_id": run_id,
            "source_visual_review_required": segment.visual_review_required,
            "machine_extracted_anchor": segment.machine_extracted_anchor,
            "machine_extracted_excerpt": candidate.excerpt_original,
            "human_corrected_excerpt": None,
            "visual_review_status": "pending_g1_visual_review" if segment.visual_review_required else "not_required_by_p3q",
            "translation_status": "pending_g1_translation_review",
        }
    )
    return payload


def lexical_auto_rejection(candidate: EvidenceCandidate, segment: SourceSegment) -> str | None:
    excerpt = normalize_for_match(candidate.excerpt_original)
    claim = normalize_for_match(candidate.normalized_claim)
    if not excerpt or not claim:
        return "empty_excerpt_or_claim"
    source_context = (segment.section_title + " " + excerpt).casefold()
    claim_lower = claim.casefold()
    if any(token in source_context for token in ("glossary", "acronym", "term definition", "lexicon")):
        return "generic_definition_or_lexicon_not_directly_research_relevant"
    claim_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", claim))
    excerpt_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", excerpt))
    if not claim_numbers.issubset(excerpt_numbers):
        return "normalized_claim_adds_numeric_result_or_scope_not_in_excerpt"
    # Capitalized entities are intentionally not a deterministic rejection.
    # OCR can render AI/RAI/DoD as Al/RAJ/OoD; semantic screening and G1 review
    # decide whether the proposition is supported.
    return None


def reason_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for reason in row.get("rejection_reasons", []):
            counter[reason] += 1
    return dict(counter)
