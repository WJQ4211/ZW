"""Execute the authorized formal G1-T0 transaction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.g1_t0 import new_run_dir, run_g1_t0


if __name__ == "__main__":
    result = run_g1_t0(output_dir=new_run_dir())
    print(f"run_id={result['run_id']}")
    print(f"formal_evidence_db_rows_added={result.get('formal_evidence_db_rows_added', result.get('transaction_result', {}).get('evidence_rows_added', 0))}")
    print(f"replayed={result.get('replayed', False)}")
