from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.g3_g4_offline import run_g3_g4_offline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline formal G3 and G4-T0 draft package")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    manifest = run_g3_g4_offline(args.run_id)
    print("run_id=" + str(manifest["run_id"]))
    print("model_api_calls=0")
    print("formal_g4_t0_executed=false")


if __name__ == "__main__":
    main()
