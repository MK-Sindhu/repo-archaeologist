"""Shared fixtures. `toy_repo` is a real git repo with a small, known history that every
layer's tests can rely on:

  c1  "Add retry helper"                         src/pkg/retry.py (backoff), README.md
  c2  "Cap backoff at 30s to avoid huge sleeps (#12)"   body: "Fixes #7. Users reported multi-minute sleeps."
  c3  "Add jitter to backoff (#15)"              body: "Thundering herd when many clients retry. See #9."
  c4  "docs: describe retry settings"            README.md only

origin = https://github.com/acme/toy.git
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RETRY_V1 = '''"""Retry helpers."""


def backoff(attempt):
    """Seconds to wait before retry number `attempt`."""
    return 2 ** attempt
'''

RETRY_V2 = '''"""Retry helpers."""

MAX_BACKOFF = 30


def backoff(attempt):
    """Seconds to wait before retry number `attempt`."""
    return min(2 ** attempt, MAX_BACKOFF)
'''

RETRY_V3 = '''"""Retry helpers."""

import random

MAX_BACKOFF = 30


def backoff(attempt, jitter=True):
    """Seconds to wait before retry number `attempt`."""
    delay = min(2 ** attempt, MAX_BACKOFF)
    if jitter:
        delay = random.uniform(0, delay)
    return delay


class Retrier:
    def __init__(self, attempts=3):
        self.attempts = attempts

    def run(self, fn):
        for i in range(self.attempts):
            try:
                return fn()
            except Exception:
                if i == self.attempts - 1:
                    raise
'''

README_V1 = "# toy\n\nA toy package.\n\n## Retries\n\nUse `backoff()`.\n"
README_V2 = (
    "# toy\n\nA toy package.\n\n## Retries\n\nUse `backoff()`. Delays are capped at 30 seconds "
    "and jittered.\n\n## Settings\n\n`MAX_BACKOFF` controls the cap.\n"
)

COMMITS = [
    ("Add retry helper", "", {"src/pkg/retry.py": RETRY_V1, "README.md": README_V1}),
    (
        "Cap backoff at 30s to avoid huge sleeps (#12)",
        "Fixes #7. Users reported multi-minute sleeps.",
        {"src/pkg/retry.py": RETRY_V2},
    ),
    (
        "Add jitter to backoff (#15)",
        "Thundering herd when many clients retry. See #9.",
        {"src/pkg/retry.py": RETRY_V3},
    ),
    ("docs: describe retry settings", "", {"README.md": README_V2}),
]


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(root: Path) -> tuple[Path, list[str]]:
    root.mkdir(parents=True, exist_ok=True)
    base_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    _git(root, "init", "-q", "-b", "main", env=base_env)
    _git(root, "remote", "add", "origin", "https://github.com/acme/toy.git", env=base_env)
    shas = []
    for i, (subject, body, files) in enumerate(COMMITS):
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            _git(root, "add", rel, env=base_env)
        date = f"2024-0{i + 1}-15T12:00:00+00:00"
        env = {**base_env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        msg = subject + (f"\n\n{body}" if body else "")
        _git(root, "commit", "-q", "-m", msg, env=env)
        shas.append(_git(root, "rev-parse", "HEAD", env=base_env))
    return root, shas


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    path, _ = make_repo(tmp_path / "toy")
    return path


@pytest.fixture
def toy_repo_shas(tmp_path: Path) -> tuple[Path, list[str]]:
    """(path, [sha_c1, sha_c2, sha_c3, sha_c4])"""
    return make_repo(tmp_path / "toy")


@pytest.fixture(autouse=True)
def _isolated_arch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_HOME", str(tmp_path / "arch_home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
