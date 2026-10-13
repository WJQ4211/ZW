"""Pydantic schemas and serialization helpers for the Phase A state machine.

The project targets Pydantic v2.  The small compatibility layer keeps the
prototype runnable in the current workspace, which has Pydantic v1 installed,
without changing the user's global Python environment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, validator

try:  # Pydantic v2 exposes model_validate on BaseModel.
    from pydantic import ConfigDict

    PYDANTIC_V2 = hasattr(BaseModel, "model_validate")
except ImportError:  # pragma: no cover - exercised by older installations
    ConfigDict = None  # type: ignore[assignment]
    PYDANTIC_V2 = False


SCHEMA_VERSION = "phase-a.1"


class StrEnum(str, Enum):
    """A JSON-friendly enum compatible with Python 3.11 and Pydantic 1/2."""


class SnapshotTag(StrEnum):
    T0 = "T0"
    T1 = "T1"


class SourceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    DOUBTFUL = "doubtful"
    OUTDATED = "outdated"
    WITHDRAWN = "withdrawn"
    REPLACED = "replaced"


class Stance(StrEnum):
    SUPPORT = "support"
    CHALLENGE = "challenge"
    CONDITIONAL = "conditional"
    INSUFFICIENT = "insufficient"


class ViewpointStatus(StrEnum):
    CANDIDATE = "candidate"
    ADMITTED = "admitted"
    CONDITIONAL = "conditional"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class DependencyRelation(StrEnum):
    SUPPORT = "support"
    CHALLENGE = "challenge"
    LIMIT = "limit"
    BACKGROUND = "background"


class Importance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DisagreementType(StrEnum):
    FACT = "fact"
    SCOPE = "scope"
    MECHANISM = "mechanism"
    ASSUMPTION = "assumption"
    RISK_WEIGHT = "risk_weight"


class DecisionImpact(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DisagreementStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    CONDITIONALLY_RETAINED = "conditionally_retained"
    CLOSED = "closed"


class Feasibility(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IndicatorCadence(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    EVENT_DRIVEN = "event_driven"


class IndicatorStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class PredicateType(StrEnum):
    EVENT_MATCH = "event_match"
    COUNT_CHANGE = "count_change"
    STATUS_CHANGE = "status_change"
    EXPERT_CONFIRMED = "expert_confirmed"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


if PYDANTIC_V2:

    class SchemaBase(BaseModel):
        model_config = ConfigDict(extra="forbid", validate_assignment=True)

        schema_version: str = SCHEMA_VERSION
        created_at: datetime = Field(default_factory=utc_now)
        run_id: str = Field(default_factory=lambda: f"RUN-{uuid4().hex[:12]}")

else:

    class SchemaBase(BaseModel):
        schema_version: str = SCHEMA_VERSION
        created_at: datetime = Field(default_factory=utc_now)
        run_id: str = Field(default_factory=lambda: f"RUN-{uuid4().hex[:12]}")

        class Config:
            extra = "forbid"
            validate_assignment = True


class EvidenceCitation(SchemaBase):
    evidence_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    relation: DependencyRelation = DependencyRelation.SUPPORT
    note: str = ""

    @property
    def ref(self) -> str:
        return evidence_ref(self.evidence_id, self.version)


EVIDENCE_NATURE_VALUES = frozenset(
    {
        "goal",
        "resource_input",
        "test_event",
        "test_result",
        "deployment_event",
        "governance_event",
        "constraint",
        "evaluation",
    }
)
CODING_DIMENSION_VALUES = frozenset(
    {
        "integration_level",
        "application_maturity",
        "human_machine_authority",
        "engineering_resource_conditions",
        "trust_governance_constraints",
        "organizational_system_fit",
    }
)


class EvidenceCandidate(SchemaBase):
    """Strict model for the eight fields a model may return during P3.

    Runtime metadata is inherited from SchemaBase and is injected by the
    program.  FileBridgeModelClient validates the raw JSON key set before
    constructing this object, so a model cannot supply deterministic fields.
    """

    source_id: str = Field(min_length=1)
    source_locator: str = Field(min_length=1)
    excerpt_original: str = Field(min_length=1)
    excerpt_zh: str = ""
    normalized_claim: str = Field(min_length=1)
    coding_dimensions: List[str] = Field(default_factory=list)
    evidence_nature: str = Field(min_length=1)
    topics: List[str] = Field(default_factory=list)

    @validator("evidence_nature")
    def validate_evidence_nature(cls, value: str) -> str:
        if value not in EVIDENCE_NATURE_VALUES:
            raise ValueError(f"Unsupported evidence_nature: {value}")
        return value

    @validator("coding_dimensions")
    def validate_coding_dimensions(cls, value: List[str], **kwargs: Any) -> List[str]:
        # Pydantic 2's v1-validator adapter passes a keyword named ``values``
        # in addition to the field value.  The old parameter name collided
        # with that adapter and rejected every otherwise-valid candidate.
        del kwargs
        invalid = sorted(set(value) - CODING_DIMENSION_VALUES)
        if invalid:
            raise ValueError(f"Unsupported coding_dimensions: {invalid}")
        return value


class Evidence(SchemaBase):
    case_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    snapshot_tag: SnapshotTag
    title: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_grade: SourceGrade
    publisher: str = Field(min_length=1)
    published_at: date
    published_at_precision: str = "day"
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    url_or_path: str = Field(min_length=1)
    page_or_section: Optional[str] = None
    # P3/G1 source identity is retained explicitly in the formal object.  The
    # database payload remains backward-compatible because both fields have
    # defaults for legacy Phase A fixtures.
    source_id: str = ""
    source_locator: str = ""
    excerpt: str = Field(min_length=1)
    excerpt_original: str = ""
    excerpt_zh: str = ""
    normalized_claim: str = Field(min_length=1)
    evidence_nature: str = "legacy_unclassified"
    coding_dimensions: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    status: EvidenceStatus = EvidenceStatus.ACTIVE
    content_hash: str = ""
    reviewed_by: str = Field(min_length=1)
    correction_of_version: Optional[int] = None
    correction_reason: str = ""
    review_mode: str = ""
    synthetic: bool = False
    # T1 formalization keeps its provenance and impact-gating metadata in the
    # same append-only payload.  These fields are optional so legacy T0 rows
    # remain valid while the strict model can read the committed T1 rows.
    candidate_evidence_id: str = ""
    extraction_run_id: str = ""
    g1_author_decision: str = ""
    human_reviewed: bool = False
    human_source_visual_review_count: int = 0
    lineage: str = ""
    reviewed_at: Optional[datetime] = None
    reviewer: str = ""
    source_visual_review_claimed: bool = False
    t1_change_signal_eligible: bool = True
    temporal_role: str = ""

    @validator("published_at_precision")
    def validate_published_at_precision(cls, value: str) -> str:
        if value not in {"day", "month", "year"}:
            raise ValueError("published_at_precision must be day, month, or year")
        return value


class EvidenceSnapshot(SchemaBase):
    snapshot_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    phase: SnapshotTag
    cutoff_time: datetime
    evidence_versions: List[str] = Field(default_factory=list)
    manifest_hash: str = Field(min_length=1)
    frozen: bool = True
    frozen_at: datetime = Field(default_factory=utc_now)
    frozen_by: str = Field(min_length=1)


class Hypothesis(SchemaBase):
    hypothesis_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    decision_dimension: str = Field(min_length=1)
    time_horizon: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    observable_implications: List[str] = Field(default_factory=list)
    falsifiers: List[str] = Field(default_factory=list)
    source_candidates: List[str] = Field(default_factory=list)
    frozen: bool = False
    # Formal G2 payload fields retained for backward-compatible reads.
    case_id: str = ""
    decision: str = ""
    status: str = ""
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    counter_evidence_ids: List[str] = Field(default_factory=list)
    key_assumptions: List[str] = Field(default_factory=list)
    falsification_conditions: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    covered_dimensions: List[str] = Field(default_factory=list)
    conversion_criteria: List[str] = Field(default_factory=list)
    source_candidate_ids: List[str] = Field(default_factory=list)
    source_p5_candidate_ids: List[str] = Field(default_factory=list)
    merge_target: str = ""
    author_authorization_sha256: str = ""
    author_signoff_count: int = 0
    author_decision_count: int = 0
    hypothesis_content_authorized: bool = False
    raw_source_visual_review_claimed: bool = False
    review_mode: str = ""
    reviewed_by: str = ""


class Viewpoint(SchemaBase):
    viewpoint_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    parent_version: Optional[int] = None
    snapshot_id: str = Field(min_length=1)
    perspective_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    stance: Stance
    judgment: str = Field(min_length=1)
    supporting_evidence: List[EvidenceCitation] = Field(default_factory=list)
    counter_evidence: List[EvidenceCitation] = Field(default_factory=list)
    key_assumptions: List[str] = Field(default_factory=list)
    mechanism_claims: List[str] = Field(default_factory=list)
    boundary_conditions: List[str] = Field(default_factory=list)
    falsifiers: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    # P7-P9 retains the information-gap and provenance fields needed by the
    # multi-perspective audit while keeping the original Phase A fields
    # backward-compatible for synthetic fixtures.
    information_gaps: List[str] = Field(default_factory=list)
    confidence: str = ""
    case_id: str = ""
    human_verified: bool = False
    admission_failure_reason: str = ""
    status: ViewpointStatus
    change_reason: str = "initial_version"
    generator: Dict[str, Any] = Field(default_factory=dict)


class EvidenceDependency(SchemaBase):
    case_id: str = ""
    edge_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    evidence_version: int = Field(ge=1)
    viewpoint_id: str = Field(min_length=1)
    viewpoint_version: int = Field(ge=1)
    target_field: str = Field(min_length=1)
    relation: DependencyRelation
    importance: Importance
    created_by: str = Field(min_length=1)
    human_verified: bool = False
    changed_fields: List[str] = Field(default_factory=list)

    @property
    def evidence_ref(self) -> str:
        return evidence_ref(self.evidence_id, self.evidence_version)


class TriggerRelation(SchemaBase):
    """A T1 evidence relation attached to fields changed in a new viewpoint."""

    evidence_id: str = Field(min_length=1)
    evidence_version: int = Field(ge=1)
    relation: DependencyRelation
    changed_fields: List[str] = Field(default_factory=list)
    note: str = ""

    @property
    def ref(self) -> str:
        return evidence_ref(self.evidence_id, self.evidence_version)


class BranchClaim(SchemaBase):
    claim: str = Field(min_length=1)
    evidence: List[EvidenceCitation] = Field(default_factory=list)
    expected_observables: List[str] = Field(default_factory=list)


class Disagreement(SchemaBase):
    disagreement_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    viewpoint_refs: List[str] = Field(default_factory=list)
    type: DisagreementType
    contested_node: str = Field(min_length=1)
    branch_a: BranchClaim
    branch_b: BranchClaim
    decision_impact: DecisionImpact
    resolvable_now: bool
    resolution_action: str = Field(min_length=1)
    status: DisagreementStatus
    human_verified: bool = False
    # G3 formalization metadata is payload-only so legacy rows remain readable.
    viewpoint_field_refs: List[Dict[str, Any]] = Field(default_factory=list)
    author_authorization_sha256: str = ""
    review_mode: str = ""
    reviewed_by: str = ""
    reviewed_at: Optional[datetime] = None
    formal_resolution_reason: str = ""
    source_candidate_disagreement: Dict[str, Any] = Field(default_factory=dict)


class DiscriminativeNeed(SchemaBase):
    need_id: str = Field(min_length=1)
    disagreement_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    possible_outcomes: List[str] = Field(default_factory=list)
    favours_if: Dict[str, str] = Field(default_factory=dict)
    source_plan: List[str] = Field(default_factory=list)
    observation_window: str = Field(min_length=1)
    feasibility: Feasibility
    discriminativeness: Optional[int] = Field(default=None, ge=1, le=5)
    status: str = "draft"
    human_verified: bool = False
    author_authorization_sha256: str = ""
    review_mode: str = ""


class MonitorIndicator(SchemaBase):
    indicator_id: str = Field(min_length=1)
    need_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    operational_definition: str = Field(min_length=1)
    source_types: List[str] = Field(default_factory=list)
    cadence: IndicatorCadence
    direction_a: str = Field(min_length=1)
    direction_b: str = Field(min_length=1)
    affected_viewpoint_ids: List[str] = Field(default_factory=list)
    status: IndicatorStatus = IndicatorStatus.DRAFT
    observation_window: str = ""
    human_verified: bool = False
    author_authorization_sha256: str = ""
    review_mode: str = ""


class TriggerRule(SchemaBase):
    trigger_id: str = Field(min_length=1)
    indicator_id: str = Field(min_length=1)
    predicate_type: PredicateType
    predicate: Dict[str, Any] = Field(default_factory=dict)
    required_source_grade: List[SourceGrade] = Field(default_factory=list)
    action: str = Field(min_length=1)
    affected_viewpoint_ids: List[str] = Field(default_factory=list)
    human_approval_required: bool = True
    status: IndicatorStatus = IndicatorStatus.DRAFT
    observation_window: str = ""
    human_verified: bool = False
    author_authorization_sha256: str = ""
    review_mode: str = ""


class UpdateEvent(SchemaBase):
    update_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    from_snapshot: str = Field(min_length=1)
    to_snapshot: str = Field(min_length=1)
    triggered_by: Dict[str, List[str]] = Field(default_factory=dict)
    affected_candidates: Dict[str, List[str]] = Field(default_factory=dict)
    system_candidates: List[str] = Field(default_factory=list)
    expert_reference_set: List[str] = Field(default_factory=list)
    intersection: List[str] = Field(default_factory=list)
    missed: List[str] = Field(default_factory=list)
    false_positives: List[str] = Field(default_factory=list)
    approved_affected: List[str] = Field(default_factory=list)
    new_viewpoint_versions: List[str] = Field(default_factory=list)
    unchanged_viewpoints: List[str] = Field(default_factory=list)
    main_judgment_changed: bool = False
    change_summary: List[str] = Field(default_factory=list)


class HumanReview(SchemaBase):
    review_id: str = Field(min_length=1)
    gate: str = Field(min_length=2)
    case_id: str = Field(min_length=1)
    approved: bool
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime = Field(default_factory=utc_now)
    artifact_path: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    notes: str = ""


def evidence_ref(evidence_id: str, version: int) -> str:
    return f"{evidence_id}:{version}"


def parse_evidence_ref(value: str) -> tuple[str, int]:
    try:
        evidence_id, version_text = value.rsplit(":", 1)
        version = int(version_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid evidence reference: {value!r}") from exc
    if not evidence_id or version < 1:
        raise ValueError(f"Invalid evidence reference: {value!r}")
    return evidence_id, version


def model_dump(model: BaseModel) -> Dict[str, Any]:
    """Return JSON-compatible data on both Pydantic major versions."""
    if PYDANTIC_V2:
        return model.model_dump(mode="json")  # type: ignore[attr-defined]
    return json.loads(model.json())


def model_validate(model_type: type[BaseModel], data: Any) -> BaseModel:
    if PYDANTIC_V2:
        return model_type.model_validate(data)  # type: ignore[attr-defined]
    return model_type.parse_obj(data)


def model_copy(model: BaseModel, *, update: Dict[str, Any]) -> BaseModel:
    if PYDANTIC_V2:
        return model.model_copy(update=update)  # type: ignore[attr-defined]
    return model.copy(update=update)


def compute_evidence_hash(evidence: Evidence) -> str:
    return compute_source_excerpt_hash(
        evidence.url_or_path,
        evidence.page_or_section or "",
        evidence.excerpt_original or evidence.excerpt,
    )


def compute_source_excerpt_hash(source: str, locator: str, excerpt_original: str) -> str:
    """Hash only source, locator, and verbatim excerpt content."""
    payload = [source, locator, excerpt_original]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def with_evidence_hash(evidence: Evidence) -> Evidence:
    content_hash = compute_evidence_hash(evidence)
    if evidence.content_hash == content_hash:
        return evidence
    return model_copy(evidence, update={"content_hash": content_hash})  # type: ignore[return-value]


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
