"""Claude wrapper implementing contracts.LLM via the official Anthropic SDK.

- Structured calls use `client.beta.messages.parse(output_format=PydanticModel)`.
- Thinking is always adaptive on Opus 5.5 (we omit `thinking`); depth is set per role
  with `output_config.effort`.
- Server-side refusal fallback (beta) is on by default: betas=["server-side-fallback-2026-07-01"],
  fallbacks="default". Turn off with LLMConfig(use_fallbacks=False) / ARCH_USE_FALLBACKS=0.
"""

from __future__ import annotations

import threading
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import LLMConfig

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


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
        resp = self.client.beta.messages.parse(
            **self._kwargs(system, prompt, effort, max_tokens, role), output_format=schema
        )
        self._check(resp)
        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            raise LLMError(f"no parsed output (stop_reason={resp.stop_reason})")
        return parsed

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
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

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
