"""Run the bounded P3b-R candidate recovery and G1 preannotation preparation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3ar import ROOT  # noqa: E402
from src.p3br import run_p3br  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    manifest = run_p3br(args.run_id, (ROOT / args.run_dir).resolve())
    print({
        "run_id": manifest["run_id"],
        "p3b_original_candidate_count": manifest["p3b_original_candidate_count"],
        "schema_recovered_final_count": manifest["schema_recovered_final_count"],
        "visual_rescue_queue_count": manifest["visual_rescue_queue_count"],
        "api_logical_call_count": manifest["api_logical_call_count"],
        "formal_evidence_db_rows_added": 0,
        "g1_executed": False,
        "p4_executed": False,
        "t1_read": False,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
