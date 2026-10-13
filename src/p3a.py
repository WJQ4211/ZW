"""Deterministic P3a evidence-candidate validation and metadata injection."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Mapping

from .model_client import RAW_CANDIDATE_FIELDS
from .schemas import EvidenceCandidate, compute_source_excerpt_hash, model_dump


def normalize_for_match(value: str) -> str:
    """Normalize Unicode and whitespace, preserving all non-whitespace content."""
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\b[\w’'/-]+\b", normalize_for_match(value), flags=re.UNICODE))


@dataclass(frozen=True)
class LocatorText:
    source_id: str
    source_locator: str
    text: str


@dataclass(frozen=True)
class QuoteValidation:
    source_id: str
    source_locator: str
    source_locator_exists: bool
    exact_match_after_normalization: bool
    excerpt_word_count: int
    evidence_nature_valid: bool
    coding_dimensions_valid: bool
    duplicate_status: str
    accepted: bool
    failure_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_locator": self.source_locator,
            "source_locator_exists": self.source_locator_exists,
            "exact_match_after_normalization": self.exact_match_after_normalization,
            "excerpt_word_count": self.excerpt_word_count,
            "evidence_nature_valid": self.evidence_nature_valid,
            "coding_dimensions_valid": self.coding_dimensions_valid,
            "duplicate_status": self.duplicate_status,
            "accepted": self.accepted,
            "failure_reasons": "; ".join(self.failure_reasons),
        }


def validate_candidate(
    candidate: EvidenceCandidate,
    locator_text: LocatorText | None,
    prior_candidates: list[EvidenceCandidate],
    allowed_source_ids: set[str],
) -> QuoteValidation:
    reasons: list[str] = []
    if candidate.source_id not in allowed_source_ids:
        reasons.append("source_id_out_of_scope")
    locator_exists = locator_text is not None
    if not locator_exists:
        reasons.append("source_locator_not_found")
    exact_match = False
    if locator_text:
        exact_match = normalize_for_match(candidate.excerpt_original) in normalize_for_match(locator_text.text)
        if not exact_match:
            reasons.append("excerpt_not_found_at_specified_locator")
    dimension_valid = all(
        dimension
        in {
            "integration_level",
            "application_maturity",
            "human_machine_authority",
            "engineering_resource_conditions",
            "trust_governance_constraints",
            "organizational_system_fit",
        }
        for dimension in candidate.coding_dimensions
    )
    nature_valid = candidate.evidence_nature in {
        "goal",
        "resource_input",
        "test_event",
        "test_result",
        "deployment_event",
        "governance_event",
        "constraint",
        "evaluation",
    }
    if not dimension_valid:
        reasons.append("coding_dimension_out_of_scope")
    if not nature_valid:
        reasons.append("evidence_nature_out_of_scope")

    duplicate_status = "unique"
    normalized_excerpt = normalize_for_match(candidate.excerpt_original)
    for prior in prior_candidates:
        if prior.source_id != candidate.source_id:
            continue
        prior_excerpt = normalize_for_match(prior.excerpt_original)
        ratio = SequenceMatcher(None, normalized_excerpt, prior_excerpt).ratio()
        if normalized_excerpt == prior_excerpt:
            duplicate_status = "exact_duplicate"
            reasons.append("same_source_exact_duplicate")
            break
        if ratio >= 0.90 or normalized_excerpt in prior_excerpt or prior_excerpt in normalized_excerpt:
            duplicate_status = "high_overlap_duplicate"
            reasons.append("same_source_high_overlap_duplicate")
            break

    accepted = not reasons
    return QuoteValidation(
        source_id=candidate.source_id,
        source_locator=candidate.source_locator,
        source_locator_exists=locator_exists,
        exact_match_after_normalization=exact_match,
        excerpt_word_count=word_count(candidate.excerpt_original),
        evidence_nature_valid=nature_valid,
        coding_dimensions_valid=dimension_valid,
        duplicate_status=duplicate_status,
        accepted=accepted,
        failure_reasons=tuple(reasons),
    )


def parse_published_at(value: str) -> tuple[str, str]:
    """Return a machine date plus precision without falsely claiming day precision."""
    parts = value.split("-")
    if len(parts) == 3:
        date.fromisoformat(value)
        return value, "day"
    if len(parts) == 2:
        year, month = (int(part) for part in parts)
        return date(year, month, 1).isoformat(), "month"
    if len(parts) == 1 and len(parts[0]) == 4:
        return date(int(parts[0]), 1, 1).isoformat(), "year"
    raise ValueError(f"Unsupported published_at value: {value}")


def stable_evidence_id(candidate: EvidenceCandidate) -> str:
    digest = compute_source_excerpt_hash(
        candidate.source_id,
        candidate.source_locator,
        candidate.excerpt_original,
    )
    return f"EV-T0-P3A-{digest[:16]}"


def inject_deterministic_metadata(
    candidate: EvidenceCandidate,
    source_metadata: Mapping[str, str],
    run_id: str,
) -> dict[str, Any]:
    published_at, precision = parse_published_at(source_metadata["published_at"])
    source_locator = candidate.source_locator
    source_path_or_url = (
        source_metadata.get("canonical_url")
        or source_metadata.get("local_path")
        or source_metadata.get("url_or_path")
    )
    content_hash = compute_source_excerpt_hash(
        source_path_or_url,
        source_locator,
        candidate.excerpt_original,
    )
    payload = {
        "case_id": source_metadata["case_id"],
        "evidence_id": stable_evidence_id(candidate),
        "version": 1,
        "snapshot_tag": "T0",
        "title": source_metadata["title"],
        "source_type": source_metadata["material_type"],
        "source_grade": source_metadata["source_grade"],
        "publisher": source_metadata["publisher"],
        "published_at": published_at,
        "published_at_precision": precision,
        "url_or_path": source_path_or_url,
        "page_or_section": source_locator,
        "source_id": candidate.source_id,
        "source_locator": source_locator,
        "excerpt": candidate.excerpt_original,
        "excerpt_original": candidate.excerpt_original,
        "excerpt_zh": candidate.excerpt_zh,
        "normalized_claim": candidate.normalized_claim,
        "coding_dimensions": list(candidate.coding_dimensions),
        "evidence_nature": candidate.evidence_nature,
        "topics": list(candidate.topics),
        "status": "candidate",
        "content_hash": content_hash,
        "reviewed_by": "",
        "run_id": run_id,
    }
    return payload


def candidate_payload(candidate: EvidenceCandidate) -> dict[str, Any]:
    """Return only the fields permitted in a raw model response."""
    return {key: model_dump(candidate)[key] for key in RAW_CANDIDATE_FIELDS}
