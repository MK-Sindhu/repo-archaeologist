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
# Free-tier daily quotas are per model, so when one model's day is used up the next one in
# this list takes over (each has its own quota). ARCH_GEMINI_FALLBACKS overrides it; an
# empty value turns fallbacks off.
DEFAULT_FALLBACKS = ("gemini-3-flash-preview", "gemini-2.5-flash", "gemini-3.5-flash-lite",
                     "gemini-3.1-flash-lite")
_exhausted: dict[str, float] = {}  # model -> unix time its daily quota resets
_exhausted_lock = threading.Lock()
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
        fallbacks: list[str] | None = None,
        concurrency: int | None = None,
        rpm: int | None = None,
        timeout_s: float = 180.0,
    ):
        self.config = config or gemini_config()
        if fallbacks is None:
            raw = os.getenv("ARCH_GEMINI_FALLBACKS")
            fallbacks = list(DEFAULT_FALLBACKS) if raw is None else [
                m.strip() for m in raw.split(",") if m.strip()]
        self.fallbacks = fallbacks
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
        primary = self._model(role)
        candidates = [primary, *(m for m in self.fallbacks if m != primary)]
        spent: list[str] = []
        for model in candidates:
            if _is_exhausted(model):
                spent.append(model)
                continue
            gen: dict[str, Any] = {"maxOutputTokens": max_tokens or self.config.max_tokens, **gen_extra}
            if _supports_thinking_level(model) and model not in self._no_thinking:
                gen["thinkingConfig"] = {"thinkingLevel": _thinking_level(effort)}
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen,
            }
            try:
                data = self._post(model, body)
            except DailyQuotaExceeded as exc:
                _mark_exhausted(model)
                spent.append(model)
                nxt = next((m for m in candidates if m not in spent and not _is_exhausted(m)), None)
                if nxt:
                    log.warning("Gemini: the free daily limit for %s is used up (%s/day); "
                                "using %s until it resets", model, exc.limit, nxt)
                continue
            return self._read(data)
        raise LLMError(
            "Gemini's free daily limit is used up for every configured model ("
            + ", ".join(spent) + f"). It resets at midnight Pacific time ({_reset_note()}); "
            "or enable billing on the Gemini API key."
        )

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
                    raise DailyQuotaExceeded(model, daily)
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


class DailyQuotaExceeded(LLMError):
    """A model's free daily quota is used up (retrying today won't help)."""

    def __init__(self, model: str, limit: str):
        super().__init__(f"Gemini free-tier daily quota for {model} is used up (limit {limit}/day).")
        self.model, self.limit = model, limit


def _next_reset(now: float | None = None) -> float:
    """Unix time of the next midnight in America/Los_Angeles (when Gemini quotas reset)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    local = datetime.fromtimestamp(now if now is not None else time.time(), tz)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _reset_note() -> str:
    hours = max(0.0, (_next_reset() - time.time()) / 3600)
    return f"in about {hours:.0f} h" if hours >= 1 else "within the hour"


def _mark_exhausted(model: str) -> None:
    with _exhausted_lock:
        _exhausted[model] = _next_reset()


def _is_exhausted(model: str) -> bool:
    with _exhausted_lock:
        until = _exhausted.get(model)
        if until is not None and time.time() >= until:
            del _exhausted[model]
            return False
        return until is not None


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
