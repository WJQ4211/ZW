from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.p16r_c1 import run_p16r_c1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline P16R-C1/G4QR-C1/PAPER-CASE-C1")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    manifest = run_p16r_c1(args.run_id)
    print({
        "run_id": manifest["run_id"],
        "model_api_calls": manifest["model_api_calls"],
        "database_writes": manifest["database_writes"],
        "p16r_c1_disagreement_count": manifest["p16r_c1_disagreement_count"],
        "paper_case_chinese_char_count": manifest["paper_case_chinese_char_count"],
        "invalid_reference_count": manifest["invalid_reference_count"],
        "semantic_mismatch_count": manifest["semantic_mismatch_count"],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
