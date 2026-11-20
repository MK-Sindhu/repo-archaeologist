"""Interfaces between layers. The graph and eval code depend ONLY on these protocols,
so they can be tested with fakes (see testing.py) and built in parallel with the
concrete implementations."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

import numpy as np
from pydantic import BaseModel

from .models import Evidence

T = TypeVar("T", bound=BaseModel)

# Sources accepted by Toolbox.search
SEARCH_SOURCES = ("code", "docs", "commits", "prs")


@runtime_checkable
class Toolbox(Protocol):
    """Everything the agent can do to the world. Implemented by toolbox.RepoToolbox."""

    def search(self, source: str, query: str, k: int = 6) -> list[Evidence]:
        """Search one source. source ∈ SEARCH_SOURCES:
        code    -> local hybrid index, code chunks at HEAD (Evidence.kind="code")
        docs    -> local hybrid index, markdown/rst sections (kind="doc")
        commits -> local hybrid index over commit messages (kind="commit")
        prs     -> GitHub search over PRs + issues (kind="pr"/"issue"); [] if offline
        Unknown source -> ValueError. Never raises on network errors: log + return [].
        """
        ...

    def history(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        symbol: str | None = None,
        k: int = 6,
    ) -> list[Evidence]:
        """Commits that shaped this code, newest first (git log -L / blame).
        Returns kind="commit" Evidence with refs filled (e.g. "pr:12" from a squash subject)."""
        ...

    def fetch(self, ref: str) -> Evidence | None:
        """Resolve a canonical ref (commit:/pr:/issue:/gh:/code:/doc:) to Evidence, or None.
        commit -> message + truncated diff, refs include linked PRs
        pr     -> title, body, review/comments digest, refs include closed/linked issues + merge commit
        issue  -> title, body, comments digest
        gh:<n> -> PR if n is a PR else issue
        """
        ...


@runtime_checkable
class LLM(Protocol):
    """Minimal LLM surface used by graph + eval. Implemented by llm.ClaudeLLM / testing.FakeLLM."""

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
        """Return a validated instance of `schema`. `role` selects model ("grade" may use grader_model)."""
        ...

    def text(
        self,
        *,
        system: str,
        prompt: str,
        effort: str | None = None,
        max_tokens: int | None = None,
        role: str = "default",
    ) -> str: ...

    def usage_snapshot(self) -> dict[str, int]:
        """Cumulative {"calls","input_tokens","output_tokens"} so far."""
        ...


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        """float32 array [len(texts), dim], L2-normalised rows."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """float32 [dim], L2-normalised."""
        ...
