"""Run the offline P3S-v5 semantic representation recovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.p3sr import recover


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = recover(args.run_id, ROOT / "outputs" / args.run_id)
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "new_model_api_calls": result["new_model_api_calls"],
                "deterministic_input_candidates": result["deterministic_input_candidates"],
                "semantic_accept_count": result["semantic_accept_count"],
                "semantic_reject_count": result["semantic_reject_count"],
                "recovered_g1_candidate_count": result["recovered_g1_candidate_count"],
                "evidence_inserted": result["evidence_inserted"],
                "t1_read": result["t1_read"],
            },
            ensure_ascii=False,
        )
    )

