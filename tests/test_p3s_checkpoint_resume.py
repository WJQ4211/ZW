"""Offline integration tests for P3S failed-checkpoint recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import src.p3s as p3s
from src.model_client import ModelAPIError


class _FakeRecord:
    def __init__(self) -> None:
        self.value = {
            "transport": "synthetic_fixture",
            "request_started_at_utc": "2026-01-01T00:00:00Z",
            "endpoint": "https://example.invalid/chat/completions",
            "requested_model_id": "synthetic-model",
            "provider_model_id": "synthetic-model",
            "request_parameters": {"temperature": 0.0},
            "attempts": 1,
            "retry_count": 0,
            "latency_ms": 1,
            "response_sha256": "fixture-response-sha",
            "provider_token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
            "provider_metrics_status": "not_observable",
            "provider_temperature": None,
            "provider_cost": None,
            "response_status_code": 200,
        }

    def as_dict(self):
        return dict(self.value)


class _FakeClient:
    model = "synthetic-model"
    transport = "synthetic_fixture"

    def __init__(self, fail_batch: str | None = None) -> None:
        self.fail_batch = fail_batch
        self.calls: list[str] = []

    def call(self, prompt: str, *, temperature: float, max_tokens: int):
        batch_id = next(line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("BATCH_ID: "))
        self.calls.append(batch_id)
        if batch_id == self.fail_batch:
            raise ModelAPIError(
                "fixture failure",
                exception_type="SSLEOFError",
                http_status_code=None,
                attempts=4,
                retry_count=3,
            )
        source_id = next(line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("SOURCE_ID: ")) if "SOURCE_ID: " in prompt else batch_id.split("-semantic-", 1)[0]
        if "semantic" in batch_id:
            content = json.dumps(
                [
                    {
                        "candidate_index": 0,
                        "status": "accept",
                        "reason": "直接支持",
                        "claim_supported": True,
                        "nature_supported": True,
                        "dimension_reviews": [
                            {
                                "dimension": "application_maturity",
                                "supported": True,
                                "supporting_span": "Synthetic fact for " + source_id + ".",
                                "reason": "占位摘录直接支持该维度",
                            }
                        ],
                        "revised_fields": {},
                    }
                ],
                ensure_ascii=False,
            )
        else:
            text = "Synthetic fact for " + source_id + "."
            content = json.dumps(
                [
                    {
                        "source_locator": "web_paragraph_0001",
                        "excerpt_original": text,
                        "excerpt_zh": "合成事实。",
                        "normalized_claim": "合成事实已经发生。",
                        "evidence_nature": "test_event",
                        "coding_dimensions": ["application_maturity"],
                        "topics": ["synthetic"],
                        "possible_corroboration_group": "",
                    }
                ],
                ensure_ascii=False,
            )
        payload = {"model": self.model, "choices": [{"message": {"content": content}}]}
        return payload, content, _FakeRecord()


def _setup_synthetic_p3s(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    original_root = p3s.ROOT
    extract_template = p3s.EXTRACTION_TEMPLATE
    semantic_template = p3s.SEMANTIC_TEMPLATE
    monkeypatch.setattr(p3s, "ROOT", tmp_path)
    monkeypatch.setattr(p3s, "EXTRACTION_TEMPLATE", extract_template)
    monkeypatch.setattr(p3s, "SEMANTIC_TEMPLATE", semantic_template)
    monkeypatch.setenv("MODEL_API_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("MODEL_API_KEY", "fixture-only-not-a-secret")
    monkeypatch.setenv("MODEL_ID", "synthetic-model")

    canonical = tmp_path / "canonical.jsonl"
    canonical.write_text("".join(json.dumps({"evidence_id": f"EV-{i:02d}", "excerpt_original": ""}) + "\n" for i in range(60)), encoding="utf-8")
    canonical_sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
    monkeypatch.setattr(p3s, "CANONICAL_PATH", canonical)
    monkeypatch.setattr(p3s, "CANONICAL_SHA256", canonical_sha)
    p2s_manifest = tmp_path / "p2s_manifest.json"
    p2s_manifest.write_text(json.dumps({"input_canonical_sha256": canonical_sha}), encoding="utf-8")
    monkeypatch.setattr(p3s, "P2S_MANIFEST", p2s_manifest)
    monkeypatch.setattr(p3s, "SOURCE_IDS", ("SYN-A", "SYN-B", "SYN-C"))

    sources = {}
    for source_id in ("SYN-A", "SYN-B", "SYN-C"):
        text_path = tmp_path / f"{source_id}.txt"
        text = f"Synthetic fact for {source_id}."
        text_path.write_text(text, encoding="utf-8")
        sources[source_id] = {
            "source_id": source_id,
            "text_path": text_path.name,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "title": source_id,
            "publisher": "Synthetic",
            "source_grade": "A",
            "published_at": "2024-01-01",
            "canonical_url": f"https://example.invalid/{source_id}",
        }
    monkeypatch.setattr(p3s, "_load_p2s_sources", lambda: sources)
    return tmp_path / "run", original_root


def test_run_p3s_recovers_successful_batches_from_failed_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    run_dir, _ = _setup_synthetic_p3s(monkeypatch, tmp_path)
    run_id = "P3S-SYNTHETIC-RESUME"
    first_client = _FakeClient(fail_batch="SYN-B-extract-001")
    with pytest.raises(ModelAPIError):
        p3s.run_p3s(run_id, run_dir, client_factory=lambda: first_client)

    checkpoint_path = run_dir / "p3s_checkpoint.json"
    failure_path = run_dir / "p3s_failure_manifest.json"
    checkpoint_before = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    failure_before = failure_path.read_bytes()
    assert checkpoint_before["status"] == "failed"
    assert checkpoint_before["completed_batches"] == ["SYN-A-extract-001", "SYN-A-semantic-001"]
    assert "SYN-B-extract-001" not in checkpoint_before["completed_batches"]
    assert json.loads(failure_path.read_text(encoding="utf-8"))["exception_type"] == "SSLEOFError"

    record = checkpoint_before["call_records"]["SYN-A-extract-001"]
    altered = dict(checkpoint_before)
    altered["prompt_sha256_by_batch"] = dict(checkpoint_before["prompt_sha256_by_batch"])
    altered["prompt_sha256_by_batch"]["SYN-A-extract-001"] = "changed-prompt-sha"
    assert not p3s.checkpoint_reuse_allowed(
        altered,
        checkpoint_before["checkpoint_identity"],
        "SYN-A-extract-001",
        "changed-prompt-sha",
        run_dir=run_dir,
        phase="extraction",
    )

    raw_path = run_dir / "raw_model_outputs" / "extraction" / "SYN-A-extract-001.json"
    raw_before = raw_path.read_bytes()
    raw_value = json.loads(raw_before)
    raw_value["choices"][0]["message"]["content"] = "tampered"
    raw_path.write_text(json.dumps(raw_value), encoding="utf-8")
    assert not p3s.checkpoint_reuse_allowed(
        checkpoint_before,
        checkpoint_before["checkpoint_identity"],
        "SYN-A-extract-001",
        record["prompt_sha256"],
        run_dir=run_dir,
        phase="extraction",
    )
    raw_path.write_bytes(raw_before)

    resumed_client = _FakeClient()
    manifest = p3s.run_p3s(run_id, run_dir, resume=True, client_factory=lambda: resumed_client)
    assert resumed_client.calls == [
        "SYN-B-extract-001",
        "SYN-B-semantic-001",
        "SYN-C-extract-001",
        "SYN-C-semantic-001",
    ]
    assert manifest["resumed_from_failed_checkpoint"] is True
    assert manifest["reused_checkpoint_batch_count"] == 2
    assert manifest["newly_executed_logical_call_count"] == 4
    assert manifest["logical_model_call_count"] == 4
    assert manifest["extraction_logical_call_count"] == 2
    assert manifest["semantic_logical_call_count"] == 2
    assert manifest["provider_call_count"] == 4
    assert manifest["prior_failure_manifest_path"] == "run/p3s_failure_manifest.json"
    assert manifest["prior_failure_stage"] == "extraction"
    assert manifest["prior_failure_source_id"] == "SYN-B"
    assert manifest["checkpoint_status"] == "completed"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert failure_path.read_bytes() == failure_before
    assert manifest["final_candidate_count"] == 3
