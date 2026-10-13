"""P3a-R prepare step: build source-grounded prompt packets only."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.p3ar import (  # noqa: E402
    P1B_DIR,
    P1C_DIR,
    P2_INVENTORY,
    P2_MANIFEST,
    P2_SOURCE,
    PROMPT_TEMPLATE,
    ROOT,
    SOURCE_IDS,
    SP_PDF,
    TD_TEXT,
    build_extraction_prompt,
    extract_pdf_segments,
    extract_web_segments,
    hash_paths,
    load_source_metadata,
    load_template,
    relative_path,
    sha256_file,
    write_json,
)


def current_git_head() -> dict[str, str]:
    candidates = [ROOT / ".github_upload_staging", ROOT]
    for candidate in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        value = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return {"commit": value, "short_commit": value[:12], "git_root": str(candidate)}
    raise RuntimeError("A real Git HEAD is required for P3a-R; refusing unverified_local_copy.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"Refusing to reuse non-empty P3a-R run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    p2_manifest = __import__("json").loads(P2_MANIFEST.read_text(encoding="utf-8"))
    if p2_manifest.get("p3_gate_open") is not True:
        raise RuntimeError("P2 gate is not open")
    inventory = {row["source_id"]: row for row in __import__("csv").DictReader(P2_INVENTORY.open(encoding="utf-8-sig", newline=""))}
    if any(source_id not in inventory or inventory[source_id].get("review_decision") != "include" for source_id in SOURCE_IDS):
        raise RuntimeError("P2 inventory does not contain the requested included sources")

    source_metadata = load_source_metadata()
    template = load_template()
    template_hash = sha256_file(PROMPT_TEMPLATE)
    sp_segments, sp_info = extract_pdf_segments(SP_PDF, "T0-SP-007")
    td_segments, td_info = extract_web_segments(TD_TEXT, "T0-TD-008")
    batches = [
        ("T0-SP-007-batch-01", sp_segments[:24]),
        ("T0-SP-007-batch-02", sp_segments[24:]),
        ("T0-TD-008-batch-01", td_segments),
    ]
    if any(not segments for _, segments in batches):
        raise RuntimeError("One or more P3a-R preparation batches are empty")

    packet_dir = run_dir / "prompt_packets" / "extraction"
    packet_records = []
    for batch_id, segments in batches:
        source_id = segments[0].source_id
        prompt = build_extraction_prompt(segments, template, batch_id)
        prompt_path = packet_dir / f"{batch_id}.txt"
        packet_path = packet_dir / f"{batch_id}.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        packet = {
            "batch_id": batch_id,
            "source_id": source_id,
            "prompt_path": relative_path(prompt_path),
            "prompt_sha256": sha256_file(prompt_path),
            "template_path": relative_path(PROMPT_TEMPLATE),
            "template_sha256": template_hash,
            "segments": [segment.as_dict() for segment in segments],
            "model_input_restriction": "raw_source_segments_research_question_field_definitions_only",
            "raw_model_outputs_created": False,
        }
        write_json(packet_path, packet)
        packet_records.append(packet)

    git_head = current_git_head()
    prepare_manifest = {
        "run_id": args.run_id,
        "case_id": "us_military_ai_deployment",
        "stage": "P3a-R-prepare",
        "prepared_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "git_head": git_head,
        "p2_run_id": p2_manifest.get("run_id"),
        "source_ids": list(SOURCE_IDS),
        "input_files": {
            "p2_manifest": {"path": relative_path(P2_MANIFEST), "sha256": sha256_file(P2_MANIFEST)},
            "p2_source_manifest": {"path": relative_path(P2_SOURCE), "sha256": sha256_file(P2_SOURCE)},
            "p2_selected_inventory": {"path": relative_path(P2_INVENTORY), "sha256": sha256_file(P2_INVENTORY)},
            "T0-SP-007": {"path": relative_path(SP_PDF), "sha256": sha256_file(SP_PDF), **sp_info},
            "T0-TD-008": {"path": relative_path(TD_TEXT), "sha256": sha256_file(TD_TEXT), **td_info},
        },
        "source_metadata_for_injection": source_metadata,
        "prompt_template": {"path": relative_path(PROMPT_TEMPLATE), "sha256": template_hash, "prompt_id": template["prompt_id"], "version": template["version"]},
        "batches": [{"batch_id": packet["batch_id"], "source_id": packet["source_id"], "segment_count": len(packet["segments"]), "prompt_sha256": packet["prompt_sha256"]} for packet in packet_records],
        "raw_model_outputs_created": False,
        "model_client_called": False,
        "evidence_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "stop_condition": "Prepare only; stop before model execution and validation.",
    }
    write_json(run_dir / "prepare_manifest.json", prepare_manifest)
    print(__import__("json").dumps({"run_id": args.run_id, "batch_count": len(batches), "raw_model_outputs_created": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
