"""Deterministically fill the P12R extraction run metadata.

The model responses and source material are untouched.  This only repairs a
program-injected provenance field that was empty in the first write.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256(path.read_bytes())
        for path in sorted(p for p in root.rglob("*") if p.is_file())
    }


def repair_jsonl(path: Path, run_id: str) -> int:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = 0
    for row in rows:
        if (row.get("recovery_origin") == "p12r_supplemental_real_api" or row.get("source_id") in {"T1-DARPA-001", "T1-DARPA-003", "T1-IR-001"}) and row.get("extraction_run_id") != run_id:
            row["extraction_run_id"] = run_id
            changed += 1
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: 38_repair_t1_recovery_metadata.py RUN_DIR_NAME")
    base = ROOT / "outputs" / sys.argv[1]
    changed = 0
    for path in (
        base / "P12R_T1_EXTRACTION" / "supplemental_evidence_candidates_validated.jsonl",
        base / "supplemental_evidence_candidates.jsonl",
    ):
        changed += repair_jsonl(path, base.name)
    audit = {
        "run_id": base.name,
        "repair_type": "program_injected_extraction_run_id",
        "changed_rows": changed,
        "model_api_calls": 0,
        "raw_model_responses_changed": False,
        "source_archives_changed": False,
        "formal_g1_t1_executed": False,
        "evidence_inserted": 0,
        "t1_snapshot_frozen": False,
        "t1_read": True,
        "api_key_written": False,
    }
    (base / "p12r_metadata_correction_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = base / "p11r_p12r_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["p12r_metadata_correction"] = "program_injected_extraction_run_id"
    manifest["p12r_metadata_correction_rows"] = changed
    manifest["code_hashes"]["src/t1_recovery.py"] = sha256((ROOT / "src" / "t1_recovery.py").read_bytes())
    output_hashes = hashes(base)
    output_hashes.pop("p11r_p12r_manifest.json", None)
    manifest["output_file_hashes"] = output_hashes
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": base.name, "changed_rows": changed, "model_api_calls": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
