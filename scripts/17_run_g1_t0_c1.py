"""Execute the append-only G1-T0-C1 correction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.g1_t0_c1 import new_run_dir, run_g1_t0_c1


if __name__ == "__main__":
    result = run_g1_t0_c1(output_dir=new_run_dir())
    print(f"run_id={result['run_id']}")
    print(f"target_latest_version={result['target_latest_version']}")
    print(f"replayed={result.get('replayed', False)}")
