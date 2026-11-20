"""Read-only repository endpoints for the Map, Ask and Guide tabs (contract:
frontend/src/api/types.ts). None of them rebuilds the index; survey, outline and history
answer {"status": "pending"} while an open/re-index job for the repository runs."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from ..index.chunkers import split_lines
from ..models import Evidence
from ..refs import pr_ref
from ..sources.git import GitRepo
from . import guide as guide_mod
from .engine import Engine
from .events import find_payload
from .outline import file_outline
from .survey import language_of

log = logging.getLogger(__name__)

MAX_CODE_LINES = 400
HISTORY_COMMITS = 30
MAX_RESOLVE = 6
PENDING = {"status": "pending"}


class NotFound(LookupError):
    """Unknown repository or file; the message is shown to the user."""


class RepoApi:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._files: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()
        self._history: OrderedDict[tuple, dict[str, Any]] = OrderedDict()

    # -- helpers -----------------------------------------------------------------------
    def _git(self, repo_id: str) -> GitRepo:
        git = self.engine.git_repo(repo_id)
        if git is None:
            raise NotFound("This repository is not open. Open it from the home page.")
        return git

    def _tracked(self, repo_id: str, git: GitRepo, path: str) -> tuple[str, str]:
        """(head, path) when `path` is a file tracked at HEAD; NotFound otherwise. This is the
        only way a request names a file, so paths outside the repository cannot be read."""
        head = git.head_sha() or ""
        key = (repo_id, head)
        with self._lock:
            files = self._files.get(key)
        if files is None:
            files = frozenset(git.file_sizes())
            with self._lock:
                self._files[key] = files
                while len(self._files) > 16:
                    self._files.popitem(last=False)
        clean = path.strip().lstrip("/")
        if clean not in files:
            raise NotFound(f"There is no file {path!r} in this repository at its current version.")
        return head, clean

    # -- survey --------------------------------------------------------------------------
    def survey(self, repo_id: str) -> dict[str, Any]:
        self._git(repo_id)
        if self.engine.repo_busy(repo_id):
            return dict(PENDING)
        data = self.engine.ensure_survey(repo_id)
        if data is None:
            raise NotFound("This repository is not open. Open it from the home page.")
        return data

    # -- outline / code ----------------------------------------------------------------------
    def outline(self, repo_id: str, path: str) -> dict[str, Any]:
        git = self._git(repo_id)
        if self.engine.repo_busy(repo_id):
            return dict(PENDING)
        _, clean = self._tracked(repo_id, git, path)
        source = git.read_file(clean)
        if source is None:
            return {"status": "ready", "path": clean, "language": None, "supported": False,
                    "symbols": []}
        return {"status": "ready", "path": clean, **file_outline(clean, source)}

    def code(self, repo_id: str, path: str, start: int | None, end: int | None) -> dict[str, Any]:
        git = self._git(repo_id)
        _, clean = self._tracked(repo_id, git, path)
        source = git.read_file(clean)
        if source is None:
            raise NotFound(f"{clean} is a binary file, so there is no text to show.")
        lines = split_lines(source)
        total = len(lines)
        a = max(1, int(start or 1))
        if total == 0:
            return {"path": clean, "start": 1, "end": 0, "total_lines": 0,
                    "language": _lang(clean), "text": ""}
        a = min(a, total)
        b = int(end) if end else a + MAX_CODE_LINES - 1
        b = max(a, min(b, total, a + MAX_CODE_LINES - 1))
        return {"path": clean, "start": a, "end": b, "total_lines": total,
                "language": _lang(clean), "text": "\n".join(lines[a - 1:b])}

    # -- history -----------------------------------------------------------------------------
    def history(self, repo_id: str, path: str, symbol: str | None, start: int | None,
                end: int | None, resolve: bool) -> dict[str, Any]:
        git = self._git(repo_id)
        if self.engine.repo_busy(repo_id):
            return dict(PENDING)
        head, clean = self._tracked(repo_id, git, path)
        symbol = (symbol or "").strip() or None
        key = (repo_id, head, clean, symbol, start, end, bool(resolve))
        with self._lock:
            hit = self._history.get(key)
        if hit is not None:
            return hit
        slug = git.remote_slug()
        owner, name = slug if slug else (None, None)
        if symbol:
            near = (int(start), int(end or start)) if start else None
            commits = git.log_symbol(clean, symbol, HISTORY_COMMITS, near=near)
        elif start:
            commits = git.log_lines(clean, int(start), int(end or start), HISTORY_COMMITS)
        else:
            commits = list(git.iter_commits(max_count=HISTORY_COMMITS, paths=[clean],
                                            timeout=git.timeout))
        evidence = [c.to_evidence(owner, name).model_copy(update={"source": "history"})
                    for c in commits]
        prs: list[dict[str, Any]] = []
        resolved = False
        if resolve:
            evidence, prs, resolved = self._resolve(repo_id, evidence)
        out = {
            "status": "ready", "path": clean, "symbol": symbol,
            "start": int(start) if start else None, "end": int(end) if end else None,
            "resolved": resolved,
            "commits": [find_payload(e) for e in evidence],
            "prs": prs,
        }
        with self._lock:
            self._history[key] = out
            while len(self._history) > 256:
                self._history.popitem(last=False)
        return out

    def _resolve(self, repo_id: str, commits: list[Evidence]) -> tuple[list[Evidence], list[dict[str, Any]], bool]:
        """Ask GitHub which pull request each commit belongs to (when its message names none)
        and fetch up to MAX_RESOLVE linked PRs / issues."""
        toolbox = self.engine.read_toolbox(repo_id)
        github = getattr(toolbox, "github", None) if toolbox is not None else None
        if toolbox is None or github is None:
            return commits, [], False
        linked: list[Evidence] = []
        for ev in commits:
            refs = list(ev.refs)
            if not any(r.startswith(("pr:", "gh:")) for r in refs):
                sha = ev.id.split(":", 1)[1]
                try:
                    prs = github.prs_for_commit(sha)[:3]
                except Exception:
                    log.debug("prs_for_commit failed for %s", sha, exc_info=True)
                    prs = []
                refs.extend(pr_ref(p.number) for p in prs)
            linked.append(ev.model_copy(update={"refs": list(dict.fromkeys(refs))}))
        wanted: list[str] = []
        for ev in linked:
            for ref in ev.refs:
                if ref.split(":", 1)[0] in ("pr", "issue", "gh") and ref not in wanted:
                    wanted.append(ref)
        fetched: list[dict[str, Any]] = []
        for ref in wanted[:MAX_RESOLVE]:
            try:
                item = toolbox.fetch(ref)
            except Exception:  # noqa: BLE001
                item = None
            if item is not None:
                fetched.append(find_payload(item))
        return linked, fetched, True

    # -- suggestions / guide -----------------------------------------------------------------
    def suggestions(self, repo_id: str) -> dict[str, Any]:
        self._git(repo_id)
        if self.engine.repo_busy(repo_id):
            return {"status": "pending", "source": "template", "head": None, "items": []}
        data = self.engine.ensure_suggestions(repo_id)
        if data is None:
            raise NotFound("This repository is not open. Open it from the home page.")
        return _suggestions_view(data)

    def refresh_suggestions(self, repo_id: str) -> dict[str, Any]:
        self._git(repo_id)
        data = self.engine.refresh_suggestions(repo_id)
        if data is None:
            raise NotFound("This repository is not open. Open it from the home page.")
        return _suggestions_view(data)

    def guide(self, repo_id: str) -> dict[str, Any]:
        git = self._git(repo_id)
        job = self.engine.guide_job(repo_id)
        if job is not None:
            return {**_empty_guide("pending"), "head": git.head_sha(), "job": job.ref()}
        data = self.engine.store.valid_artifact(repo_id, "guide", head=git.head_sha(),
                                                version=guide_mod.GUIDE_VERSION)
        if data is None:
            return _empty_guide("missing")
        return {**_empty_guide(data.get("status", "ready")), **data, "job": None}


def _lang(path: str) -> str | None:
    name = language_of(path)
    return name.lower().replace(" ", "-") if name else None


def _suggestions_view(data: dict[str, Any]) -> dict[str, Any]:
    return {"status": data.get("status", "ready"), "source": data.get("source", "template"),
            "head": data.get("head"), "items": list(data.get("items", []))}


def _empty_guide(status: str) -> dict[str, Any]:
    return {"status": status, "head": None, "markdown": None, "citations": [], "evidence": [],
            "generated_at": None, "model": None, "error": None, "job": None}

