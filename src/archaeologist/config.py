"""Settings for Repo Archaeologist. Everything is overridable via env vars or constructor args."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_MODEL = "claude-opus-5-5"


class LLMConfig(BaseModel):
    """Model + effort per role.

    Every role uses the same model by default; effort is the cost/quality lever
    (Opus 5.5 defaults to "medium" when unset, so we always set it explicitly).
    Set ARCH_MODEL / ARCH_GRADER_MODEL to override.
    """

    model: str = Field(default_factory=lambda: os.getenv("ARCH_MODEL", DEFAULT_MODEL))
    grader_model: str = Field(
        default_factory=lambda: os.getenv("ARCH_GRADER_MODEL", os.getenv("ARCH_MODEL", DEFAULT_MODEL))
    )
    effort_analyze: str = "medium"
    effort_grade: str = "low"
    effort_synthesize: str = "high"
    effort_verify: str = "medium"
    effort_judge: str = "medium"
    max_tokens: int = 16000
    # Server-side refusal fallback (beta). Disable if your account/platform rejects it.
    use_fallbacks: bool = Field(
        default_factory=lambda: os.getenv("ARCH_USE_FALLBACKS", "1") not in ("0", "false")
    )


class GraphConfig(BaseModel):
    """Agent knobs. The ablation study in eval/ flips these."""

    sources: list[str] = ["code", "commits", "prs", "docs", "history"]
    multi_hop: bool = True
    max_hops: int = 3
    max_follow_per_hop: int = 6
    k_per_source: int = 6
    max_evidence: int = 30
    verify: bool = True
    max_verify_retries: int = 1


def arch_home() -> Path:
    return Path(os.getenv("ARCH_HOME", Path.home() / ".cache" / "repo-archaeologist"))


def repo_data_dir(repo_path: str | Path) -> Path:
    """Per-target-repo data dir (index + GitHub cache), kept OUTSIDE the target repo."""
    p = Path(repo_path).resolve()
    digest = hashlib.sha1(str(p).encode()).hexdigest()[:8]
    d = arch_home() / f"{p.name}-{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d
