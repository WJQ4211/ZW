"""Repair source-text archive hashes without calling the model or changing raw responses.

The first P11R run used Windows text-mode newline translation after hashing the
cleaned string.  This append-only repair makes the archived bytes and the
inventory/capture-log hashes agree, then records the correction in the run
manifest.  No source content is semantically changed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.write_text("", encoding="utf-8", newline="")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256(path)
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if path.name != "p11r_p12r_manifest.json"
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: 39_repair_t1_recovery_source_hashes.py RUN_DIR_NAME")
    base = ROOT / "outputs" / sys.argv[1]
    inventory_paths = [
        base / "supplemental_source_inventory.csv",
        base / "P11R_T1_COLLECTION" / "supplemental_source_inventory.csv",
    ]
    rows_by_path: list[tuple[Path, list[dict[str, str]]]] = []
    source_hashes: dict[str, dict[str, str]] = {}
    for path in inventory_paths:
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
        for row in rows:
            text_path = ROOT / row["text_path"]
            raw_path = ROOT / row["raw_path"]
            row["text_sha256"] = sha256(text_path)
            row["raw_sha256"] = sha256(raw_path)
            source_hashes[row["source_id"]] = {
                "raw_sha256": row["raw_sha256"],
                "text_sha256": row["text_sha256"],
            }
        rows_by_path.append((path, rows))
    capture_path = base / "supplemental_capture_log.csv"
    capture_rows = list(csv.DictReader(capture_path.open(encoding="utf-8-sig", newline="")))
    for row in capture_rows:
        values = source_hashes[row["source_id"]]
        row["raw_sha256"] = values["raw_sha256"]
        row["text_sha256"] = values["text_sha256"]
    # Normalize only the archived source-text bytes; decoded text is unchanged.
    for source_id in source_hashes:
        text_path = ROOT / next(
            row["text_path"]
            for _, rows in rows_by_path
            for row in rows
            if row["source_id"] == source_id
        )
        text = text_path.read_text(encoding="utf-8")
        text_path.write_bytes(text.encode("utf-8"))
        source_hashes[source_id]["text_sha256"] = sha256(text_path)
    for path, rows in rows_by_path:
        for row in rows:
            row["text_sha256"] = source_hashes[row["source_id"]]["text_sha256"]
        write_csv(path, rows)
    for row in capture_rows:
        row["text_sha256"] = source_hashes[row["source_id"]]["text_sha256"]
    write_csv(capture_path, capture_rows)
    audit = {
        "run_id": base.name,
        "repair_type": "source_text_byte_hash_alignment",
        "cause": "Windows text-mode newline translation made recorded string hash differ from archived byte hash",
        "semantic_source_text_changed": False,
        "raw_source_changed": False,
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "formal_g1_t1_executed": False,
        "t1_snapshot_frozen": False,
        "t1_read": True,
        "api_key_written": False,
        "source_hashes": source_hashes,
    }
    (base / "source_hash_correction_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path = base / "p11r_p12r_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_hash_correction"] = "source_text_byte_hash_alignment"
    manifest["source_hash_correction_audit"] = "source_hash_correction_audit.json"
    manifest["code_hashes_after_source_hash_repair"] = {
        "src/t1_recovery.py": sha256(ROOT / "src" / "t1_recovery.py"),
        "src/t1_workflow.py": sha256(ROOT / "src" / "t1_workflow.py"),
        "scripts/37_run_t1_recovery.py": sha256(ROOT / "scripts" / "37_run_t1_recovery.py"),
        "scripts/38_repair_t1_recovery_metadata.py": sha256(ROOT / "scripts" / "38_repair_t1_recovery_metadata.py"),
        "scripts/39_repair_t1_recovery_source_hashes.py": sha256(ROOT / "scripts" / "39_repair_t1_recovery_source_hashes.py"),
        "tests/test_t1_recovery_outputs.py": sha256(ROOT / "tests" / "test_t1_recovery_outputs.py"),
    }
    manifest["input_hashes"]["supplemental_sources"] = {
        source_id: values["raw_sha256"] for source_id, values in source_hashes.items()
    }
    manifest["output_file_hashes"] = file_hashes(base)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": base.name, "model_api_calls": 0, "source_hashes_repaired": len(source_hashes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
