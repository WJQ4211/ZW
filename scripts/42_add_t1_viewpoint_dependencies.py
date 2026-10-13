"""Append the P15 evidence dependencies for the already committed T1 views."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "research.sqlite3"
RUN_GLOB = "G1-T1-P13-P16-*-formal-v1"
CASE_ID = "us_military_ai_deployment"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    dirs = sorted((ROOT / "outputs").glob(RUN_GLOB))
    if not dirs:
        raise SystemExit("committed merge directory not found")
    base = dirs[-1]
    manifest_path = base / "g1_t1_p13_p16_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = manifest["run_id"]
    edges = load_jsonl(base / "P14_IMPACT_LOCALIZATION" / "p14_impact_edges.jsonl")
    updated = {row["viewpoint_id"]: row for row in load_jsonl(base / "P15_VIEWPOINT_UPDATE" / "updated_viewpoints_latest.jsonl")}
    conn = sqlite3.connect(DB)
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT COUNT(*) FROM evidence_dependency WHERE edge_id LIKE ?", (f"DEP-T1-{run_id}-%",)).fetchone()[0]
        if prior:
            conn.rollback()
            print(json.dumps({"run_id": run_id, "model_api_calls": 0, "dependency_rows_added": 0, "replayed": True}, ensure_ascii=False))
            return 0
        added = 0
        for edge in edges:
            view = updated.get(edge["viewpoint_id"])
            if not view:
                continue
            relation = {"support": "support", "qualify": "limit", "boundary_change": "limit", "contradict": "challenge"}[edge["relation"]]
            importance = edge["confidence"] if edge["confidence"] in {"high", "medium", "low"} else "medium"
            edge_id = f"DEP-T1-{run_id}-{sha256(json.dumps(edge, ensure_ascii=False, sort_keys=True).encode('utf-8'))[:24]}"
            payload = {"edge_id": edge_id, "run_id": run_id, "case_id": CASE_ID, "evidence_id": edge["evidence_id"], "evidence_version": 1, "viewpoint_id": edge["viewpoint_id"], "viewpoint_version": view["version"], "target_field": edge["target_field"], "relation": relation, "importance": importance, "rationale": edge["rationale"], "origin": "P14_field_level_impact_edge", "human_verified": False}
            conn.execute("INSERT INTO evidence_dependency(edge_id, case_id, evidence_id, evidence_version, viewpoint_id, viewpoint_version, relation, importance, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (edge_id, CASE_ID, edge["evidence_id"], 1, edge["viewpoint_id"], view["version"], relation, importance, json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            added += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    audit = {"run_id": run_id, "repair_type": "append_p15_t1_evidence_dependencies", "dependency_rows_added": added, "model_api_calls": 0, "evidence_rows_changed": 0, "viewpoint_rows_changed": 0, "formal_g3_t1_executed": False, "formal_g4_t1_signed": False, "t1_read": True}
    (base / "p15_dependency_append_audit.json").write_bytes((json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependency_edge_count"] = added
    manifest["dependency_append_audit"] = "p15_dependency_append_audit.json"
    manifest["output_file_hashes"] = {
        str(path.relative_to(base)).replace("\\", "/"): sha256(path.read_bytes())
        for path in sorted(path for path in base.rglob("*") if path.is_file())
        if path.name != manifest_path.name
    }
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"run_id": run_id, "model_api_calls": 0, "dependency_rows_added": added, "replayed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
