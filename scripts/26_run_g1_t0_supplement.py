"""Run the authorized, append-only G1-T0 supplementary sign-off."""

from __future__ import annotations

import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.g1_t0_supplement import run_g1_t0_supplement  # noqa: E402


def main() -> int:
    result = run_g1_t0_supplement()
    print(json.dumps({
        "run_id": result.get("run_id"),
        "replayed": result.get("replayed", False),
        "evidence_inserted": result.get("formal_evidence_db_rows_added", result.get("evidence_rows_added", 0)),
        "decision_counts": result.get("decision_counts"),
        "model_api_call_count": result.get("model_api_call_count", 0),
        "t1_read": result.get("t1_read", False),
        "p4_executed": result.get("p4_executed", False),
        "snapshot_frozen": result.get("snapshot_frozen", False),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
