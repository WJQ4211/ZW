"""Run the authorized P15R/P16R/G4QR-T1/PAPER-CASE-R stage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.p15r_case import run_p15r  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args = parser.parse_args()
    result = run_p15r(args.run_id)
    print(json.dumps({key: result[key] for key in ("run_id", "t1_snapshot_item_count", "real_model_base_revision_calls", "independent_audit_calls", "corrective_viewpoint_versions_appended", "p16r_disagreement_count", "p16r_need_count", "p16r_indicator_count", "p16r_trigger_count", "formal_g3_t1_executed", "formal_g4_t1_signed", "t1_snapshot_refrozen", "api_key_written", "database_history_unchanged_excluding_new")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
