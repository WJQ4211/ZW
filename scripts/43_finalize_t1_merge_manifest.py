"""Finalize append-only audit metadata after the dependency repair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    runs = sorted((ROOT / "outputs").glob("G1-T1-P13-P16-*-formal-v1"))
    if not runs:
        raise SystemExit("G1-T1 merge output not found")
    base = runs[-1]
    manifest_path = base / "g1_t1_p13_p16_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_files = {
        "src/t1_merge.py": ROOT / "src" / "t1_merge.py",
        "scripts/40_run_t1_merge.py": ROOT / "scripts" / "40_run_t1_merge.py",
        "scripts/41_repair_t1_merge_manifest.py": ROOT / "scripts" / "41_repair_t1_merge_manifest.py",
        "scripts/42_add_t1_viewpoint_dependencies.py": ROOT / "scripts" / "42_add_t1_viewpoint_dependencies.py",
        "scripts/43_finalize_t1_merge_manifest.py": ROOT / "scripts" / "43_finalize_t1_merge_manifest.py",
        "tests/test_t1_merge.py": ROOT / "tests" / "test_t1_merge.py",
    }
    manifest["code_hashes_after_repairs"] = {name: sha256_file(path) for name, path in source_files.items()}
    manifest["database_append_only_repairs"] = [
        "p15_dependency_append",
        "manifest_self_excluded_hash_list",
    ]
    manifest["output_file_hashes"] = {
        str(path.relative_to(base)).replace("\\", "/"): sha256_file(path)
        for path in sorted(path for path in base.rglob("*") if path.is_file())
        if path != manifest_path
    }
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"run_id": manifest["run_id"], "code_hashes_after_repairs": len(source_files), "database_rows_changed": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
