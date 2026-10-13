"""Repair only deterministic P11 manifest bindings after a completed run.

No source content, API response, candidate, or P12 result is changed.  The
repair fills the case binding that was omitted from two generated CSV rows and
refreshes their recorded hashes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "us_military_ai_deployment"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result[str(path.relative_to(root)).replace("\\", "/")] = sha256_bytes(path.read_bytes())
    return result


def repair_csv(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0]) if rows else ["case_id"]
    changed = 0
    for row in rows:
        if not row.get("case_id"):
            row["case_id"] = CASE_ID
            changed += 1
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: 36_repair_t1_p11_artifacts.py RUN_DIR_NAME")
    base = ROOT / "outputs" / sys.argv[1]
    p11 = base / "P11_T1_COLLECTION"
    changed = 0
    for name in ("source_manifest_t1_candidate.csv", "selected_source_inventory_t1.csv"):
        changed += repair_csv(p11 / name)
    p11_manifest_path = p11 / "p11_t1_manifest.json"
    p11_manifest = json.loads(p11_manifest_path.read_text(encoding="utf-8"))
    p11_manifest["case_id_binding_repair"] = "deterministic_csv_field_injection"
    p11_manifest["case_id_binding_repaired_rows"] = changed
    p11_manifest["files"] = file_hashes(p11)
    p11_manifest_path.write_text(json.dumps(p11_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit = {
        "run_id": base.name,
        "repair_type": "p11_manifest_case_binding_only",
        "case_id": CASE_ID,
        "changed_rows": changed,
        "source_content_changed": False,
        "p12_outputs_changed": False,
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "t1_snapshot_frozen": False,
        "t1_read": True,
    }
    (base / "t1_output_integrity_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    root_manifest_path = base / "p11_p12_manifest.json"
    root_manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    root_files = file_hashes(base)
    root_files.pop("p11_p12_manifest.json", None)
    root_manifest["p11_case_id_binding_repaired"] = True
    root_manifest["p11_case_id_binding_repaired_rows"] = changed
    root_manifest["files"] = root_files
    root_manifest_path.write_text(json.dumps(root_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": base.name, "changed_rows": changed, "model_api_calls": 0, "evidence_inserted": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

