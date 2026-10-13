"""Deterministically repair the self-excluded output hash list for a committed run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)).replace("\\", "/"): sha256(path)
        for path in sorted(path for path in directory.rglob("*") if path.is_file())
        if path.name != "g1_t1_p13_p16_manifest.json"
    }


def main() -> int:
    dirs = sorted((ROOT / "outputs").glob("G1-T1-P13-P16-*-formal-v1"))
    if not dirs:
        raise SystemExit("merge output not found")
    base = dirs[-1]
    manifest_path = base / "g1_t1_p13_p16_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_hash_repair"] = "self_excluded_output_file_hashes"
    manifest["manifest_repair_api_calls"] = 0
    manifest["output_file_hashes"] = tree_hashes(base)
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"run_id": base.name, "model_api_calls": 0, "formal_database_rows_changed": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
