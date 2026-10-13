"""Run the authorized G1-T1 through G3Q/G4Q-T1 preparation stage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.t1_merge import run_merge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject-failure", action="store_true")
    args = parser.parse_args()
    try:
        result = run_merge(inject_failure=args.inject_failure)
    except Exception as exc:
        print(json.dumps({"status": "failed", "exception_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    manifest = result["manifest"]
    print(json.dumps({
        "status": "completed",
        "run_id": result["run_id"],
        "formal_g1_t1_executed": manifest["formal_g1_t1_executed"],
        "formal_t1_evidence_count": manifest["formal_t1_evidence_count"],
        "t1_snapshot_frozen": manifest["t1_snapshot_frozen"],
        "t1_snapshot_logical_item_count": manifest["t1_snapshot_logical_item_count"],
        "impact_localization_executed": manifest["impact_localization_executed"],
        "viewpoint_local_update_executed": manifest["viewpoint_local_update_executed"],
        "formal_g3_t1_executed": manifest["formal_g3_t1_executed"],
        "formal_g4_t1_signed": manifest["formal_g4_t1_signed"],
        "model_api_calls": manifest["model_api_calls"],
        "t1_read": manifest["t1_read"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
