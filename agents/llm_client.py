"""OpenAI-compatible local LLM client for BQuant live chat.

The client intentionally keeps the dependency surface small by using Python's
standard HTTP library. BQuant can point it at Ollama, vLLM, llama.cpp server, or
any local service that implements `/v1/models` and `/v1/chat/completions`.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from utils.logger import BQuantLogger


REPO_ROOT = Path(__file__).resolve().parent.parent
LLM_RUNTIME_CONFIG_PATH = REPO_ROOT / "configs" / "llm_runtime.yaml"
LOGGER = BQuantLogger("llm_client", component="agent", subcomponent="llm_client", default_channel="pipeline")


@dataclass(frozen=True)
class LLMResponse:
    """Normalized response returned by a local chat-completion backend.

    Attributes:
        text: Assistant answer after provider-specific cleanup.
        model: Model name used by the backend.
        backend: Runtime backend label from `configs/llm_runtime.yaml`.
        latency_seconds: Wall-clock request duration.
        raw: Parsed provider response for debugging and audit trails.
    """

    text: str
    model: str
    backend: str
    latency_seconds: float
    raw: dict[str, Any]


def load_llm_config(path: Path = LLM_RUNTIME_CONFIG_PATH) -> dict[str, Any]:
    """Load the LLM runtime YAML configuration.

    Args:
        path: Absolute path to the runtime YAML file.

    Returns:
        Parsed configuration mapping. Missing or empty files return `{}`.
    """
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def runtime_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the normalized `runtime` block from the LLM config.

    Args:
        config: Optional already-loaded YAML mapping.

    Returns:
        Runtime mapping with defaults for endpoint, model, timeout, and sampling.
    """
    loaded = config if config is not None else load_llm_config()
    runtime = dict(loaded.get("runtime", {}) or {})
    runtime.setdefault("enabled", False)
    runtime.setdefault("backend", "openai_compatible")
    runtime.setdefault("endpoint", "http://127.0.0.1:11434/v1")
    runtime.setdefault("api_key_env", "BQUANT_LLM_API_KEY")
    runtime.setdefault("default_model", "qwen3:8b")
    runtime.setdefault("max_new_tokens", 768)
    runtime.setdefault("temperature", 0.2)
    runtime.setdefault("timeout_seconds", 180)
    runtime.setdefault("fallback_to_deterministic", True)
    runtime.setdefault("disable_thinking", True)
    return runtime


def _join_url(endpoint: str, suffix: str) -> str:
    """Join an OpenAI-compatible endpoint root and API suffix."""
    return f"{endpoint.rstrip('/')}/{suffix.lstrip('/')}"


def _request_json(
    *,
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Send one JSON HTTP request and parse the JSON response.

    Args:
        url: Absolute HTTP URL.
        method: HTTP method, usually `GET` or `POST`.
        payload: Optional JSON request body.
        headers: Extra HTTP headers.
        timeout_seconds: Socket timeout for the request.

    Returns:
        Parsed JSON object.

    Raises:
        RuntimeError: If the server returns a non-JSON response or HTTP error.
    """
    body = None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to reach {url}: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}: {type(parsed).__name__}")
    return parsed


def _auth_headers(runtime: dict[str, Any]) -> dict[str, str]:
    """Build optional bearer-auth headers from the configured environment var."""
    api_key_env = str(runtime.get("api_key_env") or "")
    api_key = os.getenv(api_key_env) if api_key_env else None
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def check_llm_available(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe the configured OpenAI-compatible model endpoint.

    Args:
        config: Optional already-loaded YAML mapping.

    Returns:
        Availability dictionary with model count and error details when present.
    """
    runtime = runtime_config(config)
    if not bool(runtime.get("enabled")):
        return {
            "enabled": False,
            "available": False,
            "backend": runtime.get("backend"),
            "endpoint": runtime.get("endpoint"),
            "default_model": runtime.get("default_model"),
            "error": "runtime_disabled",
        }
    started = time.perf_counter()
    try:
        response = _request_json(
            url=_join_url(str(runtime["endpoint"]), "models"),
            method="GET",
            headers=_auth_headers(runtime),
            timeout_seconds=min(float(runtime.get("timeout_seconds") or 180), 15.0),
        )
        models = response.get("data") or []
        model_ids = [str(row.get("id")) for row in models if isinstance(row, dict) and row.get("id")]
        return {
            "enabled": True,
            "available": True,
            "backend": runtime.get("backend"),
            "endpoint": runtime.get("endpoint"),
            "default_model": runtime.get("default_model"),
            "model_count": len(model_ids),
            "models": model_ids,
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        LOGGER.log_error(
            "check_llm_available",
            type(exc).__name__,
            str(exc),
            context={"endpoint": runtime.get("endpoint"), "default_model": runtime.get("default_model")},
            channel="pipeline",
        )
        return {
            "enabled": True,
            "available": False,
            "backend": runtime.get("backend"),
            "endpoint": runtime.get("endpoint"),
            "default_model": runtime.get("default_model"),
            "error": str(exc),
            "latency_seconds": round(time.perf_counter() - started, 3),
        }


def _strip_thinking(text: str) -> str:
    """Remove model-visible thinking tags that some local models emit.

    Args:
        text: Raw assistant content.

    Returns:
        Cleaned answer text suitable for UI display.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"^\s*/?think\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> LLMResponse:
    """Call the configured local chat-completion backend.

    Args:
        messages: OpenAI-style message list with `role` and `content`.
        model: Optional model override. Defaults to `runtime.default_model`.
        config: Optional already-loaded YAML mapping.
        max_tokens: Optional generation-token override.
        temperature: Optional sampling-temperature override.

    Returns:
        Normalized LLM response.

    Raises:
        RuntimeError: If the runtime is disabled, unavailable, or returns empty
            assistant content.
    """
    runtime = runtime_config(config)
    if not bool(runtime.get("enabled")):
        raise RuntimeError("LLM runtime is disabled in configs/llm_runtime.yaml")

    resolved_model = model or str(runtime["default_model"])
    payload = {
        "model": resolved_model,
        "messages": messages,
        "temperature": float(runtime.get("temperature") if temperature is None else temperature),
        "max_tokens": int(runtime.get("max_new_tokens") if max_tokens is None else max_tokens),
    }
    if bool(runtime.get("disable_thinking")):
        payload["think"] = False

    started = time.perf_counter()
    response = _request_json(
        url=_join_url(str(runtime["endpoint"]), "chat/completions"),
        method="POST",
        payload=payload,
        headers=_auth_headers(runtime),
        timeout_seconds=float(runtime.get("timeout_seconds") or 180),
    )
    latency_seconds = time.perf_counter() - started
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected chat response shape: {response}") from exc

    content = str(message.get("content") or "")
    text = _strip_thinking(content)
    if not text:
        raise RuntimeError(
            "LLM returned empty assistant content. Increase max_new_tokens or switch to a non-reasoning model."
        )

    LOGGER.info(
        "Local LLM chat completion finished",
        event_type="llm_chat_completion",
        status="success",
        backend=runtime.get("backend"),
        endpoint=runtime.get("endpoint"),
        model=resolved_model,
        latency_seconds=round(latency_seconds, 3),
        output_chars=len(text),
    )
    return LLMResponse(
        text=text,
        model=resolved_model,
        backend=str(runtime.get("backend")),
        latency_seconds=latency_seconds,
        raw=response,
    )
