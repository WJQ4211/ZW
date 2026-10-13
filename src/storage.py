"""SQLite persistence for append-only Phase A objects."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Type

from .schemas import (
    DiscriminativeNeed,
    Disagreement,
    Evidence,
    EvidenceDependency,
    EvidenceSnapshot,
    EvidenceStatus,
    Hypothesis,
    IndicatorStatus,
    MonitorIndicator,
    SnapshotTag,
    TriggerRule,
    UpdateEvent,
    Viewpoint,
    ViewpointStatus,
    evidence_ref,
    model_dump,
    model_validate,
    parse_evidence_ref,
    stable_hash,
    with_evidence_hash,
)


class StorageError(RuntimeError):
    """Raised when a business or append-only storage constraint is violated."""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_config (
    case_id TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    snapshot_tag TEXT NOT NULL CHECK(snapshot_tag IN ('T0', 'T1')),
    status TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_at_precision TEXT NOT NULL DEFAULT 'day',
    content_hash TEXT NOT NULL,
    evidence_nature TEXT NOT NULL DEFAULT 'legacy_unclassified',
    excerpt_original TEXT NOT NULL DEFAULT '',
    excerpt_zh TEXT NOT NULL DEFAULT '',
    coding_dimensions_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    PRIMARY KEY (case_id, evidence_id, version)
);

CREATE TABLE IF NOT EXISTS evidence_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('T0', 'T1')),
    cutoff_time TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    frozen INTEGER NOT NULL CHECK(frozen IN (0, 1)),
    frozen_at TEXT NOT NULL,
    frozen_by TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id, case_id, evidence_id, version),
    FOREIGN KEY(snapshot_id) REFERENCES evidence_snapshot(snapshot_id),
    FOREIGN KEY(case_id, evidence_id, version) REFERENCES evidence(case_id, evidence_id, version)
);

CREATE TABLE IF NOT EXISTS hypothesis (
    hypothesis_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    frozen INTEGER NOT NULL CHECK(frozen IN (0, 1)),
    payload_json TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES evidence_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS viewpoint (
    viewpoint_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(viewpoint_id, version),
    FOREIGN KEY(snapshot_id) REFERENCES evidence_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS evidence_dependency (
    edge_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_version INTEGER NOT NULL,
    viewpoint_id TEXT NOT NULL,
    viewpoint_version INTEGER NOT NULL,
    relation TEXT NOT NULL,
    importance TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(case_id, evidence_id, evidence_version) REFERENCES evidence(case_id, evidence_id, version),
    FOREIGN KEY(viewpoint_id, viewpoint_version) REFERENCES viewpoint(viewpoint_id, version)
);

CREATE TABLE IF NOT EXISTS disagreement (
    disagreement_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    decision_impact TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES evidence_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS discriminative_need (
    need_id TEXT PRIMARY KEY,
    disagreement_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(disagreement_id) REFERENCES disagreement(disagreement_id)
);

CREATE TABLE IF NOT EXISTS monitor_indicator (
    indicator_id TEXT PRIMARY KEY,
    need_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(need_id) REFERENCES discriminative_need(need_id)
);

CREATE TABLE IF NOT EXISTS trigger_rule (
    trigger_id TEXT PRIMARY KEY,
    indicator_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(indicator_id) REFERENCES monitor_indicator(indicator_id)
);

CREATE TABLE IF NOT EXISTS update_event (
    update_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    from_snapshot TEXT NOT NULL,
    to_snapshot TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(from_snapshot) REFERENCES evidence_snapshot(snapshot_id),
    FOREIGN KEY(to_snapshot) REFERENCES evidence_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS model_run (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    generator_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_review (
    review_id TEXT PRIMARY KEY,
    gate TEXT NOT NULL,
    case_id TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK(approved IN (0, 1)),
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact (
    artifact_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_stage (
    stage_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES pipeline_run(run_id)
);

CREATE TABLE IF NOT EXISTS model_call (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES pipeline_run(run_id)
);
"""


TRIGGER_SQL = """
DROP TRIGGER IF EXISTS prevent_frozen_snapshot_update;
DROP TRIGGER IF EXISTS prevent_frozen_snapshot_delete;
DROP TRIGGER IF EXISTS prevent_frozen_snapshot_item_insert;
DROP TRIGGER IF EXISTS prevent_frozen_snapshot_item_update;
DROP TRIGGER IF EXISTS prevent_frozen_snapshot_item_delete;
DROP TRIGGER IF EXISTS prevent_evidence_update;
DROP TRIGGER IF EXISTS prevent_evidence_delete;
DROP TRIGGER IF EXISTS prevent_viewpoint_update;
DROP TRIGGER IF EXISTS prevent_viewpoint_delete;

CREATE TRIGGER prevent_frozen_snapshot_update
BEFORE UPDATE ON evidence_snapshot
WHEN OLD.frozen = 1
BEGIN
    SELECT RAISE(ABORT, 'frozen evidence snapshot is immutable');
END;

CREATE TRIGGER prevent_frozen_snapshot_delete
BEFORE DELETE ON evidence_snapshot
WHEN OLD.frozen = 1
BEGIN
    SELECT RAISE(ABORT, 'frozen evidence snapshot is immutable');
END;

CREATE TRIGGER prevent_frozen_snapshot_item_insert
BEFORE INSERT ON snapshot_items
WHEN (SELECT frozen FROM evidence_snapshot WHERE snapshot_id = NEW.snapshot_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'items of a frozen evidence snapshot are immutable');
END;

CREATE TRIGGER prevent_frozen_snapshot_item_update
BEFORE UPDATE ON snapshot_items
WHEN (SELECT frozen FROM evidence_snapshot WHERE snapshot_id = OLD.snapshot_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'items of a frozen evidence snapshot are immutable');
END;

CREATE TRIGGER prevent_frozen_snapshot_item_delete
BEFORE DELETE ON snapshot_items
WHEN (SELECT frozen FROM evidence_snapshot WHERE snapshot_id = OLD.snapshot_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'items of a frozen evidence snapshot are immutable');
END;

CREATE TRIGGER prevent_evidence_update
BEFORE UPDATE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'evidence history is append-only');
END;

CREATE TRIGGER prevent_evidence_delete
BEFORE DELETE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'evidence history is append-only');
END;

CREATE TRIGGER prevent_viewpoint_update
BEFORE UPDATE ON viewpoint
BEGIN
    SELECT RAISE(ABORT, 'viewpoint history is append-only');
END;

CREATE TRIGGER prevent_viewpoint_delete
BEFORE DELETE ON viewpoint
BEGIN
    SELECT RAISE(ABORT, 'viewpoint history is append-only');
END;
"""


def _utc_text(value: datetime) -> str:
    return value.isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_payload(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"])
    # A prior T1 merge wrote the normalized relational columns plus its
    # append-only audit fields, while the legacy Evidence model expects the
    # canonical ``excerpt``, ``source_type`` and ``reviewed_by`` fields too.
    # Normalize only the in-memory read view; never rewrite historical JSON.
    if "evidence_id" in payload and ("snapshot_tag" in payload or "published_at" in payload):
        payload.setdefault("excerpt", payload.get("excerpt_original", ""))
        payload.setdefault("source_type", "public_source")
        payload.setdefault("reviewed_by", payload.get("reviewer") or "author_authorized_pipeline")
    if "edge_id" in payload:
        # P15's append-only audit edge retains a rationale/origin alongside
        # the canonical dependency fields.  The extras stay in SQLite, but
        # are omitted from the in-memory strict schema projection.
        payload.setdefault("created_by", payload.get("origin") or "P14_field_level_impact_edge")
        payload.pop("origin", None)
        payload.pop("rationale", None)
    # Formal G3-T1 monitoring rows retain richer sign-off audit metadata in
    # SQLite.  Keep that metadata in the raw payload while projecting only the
    # fields accepted by the legacy Pydantic object readers.
    if "need_id" in payload:
        payload.pop("reviewed_by", None)
        payload.pop("reviewed_at", None)
        payload.pop("source_candidate_discriminativeness", None)
    if "indicator_id" in payload:
        payload.pop("reviewed_by", None)
        payload.pop("reviewed_at", None)
    if "trigger_id" in payload:
        # C1 keeps human-readable trigger columns alongside the canonical
        # predicate object; they are audit payload, not TriggerRule fields.
        payload.pop("condition", None)
        payload.pop("strengthens_branch", None)
        payload.pop("weakens_branch", None)
    return payload


def _canonical_payload(value: Any) -> Any:
    """Ignore runtime metadata when checking idempotent fixture replays."""
    if isinstance(value, dict):
        return {
            key: _canonical_payload(item)
            for key, item in value.items()
            if key not in {"created_at", "reviewed_at", "run_id", "created_by"}
        }
    if isinstance(value, list):
        return [_canonical_payload(item) for item in value]
    return value


class Store:
    """Small repository enforcing the Phase A append-only rules."""

    def __init__(self, path: str | Path = "data/research.sqlite3") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_legacy_schema()
        self.conn.executescript(TRIGGER_SQL)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (2, datetime.utcnow().isoformat()),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (3, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def _table_columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _has_primary_key(self, table: str, expected: Sequence[str]) -> bool:
        rows = sorted(
            (row["pk"], row["name"])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            if row["pk"]
        )
        return [name for _, name in rows] == list(expected)

    def _migrate_legacy_schema(self) -> None:
        """Migrate the Stage A database before installing append-only triggers."""
        # An intermediate Phase A+ database may already have the new trigger
        # names.  Remove them while rebuilding legacy tables; they are
        # installed again immediately after migration completes.
        self.conn.executescript(
            """
            DROP TRIGGER IF EXISTS prevent_evidence_update;
            DROP TRIGGER IF EXISTS prevent_evidence_delete;
            DROP TRIGGER IF EXISTS prevent_viewpoint_update;
            DROP TRIGGER IF EXISTS prevent_viewpoint_delete;
            """
        )
        evidence_columns = self._table_columns("evidence")
        if "case_id" not in evidence_columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN case_id TEXT NOT NULL DEFAULT '__legacy__'"
            )
            configured = [row["case_id"] for row in self.conn.execute("SELECT case_id FROM case_config").fetchall()]
            legacy_case = configured[0] if len(configured) == 1 else "__legacy__"
            self.conn.execute("UPDATE evidence SET case_id=? WHERE case_id='__legacy__'", (legacy_case,))
            evidence_columns.add("case_id")
        for column, definition in (
            ("published_at_precision", "TEXT NOT NULL DEFAULT 'day'"),
            ("evidence_nature", "TEXT NOT NULL DEFAULT 'legacy_unclassified'"),
            ("excerpt_original", "TEXT NOT NULL DEFAULT ''"),
            ("excerpt_zh", "TEXT NOT NULL DEFAULT ''"),
            ("coding_dimensions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in evidence_columns:
                self.conn.execute(f"ALTER TABLE evidence ADD COLUMN {column} {definition}")
                evidence_columns.add(column)
        for row in self.conn.execute("SELECT evidence_id, version, case_id, payload_json FROM evidence").fetchall():
            payload = json.loads(row["payload_json"])
            changed = False
            defaults = {
                "case_id": row["case_id"],
                "published_at_precision": "day",
                "evidence_nature": "legacy_unclassified",
                "excerpt_original": "",
                "excerpt_zh": "",
                "coding_dimensions": [],
            }
            for key, value in defaults.items():
                if key not in payload:
                    payload[key] = value
                    changed = True
            if changed:
                self.conn.execute(
                    "UPDATE evidence SET payload_json=?, published_at_precision=?, evidence_nature=?, excerpt_original=?, excerpt_zh=?, coding_dimensions_json=? "
                    "WHERE evidence_id=? AND version=?",
                    (
                        _json(payload),
                        payload["published_at_precision"],
                        payload["evidence_nature"],
                        payload["excerpt_original"],
                        payload["excerpt_zh"],
                        _json(payload["coding_dimensions"]),
                        row["evidence_id"],
                        row["version"],
                    ),
                )
        # Backfill fields introduced by the hardening schema so replaying an
        # old idempotent fixture does not look like a conflicting write.
        if "payload_json" in self._table_columns("evidence_dependency"):
            for row in self.conn.execute("SELECT edge_id, payload_json FROM evidence_dependency").fetchall():
                payload = json.loads(row["payload_json"])
                if "changed_fields" not in payload:
                    payload["changed_fields"] = []
                    self.conn.execute(
                        "UPDATE evidence_dependency SET payload_json=? WHERE edge_id=?",
                        (_json(payload), row["edge_id"]),
                    )

        needs_case_scoped_rebuild = (
            not self._has_primary_key("evidence", ["case_id", "evidence_id", "version"])
            or "case_id" not in self._table_columns("snapshot_items")
            or "case_id" not in self._table_columns("evidence_dependency")
        )
        if needs_case_scoped_rebuild:
            self._rebuild_case_scoped_tables()
        # These tables were added by Phase A+; CREATE IF NOT EXISTS above handles
        # new databases, while this branch documents the migration boundary.
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (1, datetime.utcnow().isoformat()),
        )

    def _rebuild_case_scoped_tables(self) -> None:
        """Rebuild the Stage A tables whose foreign keys now include case_id."""
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("ALTER TABLE evidence RENAME TO evidence_legacy")
            self.conn.execute("ALTER TABLE snapshot_items RENAME TO snapshot_items_legacy")
            self.conn.execute("ALTER TABLE evidence_dependency RENAME TO evidence_dependency_legacy")
            self.conn.executescript(
                """
                CREATE TABLE evidence (
                    case_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    snapshot_tag TEXT NOT NULL CHECK(snapshot_tag IN ('T0', 'T1')),
                    status TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    published_at_precision TEXT NOT NULL DEFAULT 'day',
                    content_hash TEXT NOT NULL,
                    evidence_nature TEXT NOT NULL DEFAULT 'legacy_unclassified',
                    excerpt_original TEXT NOT NULL DEFAULT '',
                    excerpt_zh TEXT NOT NULL DEFAULT '',
                    coding_dimensions_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (case_id, evidence_id, version)
                );
                CREATE TABLE snapshot_items (
                    snapshot_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY(snapshot_id, case_id, evidence_id, version),
                    FOREIGN KEY(snapshot_id) REFERENCES evidence_snapshot(snapshot_id),
                    FOREIGN KEY(case_id, evidence_id, version) REFERENCES evidence(case_id, evidence_id, version)
                );
                CREATE TABLE evidence_dependency (
                    edge_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    evidence_version INTEGER NOT NULL,
                    viewpoint_id TEXT NOT NULL,
                    viewpoint_version INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    importance TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(case_id, evidence_id, evidence_version) REFERENCES evidence(case_id, evidence_id, version),
                    FOREIGN KEY(viewpoint_id, viewpoint_version) REFERENCES viewpoint(viewpoint_id, version)
                );
                """
            )
            self.conn.execute(
                "INSERT INTO evidence(case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json) "
                "SELECT case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json FROM evidence_legacy"
            )
            self.conn.execute(
                "INSERT INTO snapshot_items(snapshot_id, case_id, evidence_id, version) "
                "SELECT i.snapshot_id, s.case_id, i.evidence_id, i.version "
                "FROM snapshot_items_legacy i JOIN evidence_snapshot s ON s.snapshot_id=i.snapshot_id"
            )
            self.conn.execute(
                "INSERT INTO evidence_dependency(edge_id, case_id, evidence_id, evidence_version, viewpoint_id, viewpoint_version, relation, importance, payload_json) "
                "SELECT d.edge_id, s.case_id, d.evidence_id, d.evidence_version, d.viewpoint_id, d.viewpoint_version, d.relation, d.importance, d.payload_json "
                "FROM evidence_dependency_legacy d "
                "JOIN viewpoint v ON v.viewpoint_id=d.viewpoint_id AND v.version=d.viewpoint_version "
                "JOIN evidence_snapshot s ON s.snapshot_id=v.snapshot_id"
            )
            self.conn.execute("DROP TABLE evidence_dependency_legacy")
            self.conn.execute("DROP TABLE snapshot_items_legacy")
            self.conn.execute("DROP TABLE evidence_legacy")
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def save_case_config(self, case_id: str, config: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO case_config(case_id, config_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(case_id) DO UPDATE SET config_json=excluded.config_json",
            (case_id, _json(config), datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def get_case_config(self, case_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT config_json FROM case_config WHERE case_id = ?", (case_id,)
        ).fetchone()
        return json.loads(row["config_json"]) if row else None

    def record_model_run(
        self, run_id: str, case_id: str, phase: str, generator_type: str, payload: Dict[str, Any]
    ) -> None:
        """Backward-compatible name; writes an append-only pipeline stage."""
        row = self.conn.execute("SELECT case_id FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
        if row and row["case_id"] != case_id:
            raise StorageError(f"Pipeline run {run_id} belongs to another case")
        if not row:
            self.record_pipeline_run(run_id, case_id, phase, {"generator_type": generator_type})
        self.record_pipeline_stage(
            run_id=run_id,
            case_id=case_id,
            phase=phase,
            stage_name=generator_type,
            payload=payload,
        )

    def record_pipeline_run(self, run_id: str, case_id: str, phase: str, payload: Dict[str, Any]) -> None:
        row = self.conn.execute("SELECT * FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
        data = _json(payload)
        if row:
            if row["case_id"] != case_id or row["phase"] != phase or _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(payload):
                raise StorageError(f"Pipeline run {run_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO pipeline_run(run_id, case_id, phase, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, case_id, phase, datetime.utcnow().isoformat(), data),
        )
        self.conn.commit()

    def record_pipeline_stage(
        self, *, run_id: str, case_id: str, phase: str, stage_name: str, payload: Dict[str, Any]
    ) -> str:
        stage_id = f"STAGE-{run_id}-{stable_hash([stage_name, payload, datetime.utcnow().isoformat()])[:16]}"
        self.conn.execute(
            "INSERT INTO pipeline_stage(stage_id, run_id, case_id, phase, stage_name, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stage_id, run_id, case_id, phase, stage_name, datetime.utcnow().isoformat(), _json(payload)),
        )
        self.conn.commit()
        return stage_id

    def record_model_call(self, *, run_id: str, case_id: str, phase: str, payload: Dict[str, Any]) -> str:
        call_id = f"CALL-{run_id}-{stable_hash([payload, datetime.utcnow().isoformat()])[:16]}"
        self.conn.execute(
            "INSERT INTO model_call(call_id, run_id, case_id, phase, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, run_id, case_id, phase, datetime.utcnow().isoformat(), _json(payload)),
        )
        self.conn.commit()
        return call_id

    def record_human_review(self, review: Any) -> None:
        data = model_dump(review)
        row = self.conn.execute(
            "SELECT payload_json FROM human_review WHERE review_id=?", (data["review_id"],)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Human review {data['review_id']} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO human_review(review_id, gate, case_id, approved, payload_json) VALUES (?, ?, ?, ?, ?)",
            (data["review_id"], data["gate"], data["case_id"], int(data["approved"]), _json(data)),
        )
        self.conn.commit()

    def insert_evidence(self, evidence: Evidence) -> Evidence:
        evidence = with_evidence_hash(evidence)
        data = model_dump(evidence)
        row = self.conn.execute(
            "SELECT payload_json FROM evidence WHERE case_id=? AND evidence_id=? AND version=?",
            (evidence.case_id, evidence.evidence_id, evidence.version),
        ).fetchone()
        if row:
            existing_data = json.loads(row["payload_json"])
            comparable_existing = dict(existing_data)
            comparable_data = dict(data)
            for field in ("created_at", "run_id", "schema_version", "content_hash"):
                comparable_existing.pop(field, None)
                comparable_data.pop(field, None)
            if _canonical_payload(comparable_existing) != _canonical_payload(comparable_data):
                raise StorageError(
                    f"Evidence {evidence_ref(evidence.evidence_id, evidence.version)} already exists with different content"
                )
            return model_validate(Evidence, existing_data)  # type: ignore[return-value]
        self.conn.execute(
            "INSERT INTO evidence(case_id, evidence_id, version, snapshot_tag, status, published_at, published_at_precision, content_hash, evidence_nature, excerpt_original, excerpt_zh, coding_dimensions_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence.case_id,
                evidence.evidence_id,
                evidence.version,
                evidence.snapshot_tag.value,
                evidence.status.value,
                evidence.published_at.isoformat(),
                evidence.published_at_precision,
                evidence.content_hash,
                evidence.evidence_nature,
                evidence.excerpt_original,
                evidence.excerpt_zh,
                _json(evidence.coding_dimensions),
                _json(data),
            ),
        )
        self.conn.commit()
        return evidence

    def get_evidence(self, evidence_id: str, version: int, case_id: Optional[str] = None) -> Optional[Evidence]:
        if case_id:
            row = self.conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=? AND version=? AND case_id=?",
                (evidence_id, version, case_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=? AND version=? ORDER BY case_id LIMIT 1",
                (evidence_id, version),
            ).fetchone()
        return model_validate(Evidence, _read_payload(row)) if row else None  # type: ignore[return-value]

    def list_evidence(self, *, tags: Optional[Sequence[SnapshotTag]] = None, case_id: Optional[str] = None) -> List[Evidence]:
        conditions: List[str] = []
        params: List[Any] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            conditions.append(f"snapshot_tag IN ({placeholders})")
            params.extend(tag.value for tag in tags)
        if case_id:
            conditions.append("case_id=?")
            params.append(case_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            "SELECT payload_json FROM evidence" + where + " ORDER BY published_at, evidence_id, version",
            params,
        ).fetchall()
        return [model_validate(Evidence, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def create_snapshot(
        self,
        *,
        snapshot_id: str,
        case_id: str,
        phase: SnapshotTag,
        cutoff_time: datetime,
        evidence_versions: Iterable[str],
        frozen_by: str,
        run_id: str,
    ) -> EvidenceSnapshot:
        refs = sorted(set(evidence_versions))
        if not refs:
            raise StorageError("Cannot freeze an empty evidence snapshot")
        existing = self.get_snapshot(snapshot_id, case_id)
        if existing:
            if not existing.frozen:
                raise StorageError(f"Snapshot {snapshot_id} is already being built and is not resumable")
            if existing.evidence_versions != refs or existing.manifest_hash != stable_hash(refs):
                raise StorageError(f"Snapshot {snapshot_id} already exists with different content")
            return existing
        evidence_objects: List[Evidence] = []
        for ref in refs:
            evidence_id, version = parse_evidence_ref(ref)
            evidence = self.get_evidence(evidence_id, version, case_id)
            if evidence is None:
                raise StorageError(f"Snapshot references missing evidence: {ref}")
            if evidence.case_id != case_id:
                raise StorageError(f"Evidence belongs to another case: {ref}")
            if phase == SnapshotTag.T0 and evidence.snapshot_tag != SnapshotTag.T0:
                raise StorageError(f"T0 snapshot cannot contain {evidence.snapshot_tag.value} evidence: {ref}")
            if evidence.published_at.isoformat() > cutoff_time.date().isoformat():
                raise StorageError(f"Evidence is newer than snapshot cutoff: {ref}")
            evidence_objects.append(evidence)
        if phase == SnapshotTag.T1 and not any(e.snapshot_tag == SnapshotTag.T1 for e in evidence_objects):
            raise StorageError("T1 snapshot must contain at least one T1 evidence item")
        manifest_hash = stable_hash(refs)
        draft = EvidenceSnapshot(
            snapshot_id=snapshot_id,
            case_id=case_id,
            phase=phase,
            cutoff_time=cutoff_time,
            evidence_versions=refs,
            manifest_hash=manifest_hash,
            frozen=False,
            frozen_by=frozen_by,
            run_id=run_id,
        )
        draft_data = model_dump(draft)
        with self.transaction():
            self.conn.execute(
                "INSERT INTO evidence_snapshot(snapshot_id, case_id, phase, cutoff_time, manifest_hash, frozen, frozen_at, frozen_by, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft.snapshot_id,
                    draft.case_id,
                    draft.phase.value,
                    draft.cutoff_time.isoformat(),
                    draft.manifest_hash,
                    int(draft.frozen),
                    draft.frozen_at.isoformat(),
                    draft.frozen_by,
                    _json(draft_data),
                ),
            )
            for ref in refs:
                evidence_id, version = parse_evidence_ref(ref)
                self.conn.execute(
                    "INSERT INTO snapshot_items(snapshot_id, case_id, evidence_id, version) VALUES (?, ?, ?, ?)",
                    (draft.snapshot_id, case_id, evidence_id, version),
                )
            actual_refs = [
                evidence_ref(row["evidence_id"], row["version"])
                for row in self.conn.execute(
                    "SELECT evidence_id, version FROM snapshot_items WHERE snapshot_id=? ORDER BY evidence_id, version",
                    (snapshot_id,),
                ).fetchall()
            ]
            if actual_refs != refs or stable_hash(actual_refs) != manifest_hash:
                raise StorageError(f"Snapshot {snapshot_id} manifest verification failed before freeze")
            frozen_data = dict(draft_data)
            frozen_data["frozen"] = True
            frozen_data["frozen_at"] = datetime.utcnow().isoformat()
            frozen = model_validate(EvidenceSnapshot, frozen_data)
            self.conn.execute(
                "UPDATE evidence_snapshot SET frozen=1, frozen_at=?, payload_json=? WHERE snapshot_id=?",
                (frozen.frozen_at.isoformat(), _json(model_dump(frozen)), snapshot_id),
            )
        return frozen  # type: ignore[return-value]

    def get_snapshot(self, snapshot_id: str, case_id: Optional[str] = None) -> Optional[EvidenceSnapshot]:
        if case_id:
            row = self.conn.execute(
                "SELECT payload_json FROM evidence_snapshot WHERE snapshot_id=? AND case_id=?",
                (snapshot_id, case_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT payload_json FROM evidence_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return model_validate(EvidenceSnapshot, _read_payload(row)) if row else None  # type: ignore[return-value]

    def list_snapshots(self, case_id: Optional[str] = None) -> List[EvidenceSnapshot]:
        if case_id:
            rows = self.conn.execute(
                "SELECT payload_json FROM evidence_snapshot WHERE case_id=? ORDER BY frozen_at",
                (case_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM evidence_snapshot ORDER BY frozen_at"
            ).fetchall()
        return [model_validate(EvidenceSnapshot, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_hypotheses(self, hypotheses: Iterable[Hypothesis]) -> None:
        with self.transaction():
            for hypothesis in hypotheses:
                snapshot = self.get_snapshot(hypothesis.snapshot_id)
                if not snapshot or not snapshot.frozen:
                    raise StorageError("Hypotheses require a frozen snapshot")
                if not hypothesis.frozen:
                    raise StorageError(f"Hypothesis {hypothesis.hypothesis_id} is not frozen at G2")
                if not hypothesis.observable_implications or not hypothesis.falsifiers:
                    raise StorageError(f"Hypothesis {hypothesis.hypothesis_id} needs observable implications and falsifiers")
                data = model_dump(hypothesis)
                row = self.conn.execute(
                    "SELECT payload_json FROM hypothesis WHERE hypothesis_id=?",
                    (hypothesis.hypothesis_id,),
                ).fetchone()
                if row:
                    if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                        raise StorageError(f"Hypothesis {hypothesis.hypothesis_id} already exists with different content")
                    continue
                self.conn.execute(
                    "INSERT INTO hypothesis(hypothesis_id, snapshot_id, frozen, payload_json) VALUES (?, ?, ?, ?)",
                    (hypothesis.hypothesis_id, hypothesis.snapshot_id, int(hypothesis.frozen), _json(data)),
                )

    def list_hypotheses(self, snapshot_id: str, case_id: Optional[str] = None) -> List[Hypothesis]:
        if case_id:
            rows = self.conn.execute(
                "SELECT h.payload_json FROM hypothesis h JOIN evidence_snapshot s ON h.snapshot_id=s.snapshot_id "
                "WHERE h.snapshot_id=? AND s.case_id=? ORDER BY h.hypothesis_id",
                (snapshot_id, case_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM hypothesis WHERE snapshot_id=? ORDER BY hypothesis_id",
                (snapshot_id,),
            ).fetchall()
        return [model_validate(Hypothesis, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def snapshot_contains(self, snapshot_id: str, evidence_id: str, version: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM snapshot_items i JOIN evidence_snapshot s ON s.snapshot_id=i.snapshot_id "
            "WHERE i.snapshot_id=? AND i.case_id=s.case_id AND i.evidence_id=? AND i.version=?",
            (snapshot_id, evidence_id, version),
        ).fetchone()
        return row is not None

    def insert_viewpoint(self, viewpoint: Viewpoint) -> None:
        snapshot = self.get_snapshot(viewpoint.snapshot_id)
        if not snapshot or not snapshot.frozen:
            raise StorageError("Viewpoints require a frozen evidence snapshot")
        all_citations = viewpoint.supporting_evidence + viewpoint.counter_evidence
        if viewpoint.status in {ViewpointStatus.ADMITTED, ViewpointStatus.CONDITIONAL} and not viewpoint.supporting_evidence:
            raise StorageError(f"Admitted viewpoint {viewpoint.viewpoint_id} must cite supporting evidence")
        if viewpoint.status in {ViewpointStatus.ADMITTED, ViewpointStatus.CONDITIONAL}:
            required_lists = {
                "key_assumptions": viewpoint.key_assumptions,
                "mechanism_claims": viewpoint.mechanism_claims,
                "boundary_conditions": viewpoint.boundary_conditions,
                "falsifiers": viewpoint.falsifiers,
                "uncertainties": viewpoint.uncertainties,
            }
            missing = [name for name, values in required_lists.items() if not values]
            if missing:
                raise StorageError(f"Admitted viewpoint {viewpoint.viewpoint_id} has empty required fields: {missing}")
        for citation in all_citations:
            if not self.snapshot_contains(viewpoint.snapshot_id, citation.evidence_id, citation.version):
                raise StorageError(
                    f"Viewpoint {viewpoint.viewpoint_id} cites evidence outside snapshot: {citation.ref}"
                )
        data = model_dump(viewpoint)
        existing_row = self.conn.execute(
            "SELECT payload_json FROM viewpoint WHERE viewpoint_id=? AND version=?",
            (viewpoint.viewpoint_id, viewpoint.version),
        ).fetchone()
        if existing_row:
            if _canonical_payload(json.loads(existing_row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Viewpoint {viewpoint.viewpoint_id} v{viewpoint.version} already exists with different content")
            return
        latest_row = self.conn.execute(
            "SELECT MAX(version) AS max_version FROM viewpoint WHERE viewpoint_id=?",
            (viewpoint.viewpoint_id,),
        ).fetchone()
        latest = latest_row["max_version"]
        if viewpoint.version == 1:
            if viewpoint.parent_version is not None:
                raise StorageError("Initial viewpoint cannot have parent_version")
        else:
            if latest is None or viewpoint.version != latest + 1:
                raise StorageError("Viewpoint versions must append exactly one version")
            if viewpoint.parent_version != latest:
                raise StorageError("New viewpoint version must reference the previous version")
            if not viewpoint.change_reason.strip() or viewpoint.change_reason == "initial_version":
                raise StorageError("New viewpoint version requires change_reason")
        self.conn.execute(
            "INSERT INTO viewpoint(viewpoint_id, version, snapshot_id, status, payload_json) VALUES (?, ?, ?, ?, ?)",
            (viewpoint.viewpoint_id, viewpoint.version, viewpoint.snapshot_id, viewpoint.status.value, _json(data)),
        )
        self.conn.commit()

    def get_viewpoint(self, viewpoint_id: str, version: int, case_id: Optional[str] = None) -> Optional[Viewpoint]:
        if case_id:
            row = self.conn.execute(
                "SELECT v.payload_json FROM viewpoint v JOIN evidence_snapshot s ON v.snapshot_id=s.snapshot_id "
                "WHERE v.viewpoint_id=? AND v.version=? AND s.case_id=?",
                (viewpoint_id, version, case_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT payload_json FROM viewpoint WHERE viewpoint_id=? AND version=?",
                (viewpoint_id, version),
            ).fetchone()
        return model_validate(Viewpoint, _read_payload(row)) if row else None  # type: ignore[return-value]

    def latest_viewpoint(self, viewpoint_id: str, case_id: Optional[str] = None) -> Optional[Viewpoint]:
        if case_id:
            row = self.conn.execute(
                "SELECT v.payload_json FROM viewpoint v JOIN evidence_snapshot s ON v.snapshot_id=s.snapshot_id "
                "WHERE v.viewpoint_id=? AND s.case_id=? ORDER BY v.version DESC LIMIT 1",
                (viewpoint_id, case_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT payload_json FROM viewpoint WHERE viewpoint_id=? ORDER BY version DESC LIMIT 1",
                (viewpoint_id,),
            ).fetchone()
        return model_validate(Viewpoint, _read_payload(row)) if row else None  # type: ignore[return-value]

    def list_viewpoints(self, snapshot_id: Optional[str] = None, case_id: Optional[str] = None) -> List[Viewpoint]:
        conditions: List[str] = []
        params: List[Any] = []
        if snapshot_id:
            conditions.append("v.snapshot_id=?")
            params.append(snapshot_id)
        if case_id:
            conditions.append("s.case_id=?")
            params.append(case_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            "SELECT v.payload_json FROM viewpoint v JOIN evidence_snapshot s ON v.snapshot_id=s.snapshot_id" + where + " ORDER BY v.viewpoint_id, v.version",
            params,
        ).fetchall()
        return [model_validate(Viewpoint, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_dependency(self, dependency: EvidenceDependency) -> None:
        viewpoint = self.get_viewpoint(dependency.viewpoint_id, dependency.viewpoint_version)
        if not viewpoint:
            raise StorageError("Dependency must reference existing evidence and viewpoint versions")
        snapshot = self.get_snapshot(viewpoint.snapshot_id)
        evidence = self.get_evidence(dependency.evidence_id, dependency.evidence_version, snapshot.case_id if snapshot else None)
        if not snapshot or not evidence or evidence.case_id != snapshot.case_id:
            raise StorageError("Dependency evidence and viewpoint must belong to the same case")
        if not self.snapshot_contains(viewpoint.snapshot_id, dependency.evidence_id, dependency.evidence_version):
            raise StorageError("Dependency evidence must be present in the viewpoint snapshot")
        data = model_dump(dependency)
        row = self.conn.execute(
            "SELECT payload_json FROM evidence_dependency WHERE edge_id=?", (dependency.edge_id,)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Dependency {dependency.edge_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO evidence_dependency(edge_id, case_id, evidence_id, evidence_version, viewpoint_id, viewpoint_version, relation, importance, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dependency.edge_id,
                snapshot.case_id,
                dependency.evidence_id,
                dependency.evidence_version,
                dependency.viewpoint_id,
                dependency.viewpoint_version,
                dependency.relation.value,
                dependency.importance.value,
                _json(data),
            ),
        )
        self.conn.commit()

    def list_dependencies(
        self,
        *,
        viewpoint_id: Optional[str] = None,
        viewpoint_version: Optional[int] = None,
        case_id: Optional[str] = None,
    ) -> List[EvidenceDependency]:
        if viewpoint_id or viewpoint_version or case_id:
            conditions: List[str] = []
            params: List[Any] = []
            if viewpoint_id:
                conditions.append("d.viewpoint_id=?")
                params.append(viewpoint_id)
            if viewpoint_version:
                conditions.append("d.viewpoint_version=?")
                params.append(viewpoint_version)
            if case_id:
                conditions.append("s.case_id=?")
                params.append(case_id)
            rows = self.conn.execute(
                "SELECT d.payload_json FROM evidence_dependency d JOIN viewpoint v ON d.viewpoint_id=v.viewpoint_id AND d.viewpoint_version=v.version "
                "JOIN evidence_snapshot s ON v.snapshot_id=s.snapshot_id WHERE " + " AND ".join(conditions) + " ORDER BY d.edge_id",
                params,
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM evidence_dependency ORDER BY edge_id"
            ).fetchall()
        return [model_validate(EvidenceDependency, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_disagreement(self, disagreement: Disagreement) -> None:
        snapshot = self.get_snapshot(disagreement.snapshot_id)
        if not snapshot:
            raise StorageError("Disagreement must reference an existing snapshot")
        if len(disagreement.viewpoint_refs) < 2:
            raise StorageError("Disagreement must connect at least two viewpoint versions")
        for ref in disagreement.viewpoint_refs:
            if ":" not in ref:
                raise StorageError(f"Invalid viewpoint reference: {ref}")
            vp_id, version_text = ref.rsplit(":", 1)
            viewpoint = self.get_viewpoint(vp_id, int(version_text), snapshot.case_id)
            if not viewpoint or viewpoint.snapshot_id != disagreement.snapshot_id:
                raise StorageError(f"Disagreement references missing viewpoint: {ref}")
        data = model_dump(disagreement)
        row = self.conn.execute(
            "SELECT payload_json FROM disagreement WHERE disagreement_id=?",
            (disagreement.disagreement_id,),
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Disagreement {disagreement.disagreement_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO disagreement(disagreement_id, snapshot_id, decision_impact, status, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                disagreement.disagreement_id,
                disagreement.snapshot_id,
                disagreement.decision_impact.value,
                disagreement.status.value,
                _json(data),
            ),
        )
        self.conn.commit()

    def list_disagreements(self, snapshot_id: str, case_id: Optional[str] = None) -> List[Disagreement]:
        if case_id:
            rows = self.conn.execute(
                "SELECT d.payload_json FROM disagreement d JOIN evidence_snapshot s ON d.snapshot_id=s.snapshot_id "
                "WHERE d.snapshot_id=? AND s.case_id=? ORDER BY d.disagreement_id",
                (snapshot_id, case_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM disagreement WHERE snapshot_id=? ORDER BY disagreement_id",
                (snapshot_id,),
            ).fetchall()
        return [model_validate(Disagreement, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_need(self, need: DiscriminativeNeed) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM disagreement WHERE disagreement_id=?", (need.disagreement_id,)
        ).fetchone():
            raise StorageError(f"Need references missing disagreement: {need.disagreement_id}")
        data = model_dump(need)
        row = self.conn.execute(
            "SELECT payload_json FROM discriminative_need WHERE need_id=?", (need.need_id,)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Need {need.need_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO discriminative_need(need_id, disagreement_id, payload_json) VALUES (?, ?, ?)",
            (need.need_id, need.disagreement_id, _json(data)),
        )
        self.conn.commit()

    def list_needs(self, disagreement_id: Optional[str] = None, case_id: Optional[str] = None) -> List[DiscriminativeNeed]:
        conditions: List[str] = []
        params: List[Any] = []
        if disagreement_id:
            conditions.append("n.disagreement_id=?")
            params.append(disagreement_id)
        if case_id:
            conditions.append("s.case_id=?")
            params.append(case_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            "SELECT n.payload_json FROM discriminative_need n JOIN disagreement d ON n.disagreement_id=d.disagreement_id "
            "JOIN evidence_snapshot s ON d.snapshot_id=s.snapshot_id" + where + " ORDER BY n.need_id",
            params,
        ).fetchall()
        return [model_validate(DiscriminativeNeed, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_indicator(self, indicator: MonitorIndicator) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM discriminative_need WHERE need_id=?", (indicator.need_id,)
        ).fetchone():
            raise StorageError(f"Indicator references missing need: {indicator.need_id}")
        data = model_dump(indicator)
        row = self.conn.execute(
            "SELECT payload_json FROM monitor_indicator WHERE indicator_id=?", (indicator.indicator_id,)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Indicator {indicator.indicator_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO monitor_indicator(indicator_id, need_id, status, payload_json) VALUES (?, ?, ?, ?)",
            (indicator.indicator_id, indicator.need_id, indicator.status.value, _json(data)),
        )
        self.conn.commit()

    def list_indicators(self, *, approved_only: bool = False, case_id: Optional[str] = None) -> List[MonitorIndicator]:
        query = "SELECT i.payload_json FROM monitor_indicator i JOIN discriminative_need n ON i.need_id=n.need_id "
        query += "JOIN disagreement d ON n.disagreement_id=d.disagreement_id JOIN evidence_snapshot s ON d.snapshot_id=s.snapshot_id"
        conditions: List[str] = []
        params: List[Any] = []
        if approved_only:
            conditions.append("i.status=?")
            params.append(IndicatorStatus.APPROVED.value)
        if case_id:
            conditions.append("s.case_id=?")
            params.append(case_id)
        query += (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query += " ORDER BY i.indicator_id"
        rows = self.conn.execute(query, params).fetchall()
        return [model_validate(MonitorIndicator, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_trigger(self, trigger: TriggerRule) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM monitor_indicator WHERE indicator_id=?", (trigger.indicator_id,)
        ).fetchone():
            raise StorageError(f"Trigger references missing indicator: {trigger.indicator_id}")
        data = model_dump(trigger)
        row = self.conn.execute(
            "SELECT payload_json FROM trigger_rule WHERE trigger_id=?", (trigger.trigger_id,)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Trigger {trigger.trigger_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO trigger_rule(trigger_id, indicator_id, status, payload_json) VALUES (?, ?, ?, ?)",
            (trigger.trigger_id, trigger.indicator_id, trigger.status.value, _json(data)),
        )
        self.conn.commit()

    def list_triggers(self, *, approved_only: bool = False, case_id: Optional[str] = None) -> List[TriggerRule]:
        query = "SELECT t.payload_json FROM trigger_rule t JOIN monitor_indicator i ON t.indicator_id=i.indicator_id "
        query += "JOIN discriminative_need n ON i.need_id=n.need_id JOIN disagreement d ON n.disagreement_id=d.disagreement_id "
        query += "JOIN evidence_snapshot s ON d.snapshot_id=s.snapshot_id"
        conditions: List[str] = []
        params: List[Any] = []
        if approved_only:
            conditions.append("t.status=?")
            params.append(IndicatorStatus.APPROVED.value)
        if case_id:
            conditions.append("s.case_id=?")
            params.append(case_id)
        query += (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query += " ORDER BY t.trigger_id"
        rows = self.conn.execute(query, params).fetchall()
        return [model_validate(TriggerRule, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def insert_update_event(self, event: UpdateEvent) -> None:
        from_snapshot = self.get_snapshot(event.from_snapshot, event.case_id)
        to_snapshot = self.get_snapshot(event.to_snapshot, event.case_id)
        if not from_snapshot or not to_snapshot:
            raise StorageError("Update event snapshots must belong to the event case")
        data = model_dump(event)
        row = self.conn.execute(
            "SELECT payload_json FROM update_event WHERE update_id=?", (event.update_id,)
        ).fetchone()
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(data):
                raise StorageError(f"Update event {event.update_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO update_event(update_id, case_id, from_snapshot, to_snapshot, payload_json) VALUES (?, ?, ?, ?, ?)",
            (event.update_id, event.case_id, event.from_snapshot, event.to_snapshot, _json(data)),
        )
        self.conn.commit()

    def list_update_events(self, case_id: str) -> List[UpdateEvent]:
        rows = self.conn.execute(
            "SELECT payload_json FROM update_event WHERE case_id=? ORDER BY update_id", (case_id,)
        ).fetchall()
        return [model_validate(UpdateEvent, _read_payload(row)) for row in rows]  # type: ignore[list-item]

    def record_artifact(
        self, *, artifact_id: str, case_id: str, artifact_type: str, path: str, content_hash: str, payload: Dict[str, Any]
    ) -> None:
        row = self.conn.execute("SELECT payload_json FROM artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
        data = _json(payload)
        if row:
            if _canonical_payload(json.loads(row["payload_json"])) != _canonical_payload(payload):
                raise StorageError(f"Artifact {artifact_id} already exists with different content")
            return
        self.conn.execute(
            "INSERT INTO artifact(artifact_id, case_id, artifact_type, path, content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (artifact_id, case_id, artifact_type, path, content_hash, data),
        )
        self.conn.commit()
