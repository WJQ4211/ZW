"""Generic full-T0 P3b extraction pipeline.

The module loads only P2-author-included local source paths.  It creates stable
page/paragraph coverage, calls the configured OpenAI-compatible API once for
extraction and once for semantic screening for each eligible batch, and keeps
all outputs as candidate evidence only.  It never writes the formal Evidence
table, hypotheses, snapshots, or any later-stage artifact.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
import yaml

from .model_client import APIModelClient, RAW_CANDIDATE_FIELDS, chat_completion_content
from .p3ar import (
    CASE_ID,
    CODING_DIMENSION_DEFINITIONS,
    ROOT,
    SourceSegment,
    _printed_page_number,
    _section_title,
    inject_metadata,
    lexical_auto_rejection,
    normalize_for_match,
    parse_candidate,
    relative_path,
    resolve_locator,
    sha256_bytes,
    sha256_file,
    write_csv,
    write_json,
    write_jsonl,
)


P2_SOURCE = ROOT / "data/curated/source_manifest_t0_p2.csv"
P2_RUN_ID = "P2-20260825T154432Z-author-pilot"
P3Q_RUN_ID = "P3Q-20260826T023500Z-offline-v2"
P3Q_DIR = ROOT / "outputs" / P3Q_RUN_ID
ALLOWED_NATURES = {"goal", "resource_input", "test_event", "test_result", "deployment_event", "governance_event", "constraint", "evaluation"}
ALLOWED_DIMENSIONS = set(CODING_DIMENSION_DEFINITIONS)
MAX_BATCH_CHARS = 45000
MAX_CANDIDATES_PER_BATCH = 5


@dataclass(frozen=True)
class LoadedDocument:
    source: dict[str, str]
    segments: tuple[SourceSegment, ...]
    info: dict[str, Any]
    raw_path: Path
    text_path: Path


class _BodyParser(HTMLParser):
    """Extract visible block text while excluding navigation chrome."""

    _block_tags = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    _skip_tags = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._block_depth = 0
        self._buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._skip_tags:
            self._skip_depth += 1
        if self._skip_depth == 0 and tag in self._block_tags:
            self._block_depth += 1
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth == 0 and tag in self._block_tags:
            self._flush()
            self._block_depth = max(0, self._block_depth - 1)
        if tag in self._skip_tags and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._buffer.append(data)

    def _flush(self) -> None:
        value = normalize_for_match(" ".join(self._buffer))
        self._buffer.clear()
        if value:
            self.paragraphs.append(value)

    def finish(self) -> list[str]:
        self._flush()
        return self.paragraphs


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _decode_json(content: str) -> Any:
    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        value = fenced.group(1).strip()
    return json.loads(value)


def _normalize_claim_is_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _suspicious_text(text: str) -> bool:
    return bool(re.search(r"\b(?:Al|AJ|RAJ|RAl|OoD|D0D|AI/ML)\b", text))


def _pdf_layout_review(page: Any, text: str) -> bool:
    if _suspicious_text(text):
        return True
    try:
        if page.find_tables():
            return True
    except Exception:
        return True
    try:
        words = page.extract_words(x_tolerance=1, y_tolerance=3, keep_blank_chars=False)
        x_values = sorted(float(word.get("x0", 0)) for word in words)
        gaps = [right - left for left, right in zip(x_values, x_values[1:])]
        if len(words) >= 30 and any(gap > 110 for gap in gaps):
            return True
    except Exception:
        return True
    return False


def _resolve_p2_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_pdf_document(source: dict[str, str], raw_path: Path, text_path: Path) -> LoadedDocument:
    segments: list[SourceSegment] = []
    non_text_pages: list[int] = []
    layout_review_pages: list[int] = []
    page_dispositions: dict[str, str] = {}
    with pdfplumber.open(raw_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3, layout=False) or ""
            status = "extracted" if text.strip() else "non_text_page"
            if not text.strip():
                non_text_pages.append(page_number)
                if page_number == len(pdf.pages) and page.images:
                    status = "decorative_back_cover_no_ocr_required"
                    page_dispositions[str(page_number)] = status
                else:
                    status = "ocr_pending"
            lines = text.splitlines()
            printed = _printed_page_number(lines)
            section = _section_title(lines)
            visual = _pdf_layout_review(page, text)
            if visual:
                layout_review_pages.append(page_number)
            anchor = f"pdf_page_{page_number}|printed_page_{printed if printed is not None else 'pending'}|section_{re.sub(r'[^A-Za-z0-9]+', '_', section).strip('_') or 'pending'}"
            segments.append(
                SourceSegment(
                    source_id=source["source_id"],
                    source_locator=f"pdf_page_{page_number}",
                    text=text,
                    pdf_page_number=page_number,
                    printed_page_number=printed,
                    section_title=section,
                    machine_extracted_anchor=anchor,
                    visual_review_required=visual,
                    extraction_status=status,
                )
            )
    return LoadedDocument(
        source=source,
        segments=tuple(segments),
        raw_path=raw_path,
        text_path=text_path,
        info={
            "format": "pdf",
            "raw_path": relative_path(raw_path),
            "text_path": relative_path(text_path),
            "raw_sha256": sha256_file(raw_path),
            "text_sha256": sha256_file(text_path),
            "pdf_page_count": len(segments),
            "processed_page_count": len(segments),
            "non_text_pages": non_text_pages,
            "page_dispositions": page_dispositions,
            "ocr_required_pages": [segment.pdf_page_number for segment in segments if segment.extraction_status == "ocr_pending"],
            "layout_visual_review_required_pages": layout_review_pages,
            "extraction_method": "pdfplumber_page_extract_text_no_ocr",
        },
    )


def _fallback_web_paragraphs(text: str) -> list[str]:
    normalized = normalize_for_match(text)
    chunks = [normalize_for_match(item) for item in re.split(r"\n\s*\n+", text) if normalize_for_match(item)]
    if len(chunks) > 1:
        return chunks
    return [normalize_for_match(item) for item in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“])", normalized) if normalize_for_match(item)]


def load_web_document(source: dict[str, str], raw_path: Path, text_path: Path) -> LoadedDocument:
    raw_html = raw_path.read_text(encoding="utf-8", errors="replace")
    parser = _BodyParser()
    parser.feed(raw_html)
    paragraphs = [normalize_for_match(html.unescape(item)) for item in parser.finish()]
    paragraphs = [item for item in paragraphs if item]
    if len(paragraphs) < 2:
        paragraphs = _fallback_web_paragraphs(text_path.read_text(encoding="utf-8", errors="replace"))
    segments = tuple(
        SourceSegment(
            source_id=source["source_id"],
            source_locator=f"web_paragraph_{index:04d}",
            text=paragraph,
            section_title="web_body",
            machine_extracted_anchor=f"web_paragraph_{index:04d}",
            visual_review_required=_suspicious_text(paragraph),
            extraction_status="extracted",
        )
        for index, paragraph in enumerate(paragraphs, start=1)
    )
    return LoadedDocument(
        source=source,
        segments=segments,
        raw_path=raw_path,
        text_path=text_path,
        info={
            "format": "html",
            "raw_path": relative_path(raw_path),
            "text_path": relative_path(text_path),
            "raw_sha256": sha256_file(raw_path),
            "text_sha256": sha256_file(text_path),
            "paragraph_count": len(segments),
            "processed_paragraph_count": len(segments),
            "non_text_paragraphs": [],
            "extraction_method": "html_parser_body_blocks_with_p2_text_fallback",
        },
    )


def load_include_documents() -> list[LoadedDocument]:
    with P2_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("review_decision") == "include"]
    documents: list[LoadedDocument] = []
    for source in sorted(rows, key=lambda row: row["source_id"]):
        raw_path = _resolve_p2_path(source["p2_raw_path"])
        text_path = _resolve_p2_path(source["p2_text_path"])
        if not raw_path.is_file() or not text_path.is_file():
            raise FileNotFoundError(f"P2 source paths unavailable for {source['source_id']}: {raw_path} / {text_path}")
        if raw_path.suffix.lower() == ".pdf":
            documents.append(load_pdf_document(source, raw_path, text_path))
        else:
            documents.append(load_web_document(source, raw_path, text_path))
    return documents


def _batch_segments(segments: Iterable[SourceSegment]) -> list[list[SourceSegment]]:
    batches: list[list[SourceSegment]] = []
    current: list[SourceSegment] = []
    current_chars = 0
    for segment in segments:
        if not segment.text.strip():
            continue
        length = len(segment.text)
        if current and current_chars + length > MAX_BATCH_CHARS:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += length
    if current:
        batches.append(current)
    return batches


def build_extraction_prompt(document: LoadedDocument, segments: list[SourceSegment], batch_id: str, template: dict[str, Any]) -> str:
    source_id = document.source["source_id"]
    lines = [
        f"PROMPT_ID: {template['prompt_id']}",
        f"VERSION: {template['version']}",
        f"BATCH_ID: {batch_id}",
        f"SOURCE_DOCUMENT_ID: {source_id}",
        "RESEARCH_QUESTION: Based only on public, non-classified material available through 2024-12-31, what dominant deployment form is more likely for U.S. military AI capability building in 2025-2027?",
        "TASK: Extract at most five auditable evidence candidates from these source segments. Output zero candidates when the supplied text contains insufficient directly relevant evidence.",
        "INPUT LIMIT: Use only the source segments and definitions supplied here. Do not use prior runs, other sources, or outside knowledge.",
        "OUTPUT: strict JSON array; each object must contain exactly the eight model fields in the schema.",
        "normalized_claim must be Chinese only, state one evidence proposition, and contain no source-attribution prefix such as 本报告认为 or 某机构指出. Source metadata is supplied by the program.",
        "coding_dimensions must be a JSON array of zero or more exact dimension key strings, never an object/map and never Chinese descriptions or maturity labels. Allowed keys: integration_level, application_maturity, human_machine_authority, engineering_resource_conditions, trust_governance_constraints, organizational_system_fit.",
        "excerpt_original must be copied verbatim from one exact source_locator. Do not silently repair OCR, spelling, or layout extraction.",
        "If an English excerpt is returned, excerpt_zh must be non-empty and remains pending G1 translation review.",
        "Use goal for strategy/vision/plan; resource_input for resources or investments; test_event only for a test or exercise occurrence, participants, or configuration; test_result only for an explicitly observed function, performance, or result; deployment_event only for deployment, fielding, adoption, or sustained use that has already occurred; governance_event only for a policy release, responsibility assignment, governance tool, standard/guideline adoption, or organizational mechanism that has already occurred, without inferring technical deployment.",
        "Ordinary coordination, information transfer, training, or documentation does not automatically support human_machine_authority.",
        "Do not add subjects, actions, numbers, scope, or results absent from the excerpt. Do not generate hypotheses, viewpoints, conclusions, or a 2025-2027 route judgment.",
        "Do not use pseudo-continuous locators. One candidate expresses one checkable fact. Do not duplicate an equivalent excerpt within this source.",
        "CANDIDATE_LIMIT: 5",
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
            json.dumps(template.get("coding_dimensions", CODING_DIMENSION_DEFINITIONS), ensure_ascii=False, sort_keys=True),
            json.dumps(template.get("evidence_nature", {}), ensure_ascii=False, sort_keys=True),
            "JSON_REPAIR: " + str(template.get("json_repair_prompt", "Return only strict JSON.")),
        ]
    )
    return "\n".join(lines) + "\n"


def build_semantic_prompt(candidates: list[dict[str, Any]], batch_id: str, template: dict[str, Any]) -> str:
    items = []
    for index, candidate in enumerate(candidates):
        items.append({"candidate_index": index, **{key: candidate.get(key) for key in RAW_CANDIDATE_FIELDS}})
    return "\n".join(
        [
            "PROMPT_ID: same_model_separate_call_semantic_screening",
            "VERSION: 1.0",
            f"BATCH_ID: {batch_id}",
            "TASK: In a separate model call, screen whether each candidate claim and evidence nature are directly supported by its own excerpt, and review every declared coding dimension separately.",
            "INPUT LIMIT: Use only the candidate fields below. Do not use source metadata, other candidates, prior runs, or outside knowledge.",
            "OUTPUT: strict JSON array. Each item must contain exactly candidate_index, status, reason, claim_supported, nature_supported, dimension_reviews.",
            "dimension_reviews must contain one object for every declared dimension, each with exactly dimension, supported, supporting_span, reason. Do not rewrite the candidate.",
            "status must be pass, revise, or reject. Reject claims that add a subject, action, number, scope, or result absent from the excerpt, or that are only glossary/general definitions unrelated to the research question.",
            "A test/exercise occurrence is test_event, not test_result unless the excerpt explicitly reports an observed function, performance, or result. Do not treat ordinary coordination as human_machine_authority; information delivered to watchstanders can support an auxiliary relationship; training or documentation alone does not establish authority allocation.",
            "CODING_DIMENSION_DEFINITIONS: " + json.dumps(template.get("coding_dimensions", CODING_DIMENSION_DEFINITIONS), ensure_ascii=False, sort_keys=True),
            json.dumps(items, ensure_ascii=False, indent=2),
        ]
    ) + "\n"


def _deterministic_reasons(candidate: Any, segment: SourceSegment | None, seen: set[str]) -> list[str]:
    reasons: list[str] = []
    if candidate is None:
        return ["schema_invalid"]
    if candidate.source_id not in {segment.source_id} if segment else True:
        reasons.append("source_id_out_of_scope_or_segment_mismatch")
    if segment is None:
        reasons.append("locator_not_found")
    else:
        if normalize_for_match(candidate.excerpt_original) not in normalize_for_match(segment.text):
            reasons.append("excerpt_not_found_at_specified_locator")
    if not _normalize_claim_is_chinese(candidate.normalized_claim):
        reasons.append("normalized_claim_not_chinese")
    if not candidate.excerpt_zh.strip():
        reasons.append("excerpt_zh_empty")
    if candidate.evidence_nature not in ALLOWED_NATURES:
        reasons.append("evidence_nature_invalid")
    if candidate.evidence_nature in {"test_result", "deployment_event"} and re.search(r"\b(plan|planned|intend|will|should|expected)\b", candidate.excerpt_original, flags=re.IGNORECASE):
        reasons.append("plan_or_intent_coded_as_result")
    if segment is not None:
        lexical_reason = lexical_auto_rejection(candidate, segment)
        if lexical_reason:
            reasons.append(lexical_reason)
    key = normalize_for_match(candidate.excerpt_original).casefold()
    if key in seen:
        reasons.append("same_source_duplicate")
    return reasons


def _semantic_rows(value: Any) -> tuple[dict[int, dict[str, Any]], str | None]:
    if not isinstance(value, list):
        return {}, "semantic_response_not_array"
    result: dict[int, dict[str, Any]] = {}
    allowed = {"candidate_index", "status", "reason", "claim_supported", "nature_supported", "dimension_reviews"}
    for item in value:
        if not isinstance(item, dict) or set(item) != allowed:
            return {}, "semantic_review_schema_invalid"
        if not isinstance(item.get("candidate_index"), int) or item["status"] not in {"pass", "revise", "reject"}:
            return {}, "semantic_review_value_invalid"
        reviews = item.get("dimension_reviews")
        if not isinstance(reviews, list):
            return {}, "dimension_reviews_not_array"
        for review in reviews:
            if not isinstance(review, dict) or set(review) != {"dimension", "supported", "supporting_span", "reason"}:
                return {}, "dimension_review_schema_invalid"
            if review["dimension"] not in ALLOWED_DIMENSIONS or not isinstance(review["supported"], bool):
                return {}, "dimension_review_value_invalid"
        result[item["candidate_index"]] = item
    return result, None


def _candidate_fields(candidate: Any) -> dict[str, Any]:
    return {field: getattr(candidate, field) for field in RAW_CANDIDATE_FIELDS}


def _current_git_head() -> dict[str, str]:
    for candidate in (ROOT / ".github_upload_staging", ROOT):
        try:
            result = subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        value = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return {"commit": value, "short_commit": value[:12], "git_root": str(candidate)}
    return {"commit": "unverified_local_copy", "short_commit": "unverified_local_copy", "git_root": ""}


def immutable_p3q_hashes() -> dict[str, str]:
    if not P3Q_DIR.exists():
        raise FileNotFoundError(P3Q_DIR)
    return {relative_path(path): sha256_file(path) for path in sorted(P3Q_DIR.rglob("*")) if path.is_file()}


def create_p3q_audit_correction(audit_dir: Path) -> dict[str, Any]:
    before = immutable_p3q_hashes()
    old_manifest = json.loads((P3Q_DIR / "p3q_manifest.json").read_text(encoding="utf-8"))
    old_rows = [json.loads(line) for line in (P3Q_DIR / "candidate_rejudgment_preview.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    inherited = 0
    for path in sorted((ROOT / "outputs/P3AR-20260826T010914-real-api-v6/raw_semantic_reviews").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = _decode_json(chat_completion_content(payload))
        except Exception:
            value = []
        if isinstance(value, list):
            inherited += sum(1 for item in value if isinstance(item, dict) and item.get("status") == "pass" and item.get("claim_supported") is True and item.get("nature_supported") is True)
    offline = sum(1 for row in old_rows if any(item.get("supported") is True for item in row.get("dimension_reviews", [])))
    hybrid = sum(1 for row in old_rows if row.get("final_candidate") is True)
    correction = {
        "correction_id": "P3Q-AUDIT-CORRECTION-20260826T",
        "created_at": _now_utc(),
        "input_run_id": P3Q_RUN_ID,
        "input_hashes": before,
        "input_hashes_unchanged_after_correction": True,
        "old_p3a_generation_mode": "deterministic_hardcoded_fixture",
        "p3ar_status": "real_api_technical_pilot_passed",
        "p3ar_not_evidence_quality_validated": True,
        "dimension_support_origin": "offline_keyword_dimension_heuristic",
        "dimension_support_is_model_generated": False,
        "dimension_support_is_human_reviewed": False,
        "inherited_v6_same_model_claim_nature_pass_count": inherited,
        "offline_keyword_dimension_screen_pass_count": offline,
        "hybrid_preview_pass_count": hybrid,
        "deprecated_metric": {
            "name": "same_model_semantic_screen_pass_count",
            "old_value": old_manifest.get("same_model_semantic_screen_pass_count"),
            "corrected_interpretation": "not a new same-model semantic review; the old value mixed inherited v6 model screening with P3Q offline keyword dimension filtering",
        },
        "validation_scope": "historical_v6_claim_nature_inheritance_plus_offline_keyword_dimension_preview",
        "not_eligible_for_g1": True,
        "model_api_called_by_correction": False,
        "human_reviewed_by_correction": False,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    write_json(audit_dir / "p3q_audit_correction.json", correction)
    corrected_rows = []
    for row in old_rows:
        value = dict(row)
        value["dimension_reviews"] = [
            {
                **review,
                "review_origin": "offline_keyword_heuristic",
                "model_generated": False,
                "human_reviewed": False,
            }
            for review in row.get("dimension_reviews", [])
        ]
        corrected_rows.append(value)
    write_jsonl(audit_dir / "p3q_dimension_review_correction_preview.jsonl", corrected_rows)
    (audit_dir / "p3q_audit_correction.md").write_text(
        "# P3Q audit correction\n\n"
        f"Input `{P3Q_RUN_ID}` remains read-only; its full hash set is recorded in the JSON correction.\n\n"
        "`src/p3q.py::_dimension_support` is an `offline_keyword_dimension_heuristic`, not a model semantic review, not an independent model, and not a human review.\n\n"
        f"- inherited_v6_same_model_claim_nature_pass_count: {inherited}\n"
        f"- offline_keyword_dimension_screen_pass_count: {offline}\n"
        f"- hybrid_preview_pass_count: {hybrid}\n"
        f"- deprecated old same_model_semantic_screen_pass_count: {old_manifest.get('same_model_semantic_screen_pass_count')}\n\n"
        "Every corrected dimension review is explicitly marked `review_origin=offline_keyword_heuristic`, `model_generated=false`, and `human_reviewed=false`. Keyword spans are not described as semantic judgments.\n",
        encoding="utf-8",
    )
    if before != immutable_p3q_hashes():
        raise RuntimeError("P3Q immutable input changed while writing audit correction")
    return correction


def _corroboration_group(candidate: dict[str, Any]) -> str:
    value = normalize_for_match(candidate.get("normalized_claim", "")).casefold()
    return "CORR-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()


def build_full_coverage_rows(documents: list[LoadedDocument], summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return summary plus one deterministic row for every page/paragraph."""
    rows: list[dict[str, Any]] = []
    for summary in summary_rows:
        rows.append({
            "row_type": "source_summary",
            "source_id": summary["source_id"],
            "format": "",
            "source_locator": "",
            "pdf_page_number": "",
            "printed_page_number": "",
            "section_title": "",
            "batch_id": "",
            "text_present": "",
            "extraction_status": "",
            **summary,
        })
    for document in documents:
        batch_map: dict[str, str] = {}
        for index, batch in enumerate(_batch_segments(document.segments), start=1):
            batch_id = f"{document.source['source_id']}-batch-{index:03d}"
            for segment in batch:
                batch_map[segment.source_locator] = batch_id
        for segment in document.segments:
            rows.append({
                "row_type": "page_or_paragraph",
                "source_id": document.source["source_id"],
                "format": document.info["format"],
                "source_locator": segment.source_locator,
                "pdf_page_number": segment.pdf_page_number if segment.pdf_page_number is not None else "",
                "printed_page_number": segment.printed_page_number if segment.printed_page_number is not None else "",
                "section_title": segment.section_title,
                "batch_id": batch_map.get(segment.source_locator, ""),
                "text_present": bool(segment.text.strip()),
                "extraction_status": segment.extraction_status,
                "total_page_or_paragraph_count": "",
                "processed_page_or_paragraph_count": "",
                "empty_or_non_text_count": "",
                "batch_count": "",
                "raw_candidate_count": "",
                "final_candidate_count": "",
                "pending_g1_visual_review_count": "",
                "pending_g1_translation_review_count": "",
                "raw_path": relative_path(document.raw_path),
                "text_path": relative_path(document.text_path),
                "raw_sha256": document.info["raw_sha256"],
                "text_sha256": document.info["text_sha256"],
            })
    return rows


def run_p3b(run_id: str, run_dir: Path) -> dict[str, Any]:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty P3b directory: {run_dir}")
    p3q_before = immutable_p3q_hashes()
    documents = load_include_documents()
    template = yaml.safe_load((ROOT / "prompts/evidence_extract_p3b_v2.yaml").read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise RuntimeError("P3b extraction template is invalid")
    audit_dir = run_dir / "audit_correction"
    correction = create_p3q_audit_correction(audit_dir)
    client = APIModelClient()
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_model_outputs"
    semantic_dir = run_dir / "raw_semantic_reviews"
    prompt_extraction_dir = run_dir / "prompt_packets/extraction"
    prompt_semantic_dir = run_dir / "prompt_packets/semantic_review"
    metadata_dir = run_dir / "api_call_metadata"
    calls: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    final_candidates: list[dict[str, Any]] = []
    semantic_results: list[dict[str, Any]] = []
    dimension_revisions: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    seen_by_source: dict[str, set[str]] = {document.source["source_id"]: set() for document in documents}
    all_candidate_rows: list[dict[str, Any]] = []
    counts = Counter()
    rejected_reasons: Counter[str] = Counter()
    candidates_by_source: Counter[str] = Counter()
    candidates_by_nature: Counter[str] = Counter()
    candidates_by_dimension: Counter[str] = Counter()
    batch_count = 0
    semantic_batch_count = 0
    raw_candidate_count = 0
    schema_valid_count = 0
    exact_quote_count = 0
    deterministic_pass_count = 0
    semantic_pass_count = 0
    unsupported_dimension_removals = 0
    pending_visual_count = 0
    pending_translation_count = 0

    for document in documents:
        batches = _batch_segments(document.segments)
        batch_count += len(batches)
        for batch_index, segments in enumerate(batches, start=1):
            batch_id = f"{document.source['source_id']}-batch-{batch_index:03d}"
            packet = {
                "batch_id": batch_id,
                "source_id": document.source["source_id"],
                "source_locator_count": len(segments),
                "segments": [segment.as_dict() for segment in segments],
                "source_raw_path": relative_path(document.raw_path),
                "source_text_path": relative_path(document.text_path),
                "source_raw_sha256": sha256_file(document.raw_path),
                "source_text_sha256": sha256_file(document.text_path),
                "max_batch_chars": MAX_BATCH_CHARS,
                "max_candidates": MAX_CANDIDATES_PER_BATCH,
            }
            packet_path = prompt_extraction_dir / f"{batch_id}.json"
            prompt_path = prompt_extraction_dir / f"{batch_id}.txt"
            write_json(packet_path, packet)
            prompt = build_extraction_prompt(document, segments, batch_id, template)
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            payload, content, record = client.call(prompt, temperature=0.0, max_tokens=6000)
            raw_path = raw_dir / f"{batch_id}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            call_record = record.as_dict()
            call_record.update({"phase": "extraction", "batch_id": batch_id, "prompt_path": relative_path(prompt_path), "prompt_sha256": sha256_file(prompt_path), "raw_response_path": relative_path(raw_path), "raw_response_sha256": sha256_file(raw_path), "response_content_sha256": sha256_bytes(content.encode("utf-8")), "requested_temperature": 0.0, "provider_echoed_temperature": record.provider_temperature})
            write_json(metadata_dir / f"{batch_id}.json", call_record)
            calls.append(call_record)
            try:
                decoded = _decode_json(content)
                values = decoded if isinstance(decoded, list) else []
            except (ValueError, json.JSONDecodeError):
                values = []
                rejected.append({"batch_id": batch_id, "candidate_index": None, "candidate": None, "rejection_reasons": ["model_content_not_strict_json"]})
                rejected_reasons["model_content_not_strict_json"] += 1
            raw_candidate_count += len(values)
            segment_map = {segment.source_locator: segment for segment in segments}
            valid_for_semantic: list[tuple[int, Any, SourceSegment]] = []
            for index, value in enumerate(values):
                reasons: list[str] = []
                candidate = None
                segment = None
                if index >= MAX_CANDIDATES_PER_BATCH:
                    reasons.append("batch_candidate_limit_exceeded")
                try:
                    candidate = parse_candidate(value)
                    schema_valid_count += 1
                except Exception:
                    reasons.append("schema_invalid")
                if candidate is not None:
                    if candidate.source_id != document.source["source_id"]:
                        reasons.append("source_id_does_not_match_batch")
                    segment = resolve_locator(candidate.source_locator, segment_map)
                    before_quote = len(reasons)
                    reasons.extend(_deterministic_reasons(candidate, segment, seen_by_source[document.source["source_id"]]))
                    if "excerpt_not_found_at_specified_locator" not in reasons and segment is not None:
                        exact_quote_count += 1
                    if "same_source_duplicate" not in reasons:
                        seen_by_source[document.source["source_id"]].add(normalize_for_match(candidate.excerpt_original).casefold())
                    if not reasons:
                        valid_for_semantic.append((index, candidate, segment))
                    if before_quote == len(reasons) and segment is not None:
                        pass
                if reasons:
                    for reason in set(reasons):
                        rejected_reasons[reason] += 1
                    rejected.append({"batch_id": batch_id, "candidate_index": index, "candidate": value, "rejection_reasons": reasons})
                all_candidate_rows.append({"batch_id": batch_id, "candidate_index": index, "candidate": value, "deterministic_reasons": reasons})
            deterministic_pass_count += len(valid_for_semantic)

            semantic_map: dict[int, dict[str, Any]] = {}
            if valid_for_semantic:
                semantic_batch_count += 1
                review_values = [dict(_candidate_fields(candidate), candidate_index=local_index) for local_index, (_, candidate, _) in enumerate(valid_for_semantic)]
                semantic_prompt = build_semantic_prompt(review_values, batch_id, template)
                semantic_prompt_path = prompt_semantic_dir / f"{batch_id}.txt"
                semantic_prompt_path.parent.mkdir(parents=True, exist_ok=True)
                semantic_prompt_path.write_text(semantic_prompt, encoding="utf-8")
                semantic_payload, semantic_content, semantic_record = client.call(semantic_prompt, temperature=0.0, max_tokens=5000)
                semantic_path = semantic_dir / f"{batch_id}.json"
                semantic_path.parent.mkdir(parents=True, exist_ok=True)
                semantic_path.write_text(json.dumps(semantic_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                semantic_call_record = semantic_record.as_dict()
                semantic_call_record.update({"phase": "same_model_separate_call_semantic_screening", "batch_id": batch_id, "prompt_path": relative_path(semantic_prompt_path), "prompt_sha256": sha256_file(semantic_prompt_path), "raw_response_path": relative_path(semantic_path), "raw_response_sha256": sha256_file(semantic_path), "response_content_sha256": sha256_bytes(semantic_content.encode("utf-8")), "requested_temperature": 0.0, "provider_echoed_temperature": semantic_record.provider_temperature})
                write_json(metadata_dir / f"{batch_id}-semantic.json", semantic_call_record)
                calls.append(semantic_call_record)
                try:
                    semantic_value = _decode_json(semantic_content)
                    semantic_map, semantic_error = _semantic_rows(semantic_value)
                except (ValueError, json.JSONDecodeError):
                    semantic_error = "semantic_content_not_strict_json"
                if semantic_error:
                    for local_index, (_, _, _) in enumerate(valid_for_semantic):
                        rejected_reasons[semantic_error] += 1
                        rejected.append({"batch_id": batch_id, "candidate_index": valid_for_semantic[local_index][0], "candidate": json.loads(json.dumps(review_values[local_index])), "rejection_reasons": [semantic_error]})
                    semantic_map = {}
            else:
                semantic_map = {}

            valid_lookup = {local_index: item for local_index, item in enumerate(valid_for_semantic)}
            for local_index, (original_index, candidate, segment) in valid_lookup.items():
                review = semantic_map.get(local_index)
                semantic_row = {"batch_id": batch_id, "candidate_index": original_index, "candidate": _candidate_fields(candidate), "review": review}
                semantic_results.append(semantic_row)
                if review is None:
                    rejected_reasons["semantic_review_missing"] += 1
                    rejected.append({"batch_id": batch_id, "candidate_index": original_index, "candidate": semantic_row["candidate"], "rejection_reasons": ["semantic_review_missing"]})
                    continue
                if review.get("status") != "pass" or review.get("claim_supported") is not True or review.get("nature_supported") is not True:
                    reason = "semantic_claim_or_nature_not_supported"
                    rejected_reasons[reason] += 1
                    rejected.append({"batch_id": batch_id, "candidate_index": original_index, "candidate": semantic_row["candidate"], "rejection_reasons": [reason, str(review.get("reason", ""))]})
                    continue
                semantic_pass_count += 1
                declared = list(candidate.coding_dimensions)
                reviews = {item["dimension"]: item for item in review.get("dimension_reviews", [])}
                if not set(declared).issubset(reviews):
                    reason = "semantic_dimension_review_incomplete"
                    rejected_reasons[reason] += 1
                    rejected.append({"batch_id": batch_id, "candidate_index": original_index, "candidate": semantic_row["candidate"], "rejection_reasons": [reason]})
                    continue
                supported = [dimension for dimension in declared if reviews.get(dimension, {}).get("supported") is True]
                removed = [dimension for dimension in declared if dimension not in supported]
                unsupported_dimension_removals += len(removed)
                dimension_revisions.append({"batch_id": batch_id, "candidate_index": original_index, "source_id": candidate.source_id, "source_locator": candidate.source_locator, "original_dimensions": declared, "final_dimensions": supported, "removed_dimensions": removed, "dimension_reviews": list(review.get("dimension_reviews", [])), "review_origin": "same_model_separate_call_semantic_screening", "model_generated": True, "human_reviewed": False})
                candidate.coding_dimensions = supported
                source_row = document.source
                record = inject_metadata(candidate, source_row, run_id, segment)
                record["pending_g1_visual_review"] = bool(segment.visual_review_required)
                record["pending_g1_translation_review"] = bool(candidate.excerpt_zh.strip())
                record["dimension_reviews"] = list(review.get("dimension_reviews", []))
                record["removed_unsupported_dimensions"] = removed
                record["semantic_screening_mode"] = "same_model_separate_call_semantic_screening"
                record["p3b_candidate_only"] = True
                record["human_corrected_excerpt"] = None
                record["possible_corroboration_group"] = ""
                final_candidates.append(record)
                candidates_by_source[candidate.source_id] += 1
                candidates_by_nature[candidate.evidence_nature] += 1
                candidates_by_dimension.update(supported)
                pending_visual_count += int(segment.visual_review_required)
                pending_translation_count += int(bool(candidate.excerpt_zh.strip()))

        nonempty = [segment for segment in document.segments if segment.text.strip()]
        coverage_rows.append({"source_id": document.source["source_id"], "format": document.info["format"], "raw_path": relative_path(document.raw_path), "text_path": relative_path(document.text_path), "total_page_or_paragraph_count": len(document.segments), "processed_page_or_paragraph_count": len(nonempty), "empty_or_non_text_count": len(document.segments) - len(nonempty), "batch_count": len(batches), "raw_candidate_count": sum(1 for row in all_candidate_rows if row["batch_id"].startswith(document.source["source_id"] + "-")), "final_candidate_count": candidates_by_source[document.source["source_id"]], "pending_g1_visual_review_count": sum(1 for record in final_candidates if record["source_id"] == document.source["source_id"] and record["pending_g1_visual_review"]), "pending_g1_translation_review_count": sum(1 for record in final_candidates if record["source_id"] == document.source["source_id"] and record["pending_g1_translation_review"]), "raw_sha256": document.info["raw_sha256"], "text_sha256": document.info["text_sha256"], "extraction_status": document.info["extraction_method"]})
        summary_rows.append({"source_id": document.source["source_id"], "total_page_or_paragraph_count": len(document.segments), "processed_page_or_paragraph_count": len(nonempty), "batch_count": len(batches), "raw_candidate_count": coverage_rows[-1]["raw_candidate_count"], "final_candidate_count": coverage_rows[-1]["final_candidate_count"], "pending_g1_visual_review_count": coverage_rows[-1]["pending_g1_visual_review_count"], "pending_g1_translation_review_count": coverage_rows[-1]["pending_g1_translation_review_count"]})

    claim_sources: dict[str, set[str]] = {}
    for record in final_candidates:
        claim_key = normalize_for_match(record.get("normalized_claim", "")).casefold()
        claim_sources.setdefault(claim_key, set()).add(record["source_id"])
    for record in final_candidates:
        claim_key = normalize_for_match(record.get("normalized_claim", "")).casefold()
        if len(claim_sources.get(claim_key, set())) > 1:
            record["possible_corroboration_group"] = _corroboration_group(record)

    p3q_after = immutable_p3q_hashes()
    if p3q_before != p3q_after:
        raise RuntimeError("P3Q input changed during P3b")
    write_jsonl(run_dir / "evidence_candidates_p3b.jsonl", final_candidates)
    write_jsonl(run_dir / "rejected_candidates_p3b.jsonl", rejected)
    write_jsonl(run_dir / "dimension_revision_log.jsonl", dimension_revisions)
    write_jsonl(run_dir / "semantic_screen_results_p3b.jsonl", semantic_results)
    full_coverage_rows = build_full_coverage_rows(documents, summary_rows)
    write_csv(run_dir / "source_coverage_matrix.csv", list(full_coverage_rows[0]) if full_coverage_rows else ["source_id"], full_coverage_rows)
    write_csv(run_dir / "extraction_summary_by_source.csv", list(summary_rows[0]) if summary_rows else ["source_id"], summary_rows)
    g1_fields = ["evidence_id", "source_id", "source_locator", "evidence_nature", "normalized_claim", "coding_dimensions", "excerpt_original", "excerpt_zh", "pending_visual_review", "pending_translation_review", "ai_recommendation", "final_decision", "final_nature", "final_dimensions", "human_corrected_excerpt", "reviewer", "reviewed_at", "review_note"]
    g1_rows = []
    for record in final_candidates:
        g1_rows.append({"evidence_id": record["evidence_id"], "source_id": record["source_id"], "source_locator": record["source_locator"], "evidence_nature": record["evidence_nature"], "normalized_claim": record["normalized_claim"], "coding_dimensions": json.dumps(record["coding_dimensions"], ensure_ascii=False), "excerpt_original": record["excerpt_original"], "excerpt_zh": record["excerpt_zh"], "pending_visual_review": record["pending_g1_visual_review"], "pending_translation_review": record["pending_g1_translation_review"], "ai_recommendation": "candidate_only", "final_decision": "", "final_nature": "", "final_dimensions": "", "human_corrected_excerpt": "", "reviewer": "", "reviewed_at": "", "review_note": ""})
    write_csv(run_dir / "g1_t0_review_template.csv", g1_fields, g1_rows)
    p3b_quality_report = (
        "# P3b quality report\n\n"
        "This run stops before P4/G1. It creates candidate Evidence only; no formal Evidence row, hypothesis, snapshot, or T1 input was used.\n\n"
        f"- source_count: {len(documents)}\n- extraction_batch_count: {batch_count}\n- semantic_screen_batch_count: {semantic_batch_count}\n- provider_call_count: {sum(int(call.get('attempts', 0)) for call in calls)}\n- raw_candidate_count: {raw_candidate_count}\n- schema_valid_count: {schema_valid_count}\n- exact_quote_matched_count: {exact_quote_count}\n- deterministic_gate_passed_count: {deterministic_pass_count}\n- actual_same_model_semantic_screen_pass_count: {semantic_pass_count}\n- final_candidate_count: {len(final_candidates)}\n- final_candidate_acceptance_rate: {(len(final_candidates) / raw_candidate_count if raw_candidate_count else 0.0):.6f}\n- unsupported_dimension_removal_count: {unsupported_dimension_removals}\n- pending_g1_visual_review_count: {pending_visual_count}\n- pending_g1_translation_review_count: {pending_translation_count}\n\n"
        "The semantic screen is a real same-model separate call, not an independent model, human review, or expert review. No rate is called accuracy. Different-source equivalent claims retain a possible_corroboration_group for G1.\n"
    )
    (run_dir / "p3b_quality_report.md").write_text(p3b_quality_report, encoding="utf-8")
    output_hashes = {relative_path(path): sha256_file(path) for path in sorted(run_dir.rglob("*")) if path.is_file()}
    p3b_manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "stage": "P3b",
        "executed_at_utc": _now_utc(),
        "git_head": _current_git_head(),
        "p2_run_id": P2_RUN_ID,
        "source_count": len(documents),
        "included_source_ids": [document.source["source_id"] for document in documents],
        "p3q_input_run_id": P3Q_RUN_ID,
        "p3q_input_hashes": p3q_before,
        "p3q_hashes_unchanged": p3q_before == p3q_after,
        "p3q_audit_correction": correction,
        "transport": client.transport,
        "model_id": client.model,
        "p3b_config_path": relative_path(ROOT / "config/p3b_preflight.yaml"),
        "p3b_config_sha256": sha256_file(ROOT / "config/p3b_preflight.yaml"),
        "extraction_prompt_template_path": relative_path(ROOT / "prompts/evidence_extract_p3b_v2.yaml"),
        "extraction_prompt_template_sha256": sha256_file(ROOT / "prompts/evidence_extract_p3b_v2.yaml"),
        "semantic_prompt_template_path": relative_path(ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml"),
        "semantic_prompt_template_sha256": sha256_file(ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml"),
        "requested_temperature": 0.0,
        "provider_echoed_temperature": None,
        "extraction_batch_count": batch_count,
        "semantic_screen_batch_count": semantic_batch_count,
        "logical_extraction_batch_count": batch_count,
        "logical_semantic_screen_batch_count": semantic_batch_count,
        "provider_call_count": sum(int(call.get("attempts", 0)) for call in calls),
        "raw_candidate_count": raw_candidate_count,
        "schema_valid_count": schema_valid_count,
        "exact_quote_matched_count": exact_quote_count,
        "deterministic_gate_passed_count": deterministic_pass_count,
        "actual_same_model_semantic_screen_pass_count": semantic_pass_count,
        "final_candidate_count": len(final_candidates),
        "final_candidate_acceptance_rate": len(final_candidates) / raw_candidate_count if raw_candidate_count else 0.0,
        "rejected_reason_counts": dict(rejected_reasons),
        "unsupported_dimension_removal_count": unsupported_dimension_removals,
        "pending_g1_visual_review_count": pending_visual_count,
        "pending_g1_translation_review_count": pending_translation_count,
        "candidates_by_source": dict(candidates_by_source),
        "candidates_by_evidence_nature": dict(candidates_by_nature),
        "candidates_by_coding_dimension": dict(candidates_by_dimension),
        "coverage_rows": coverage_rows,
        "coverage_segment_count": sum(len(document.segments) for document in documents),
        "api_call_metadata_count": len(calls),
        "api_key_written": False,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "p4_executed": False,
        "g1_executed": False,
        "snapshot_frozen": False,
        "output_file_sha256": output_hashes,
        "manifest_sha256": None,
        "manifest_hash_note": "Self-hash is intentionally null to avoid a circular hash.",
    }
    write_json(run_dir / "p3b_manifest.json", p3b_manifest)
    return p3b_manifest
