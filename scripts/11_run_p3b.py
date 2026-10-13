"""Execute the full local-P2 T0 P3b candidate extraction run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3ar import ROOT  # noqa: E402
from src.p3b import run_p3b  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    manifest = run_p3b(args.run_id, run_dir)
    print({
        "run_id": args.run_id,
        "source_count": manifest["source_count"],
        "extraction_batch_count": manifest["extraction_batch_count"],
        "semantic_screen_batch_count": manifest["semantic_screen_batch_count"],
        "provider_call_count": manifest["provider_call_count"],
        "raw_candidate_count": manifest["raw_candidate_count"],
        "final_candidate_count": manifest["final_candidate_count"],
        "formal_evidence_db_rows_added": 0,
        "t1_read": False,
        "p4_executed": False,
        "g1_executed": False,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
