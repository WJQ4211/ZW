"""Run the continuous P7-P9 and G3 preparation workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.model_client import ModelAPIError  # noqa: E402
from src.p7_p9 import run_p7_p9  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    output_dir = ROOT / "outputs" / args.run_id
    try:
        manifest = run_p7_p9(run_id=args.run_id, output_dir=output_dir, resume=args.resume)
    except ModelAPIError as exc:
        print(json.dumps({
            "run_id": args.run_id,
            "failure": "model_api_error",
            "exception_type": exc.root_exception_type,
            "http_status_code": exc.http_status_code,
        }, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({
            "run_id": args.run_id,
            "failure": "pipeline_error",
            "exception_type": type(exc).__name__,
        }, ensure_ascii=False))
        return 1
    print(json.dumps({
        "run_id": manifest.get("run_id"),
        "snapshot_id": manifest.get("snapshot_id"),
        "initial_viewpoint_count": manifest.get("initial_viewpoint_count"),
        "final_viewpoint_count": manifest.get("final_viewpoint_count"),
        "dependency_edge_count": manifest.get("dependency_edge_count"),
        "candidate_disagreement_count": manifest.get("candidate_disagreement_count"),
        "discriminative_need_count": manifest.get("discriminative_need_count"),
        "logical_model_call_count": manifest.get("logical_model_call_count"),
        "provider_call_count": manifest.get("provider_call_count"),
        "formal_evidence_inserted": manifest.get("formal_evidence_inserted"),
        "formal_g3_executed": manifest.get("formal_g3_executed"),
        "t1_read": manifest.get("t1_read"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

