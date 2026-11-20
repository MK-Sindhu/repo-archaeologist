"""The repository survey behind the Map tab (contract: frontend/src/api/types.ts `Survey`).

One bounded history walk, no LLM:

    git log -z --no-merges -M --name-status --format=%x1e%H%x1f%aN%x1f%at -n5000 HEAD --

Walking newest first, renames are followed with an alias map (a file's older names map to its
current one), so a moved file keeps its history. Files at HEAD come from the tree sizes,
filtered by the index's include/exclude rules.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from ..index.build import is_excluded, is_included
from ..sources.git import GitRepo

SURVEY_VERSION = 1
MAX_COMMITS = 5000
WALK_TIMEOUT_S = 120.0
TOP_AUTHORS = 20
TOP_HOTSPOTS = 20
RECENT_S = 365 * 86400

# extension (lower case) or exact file name -> language name
LANGUAGES: dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".pyx": "Cython",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".fs": "F#",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++",
    ".hh": "C++", ".m": "Objective-C", ".mm": "Objective-C", ".swift": "Swift",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell",
    ".ml": "OCaml", ".clj": "Clojure", ".lua": "Lua", ".r": "R", ".jl": "Julia",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".proto": "Protocol Buffers", ".graphql": "GraphQL",
    ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".md": "Markdown", ".mdx": "MDX", ".rst": "reStructuredText", ".txt": "Text",
    ".toml": "TOML", ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".xml": "XML",
    ".ini": "INI", ".cfg": "INI", ".tf": "Terraform", ".nix": "Nix",
    "Makefile": "Makefile", "Dockerfile": "Dockerfile", "CMakeLists.txt": "CMake",
}


def language_of(path: str) -> str | None:
    p = PurePosixPath(path)
    return LANGUAGES.get(p.name) or LANGUAGES.get(p.suffix.lower())


def surveyed_files(repo: GitRepo) -> dict[str, int]:
    """{path: bytes} for files at HEAD that the index considers (include/exclude rules)."""
    return {p: n for p, n in repo.file_sizes().items() if is_included(p) and not is_excluded(p)}


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _month(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m")


def _months_between(first: str, last: str) -> list[str]:
    y, m = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _git_text(repo: GitRepo, args: list[str]) -> str:
    out = repo._run(args)
    return out.decode("utf-8", "replace").strip() if out else ""


def walk_history(repo: GitRepo, *, max_count: int = MAX_COMMITS,
                 timeout: float = WALK_TIMEOUT_S, now: float | None = None) -> dict[str, Any]:
    """Per-file change counts and times, commits per month and per author, newest first."""
    now = time.time() if now is None else now
    args = ["log", "-z", "--no-merges", "-M", "--name-status",
            "--format=%x1e%H%x1f%aN%x1f%at", f"-n{max_count}", "HEAD", "--"]
    alias: dict[str, str] = {}  # older path -> current path
    changes: Counter[str] = Counter()
    recent: Counter[str] = Counter()
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    months: Counter[str] = Counter()
    authors: Counter[str] = Counter()
    walked = 0
    started = time.monotonic()
    for record in repo._stream(args, timeout=timeout):
        head, _, rest = record.partition("\x00")
        fields = head.split("\x1f")
        if len(fields) < 3 or not fields[2].strip().isdigit():
            continue
        walked += 1
        at = float(fields[2].strip())
        authors[fields[1].strip() or "unknown"] += 1
        months[_month(at)] += 1
        tokens = [t for t in rest.lstrip("\n").split("\x00") if t != ""]
        i = 0
        touched: set[str] = set()
        while i < len(tokens):
            status = tokens[i].strip()
            i += 1
            if not status:
                continue
            if status[0] in "RC" and i + 1 < len(tokens):
                old, new = tokens[i], tokens[i + 1]
                i += 2
                current = alias.get(new, new)
                if status[0] == "R":
                    alias[old] = current
                touched.add(current)
            elif i < len(tokens):
                path = tokens[i]
                i += 1
                touched.add(alias.get(path, path))
        for path in touched:
            changes[path] += 1
            if now - at <= RECENT_S:
                recent[path] += 1
            last[path] = max(last.get(path, at), at)
            first[path] = min(first.get(path, at), at)
    elapsed = time.monotonic() - started
    return {
        "walked": walked,
        "truncated": walked >= max_count or elapsed >= timeout,
        "changes": changes,
        "recent": recent,
        "first": first,
        "last": last,
        "months": months,
        "authors": authors,
    }


def compute_survey(repo: GitRepo, *, max_count: int = MAX_COMMITS,
                   timeout: float = WALK_TIMEOUT_S, now: float | None = None) -> dict[str, Any]:
    """The Survey payload (status "ready") for the repository at HEAD."""
    head = repo.head_sha() or ""
    sizes = surveyed_files(repo)
    hist = walk_history(repo, max_count=max_count, timeout=timeout, now=now)
    changes, last = hist["changes"], hist["last"]

    files = [
        [path, size, int(changes.get(path, 0)),
         int(last[path]) if path in last else None]
        for path, size in sorted(sizes.items())
    ]

    langs: dict[str, list[int]] = {}
    for path, size in sizes.items():
        name = language_of(path) or (PurePosixPath(path).suffix.lstrip(".").upper() or "Other")
        bucket = langs.setdefault(name, [0, 0])
        bucket[0] += size
        bucket[1] += 1
    languages = [{"name": n, "bytes": b, "files": f}
                 for n, (b, f) in sorted(langs.items(), key=lambda kv: (-kv[1][0], kv[0]))]

    month_counts = hist["months"]
    months: list[dict[str, Any]] = []
    if month_counts:
        keys = sorted(month_counts)
        months = [{"month": m, "commits": int(month_counts.get(m, 0))}
                  for m in _months_between(keys[0], keys[-1])]

    authors = [{"name": n, "commits": c}
               for n, c in sorted(hist["authors"].items(), key=lambda kv: (-kv[1], kv[0]))]

    hot = sorted((p for p in sizes if changes.get(p)),
                 key=lambda p: (-changes[p], -hist["recent"].get(p, 0), p))[:TOP_HOTSPOTS]
    hotspots = [{"path": p, "changes": int(changes[p]),
                 "recent_changes": int(hist["recent"].get(p, 0)),
                 "last_change": _iso(last.get(p))} for p in hot]

    total_commits = _git_text(repo, ["rev-list", "--count", "HEAD"])
    roots = _git_text(repo, ["log", "--max-parents=0", "--format=%at", "HEAD"])
    root_times = [int(x) for x in roots.split() if x.isdigit()]
    last_commit = _git_text(repo, ["log", "-1", "--format=%at", "HEAD"])
    return {
        "status": "ready",
        "head": head,
        "truncated": bool(hist["truncated"]),
        "totals": {
            "files": len(sizes),
            "bytes": sum(sizes.values()),
            "commits": int(total_commits) if total_commits.isdigit() else hist["walked"],
            "authors": len(hist["authors"]),
            "first_commit": _iso(min(root_times)) if root_times else None,
            "last_commit": _iso(int(last_commit)) if last_commit.isdigit() else None,
        },
        "languages": languages,
        "files": files,
        "months": months,
        "authors": authors[:TOP_AUTHORS],
        "hotspots": hotspots,
    }
