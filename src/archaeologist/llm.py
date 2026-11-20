"""Claude wrapper implementing contracts.LLM via the official Anthropic SDK.

- Structured calls send the Pydantic model's JSON schema as `output_config.format` (the same
  schema `client.beta.messages.parse(output_format=...)` would send) through
  `client.beta.messages.create`, and validate the reply themselves. `parse` validates inside
  the SDK before the stop reason can be checked, so a reply cut off at max_tokens or a
  refusal would surface as a raw pydantic ValidationError with its tokens uncounted.
- Thinking is always adaptive on Opus 5.5 (we omit `thinking`); depth is set per role
  with `output_config.effort`.
- Server-side refusal fallback (beta) is on by default: betas=["server-side-fallback-2026-07-01"],
  fallbacks="default". Turn off with LLMConfig(use_fallbacks=False) / ARCH_USE_FALLBACKS=0.
"""

from __future__ import annotations

import functools
import threading
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import LLMConfig

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@functools.lru_cache(maxsize=64)
def json_output_format(schema: type[BaseModel]) -> dict[str, Any]:
    """`output_config.format` asking for replies that match `schema` (strict JSON schema)."""
    return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class ClaudeLLM:
    def __init__(self, config: LLMConfig | None = None, client: Any | None = None):
        self.config = config or LLMConfig()
        self.client = client or anthropic.Anthropic(max_retries=4)
        self._lock = threading.Lock()
        self._usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

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
        kw = self._kwargs(system, prompt, effort, max_tokens, role)
        kw["output_config"] = {**kw["output_config"], "format": json_output_format(schema)}
        resp = self.client.beta.messages.create(**kw)
        self._check(resp)  # usage first; refusals and max_tokens stops raise before validation
        text = _text_of(resp)
        if not text.strip():
            raise LLMError(f"no structured output (stop_reason={resp.stop_reason})")
        try:
            return schema.model_validate_json(text)
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            raise LLMError(
                f"reply does not match {schema.__name__} (stop_reason={resp.stop_reason}): {exc}"
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
        resp = self.client.beta.messages.create(**self._kwargs(system, prompt, effort, max_tokens, role))
        self._check(resp)
        return _text_of(resp)

    def usage_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._usage)

    # -- internals ------------------------------------------------------------------
    def _kwargs(self, system, prompt, effort, max_tokens, role) -> dict[str, Any]:
        cfg = self.config
        kw: dict[str, Any] = {
            "model": cfg.grader_model if role == "grade" else cfg.model,
            "max_tokens": max_tokens or cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort or "medium"},
        }
        if cfg.use_fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    def _check(self, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        with self._lock:
            self._usage["calls"] += 1
            if u is not None:
                self._usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
                self._usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"model declined: {getattr(details, 'category', None)}")
        if resp.stop_reason == "max_tokens":
            raise LLMError("hit max_tokens; raise LLMConfig.max_tokens")


def _text_of(resp: Any) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
