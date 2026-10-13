"""Run the supplied synthetic Phase A fixture without any model call."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import run_synthetic


def main() -> int:
    parser = argparse.ArgumentParser(description="Run synthetic Phase A end-to-end")
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args()
    print(run_synthetic(args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
