"""Turn what a user typed ("pallets/click", a GitHub URL, a local path) into a clone on disk."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import arch_home

_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]*"  # GitHub users/orgs start with a letter or digit
_NAME = r"[A-Za-z0-9_.-]+"
_HTTPS = re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({_OWNER})/({_NAME})(?:/.*)?$")
_SSH = re.compile(rf"^git@github\.com:({_OWNER})/({_NAME})$")
_SLUG = re.compile(rf"^({_OWNER})/({_NAME})$")
_GIT_PERCENT = re.compile(r"([A-Za-z ]+):\s+(\d+)% \((\d+)/(\d+)\)")

Progress = Callable[[str, int, "int | None", str], None]


class SourceError(ValueError):
    """The source cannot be used; the message is shown to the user as is."""


@dataclass(frozen=True)
class Source:
    kind: str  # "github" | "local"
    path: Path  # where the clone lives (for github: where it will be cloned)
    slug: str | None = None  # "owner/name" for github sources


def clone_base() -> str:
    """Where GitHub repos are cloned from (overridable for tests)."""
    return os.getenv("ARCH_CLONE_BASE", "https://github.com").rstrip("/")


def clones_dir() -> Path:
    return arch_home() / "clones"


def repo_id_for(path: Path) -> str:
    """Stable URL-safe id for a clone path."""
    path = path.resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-.") or "repo"
    return f"{name}-{hashlib.sha1(str(path).encode()).hexdigest()[:8]}"


def parse_source(text: str, *, allow_local: bool) -> Source:
    """Parse "owner/name", a GitHub https/ssh URL, or (when allowed) a local path."""
    text = text.strip()
    if not text:
        raise SourceError("Enter a GitHub repository (owner/name or URL) or a local path.")
    slug = _github_slug(text)
    if slug:
        owner, name = slug.split("/")
        return Source("github", (clones_dir() / f"{owner}__{name}").resolve(), slug)
    if not allow_local:
        raise SourceError(
            "This server only opens GitHub repositories. Enter owner/name or a GitHub URL."
        )
    path = Path(text).expanduser()
    if not path.exists():
        raise SourceError(f"No such directory: {text}")
    if not path.is_dir():
        raise SourceError(f"{text} is a file, not a repository directory.")
    root = _git_toplevel(path)
    if root is None:
        raise SourceError(f"{text} is not a git repository.")
    return Source("local", root)


def _github_slug(text: str) -> str | None:
    for pattern in (_HTTPS, _SSH):
        m = pattern.match(text)
        if m:
            return _checked(m.group(1), m.group(2))
    m = _SLUG.match(text)
    if m and not Path(text).expanduser().exists():
        return _checked(m.group(1), m.group(2))
    return None


def _checked(owner: str, name: str) -> str:
    name = name.removesuffix(".git")
    if owner in (".", "..") or name in ("", ".", ".."):
        raise SourceError(f"{owner}/{name} is not a valid GitHub repository name.")
    return f"{owner}/{name}"


def _git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, env=_git_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"  # fail instead of asking for credentials
    return env


def fetch(source: Source, progress: Progress, warn: Callable[[str], None]) -> None:
    """Clone a GitHub source (full history) or fast-forward an existing clone. Local sources
    are left alone."""
    if source.kind != "github":
        return
    if (source.path / ".git").exists():
        progress("pull", 0, None, f"Updating {source.slug}")
        out = subprocess.run(
            ["git", "-C", str(source.path), "pull", "--ff-only", "--quiet"],
            capture_output=True, text=True, env=_git_env(), timeout=600, check=False,
        )
        if out.returncode != 0:
            warn(f"Could not update {source.slug}; using the existing clone. "
                 f"({out.stderr.strip().splitlines()[-1] if out.stderr.strip() else 'git pull failed'})")
        return
    check_size(source.slug or "")
    partial = source.path.with_name(source.path.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.parent.mkdir(parents=True, exist_ok=True)
    url = f"{clone_base()}/{source.slug}.git"
    progress("clone", 0, None, f"Cloning {source.slug}")
    proc = subprocess.Popen(
        ["git", "clone", "--progress", "--no-tags", url, str(partial)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_git_env(),
    )
    tail: list[str] = []
    assert proc.stderr is not None
    buf = b""
    while chunk := proc.stderr.read(256):
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for raw in lines:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            tail = [*tail[-4:], line]
            m = _GIT_PERCENT.search(line)
            if m:
                label = m.group(1).strip()
                progress("clone", int(m.group(3)), int(m.group(4)), f"{label} ({source.slug})")
    if proc.wait() != 0:
        shutil.rmtree(partial, ignore_errors=True)
        detail = " ".join(tail).lower()
        if "not found" in detail or "could not read username" in detail or "terminal prompts" in detail:
            raise SourceError(f"{source.slug} was not found on GitHub (or it is private).")
        raise SourceError(f"Could not clone {source.slug}: {tail[-1] if tail else 'git clone failed'}")
    partial.rename(source.path)


def max_repo_mb() -> int:
    """ARCH_MAX_REPO_MB: largest GitHub repository this server clones (0 = no limit)."""
    try:
        return max(0, int(os.getenv("ARCH_MAX_REPO_MB", "0") or 0))
    except ValueError:
        return 0


def github_repo_size_mb(slug: str) -> float | None:
    """Size GitHub reports for the repository in MB; None if unknown. Raises SourceError
    when GitHub says it does not exist."""
    import httpx

    from ..sources.github import resolve_token

    headers = {"Accept": "application/vnd.github+json"}
    token = resolve_token(None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(f"https://api.github.com/repos/{slug}", headers=headers, timeout=15)
    except httpx.HTTPError:
        return None
    if resp.status_code == 404:
        raise SourceError(f"{slug} was not found on GitHub (or it is private).")
    if resp.status_code != 200:
        return None
    size_kb = resp.json().get("size")
    return size_kb / 1024 if isinstance(size_kb, (int, float)) else None


def check_size(slug: str) -> None:
    """Refuse to clone repositories above ARCH_MAX_REPO_MB (when set)."""
    limit = max_repo_mb()
    if not limit or not slug:
        return
    size = github_repo_size_mb(slug)
    if size is not None and size > limit:
        raise SourceError(
            f"{slug} is too large for this server ({size:,.0f} MB; the limit is {limit:,} MB)."
        )
