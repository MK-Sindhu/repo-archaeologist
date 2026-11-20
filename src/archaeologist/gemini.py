"""Gemini backend implementing contracts.LLM over the Gemini REST API (httpx, no extra SDK).

- Structured calls send the Pydantic model's JSON schema as
  `generationConfig.responseJsonSchema` (with `responseMimeType: application/json`) and
  validate the reply locally, mirroring ClaudeLLM: stop reasons are checked first, so a
  truncated or blocked reply raises LLMError / LLMRefusal with its tokens counted.
- `effort` maps to `thinkingConfig.thinkingLevel` (low / medium / high) on Gemini 3+ models.
  If a model rejects thinkingLevel, it is dropped for that model and the call is resent.
- Free-tier friendly: 429 / 5xx / network errors are retried, waiting the server's
  RetryInfo delay when given; a per-day quota error fails fast with a clear message;
  in-flight calls are capped (ARCH_GEMINI_CONCURRENCY, default 2) and calls can be paced
  (ARCH_GEMINI_RPM requests per minute, default off).

Key: GEMINI_API_KEY, GOOGLE_API_KEY or GOOGLE_GEMINI_KEY. Models: ARCH_MODEL /
ARCH_GRADER_MODEL, defaulting to GEMINI_MODEL / GEMINI_GRADER_MODEL.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from .config import GEMINI_GRADER_MODEL, GEMINI_MODEL, LLMConfig, gemini_api_key
from .llm import LLMAuthError, LLMError, LLMRefusal

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
KEY_HINT = (
    "Gemini needs an API key: put GEMINI_API_KEY=... in the project's .env "
    "(free key: https://aistudio.google.com/apikey)."
)
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
REFUSAL_FINISH = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE"}
MAX_WAIT_S = 65.0


def gemini_config() -> LLMConfig:
    """LLMConfig for the Gemini backend: ARCH_MODEL / ARCH_GRADER_MODEL, else the Gemini
    defaults (grading on the lighter model, which has its own free-tier quota)."""
    model = os.getenv("ARCH_MODEL") or GEMINI_MODEL
    grader = os.getenv("ARCH_GRADER_MODEL") or os.getenv("ARCH_MODEL") or GEMINI_GRADER_MODEL
    return LLMConfig(model=model, grader_model=grader, use_fallbacks=False)


@functools.lru_cache(maxsize=64)
def response_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `generationConfig.responseJsonSchema` ($defs/$ref are supported)."""
    return schema.model_json_schema()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


class GeminiLLM:
    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 6,
        concurrency: int | None = None,
        rpm: int | None = None,
        timeout_s: float = 180.0,
    ):
        self.config = config or gemini_config()
        self._api_key = api_key if api_key is not None else gemini_api_key()
        self._http = httpx.Client(base_url=BASE_URL, transport=transport, timeout=timeout_s)
        self._sleep = sleep
        self.max_retries = max_retries
        self._slots = threading.Semaphore(
            max(1, concurrency or _env_int("ARCH_GEMINI_CONCURRENCY", 2))
        )
        rpm = rpm if rpm is not None else _env_int("ARCH_GEMINI_RPM", 0)
        self._interval = 60.0 / rpm if rpm > 0 else 0.0
        self._pace_lock = threading.Lock()
        self._next_at = 0.0
        self._lock = threading.Lock()
        self._usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._no_thinking: set[str] = set()

    # -- public API (contracts.LLM) -------------------------------------------------
    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        effort: str | None = None,
        max_tokens: int | None = None,
        role: str = "default",
    ) -> T:
        gen = {"responseMimeType": "application/json", "responseJsonSchema": response_schema(schema)}
        text, finish = self._generate(system, prompt, effort, max_tokens, role, gen)
        try:
            return schema.model_validate_json(text)
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            raise LLMError(
                f"reply does not match {schema.__name__} (finishReason={finish}): {exc}"
            ) from exc

    def text(
        self,
        *,
        system: str,
        prompt: str,
        effort: str | None = None,
        max_tokens: int | None = None,
        role: str = "default",
    ) -> str:
        return self._generate(system, prompt, effort, max_tokens, role, {})[0]

    def usage_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._usage)

    def close(self) -> None:
        self._http.close()

    # -- internals ------------------------------------------------------------------
    def _model(self, role: str) -> str:
        return self.config.grader_model if role == "grade" else self.config.model

    def _generate(self, system, prompt, effort, max_tokens, role, gen_extra) -> tuple[str, str]:
        if not self._api_key:
            raise LLMAuthError(KEY_HINT)
        model = self._model(role)
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens or self.config.max_tokens, **gen_extra}
        if _supports_thinking_level(model) and model not in self._no_thinking:
            gen["thinkingConfig"] = {"thinkingLevel": _thinking_level(effort)}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        data = self._post(model, body)
        return self._read(data)

    def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"/models/{model}:generateContent"
        attempt = 0
        while True:
            self._pace()
            try:
                with self._slots:
                    resp = self._http.post(url, json=body, headers={"x-goog-api-key": self._api_key})
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise LLMError(f"Gemini request failed: {exc}") from exc
                self._wait(attempt, None, f"network error ({exc.__class__.__name__})")
                attempt += 1
                continue
            if resp.status_code == 200:
                return resp.json()
            err = _error(resp)
            message = err.get("message") or resp.text[:300]
            if resp.status_code == 400 and "thinking" in message.lower() and (
                "thinkingConfig" in body["generationConfig"]
            ):
                log.info("%s rejected thinkingLevel; resending without it", model)
                self._no_thinking.add(model)
                body = {**body, "generationConfig": {
                    k: v for k, v in body["generationConfig"].items() if k != "thinkingConfig"
                }}
                continue
            if _is_auth_failure(resp.status_code, err):
                raise LLMAuthError(f"Gemini rejected the API key ({message}). {KEY_HINT}")
            if resp.status_code == 429:
                daily = _daily_quota(err)
                if daily is not None:
                    raise LLMError(
                        f"Gemini free-tier daily quota for {model} is used up (limit {daily}/day). "
                        "It resets daily; meanwhile set ARCH_MODEL / ARCH_GRADER_MODEL to another "
                        "model, or enable billing."
                    )
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._wait(attempt, _retry_delay(err), f"HTTP {resp.status_code} from {model}")
                attempt += 1
                continue
            raise LLMError(f"Gemini API error {resp.status_code} from {model}: {message}")

    def _read(self, data: dict[str, Any]) -> tuple[str, str]:
        u = data.get("usageMetadata") or {}
        with self._lock:
            self._usage["calls"] += 1
            self._usage["input_tokens"] += u.get("promptTokenCount", 0) or 0
            self._usage["output_tokens"] += (u.get("candidatesTokenCount", 0) or 0) + (
                u.get("thoughtsTokenCount", 0) or 0
            )
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMRefusal(f"Gemini blocked the prompt: {block}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates")
        cand = candidates[0]
        finish = cand.get("finishReason") or ""
        if finish in REFUSAL_FINISH:
            raise LLMRefusal(f"Gemini declined: {finish}")
        if finish == "MAX_TOKENS":
            raise LLMError("hit max_tokens (Gemini MAX_TOKENS); raise LLMConfig.max_tokens")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise LLMError(f"Gemini returned no text (finishReason={finish or 'none'})")
        return text, finish

    def _wait(self, attempt: int, server_delay: float | None, why: str) -> None:
        delay = server_delay if server_delay is not None else min(2.0 * 2**attempt, 60.0)
        delay = min(delay + random.uniform(0, 1), MAX_WAIT_S)
        log.warning("Gemini: %s; retrying in %.0fs (attempt %d/%d)",
                    why, delay, attempt + 1, self.max_retries)
        self._sleep(delay)

    def _pace(self) -> None:
        if not self._interval:
            return
        with self._pace_lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            self._sleep(wait)


# -- helpers --------------------------------------------------------------------------


def _supports_thinking_level(model: str) -> bool:
    """thinkingLevel is a Gemini 3+ setting; 2.x models use a token budget instead."""
    return model.startswith("gemini-") and not model.startswith(("gemini-1", "gemini-2"))


def _thinking_level(effort: str | None) -> str:
    return {"low": "low", "high": "high", "max": "high", "xhigh": "high"}.get(effort or "", "medium")


def _error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _details(err: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [d for d in err.get("details") or [] if str(d.get("@type", "")).endswith(kind)]


def _is_auth_failure(status: int, err: dict[str, Any]) -> bool:
    if status in (401, 403) and err.get("status") in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return True
    reasons = {d.get("reason") for d in _details(err, "ErrorInfo")}
    return bool(reasons & {"API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED"}) or (
        status == 400 and "api key not valid" in str(err.get("message", "")).lower()
    )


def _daily_quota(err: dict[str, Any]) -> str | None:
    """The quota limit when a 429 is a per-day quota (retrying today will not help)."""
    for detail in _details(err, "QuotaFailure"):
        for v in detail.get("violations") or []:
            if "perday" in str(v.get("quotaId", "")).lower():
                return str(v.get("quotaValue") or "?")
    return None


def _retry_delay(err: dict[str, Any]) -> float | None:
    for detail in _details(err, "RetryInfo"):
        raw = str(detail.get("retryDelay", ""))
        try:
            return float(raw.removesuffix("s"))
        except ValueError:
            continue
    return None
