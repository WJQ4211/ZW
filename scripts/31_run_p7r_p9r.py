from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.p7r_p9r import run_p7r_p9r


def main() -> None:
    parser = argparse.ArgumentParser(description="Run append-only P7R-P9R case strengthening recovery")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    manifest = run_p7r_p9r(run_id=args.run_id, output_dir=ROOT / "outputs" / args.run_id, resume=args.resume)
    print("run_id=" + str(manifest.get("run_id")))
    print("p9r_gate_open=" + str(manifest.get("p9r_gate_open")))
    print("p9r_executed=" + str(manifest.get("p9r_executed")))
    print("formal_evidence_inserted=" + str(manifest.get("formal_evidence_inserted", 0)))
    print("author_decision_count=" + str(manifest.get("author_decision_count", 0)))


if __name__ == "__main__":
    main()
