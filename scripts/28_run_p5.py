"""Execute P5 against the frozen T0 snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model_client import ModelAPIError  # noqa: E402
from src.p5 import ROOT as P5_ROOT, run_p5  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_dir = P5_ROOT / "outputs" / args.run_id
    try:
        result = run_p5(args.run_id, output_dir=run_dir)
    except ModelAPIError:
        failure = run_dir / "p5_failure_manifest.json"
        if failure.exists():
            print(failure.read_text(encoding="utf-8"))
        return 1
    print(json.dumps({
        "run_id": result["run_id"], "snapshot_id": result["snapshot_id"], "p5_executed": result["p5_executed"], "generation_api_calls": result["generation_api_calls"], "merge_api_calls": result["merge_api_calls"], "logical_model_call_count": result["logical_model_call_count"], "total_candidate_count": result["total_candidate_count"], "proposed_hypothesis_count": result["proposed_hypothesis_count"], "hypothesis_inserted": result["hypothesis_inserted"], "g2_executed": result["g2_executed"], "t1_read": result["t1_read"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
