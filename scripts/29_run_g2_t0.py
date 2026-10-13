"""Run the authorized formal G2 governance step."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.g2_t0 import run_g2_t0  # noqa: E402


def main() -> int:
    result = run_g2_t0()
    print(json.dumps({
        "run_id": result.get("run_id"), "snapshot_id": result.get("snapshot_id"), "replayed": result.get("replayed", False), "shared_baseline_count": result.get("shared_baseline_count"), "formal_hypothesis_count": result.get("formal_hypothesis_count"), "hypothesis_inserted": result.get("hypothesis_inserted"), "model_api_calls": result.get("model_api_calls"), "viewpoint_inserted": result.get("viewpoint_inserted"), "t1_read": result.get("t1_read"), "p7_executed": result.get("p7_executed"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
