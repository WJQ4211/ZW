"""Prepare and, when configured, execute the P3S supplementary extraction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.model_client import ModelAPIError
from src.p3s import ROOT, run_p3s


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / args.run_id
    try:
        result = run_p3s(args.run_id, run_dir, resume=args.resume)
    except ModelAPIError:
        failure_path = run_dir / "p3s_failure_manifest.json"
        if failure_path.exists():
            print(failure_path.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"run_id": args.run_id, "failure": "model_api_error"}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({key: result.get(key) for key in ("run_id", "prepare_completed", "p3s_executed", "stop_reason", "logical_model_call_count", "final_candidate_count", "evidence_inserted", "t1_read")}, ensure_ascii=False, indent=2))
