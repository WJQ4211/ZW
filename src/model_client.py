"""Model transports used by the P3a-R workflow.

The FileBridge client remains available for a human-operated fallback, but it
is intentionally separate from the API client and is never counted as a
provider call. The API client reads all connection settings from the
environment and never serializes the API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import EvidenceCandidate, model_validate


RAW_CANDIDATE_FIELDS = frozenset(
    {
        "source_id",
        "source_locator",
        "excerpt_original",
        "excerpt_zh",
        "normalized_claim",
        "coding_dimensions",
        "evidence_nature",
        "topics",
    }
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_json_content(value: str) -> Any:
    """Parse JSON, tolerating only a surrounding markdown code fence."""
    cleaned = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return json.loads(cleaned)


def chat_completion_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("API response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("API response choices[0].message.content is not a string")
    return content


def parse_chat_completion_content(payload: dict[str, Any]) -> tuple[str, Any]:
    content = chat_completion_content(payload)
    return content, _strict_json_content(content)


@dataclass(frozen=True)
class FileBridgeCall:
    transport: str
    prompt_path: str
    response_path: str
    prompt_sha256: str
    response_sha256: str
    candidate_count: int
    provider_call_count: None = None
    token_count: None = None
    temperature: None = None
    cost: None = None
    provider_metrics_status: str = "not_observable"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FileBridgeModelClient:
    """Read a strict JSON response supplied by a human through a file."""

    transport = "codex_agent_file_bridge"

    def read_response(self, prompt_path: Path, response_path: Path) -> tuple[list[EvidenceCandidate], FileBridgeCall]:
        prompt_bytes = prompt_path.read_bytes()
        response_bytes = response_path.read_bytes()
        try:
            payload = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"FileBridge response is not UTF-8 strict JSON: {response_path}") from exc
        if not isinstance(payload, list):
            raise ValueError("FileBridge response must be a JSON array")
        candidates: list[EvidenceCandidate] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict) or set(item) != RAW_CANDIDATE_FIELDS:
                raise ValueError(f"FileBridge candidate {index} has forbidden or missing fields")
            candidates.append(model_validate(EvidenceCandidate, item))
        call = FileBridgeCall(
            transport=self.transport,
            prompt_path=str(prompt_path),
            response_path=str(response_path),
            prompt_sha256=_sha256_bytes(prompt_bytes),
            response_sha256=_sha256_bytes(response_bytes),
            candidate_count=len(candidates),
        )
        return candidates, call


class ModelAPIError(RuntimeError):
    """Safe API failure metadata without carrying provider response text."""

    def __init__(
        self,
        message: str,
        *,
        exception_type: str,
        http_status_code: int | None,
        attempts: int,
        retry_count: int,
    ) -> None:
        super().__init__(message)
        self.root_exception_type = exception_type
        self.http_status_code = http_status_code
        self.attempts = attempts
        self.retry_count = retry_count


@dataclass(frozen=True)
class ApiCallRecord:
    transport: str
    request_started_at_utc: str
    endpoint: str
    requested_model_id: str
    provider_model_id: str | None
    request_parameters: dict[str, Any]
    attempts: int
    retry_count: int
    latency_ms: int
    response_sha256: str
    provider_token_usage: dict[str, int | None]
    provider_metrics_status: str
    provider_temperature: float | None
    provider_cost: float | None
    response_status_code: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class APIModelClient:
    """Minimal OpenAI-compatible chat-completions client for P3a-R."""

    transport = "openai_compatible_api"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 180.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = (base_url or os.environ.get("MODEL_API_BASE_URL", "")).strip()
        self.api_key = api_key if api_key is not None else os.environ.get("MODEL_API_KEY", "")
        self.model = (model or os.environ.get("MODEL_ID", "")).strip()
        self.enable_thinking = os.environ.get("MODEL_ENABLE_THINKING", "false").strip().lower() in {"1", "true", "yes"}
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        missing = [
            name
            for name, value in (
                ("MODEL_API_BASE_URL", self.base_url),
                ("MODEL_API_KEY", self.api_key),
                ("MODEL_ID", self.model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Missing required model API environment variables: " + ", ".join(missing))

    @property
    def endpoint(self) -> str:
        value = self.base_url.rstrip("/")
        if value.endswith("/chat/completions"):
            return value
        return value + "/chat/completions"

    def call(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], str, ApiCallRecord]:
        request_parameters: dict[str, Any] = {"temperature": temperature, "enable_thinking": self.enable_thinking}
        if max_tokens is not None:
            request_parameters["max_tokens"] = max_tokens
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            **request_parameters,
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_started = _now_utc()
        started = time.perf_counter()
        last_error: Exception | None = None
        attempts = 0
        response_bytes = b""
        status_code = 0
        payload: dict[str, Any] | None = None
        for retry_index in range(self.max_retries + 1):
            attempts += 1
            request = urllib.request.Request(
                self.endpoint,
                data=encoded,
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    status_code = int(response.status)
                    response_bytes = response.read()
                payload_value = json.loads(response_bytes.decode("utf-8"))
                if not isinstance(payload_value, dict):
                    raise ValueError("API response envelope is not a JSON object")
                payload = payload_value
                break
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_error = exc
                if retry_index < self.max_retries:
                    time.sleep((2.0, 5.0, 10.0)[min(retry_index, 2)])
        if payload is None:
            # Do not include exception text: provider errors can echo request data.
            root_type = type(last_error).__name__ if last_error is not None else "UnknownError"
            if isinstance(last_error, urllib.error.URLError) and getattr(last_error, "reason", None) is not None:
                root_type = type(last_error.reason).__name__
            raise ModelAPIError(
                "Model API call failed after retries",
                exception_type=root_type,
                http_status_code=status_code or None,
                attempts=attempts,
                retry_count=attempts - 1,
            ) from last_error

        content = chat_completion_content(payload)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        token_usage = {
            "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            "completion_tokens": usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
            "total_tokens": usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else None,
        }
        metrics_status = "observed" if any(value is not None for value in token_usage.values()) else "not_observable"
        record = ApiCallRecord(
            transport=self.transport,
            request_started_at_utc=request_started,
            endpoint=self.endpoint,
            requested_model_id=self.model,
            provider_model_id=payload.get("model") if isinstance(payload.get("model"), str) else None,
            request_parameters=request_parameters,
            attempts=attempts,
            retry_count=attempts - 1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            response_sha256=_sha256_bytes(response_bytes),
            provider_token_usage=token_usage,
            provider_metrics_status=metrics_status,
            provider_temperature=None,
            provider_cost=None,
            response_status_code=status_code,
        )
        return payload, content, record
