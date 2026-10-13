from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.formal_g3_g4_t1 import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline formal G3-T1/G4-T1 signoff and final paper package")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    print(run(args.run_id))


if __name__ == "__main__":
    main()
