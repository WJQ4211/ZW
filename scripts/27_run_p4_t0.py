"""Run the formal T0 Evidence snapshot freeze."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.p4_t0 import run_p4_t0  # noqa: E402


def main() -> int:
    result = run_p4_t0()
    print(json.dumps({
        "run_id": result.get("run_id"),
        "snapshot_id": result.get("snapshot_id"),
        "replayed": result.get("replayed", False),
        "snapshot_item_count": result.get("snapshot_item_count"),
        "manifest_hash": result.get("manifest_hash"),
        "frozen": result.get("frozen"),
        "p4_executed": result.get("p4_executed"),
        "model_api_calls": result.get("model_api_calls"),
        "t1_read": result.get("t1_read"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
