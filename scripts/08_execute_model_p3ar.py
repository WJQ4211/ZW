"""P3a-R execute-model step: call the configured API and save raw responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.model_client import APIModelClient, RAW_CANDIDATE_FIELDS, chat_completion_content  # noqa: E402
from src.p3ar import ROOT, build_semantic_review_prompt, load_template, sha256_file, write_json  # noqa: E402


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_json_content(value: str) -> Any:
    cleaned = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return json.loads(cleaned)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    prepare_manifest_path = run_dir / "prepare_manifest.json"
    if not prepare_manifest_path.exists():
        raise RuntimeError("prepare_manifest.json is required; run prepare first")
    if (run_dir / "raw_model_outputs").exists():
        raise RuntimeError("raw_model_outputs already exists; refusing to mix or overwrite model runs")

    prepare_manifest = json.loads(prepare_manifest_path.read_text(encoding="utf-8"))
    template = load_template()
    client = APIModelClient()
    extraction_raw_dir = run_dir / "raw_model_outputs"
    extraction_meta_dir = run_dir / "api_call_metadata"
    semantic_prompt_dir = run_dir / "prompt_packets" / "semantic_review"
    semantic_raw_dir = run_dir / "raw_semantic_reviews"
    calls: list[dict[str, Any]] = []
    extraction_payloads: dict[str, list[dict[str, Any]]] = {}

    packet_paths = sorted((run_dir / "prompt_packets" / "extraction").glob("*.json"))
    if not packet_paths:
        raise RuntimeError("No extraction prompt packets found")
    for packet_path in packet_paths:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        prompt_path = run_dir / "prompt_packets" / "extraction" / (packet_path.stem + ".txt")
        prompt = prompt_path.read_text(encoding="utf-8")
        payload, content, record = client.call(prompt, temperature=0.0, max_tokens=args.max_tokens)
        response_path = extraction_raw_dir / f"{packet['batch_id']}.json"
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        call_record = record.as_dict()
        call_record.update(
            {
                "phase": "extraction",
                "batch_id": packet["batch_id"],
                "prompt_path": str(prompt_path.relative_to(ROOT)).replace("\\", "/"),
                "prompt_sha256": sha256_file(prompt_path),
                "raw_response_path": str(response_path.relative_to(ROOT)).replace("\\", "/"),
                "raw_response_sha256": sha256_file(response_path),
                "response_content_sha256": sha256_bytes(content.encode("utf-8")),
            }
        )
        write_json(extraction_meta_dir / f"{packet['batch_id']}.json", call_record)
        calls.append(call_record)
        try:
            decoded = parse_json_content(content)
        except (json.JSONDecodeError, ValueError):
            decoded = []
        extraction_payloads[packet["batch_id"]] = decoded if isinstance(decoded, list) else []

    for batch_id, raw_candidates in extraction_payloads.items():
        review_candidates = []
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                continue
            review_candidates.append({key: candidate.get(key) for key in RAW_CANDIDATE_FIELDS} | {"candidate_index": index})
        packet = next(item for item in (json.loads(path.read_text(encoding="utf-8")) for path in packet_paths) if item["batch_id"] == batch_id)
        semantic_prompt = build_semantic_review_prompt(review_candidates, template, batch_id)
        semantic_prompt_path = semantic_prompt_dir / f"{batch_id}.txt"
        semantic_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        semantic_prompt_path.write_text(semantic_prompt, encoding="utf-8")
        if not review_candidates:
            write_json(semantic_raw_dir / f"{batch_id}.json", {"choices": [], "note": "No parseable extraction candidates were available for semantic review."})
            continue
        payload, content, record = client.call(semantic_prompt, temperature=0.0, max_tokens=args.max_tokens)
        response_path = semantic_raw_dir / f"{batch_id}.json"
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        call_record = record.as_dict()
        call_record.update(
            {
                "phase": "claim_evidence_semantic_review",
                "batch_id": batch_id,
                "prompt_path": str(semantic_prompt_path.relative_to(ROOT)).replace("\\", "/"),
                "prompt_sha256": sha256_file(semantic_prompt_path),
                "raw_response_path": str(response_path.relative_to(ROOT)).replace("\\", "/"),
                "raw_response_sha256": sha256_file(response_path),
                "response_content_sha256": sha256_bytes(content.encode("utf-8")),
            }
        )
        write_json(extraction_meta_dir / f"{batch_id}-semantic.json", call_record)
        calls.append(call_record)

    execute_manifest = {
        "run_id": args.run_id,
        "case_id": prepare_manifest.get("case_id"),
        "stage": "P3a-R-execute-model",
        "executed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_ids": prepare_manifest.get("source_ids"),
        "transport": client.transport,
        "requested_model_id": client.model,
        "api_endpoint": client.endpoint,
        "provider_call_count": sum(int(call["attempts"]) for call in calls),
        "provider_call_count_status": "observed_network_attempts",
        "provider_temperature": None,
        "provider_cost": None,
        "logical_extraction_batch_count": len(packet_paths),
        "logical_semantic_review_batch_count": len([call for call in calls if call["phase"] == "claim_evidence_semantic_review"]),
        "api_call_records": calls,
        "api_key_written": False,
        "file_bridge_calls_mixed": False,
        "raw_model_outputs_created": True,
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": False,
        "p4_executed": False,
    }
    write_json(run_dir / "execute_manifest.json", execute_manifest)
    print(json.dumps({"run_id": args.run_id, "provider_call_count": execute_manifest["provider_call_count"], "transport": client.transport}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
