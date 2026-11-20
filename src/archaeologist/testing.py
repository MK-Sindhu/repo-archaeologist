"""Test doubles shared by all test modules: FakeLLM, FakeToolbox, HashEmbedder."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any, TypeVar

import numpy as np
from pydantic import BaseModel

from .models import Evidence

T = TypeVar("T", bound=BaseModel)

Responder = Callable[[str, str], Any]  # (system, prompt) -> model instance | dict | str


class FakeLLM:
    """Deterministic LLM. `responders` maps a schema class NAME (e.g. "Analysis") to a
    callable (system, prompt) -> instance-or-dict. `text_responder` handles .text().
    Every call is recorded in `.calls` as (kind, schema_name_or_None, role, prompt)."""

    def __init__(
        self,
        responders: dict[str, Responder] | None = None,
        text_responder: Responder | None = None,
    ):
        self.responders = responders or {}
        self.text_responder = text_responder
        self.calls: list[tuple[str, str | None, str, str]] = []

    def structured(self, *, system, prompt, schema: type[T], effort=None, max_tokens=None, role="default") -> T:
        name = schema.__name__
        self.calls.append(("structured", name, role, prompt))
        if name not in self.responders:
            raise KeyError(f"FakeLLM has no responder for schema {name!r}; have {sorted(self.responders)}")
        out = self.responders[name](system, prompt)
        return out if isinstance(out, schema) else schema.model_validate(out)

    def text(self, *, system, prompt, effort=None, max_tokens=None, role="default") -> str:
        self.calls.append(("text", None, role, prompt))
        if self.text_responder is None:
            raise KeyError("FakeLLM has no text_responder")
        return str(self.text_responder(system, prompt))

    def usage_snapshot(self) -> dict[str, int]:
        return {"calls": len(self.calls), "input_tokens": 0, "output_tokens": 0}

    def count(self, schema_name: str) -> int:
        return sum(1 for c in self.calls if c[1] == schema_name)


class FakeToolbox:
    """Toolbox with canned results.
    search_results: {source: [Evidence]}; history_results: [Evidence]; fetchable: {ref: Evidence}."""

    def __init__(
        self,
        search_results: dict[str, list[Evidence]] | None = None,
        history_results: list[Evidence] | None = None,
        fetchable: dict[str, Evidence] | None = None,
    ):
        self.search_results = search_results or {}
        self.history_results = history_results or []
        self.fetchable = fetchable or {}
        self.calls: list[tuple[str, tuple]] = []

    def search(self, source: str, query: str, k: int = 6) -> list[Evidence]:
        self.calls.append(("search", (source, query, k)))
        return list(self.search_results.get(source, []))[:k]

    def history(self, path, start_line=None, end_line=None, symbol=None, k=6) -> list[Evidence]:
        self.calls.append(("history", (path, start_line, end_line, symbol, k)))
        return list(self.history_results)[:k]

    def fetch(self, ref: str) -> Evidence | None:
        self.calls.append(("fetch", (ref,)))
        return self.fetchable.get(ref)


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder for tests (no model download)."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def ev(id: str, kind: str = "commit", title: str = "t", text: str = "x", **kw) -> Evidence:
    """Terse Evidence constructor for tests."""
    return Evidence(id=id, kind=kind, title=title, text=text, **kw)
