"""Sign G4-T0 and stop immediately after the T1-unseal gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.g4_formal import ROOT, sign_g4_t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / args.run_id
    result = sign_g4_t0(run_dir)
    print(json.dumps({
        "run_id": result["run_id"],
        "g4_signed": result["g4_signed"],
        "t1_unsealed": result["t1_unsealed"],
        "model_api_calls": result["model_api_calls"],
        "evidence_inserted": result["evidence_inserted"],
        "t1_read": result["t1_read"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
