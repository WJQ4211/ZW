"""Run the offline P3b-R2 audit-gate correction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3br2 import build_run, new_run_dir


if __name__ == "__main__":
    output_dir = new_run_dir()
    manifest = build_run(output_dir)
    print(f"run_id={manifest['run_id']}")
    print(f"author_decision_row_count={manifest['author_decision_row_count']}")
    print(f"protected_prior_outputs_unchanged={manifest['protected_prior_outputs_unchanged']}")
