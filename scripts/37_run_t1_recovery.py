from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.t1_recovery import run_recovery


def main() -> int:
    parser = argparse.ArgumentParser(description="Run incremental T1 source recovery, extraction, and G1 preparation")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run_recovery(args.run_id)
    manifest = result["manifest"]
    print(json.dumps({
        "run_id": args.run_id,
        "supplemental_source_count": manifest["supplemental_source_count"],
        "supplemental_full_text_source_count": manifest["supplemental_full_text_source_count"],
        "p12r_model_call_count": manifest["p12r_model_call_count"],
        "supplemental_final_candidate_count": manifest["supplemental_final_candidate_count"],
        "combined_g1_candidate_count": manifest["combined_g1_candidate_count"],
        "formal_g1_t1_executed": manifest["formal_g1_t1_executed"],
        "evidence_inserted": manifest["evidence_inserted"],
        "api_key_written": manifest["api_key_written"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

