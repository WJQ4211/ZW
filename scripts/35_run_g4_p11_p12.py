from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.t1_workflow import run_workflow


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign G4-T0 then run blind P11/P12 and prepare G1-T1")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run_workflow(args.run_id)
    print(json.dumps({
        "run_id": args.run_id,
        "g4_signed": result["root_manifest"]["g4_signed"],
        "t1_unsealed": result["root_manifest"]["t1_unsealed"],
        "p11_candidate_source_count": result["root_manifest"]["p11_candidate_source_count"],
        "p11_full_text_source_count": result["root_manifest"]["p11_full_text_source_count"],
        "p12_model_call_count": result["root_manifest"]["p12_model_call_count"],
        "g1_t1_preannotation_count": result["root_manifest"]["g1_t1_preannotation_count"],
        "evidence_inserted": result["root_manifest"]["evidence_inserted"],
        "formal_g1_t1_executed": result["root_manifest"]["formal_g1_t1_executed"],
        "t1_read": result["root_manifest"]["t1_read"],
        "api_key_written": result["root_manifest"]["api_key_written"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

