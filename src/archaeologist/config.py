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


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(paths: list[Path] | None = None) -> list[Path]:
    """Load KEY=VALUE lines from .env files into os.environ and return the files read.

    Defaults to ./.env, then the project checkout's .env. Variables already set (in the shell
    or by an earlier file) win; empty values are skipped. Set ARCH_NO_DOTENV=1 to disable.
    """
    if os.getenv("ARCH_NO_DOTENV"):
        return []
    if paths is None:
        paths = [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    loaded: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key and value and key not in os.environ:
                os.environ[key] = value
        loaded.append(path)
    return loaded


def arch_home() -> Path:
    return Path(os.getenv("ARCH_HOME", Path.home() / ".cache" / "repo-archaeologist"))


def repo_data_dir(repo_path: str | Path) -> Path:
    """Per-target-repo data dir (index + GitHub cache), kept OUTSIDE the target repo."""
    p = Path(repo_path).resolve()
    digest = hashlib.sha1(str(p).encode()).hexdigest()[:8]
    d = arch_home() / f"{p.name}-{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d
