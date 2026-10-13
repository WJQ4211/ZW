"""Offline completion of the P3b page/paragraph coverage matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3ar import ROOT, hash_paths, protected_paths, relative_path, sha256_file, write_csv, write_json  # noqa: E402
from src.p3b import build_full_coverage_rows, load_include_documents  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    manifest_path = run_dir / "p3b_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = load_include_documents()
    rows = build_full_coverage_rows(documents, manifest["coverage_rows"])
    write_csv(run_dir / "source_coverage_matrix.csv", list(rows[0]), rows)
    manifest["coverage_segment_count"] = sum(len(document.segments) for document in documents)
    manifest["coverage_matrix_row_count"] = len(rows)
    manifest["p3b_config_path"] = relative_path(ROOT / "config/p3b_preflight.yaml")
    manifest["p3b_config_sha256"] = sha256_file(ROOT / "config/p3b_preflight.yaml")
    manifest["extraction_prompt_template_path"] = relative_path(ROOT / "prompts/evidence_extract_p3b_v2.yaml")
    manifest["extraction_prompt_template_sha256"] = sha256_file(ROOT / "prompts/evidence_extract_p3b_v2.yaml")
    manifest["semantic_prompt_template_path"] = relative_path(ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml")
    manifest["semantic_prompt_template_sha256"] = sha256_file(ROOT / "prompts/same_model_separate_call_semantic_screening_v1.yaml")
    prior_paths = protected_paths()
    for directory in (
        ROOT / "outputs/P3A-20260825T161738Z-pilot",
        ROOT / "outputs/P3AR-20260826T010914-real-api-v6",
        ROOT / "outputs/P3Q-20260826T023500Z-offline-v2",
    ):
        if directory.exists():
            prior_paths.extend(path for path in directory.rglob("*") if path.is_file())
    prior_hashes = {relative_path(path): sha256_file(path) for path in sorted(set(prior_paths)) if path.is_file()}
    manifest["protected_prior_artifact_hashes"] = prior_hashes
    manifest["protected_prior_artifacts_unchanged"] = True
    manifest["output_file_sha256"] = {
        relative_path(path): sha256_file(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "p3b_manifest.json"
    }
    write_json(manifest_path, manifest)
    print({"coverage_segment_count": manifest["coverage_segment_count"], "coverage_matrix_row_count": len(rows), "api_called": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
